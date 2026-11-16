"""Round 65: long VODs (7 hours and more). Transcribe only where something happened.

Transcribing is the slow, limited step: a 7-hour stream is 25,200 s of audio, and chat and
loudness (both cheap: seconds, not minutes) already say where the moments are. So:

  1. chat + loudness for the whole stream -> the heatmap, with the signals as they are
     (core.clip_mode.signals, nothing changed);
  2. choose(): a window around each run of raised buckets, strongest first, merged where they
     overlap, until MAX_WINDOWS windows or MAX_AUDIO seconds are used;
  3. dead air inside a window (quiet AND nobody typing, for 20 s or more) is cut out of what
     gets transcribed;
  4. transcribe_windows(): each piece is cut from the audio, transcribed on its own and its
     word times moved back onto the stream's clock; each piece's words are saved as soon as they
     exist, so a stopped run carries on where it was;
  5. the speech signal and the picker then run on those words. allowed() says where a moment
     may be seeded: only where the transcript reaches far enough either side of it.

Progress (below) is the small file that lets a re-run skip finished steps.
The numbers and why: docs/CLIP_MODE_TUNING.md, "Long VODs".
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from core import heatmap_scorer
from core.clip_mode import signals as SG
from core.clip_mode.chat import Message
from core.transcriber import Word

logger = logging.getLogger(__name__)

HOT = heatmap_scorer.HOT_VALUE
# A bucket this warm before the transcript exists can still become hot once the speech signal
# agrees with it (signals.fuse adds AGREE_BONUS): 0.65 - 0.25 = 0.40. Below it, no words can
# lift a bucket to hot unless speech alone is at 0.93+, which is rare (see the tuning notes).
WARM = HOT - SG.AGREE_BONUS
PAD = 60.0  # seconds of audio kept before a run's start and after its end, with chat
PAD_NO_CHAT = 90.0  # ... with loudness alone: a scream says less about where the setup began
CORE_MAX = 45.0  # of a long run, the part a clip can still use (clips are 60 s at most)
# What the picker reads around a moment: 25 s of words before + 14 s of setup + 2.5 s to reach a
# pause; 20 s after the run + 20 s of reaction + 2.5 s. A moment is only seeded where the
# window has that much either side.
EDGE = 45.0
MAX_WINDOWS = 30
MAX_AUDIO = 45 * 60.0  # seconds of audio transcribed per VOD at most
DEAD_DB = 12.0  # a bucket this far under the stream's median loudness is quiet enough to be dead air
DEAD_BUCKETS = 4  # ... when it lasts this many buckets (20 s) with no chat message
# Rough speeds, for the estimates printed before a step starts (the real time is printed after).
LOUDNESS_SECONDS_PER_HOUR = 4.0  # FFmpeg ebur128 over audio only: 6.3 s for 2 h 12 min on an M-series Mac
AUDIO_MB_PER_HOUR = 75.0  # Twitch's audio-only rendition: 162 MB for 2 h 12 min
GROQ_SECONDS_PER_MINUTE = 1.5  # Groq, per minute of audio, cutting and uploading included: 9.8 s for 385 s


@dataclass
class Window:
    start: float  # the span on the stream's clock, before dead air is taken out
    end: float
    peak: float  # the strongest bucket in it
    pieces: list[tuple[float, float]] = field(default_factory=list)  # what gets transcribed
    zones: list[tuple[float, float]] = field(default_factory=list)  # where a moment may be seeded

    @property
    def seconds(self) -> float:
        return sum(b - a for a, b in self.pieces)

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "peak": round(self.peak, 4), "pieces": [list(p) for p in self.pieces],
                "zones": [list(z) for z in self.zones], "seconds": round(self.seconds, 3)}

    @classmethod
    def from_dict(cls, data: dict) -> "Window":
        return cls(float(data["start"]), float(data["end"]), float(data.get("peak", 0.0)),
                   [(float(a), float(b)) for a, b in data.get("pieces") or []],
                   [(float(a), float(b)) for a, b in data.get("zones") or []])


def _merge(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """[first, past-the-last) index pairs of each run of True."""
    out: list[tuple[int, int]] = []
    i, n = 0, len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j < n and flags[j]:
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def dead_air(lufs: np.ndarray | None, messages: list[Message] | None, n: int) -> np.ndarray:
    """True per bucket that can be left out of the transcript: the inside of a stretch of
    DEAD_BUCKETS or more buckets that are all quiet (DEAD_DB under the stream's median) with no
    chat message. The first and last bucket of each stretch are kept: a word may straddle them."""
    dead = np.zeros(n, dtype=bool)
    if lufs is None or not len(lufs):
        return dead
    mean = (lufs[:, 1] if lufs.ndim == 2 else lufs)[:n]
    heard = mean[mean > -69.0]
    if not len(heard):
        return dead  # nothing but silence: no level to judge by, leave everything in
    quiet = np.zeros(n, dtype=bool)
    quiet[:len(mean)] = mean < np.median(heard) - DEAD_DB
    for m in messages or []:
        i = int(m.t // SG.BUCKET)
        if 0 <= i < n:
            quiet[i] = False
    for a, b in _runs(quiet):
        if b - a >= DEAD_BUCKETS:
            dead[a + 1:b - 1] = True
    return dead


def _pieces(span: tuple[float, float], dead: np.ndarray) -> list[tuple[float, float]]:
    """The span without its dead buckets."""
    a, b = span
    out: list[tuple[float, float]] = []
    start: float | None = None
    for i in range(int(a // SG.BUCKET), int(np.ceil(b / SG.BUCKET))):
        lo = max(a, i * SG.BUCKET)
        if i < len(dead) and dead[i]:
            if start is not None:
                out.append((start, lo))
                start = None
        elif start is None:
            start = lo
    if start is not None:
        out.append((start, b))
    return [(round(x, 3), round(y, 3)) for x, y in out if y - x >= 1.0]


def choose(values: np.ndarray, duration: float, lufs: np.ndarray | None = None, messages: list[Message] | None = None,
           *, pad: float | None = None, max_windows: int = MAX_WINDOWS, max_audio: float = MAX_AUDIO,
           report: dict | None = None) -> list[Window]:
    """The windows worth transcribing. ``values``: the fused heatmap before any transcript
    (chat + loudness; loudness alone when there is no chat, which also widens the windows).

    Runs of buckets at WARM or more are taken strongest first (peak, then total heat). Each gets
    ``pad`` seconds before its start and after its end (a long run counts for CORE_MAX seconds);
    overlapping windows are merged; one that would take the total past ``max_windows`` windows or
    ``max_audio`` seconds of audio (after dead air is taken out) is left out."""
    if pad is None:
        pad = PAD if messages else PAD_NO_CHAT
    n = len(values)
    flags = np.asarray(values) >= WARM
    flags[:int(heatmap_scorer.SKIP_OPENING // SG.BUCKET)] = False
    runs = sorted(_runs(flags), key=lambda r: (-float(values[r[0]:r[1]].max()), -float(values[r[0]:r[1]].sum()), r[0]))
    dead = dead_air(lufs, messages, n)
    spans: list[tuple[float, float]] = []
    zones: list[tuple[float, float]] = []
    peaks: list[tuple[float, float]] = []  # (run start, peak)
    used, left_out = 0.0, 0
    for a, b in runs:
        start = a * SG.BUCKET
        core_end = min(b * SG.BUCKET, start + CORE_MAX, duration)
        span = (max(0.0, start - pad), min(duration, core_end + pad))
        merged = _merge(spans + [span])
        total = sum(y - x for m in merged for x, y in _pieces(m, dead))
        if len(merged) > max_windows or total > max_audio:
            left_out += 1
            continue
        spans, used = merged, total
        zones.append((max(0.0, start - (pad - EDGE)), min(duration, core_end + (pad - EDGE))))
        peaks.append((start, float(values[a:b].max())))
    windows: list[Window] = []
    for a, b in spans:
        windows.append(Window(round(a, 3), round(b, 3), max((p for t, p in peaks if a <= t <= b), default=0.0),
                              _pieces((a, b), dead), _merge([z for z in zones if z[0] >= a and z[1] <= b])))
    if report is not None:
        span_seconds = sum(w.end - w.start for w in windows)
        report.update({"runs": len(runs), "runs_left_out": left_out, "windows": len(windows), "pad": pad,
                       "audio_seconds": round(used, 1), "dead_air_seconds": round(span_seconds - used, 1),
                       "share_of_stream": round(used / duration, 4) if duration else 0.0})
    return windows


def covered(windows: list[Window], n: int) -> np.ndarray:
    """True per bucket that has (any) transcribed audio."""
    out = np.zeros(n, dtype=bool)
    for w in windows:
        for a, b in w.pieces:
            out[int(a // SG.BUCKET):int(np.ceil(b / SG.BUCKET))] = True
    return out


def allowed(windows: list[Window], n: int) -> np.ndarray:
    """True per bucket where the picker may seed a moment (inside a window's zones)."""
    out = np.zeros(n, dtype=bool)
    for w in windows:
        for a, b in w.zones:
            out[int(a // SG.BUCKET):int(np.ceil(b / SG.BUCKET))] = True
    return out


# ------------------------------------------------------------------ transcribing the pieces
NO_SPEECH = ("no word timestamps", "no timestamped speech")  # core.transcriber's words for an empty result


def _cache_file(folder: Path, a: float, b: float) -> Path:
    return folder / f"{int(round(a * 1000)):010d}_{int(round(b * 1000)):010d}.json"


def _extract(audio: Path, start: float, end: float, wav: Path) -> Path:
    from core.downloader import extract_audio_segment

    return extract_audio_segment(audio, start, end, wav)


def _mmss(seconds: float) -> str:
    seconds = int(round(max(0.0, seconds)))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else \
        f"{seconds // 60}:{seconds % 60:02d}"


def transcribe_windows(audio: Path, windows: list[Window], cache_dir: Path, transcribe: Callable[[Path], list[Word]],
                       *, say: Callable[[str], None] = print, extract: Callable[[Path, float, float, Path], Path] = _extract,
                       on_piece: Callable[[int, int], None] | None = None) -> list[Word]:
    """The words of every window piece, on the stream's clock, in time order.

    ``transcribe(wav) -> words`` gets one piece at a time (times from 0); a piece with no speech
    may raise core.transcriber's "no word timestamps" error, which counts as no words. Each
    piece's words are written to ``cache_dir`` as soon as they exist; pieces already there are
    not transcribed again. Any other error stops the run with the finished pieces kept."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    pieces = [p for w in windows for p in w.pieces]
    todo = [p for p in pieces if not _cache_file(cache_dir, *p).is_file()]
    todo_seconds = sum(b - a for a, b in todo)
    if len(todo) < len(pieces):
        say(f"  {len(pieces) - len(todo)} of {len(pieces)} windows already transcribed (kept from the last run)")
    words: list[Word] = []
    began, done_seconds, fresh = time.time(), 0.0, 0
    for n, (a, b) in enumerate(pieces, 1):
        cache = _cache_file(cache_dir, a, b)
        if cache.is_file():
            found = [Word(**w) for w in json.loads(cache.read_text(encoding="utf-8"))]
        else:
            wav = cache_dir / "piece.wav"
            extract(audio, a, b, wav)
            try:
                local = transcribe(wav)
            except RuntimeError as exc:
                if not any(text in str(exc) for text in NO_SPEECH):
                    raise
                local = []
            finally:
                wav.unlink(missing_ok=True)
            found = []
            for w in local:
                start, end = a + max(0.0, w.start), min(b, a + w.end)
                if end <= start:
                    continue
                found.append(Word(text=w.text, start=round(start, 3), end=round(end, 3),
                                  segment_start=None if w.segment_start is None else round(a + w.segment_start, 3),
                                  segment_end=None if w.segment_end is None else round(a + w.segment_end, 3)))
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps([vars(w) for w in found]), encoding="utf-8")
            os.replace(tmp, cache)
            fresh += 1
            done_seconds += b - a
            left = todo_seconds - done_seconds
            eta = (time.time() - began) / done_seconds * left if done_seconds else 0.0
            say(f"  window {n}/{len(pieces)}  {_mmss(a)}-{_mmss(b)}  {len(found)} words"
                + (f"  ·  {_mmss(left)} of audio left, about {_mmss(eta)}" if left > 0.5 else ""))
        if on_piece is not None:
            on_piece(n, len(pieces))
        words += found
    return sorted(words, key=lambda w: w.start)


# ------------------------------------------------------------------ the progress file
class Progress:
    """progress.json in a stream's output folder: which steps are finished, and with what
    inputs. A step counts as done only when its ``key`` (the inputs that matter to it) is the
    same as last time, so changing --count re-picks but does not re-transcribe."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data.get("steps"), dict):
                raise ValueError
        except (OSError, ValueError, AttributeError):
            self.data = {"steps": {}}
        self._began: dict[str, float] = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)

    def done(self, step: str, key: str = "") -> bool:
        s = self.data["steps"].get(step) or {}
        return bool(s.get("done")) and s.get("key", "") == key

    def info(self, step: str) -> dict:
        return dict(self.data["steps"].get(step) or {})

    def start(self, step: str, key: str = "", **info) -> None:
        self._began[step] = time.time()
        self.data["steps"][step] = {"done": False, "key": key, "started": time.strftime("%Y-%m-%d %H:%M:%S"), **info}
        self._save()

    def update(self, step: str, **info) -> None:
        self.data["steps"].setdefault(step, {"done": False, "key": ""}).update(info)
        self._save()

    def finish(self, step: str, key: str = "", **info) -> float:
        """Mark the step done. Returns how long it took (seconds)."""
        took = round(time.time() - self._began.pop(step, time.time()), 1)
        entry = self.data["steps"].setdefault(step, {})
        entry.update({"done": True, "key": key, "seconds": took, "finished": time.strftime("%Y-%m-%d %H:%M:%S"), **info})
        self._save()
        return took
