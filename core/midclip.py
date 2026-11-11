"""Round 46: studio-grade mid-clip silence and filler trimming.

How it works: the cuts are planned on the clip's words (clip seconds), confirmed against the
real sound (FFmpeg silencedetect), and rendered into a short trimmed copy of the clip. The
rest of the pipeline (framing, face tracking, captions, camera path, cold open, lower-third)
runs on that copy, so every timestamp downstream is on the post-trim timeline by
construction. TrimPlan.to_trimmed() maps an original clip second onto it (RenderedClip.output_time).

- Dead air: a pause over MIN_PAUSE (0.60 s) inside a sentence keeps PAUSE_KEEP (0.20 s),
  TAIL after the last word (its tail, a breath) and LEAD before the next (its attack); after
  a sentence end it keeps SENTENCE_KEEP (0.30 s) so the speech still breathes. A cut is made
  only where the audio is actually silent.
- Fillers: "um", "uh", "er", "erm", "hmm" standing alone are cut; "like" and "you know" only
  when there is SOFT_FILLER_SILENCE (0.25 s) of silence on both sides.
- Splices: a CROSSFADE (35 ms) equal-power audio crossfade (quarter-sine) centred on every cut,
  so room tone never clicks. Round 46 alternated 1.00x / ZOOM (1.06x) on the speaker's eyes from
  one segment to the next; Round 48 made the cuts plain (it read as the frame breathing in and
  out): MIDCLIP_ZOOM=1 brings the zoom back.
- Never a segment under MIN_SEGMENT (0.4 s): that would look twitchy.
MIDCLIP_TRIM=0 in .env turns it off; it is off with VISUAL_OVERLAYS=full (the creator look).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MIN_PAUSE = 0.60
PAUSE_KEEP = 0.20
SENTENCE_KEEP = 0.30
TAIL_SHARE = 0.6  # of the kept pause, after the last word (0.12 s of 0.20)
MIN_CUT = 0.15  # a cut shorter than this isn't worth a splice
FILLERS = frozenset({"um", "umm", "uh", "uhh", "uhm", "er", "erm", "hmm", "mm"})
SOFT_FILLER_SILENCE = 0.25
HARD_FILLER_GAP = 0.05  # a filler glued to the next word (no gap) is left alone
MIN_SEGMENT = 0.40
CROSSFADE = 0.035
ZOOM = 1.06
SILENCE_DB = -38.0
SILENCE_MIN = 0.12


def enabled(settings: Any = None) -> bool:
    if (os.getenv("MIDCLIP_TRIM") or "1").strip().lower() in {"0", "off", "false", "no"}:
        return False
    from core.executive import overlay_mode

    return overlay_mode(settings) != "full"


@dataclass
class Cut:
    start: float  # clip seconds (original timeline)
    end: float
    reason: str


@dataclass
class TrimPlan:
    duration: float  # the original clip's length
    cuts: list[Cut] = field(default_factory=list)
    zooms: list[float] = field(default_factory=list)  # per kept segment
    centers: list[tuple[float, float] | None] = field(default_factory=list)  # eyes, 0-1 of the frame

    @property
    def keep(self) -> list[tuple[float, float]]:
        out, cursor = [], 0.0
        for cut in self.cuts:
            out.append((cursor, cut.start))
            cursor = cut.end
        out.append((cursor, self.duration))
        return out

    @property
    def trimmed_duration(self) -> float:
        return round(sum(b - a for a, b in self.keep), 6)

    @property
    def removed(self) -> float:
        return round(self.duration - self.trimmed_duration, 6)

    def to_trimmed(self, t: float) -> float:
        """An original clip second on the trimmed timeline (inside a cut: where the cut is)."""
        removed = 0.0
        for cut in self.cuts:
            if t >= cut.end:
                removed += cut.end - cut.start
            elif t > cut.start:
                return round(cut.start - removed, 6)
            else:
                break
        return round(t - removed, 6)

    def remap_words(self, words: list[Any]) -> list[Any]:
        """The clip's words on the trimmed timeline; words inside a cut (fillers) are gone."""
        out = []
        for word in words:
            a, b = float(word.start), float(word.end)
            if any(c.start <= a and b <= c.end for c in self.cuts) or any(
                    c.start <= (a + b) / 2 <= c.end and c.reason.startswith("filler") for c in self.cuts):
                continue
            out.append(replace(word, start=self.to_trimmed(a), end=max(self.to_trimmed(b), self.to_trimmed(a) + 0.01)))
        return out


def _bare(text: str) -> str:
    return re.sub(r"[^a-z']", "", str(text).lower())


def _silent(silences: list[tuple[float, float]] | None, a: float, b: float, need: float) -> bool:
    """True when [a, b] has at least ``need`` seconds of detected silence (None: no audio check)."""
    if silences is None:
        return True
    covered = sum(max(0.0, min(b, e) - max(a, s)) for s, e in silences)
    return covered >= need - 1e-6


def _confirm(silences: list[tuple[float, float]] | None, a: float, b: float) -> tuple[float, float] | None:
    """The part of a planned cut [a, b] that is really silent (the largest silent run)."""
    if silences is None:
        return (a, b)
    best = None
    for s, e in silences:
        lo, hi = max(a, s), min(b, e)
        if hi - lo >= MIN_CUT and (best is None or hi - lo > best[1] - best[0]):
            best = (lo, hi)
    return best


def plan(words: list[Any], duration: float, silences: list[tuple[float, float]] | None = None) -> TrimPlan:
    """The cuts for a clip: ``words`` in clip seconds; ``silences`` (clip seconds) from
    detect_silences(), None to trust the word timings alone."""
    spoken = sorted(words, key=lambda w: float(w.start))
    cuts: list[Cut] = []
    removed_words: set[int] = set()
    n = len(spoken)
    # 1. Fillers (never the first or last word of the clip).
    k = 1
    while k < n - 1:
        word, bare = spoken[k], _bare(spoken[k].text)
        span = 1
        if bare == "you" and k + 1 < n - 1 and _bare(spoken[k + 1].text) == "know":
            span, kind = 2, "soft"
        elif bare == "like":
            kind = "soft"
        elif bare in FILLERS:
            kind = "hard"
        else:
            k += 1
            continue
        while kind == "hard" and k + span < n - 1 and _bare(spoken[k + span].text) in FILLERS:
            span += 1  # "um, uh"
        before, after = spoken[k - 1], spoken[k + span]
        last = spoken[k + span - 1]
        gap_in, gap_out = float(word.start) - float(before.end), float(after.start) - float(last.end)
        if kind == "soft":
            ok = gap_in >= SOFT_FILLER_SILENCE and gap_out >= SOFT_FILLER_SILENCE and \
                _silent(silences, float(before.end), float(word.start), SOFT_FILLER_SILENCE * 0.8) and \
                _silent(silences, float(last.end), float(after.start), SOFT_FILLER_SILENCE * 0.8)
        else:
            ok = gap_in >= HARD_FILLER_GAP and gap_out >= HARD_FILLER_GAP
        if ok:
            keep = SENTENCE_KEEP if str(before.text).rstrip("\"'”’)").endswith((".", "?", "!")) else PAUSE_KEEP
            a = float(before.end) + keep * TAIL_SHARE
            b = float(after.start) - keep * (1 - TAIL_SHARE)
            a, b = min(a, float(word.start)), max(b, float(last.end))  # the filler itself always goes
            if b - a >= MIN_CUT:
                cuts.append(Cut(round(a, 3), round(b, 3), f"filler: {' '.join(w.text for w in spoken[k:k + span])}"))
                removed_words.update(range(k, k + span))
            k += span
            continue
        k += span
    # 2. Dead air between the words that stay.
    kept = [w for i, w in enumerate(spoken) if i not in removed_words]
    for before, after in zip(kept, kept[1:]):
        gap = float(after.start) - float(before.end)
        if gap <= MIN_PAUSE:
            continue
        keep = SENTENCE_KEEP if str(before.text).rstrip("\"'”’)").endswith((".", "?", "!")) else PAUSE_KEEP
        a = float(before.end) + keep * TAIL_SHARE
        b = float(after.start) - keep * (1 - TAIL_SHARE)
        if any(c.start < b and a < c.end for c in cuts):
            continue  # a filler cut already took this gap
        confirmed = _confirm(silences, a, b)
        if confirmed and confirmed[1] - confirmed[0] >= MIN_CUT:
            cuts.append(Cut(round(confirmed[0], 3), round(confirmed[1], 3), f"pause {gap:.2f} s"))
    cuts = sorted(cuts, key=lambda c: c.start)
    cuts = [c for c in cuts if 0.0 < c.start < c.end < duration]
    # 3. No segment shorter than MIN_SEGMENT: drop the shorter neighbouring cut.
    changed = True
    while changed and cuts:
        changed = False
        result = TrimPlan(duration, cuts)
        for index, (a, b) in enumerate(result.keep):
            if b - a < MIN_SEGMENT:
                neighbours = [i for i in (index - 1, index) if 0 <= i < len(cuts)]
                drop = min(neighbours, key=lambda i: cuts[i].end - cuts[i].start)
                cuts.pop(drop)
                changed = True
                break
    trim = TrimPlan(duration, cuts)
    # Round 48: no 1.00x / 1.06x zoom toggling on every trimmed pause (it read as the frame
    # breathing in and out); the cuts are plain. MIDCLIP_ZOOM=1 brings Round 46's back.
    zoom = ZOOM if zoom_cuts() else 1.0
    trim.zooms = [1.0 if i % 2 == 0 else zoom for i in range(len(trim.keep))]
    trim.centers = [None] * len(trim.keep)
    return trim


def zoom_cuts() -> bool:
    return os.getenv("MIDCLIP_ZOOM", "0").strip().lower() in {"1", "true", "yes", "on"}


def detect_silences(source: str | Path, start: float, end: float) -> list[tuple[float, float]] | None:
    """Silent runs in the clip (clip seconds); None when FFmpeg can't tell (then the word
    timings alone decide)."""
    from core.audio_polish import silences

    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    try:
        found = silences(ffmpeg, source, start, end - start, noise_db=SILENCE_DB, min_gap=SILENCE_MIN)
    except Exception:  # noqa: BLE001
        return None
    return found


def frame_size(source: str | Path) -> tuple[int, int]:
    """The video's display size (core.face_tracker's probe; a plain ffprobe when that fails)."""
    try:
        from core.face_tracker import _probe_display_size

        return _probe_display_size(Path(source))
    except Exception:  # noqa: BLE001 - an older ffprobe without stream_side_data
        out = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(source)],
                             capture_output=True, text=True, timeout=60, check=True).stdout.strip().split("x")
        return int(out[0]), int(out[1])


def eye_centers(source: str | Path, clip_start: float, trim: TrimPlan) -> None:
    """Fill trim.centers with the main face's eyes (0-1) in each zoomed segment; a segment
    with no face (slides) gets no zoom."""
    try:
        from core.face_tracker import _face_detector, _frame_at

        width, height = frame_size(source)
        small = (640, max(2, round(640 * height / width / 2) * 2))
        detector = _face_detector()
    except Exception:  # noqa: BLE001
        trim.zooms = [1.0] * len(trim.keep)
        return
    for index, (a, b) in enumerate(trim.keep):
        if trim.zooms[index] == 1.0:
            continue
        frame = _frame_at(Path(source), clip_start + (a + b) / 2, small)
        faces = detector.detect(frame) if frame is not None else []
        if not faces:
            trim.zooms[index] = 1.0
            continue
        cx, cy, fw, fh, _score = max(faces, key=lambda f: f[2] * f[3])[:5]
        trim.centers[index] = (cx / small[0], max(0.0, (cy - 0.12 * fh)) / small[1])


def frame_rate(source: str | Path) -> str:
    """The video's frame rate as FFmpeg writes it ('30000/1001', '30/1'); '30/1' when unknown."""
    try:
        out = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=r_frame_rate,avg_frame_rate", "-of", "csv=p=0", str(source)],
                             capture_output=True, text=True, timeout=60, check=True).stdout.strip().split(",")
        for rate in out:
            num, _, den = rate.strip().partition("/")
            if num and float(num) > 0 and float(den or 1) > 0 and 10 <= float(num) / float(den or 1) <= 120:
                return f"{int(num)}/{int(den or 1)}"
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return "30/1"


def retime_expression(trim: TrimPlan) -> str:
    """Round 49: the setpts expression that puts every kept frame at exactly
    trim.to_trimmed(its time): T minus the length of every cut that ended before it."""
    removed = [f"gte(T\\,{c.end:.4f})*{c.end - c.start:.4f}" for c in trim.cuts]
    return "PTS-(" + ("+".join(removed) or "0") + ")/TB"


def filter_graph(trim: TrimPlan, width: int, height: int, rate: str = "30/1") -> str:
    """The FFmpeg filter_complex for the trimmed copy: [vout] and [aout]."""
    parts: list[str] = []
    keep = trim.keep
    n = len(keep)
    half = CROSSFADE / 2
    zoomed = any(z > 1.0 for z in trim.zooms[:n])
    if not zoomed:
        # Round 48: one pass over the picture, keeping the kept spans' frames (exactly trim's
        # start <= t < end). The Round 46 graph (one trim branch per segment into concat) made
        # FFmpeg hold every decoded frame of the later segments while it encoded the first: 6 GB
        # on a 1440p 60 fps talk, killed for memory, and the clip went out uncut.
        # Round 49 (sync audit): the kept frames used to be renumbered (setpts=N/FRAME_RATE/TB).
        # Each segment's frame count rounds, and the roundings add up over the cuts: up to 3-5
        # frames (100-170 ms) late or early by the clip's end against the audio and the captions,
        # far more on a variable-frame-rate recording (Zoom, Riverside). Now each frame keeps its
        # own time minus the cuts before it (to_trimmed, the captions' mapping), and fps= lays it
        # on a constant grid: never more than half a frame off, anywhere in the clip.
        keep_expr = "+".join(f"gte(t\\,{a:.3f})*lt(t\\,{b:.3f})" for a, b in keep)
        parts.append(f"[0:v]select='{keep_expr}',setpts='{retime_expression(trim)}',fps={rate}:round=near,"
                     f"setsar=1[vout]")
    for i, (a, b) in enumerate(keep):
        if zoomed:
            zoom = trim.zooms[i] if i < len(trim.zooms) else 1.0
            center = trim.centers[i] if i < len(trim.centers) else None
            video = f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS"
            if zoom > 1.0 and center is not None:
                cw, ch = int(width / zoom) // 2 * 2, int(height / zoom) // 2 * 2
                x = int(min(max(center[0] * width - cw / 2, 0), width - cw))
                y = int(min(max(center[1] * height - ch / 2, 0), height - ch))
                video += f",crop={cw}:{ch}:{x}:{y},scale={width}:{height}:flags=lanczos"
            parts.append(video + ",setsar=1[v%d]" % i)
        a_start = a - (half if i > 0 else 0.0)
        a_end = b + (half if i < n - 1 else 0.0)
        parts.append(f"[0:a]atrim=start={max(0.0, a_start):.4f}:end={a_end:.4f},asetpts=PTS-STARTPTS[a{i}]")
    if zoomed:
        parts.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vout]")
    if n == 1:
        parts.append("[a0]anull[aout]")
    else:
        current = "a0"
        for i in range(1, n):
            out = "aout" if i == n - 1 else f"x{i}"
            parts.append(f"[{current}][a{i}]acrossfade=d={CROSSFADE}:c1=qsin:c2=qsin[{out}]")
            current = out
    return ";".join(parts)


def render_trimmed(source: str | Path, clip_start: float, trim: TrimPlan, output: Path) -> Path:
    """The trimmed copy of the clip (near-lossless), for the rest of the pipeline."""
    width, height = frame_size(source)
    eye_centers(source, clip_start, trim)
    graph = filter_graph(trim, width, height, frame_rate(source))
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    command = [ffmpeg, "-v", "error", "-y", "-ss", f"{clip_start:.3f}", "-t", f"{trim.duration:.3f}", "-i", str(source),
               "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]", "-t", f"{trim.trimmed_duration:.3f}",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-movflags", "+faststart", str(output)]
    subprocess.run(command, check=True, capture_output=True, timeout=max(900.0, 8.0 * float(trim.duration)))
    return output


def describe(trim: TrimPlan) -> str:
    fillers = sum(c.reason.startswith("filler") for c in trim.cuts)
    pauses = len(trim.cuts) - fillers
    return (f"{len(trim.cuts)} cut(s): {pauses} pause(s), {fillers} filler(s); "
            f"{trim.removed:.2f} s removed ({trim.duration:.1f} -> {trim.trimmed_duration:.1f} s)")
