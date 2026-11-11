"""Round 40: the climax teaser hook (a flash-forward cold open).

A clip of 28 s or more opens on 2.5-4.0 s taken from its own back half (15 s in or
later): the start of its highest-stakes line (a realization, a contrarian claim, a
punchline's setup), cut on the hinge word just before the answer is said ("The real
lesson is that..."), so the viewer has to stay for it. Then a two-frame warm-white flash
and a sub-bass thud, and the clip plays from its own first frame.

How it's built, frame-exact:
  1. find_climax() reads the clip's Whisper word timings and picks the hinge (no AI):
     a setup phrase ("the real", "what nobody tells you", "it turns out", "it's not X,
     it's ...") followed by its linking word ("is", "was", "comes down to", "that"),
     scored for stakes, with the answer still to come in the same sentence. The window
     starts on a sentence or clause boundary and its length is a whole number of frames
     of the clip's own frame rate.
  2. The teaser is rendered like the clip (same layout, framing, captions, title card)
     with a steady 1.05x punch-in on the picture (core.video_renderer.render_clip's
     ``punch_in``); the title card banner is up for the whole teaser.
  3. stitch() joins teaser + clip in one FFmpeg pass: the teaser's audio fades out over
     its last 0.06 s (no click on the cut word); the clip's first two frames get the
     flash (#FDFBF7 at 85 %, then 40 %: 0.067 s at 30 fps); the sub-bass hit
     (core.audio_polish.sub_hit: an 80 -> 45 Hz sine drop, 180 ms exponential decay) lands
     on the clip's first sample, mixed through the 1 ms limiter under the master ceiling.

The downstream offset: the clip after the teaser is the clip exactly as rendered before
(its captions, crop keyframes, card triggers and their sounds all in its own timeline),
placed after a teaser of exactly N frames. So every one of its times moves by the same
+N/fps (Teaser.offset) and nothing in it can drift against another: word sync stays
frame-exact by construction. The helpers below (to_output, shift_words, shift_cards,
shift_edit, shift_spans, shift_expression) give those times in the finished file, for
anything that reads the file's timeline later (clip pages, thumbnails, checks).

ENABLE_CLIMAX_HOOK=0 in .env (Settings.enable_climax_hook) turns it off: clips render
exactly as in Round 39.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from core import clip_length

logger = logging.getLogger(__name__)

MIN_CLIP_SECONDS = 28.0  # shorter clips don't get a teaser (it would be most of the clip)
MIN_TEASER_AT = 15.0  # the teaser comes from 15 s into the clip or later (the back half)
TEASER_MIN, TEASER_MAX = 2.5, 4.0  # seconds
IDEAL_SECONDS = 3.2  # among equally good windows, the one closest to this
CUT_FADE = 0.06  # the teaser's audio fades out over its last 0.06 s: no click on the cut
PUNCH_IN = 1.05  # the teaser's picture: 5 % closer on the speaker
FLASH_COLOR = "#FDFBF7"  # warm white (the nyt_archival parchment)
FLASH_OPACITY = (0.85, 0.40)  # frame 1, frame 2 of the clip after the teaser
# Teaser + clip stay within the Shorts / Reels limit. Round 55 (core.clip_length): 180 s; 59 s
# under CLIP_LENGTH=legacy (EditProfile.max_seconds).
MAX_TOTAL_SECONDS = clip_length.TEASER_MAX_TOTAL_SECONDS
MIN_SCORE = 3.0  # below this, no line is strong enough to open on: no teaser
MIN_REVEAL_WORDS = 2  # the answer still to come after the cut, in the same sentence
PRE_ROLL = 0.04  # the teaser starts this much before its first word (its attack)
CUT_TAIL = 0.03  # ...and ends this much after the hinge word (never into the next word)

# Setup phrases: (tokens, weight, kind). "copula": the hinge is the linking word that
# follows ("The real lesson IS that..."). "verb": the phrase itself ends on the hinge
# ("It turns OUT that...", "Here's the THING:").
SETUPS: tuple[tuple[tuple[str, ...], float, str], ...] = (
    (("what", "nobody"), 3.5, "copula"), (("nobody", "tells"), 3.5, "copula"), (("no", "one", "tells"), 3.5, "copula"),
    (("nobody", "talks"), 3.0, "copula"), (("no", "one", "talks"), 3.0, "copula"),
    (("the", "real"), 3.0, "copula"), (("the", "truth"), 3.0, "copula"), (("the", "secret"), 3.0, "copula"),
    (("the", "answer"), 3.0, "copula"), (("the", "one", "thing"), 3.0, "copula"),
    (("the", "only", "thing"), 3.0, "copula"), (("the", "craziest"), 3.0, "copula"),
    (("the", "crazy", "thing"), 3.0, "copula"), (("the", "wild", "thing"), 3.0, "copula"),
    (("the", "opposite"), 3.0, "copula"), (("the", "punchline"), 3.0, "copula"),
    (("the", "key"), 2.5, "copula"), (("the", "biggest"), 2.5, "copula"), (("the", "problem"), 2.5, "copula"),
    (("the", "mistake"), 2.5, "copula"), (("the", "lesson"), 2.5, "copula"), (("the", "reason"), 2.5, "copula"),
    (("the", "catch"), 2.5, "copula"), (("the", "funny", "thing"), 2.5, "copula"),
    (("the", "most", "important"), 2.5, "copula"), (("the", "bottom", "line"), 2.5, "copula"),
    (("the", "thing"), 2.0, "copula"), (("what", "i", "learned"), 2.5, "copula"), (("what", "we", "learned"), 2.5, "copula"),
    (("what", "i", "realized"), 3.0, "copula"), (("what", "we", "realized"), 3.0, "copula"),
    (("what", "actually"), 2.5, "copula"), (("what", "really"), 2.5, "copula"),
    (("here's", "the", "thing"), 3.0, "verb"), (("heres", "the", "thing"), 3.0, "verb"),
    (("here's", "what"), 2.5, "copula"), (("it", "turns", "out"), 3.0, "verb"), (("turns", "out"), 2.5, "verb"),
    (("it", "turned", "out"), 3.0, "verb"), (("i", "realized"), 3.0, "verb"), (("we", "realized"), 3.0, "verb"),
    (("everyone", "thinks"), 3.0, "verb"), (("everybody", "thinks"), 3.0, "verb"), (("people", "think"), 2.5, "verb"),
    (("most", "people", "think"), 3.0, "verb"), (("i", "found", "out"), 2.5, "verb"),
)
LINKERS = (("comes", "down", "to"), ("came", "down", "to"), ("boils", "down", "to"), ("turned", "out", "to", "be"),
           ("turns", "out", "to", "be"))
COPULAS = frozenset({"is", "was", "are", "were", "isn't", "wasn't", "aren't", "weren't", "be", "means", "became",
                     "becomes"})
ABSORB = frozenset({"that", "this", "simply", "actually", "basically", "literally", "just", "really"})
NEGATIONS = frozenset({"not", "isn't", "wasn't", "never"})
STAKES = frozenset({"never", "nobody", "everyone", "everybody", "wrong", "opposite", "biggest", "worst", "best",
                    "only", "most", "secret", "truth", "crazy", "craziest", "insane", "shocking", "mistake",
                    "million", "billion", "trillion", "percent", "everything", "nothing", "always"})
COPULA_SEARCH = 7  # words after a setup phrase to look for its linking word
_NON_WORD = re.compile(r"[^\w']+")


@dataclass(frozen=True)
class Teaser:
    """A climax teaser: source seconds ``start`` to ``cut`` (a whole number of frames,
    ``frames`` at ``rate`` fps), cut on ``hinge`` (the words the clip's answer follows).
    ``offset``: how far every time in the clip after it moves in the finished file."""

    start: float
    cut: float
    frames: int
    rate: float
    hinge: str
    line: str  # the teaser's words, as said
    score: float
    reason: str

    @property
    def offset(self) -> float:
        return self.frames / self.rate

    @property
    def seconds(self) -> float:
        return self.offset


def enabled() -> bool:
    """The teaser is on unless ENABLE_CLIMAX_HOOK=0 (also Settings.enable_climax_hook)."""
    import os

    return os.getenv("ENABLE_CLIMAX_HOOK", "1").strip().lower() not in {"0", "false", "no", "off"}


def _clean(text: Any) -> str:
    return _NON_WORD.sub("", str(text).lower().replace("’", "'")).strip("'")


def _ends(text: Any, marks: tuple[str, ...]) -> bool:
    return str(text).rstrip("\"'”’)").endswith(marks)


def _sentence_end(words: Sequence[Any], k: int) -> bool:
    """Word k closes its sentence (its own punctuation, or a long pause after it)."""
    if _ends(words[k].text, (".", "?", "!")):
        return True
    return k + 1 < len(words) and float(words[k + 1].start) - float(words[k].end) > 0.8


def _boundary(words: Sequence[Any], k: int) -> int:
    """How good a place word k is to start the teaser: 3 a sentence's first word, 2 a
    clause's (after a comma or a pause), 0 mid-clause."""
    if k == 0:
        return 3
    before = words[k - 1]
    gap = float(words[k].start) - float(before.end)
    if _sentence_end(words, k - 1) or gap >= 0.5:
        return 3
    if _ends(before.text, (",", ";", ":", "—", "–", "-")) or gap >= 0.25:
        return 2
    return 0


def _matches(tokens: list[str], k: int, phrase: tuple[str, ...]) -> bool:
    return tuple(tokens[k:k + len(phrase)]) == phrase


def _hinges(words: Sequence[Any], tokens: list[str]) -> Iterable[tuple[int, int, float, str]]:
    """(setup's first word, hinge's last word, weight, the setup) for every setup phrase."""
    n = len(tokens)
    for k in range(n):
        for phrase, weight, kind in SETUPS:
            if not _matches(tokens, k, phrase):
                continue
            last = k + len(phrase) - 1
            if any(_sentence_end(words, j) for j in range(k, last)):
                continue
            hinge = None
            if kind == "verb":
                hinge = last
            else:
                for j in range(last + 1, min(n, last + 1 + COPULA_SEARCH)):
                    linker = next((p for p in LINKERS if _matches(tokens, j, p)), None)
                    if linker is not None:
                        hinge = j + len(linker) - 1
                        break
                    if tokens[j] in COPULAS or _ends(words[j].text, (":",)):
                        hinge = j
                        break
                    if _sentence_end(words, j):
                        break
            if hinge is None or _sentence_end(words, hinge) and not _ends(words[hinge].text, (":",)):
                continue
            yield k, _absorb(words, tokens, hinge), weight, " ".join(phrase)
        # The contrarian turn: "It's not about the money, it's ..." (the hinge: the second "it's").
        if tokens[k] in NEGATIONS and k > 0 and tokens[k - 1] in {"it's", "its", "it", "that's", "thats", "this"}:
            for j in range(k + 2, min(n - 1, k + 12)):
                if tokens[j] in {"it's", "its"} or (tokens[j] == "it" and tokens[j + 1] in {"is", "was"}):
                    hinge = j + (1 if tokens[j] == "it" else 0)
                    if not _sentence_end(words, hinge):
                        yield k - 1, _absorb(words, tokens, hinge), 3.5, "it's not ..., it's"
                    break


def _absorb(words: Sequence[Any], tokens: list[str], hinge: int) -> int:
    """The hinge takes in a following "that" / "actually" / a copula (at most 3 words):
    the cut lands right before the answer itself."""
    for _ in range(3):
        nxt = hinge + 1
        if nxt >= len(tokens) or _sentence_end(words, hinge) or _ends(words[hinge].text, (":",)):
            break
        if tokens[nxt] in ABSORB or tokens[nxt] in COPULAS:
            hinge = nxt
        else:
            break
    return hinge


def _reveal_words(words: Sequence[Any], hinge: int) -> list[Any]:
    """The words after the hinge up to the end of its sentence: the answer."""
    out = []
    for j in range(hinge + 1, len(words)):
        out.append(words[j])
        if _sentence_end(words, j):
            break
    return out


def _stakes(tokens: list[str], first: int, last: int, reveal: list[Any]) -> float:
    bonus = min(2.0, 0.5 * sum(1 for t in tokens[max(0, first - 6):last + 1] if t in STAKES))
    if any(re.search(r"\d", str(w.text)) for w in reveal) or any(_clean(w.text) in STAKES for w in reveal):
        bonus += 1.0
    return bonus


def find_climax(
    words: Sequence[Any],
    clip_start: float,
    clip_end: float,
    rate: float = 30.0,
    max_total: float = MAX_TOTAL_SECONDS,
) -> Teaser | None:
    """The clip's climax teaser, or None (the clip is under 28 s, no line after 15 s is
    strong enough, or the teaser would take the clip over ``max_total``). ``words``:
    Whisper's word timings in source seconds (clip_start..clip_end); ``rate``: the clip's
    frame rate (the teaser is a whole number of its frames)."""
    duration = clip_end - clip_start
    if duration < MIN_CLIP_SECONDS or rate <= 0:
        return None
    spoken = sorted((w for w in words if clip_start - 1e-6 <= float(w.start) and float(w.end) <= clip_end + 1e-6),
                    key=lambda w: float(w.start))
    tokens = [_clean(w.text) for w in spoken]
    earliest = clip_start + MIN_TEASER_AT
    best: tuple[tuple[float, int, float], Teaser] | None = None
    # Setups that end on the same hinge ("what nobody" / "nobody tells") are one climax:
    # it starts on the earliest of them (the whole clause) and counts the strongest.
    merged: dict[int, tuple[int, float, str]] = {}
    for first, hinge, weight, setup in _hinges(spoken, tokens):
        if hinge in merged:
            was_first, was_weight, was_setup = merged[hinge]
            merged[hinge] = (min(first, was_first), max(weight, was_weight), was_setup if was_first <= first else setup)
        else:
            merged[hinge] = (first, weight, setup)
    for hinge, (first, weight, setup) in sorted(merged.items()):
        reveal = _reveal_words(spoken, hinge)
        if len(reveal) < MIN_REVEAL_WORDS or float(reveal[0].start) > clip_end - 1.0:
            continue
        nxt = float(reveal[0].start)
        cut = min(float(spoken[hinge].end) + CUT_TAIL, nxt - 0.01)
        if cut <= float(spoken[hinge].start) + 0.08:
            continue
        window = _window(spoken, first, cut, earliest)
        if window is None:
            continue
        start_index, boundary = window
        start = max(float(spoken[start_index].start) - PRE_ROLL,
                    float(spoken[start_index - 1].end) if start_index > 0 else clip_start, earliest)
        frames = int(round((cut - start) * rate))
        frames = max(int(round(TEASER_MIN * rate + 0.4999)), min(frames, int(TEASER_MAX * rate)))
        start = cut - frames / rate  # frame-exact: the cut stays on the hinge, the start moves < 1 frame
        if start < earliest - 1e-6 or duration + frames / rate > max_total + 1e-6:
            continue
        score = weight + _stakes(tokens, first, hinge, reveal)
        if score < MIN_SCORE:
            continue
        hinge_text = " ".join(str(w.text) for w in spoken[max(first, hinge - 2):hinge + 1])
        line = " ".join(str(w.text) for w in spoken if start - 1e-6 <= float(w.start) < cut)
        teaser = Teaser(start=round(start, 4), cut=round(cut, 4), frames=frames, rate=rate, hinge=hinge_text,
                        line=line, score=round(score, 2),
                        reason=f"\"{setup}\" ... cut on \"{spoken[hinge].text}\", the answer "
                               f"(\"{' '.join(str(w.text) for w in reveal[:4])}...\") still to come")
        rank = (score, boundary, -abs(frames / rate - IDEAL_SECONDS))
        if best is None or rank > best[0]:
            best = (rank, teaser)
    return best[1] if best else None


def _window(words: Sequence[Any], first: int, cut: float, earliest: float) -> tuple[int, int] | None:
    """(the teaser's first word, how good a boundary it is) for a climax whose setup starts
    at word ``first`` and is cut at ``cut``: the window takes in the whole setup, lasts
    TEASER_MIN-TEASER_MAX s, starts no earlier than ``earliest`` and on the best boundary
    there is (a sentence's start, then a clause's), else on the setup itself."""
    found: list[tuple[int, int, float]] = []
    for k in range(first, -1, -1):
        at = float(words[k].start)
        length = cut - at
        if length > TEASER_MAX + 0.5 / 30 or at < earliest - PRE_ROLL:
            break
        if length < TEASER_MIN - 0.5 / 30:
            continue
        boundary = _boundary(words, k)
        if boundary or k == first:
            found.append((k, boundary if boundary else 1, length))
    if not found:
        return None
    k, boundary, _length = max(found, key=lambda item: (item[1], -abs(item[2] - IDEAL_SECONDS)))
    return k, boundary


# ------------------------------------------------------------------ the downstream offset
def to_output(t_source: float, clip_start: float, teaser: Teaser | None) -> float:
    """Where source second ``t_source`` of the clip lands in the finished file."""
    return (t_source - clip_start) + (teaser.offset if teaser else 0.0)


def shift_words(words: Iterable[Any], offset: float) -> list[Any]:
    """Words (clip seconds) as they land in the finished file: every one + ``offset``."""
    return [replace(w, start=round(float(w.start) + offset, 6), end=round(float(w.end) + offset, 6)) for w in words]


def shift_spans(spans: Iterable[tuple[float, float]], offset: float) -> list[tuple[float, float]]:
    return [(round(a + offset, 6), round(b + offset, 6)) for a, b in spans]


def shift_cards(cards: Iterable[Any], offset: float) -> list[Any]:
    """Cards with their triggers (start, end, every item's reveal) moved by ``offset``."""
    out = []
    for card in cards:
        rows = [replace(row, reveal_at=None if row.reveal_at is None else round(row.reveal_at + offset, 6),
                        reveal_to=None if row.reveal_to is None else round(row.reveal_to + offset, 6))
                for row in card.rows]
        out.append(replace(card, start=round(card.start + offset, 6), end=round(card.end + offset, 6), rows=rows))
    return out


def shift_edit(edit: Any, teaser: Teaser | None) -> Any:
    """A clip's graphics (core.edl.ClipEdit, source seconds) on the finished file's own
    timeline: seconds from its first frame, the teaser's offset included."""
    offset = (teaser.offset if teaser else 0.0) - edit.start
    return replace(edit, start=0.0, end=round(edit.end - edit.start + (teaser.offset if teaser else 0.0), 6),
                   cards=shift_cards(edit.cards, offset))


def shift_expression(expression: str, offset: float) -> str:
    """An FFmpeg expression in t (a crop's x keyframes: CropPlan.x_expression) for the
    finished file's timeline: t becomes (t-offset)."""
    return re.sub(r"(?<![\w.])t(?![\w(])", f"(t-{offset:.6f})", expression)


# ------------------------------------------------------------------ the stitch
def _hex(color: str) -> str:
    return "0x" + color.lstrip("#").upper()


def _has_audio(path: Path) -> bool:
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(probe.stdout.strip())


def _duration(path: Path) -> float:
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True)
    try:
        return float(probe.stdout.strip())
    except ValueError:
        return 0.0


def flash_filter(label_in: str, label_out: str) -> str:
    """The two-frame warm-white flash on a stream's first two frames."""
    one, two = FLASH_OPACITY
    color = _hex(FLASH_COLOR)
    return (f"[{label_in}]drawbox=x=0:y=0:w=iw:h=ih:color={color}@{one:.2f}:t=fill:enable='eq(n\\,0)',"
            f"drawbox=x=0:y=0:w=iw:h=ih:color={color}@{two:.2f}:t=fill:enable='eq(n\\,1)'[{label_out}]")


def stitch(teaser_video: Path, body_video: Path, output: Path, teaser: Teaser, work_dir: Path) -> Path:
    """teaser + flash + sub-bass hit + the clip, in one pass (see the module docstring).
    The teaser is trimmed to exactly ``teaser.frames`` frames and its audio to exactly
    that long, so the clip starts on frame ``teaser.frames`` and sample offset*48000."""
    from core import audio_polish
    from core.video_renderer import AUDIO_SAMPLE_RATE, _video_encoder_args

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg was not found.")
    work_dir.mkdir(parents=True, exist_ok=True)
    seconds = teaser.offset
    body_seconds = _duration(body_video)
    total = seconds + body_seconds
    inputs = ["-i", str(teaser_video), "-i", str(body_video)]
    graph = [
        f"[0:v]trim=end_frame={teaser.frames},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[tv]",
        "[1:v]setpts=PTS-STARTPTS,setsar=1,format=yuv420p[bv0]",
        flash_filter("bv0", "bv"),
    ]
    teaser_audio, body_audio = _has_audio(teaser_video), _has_audio(body_video)
    audio_map: list[str] = []
    if teaser_audio or body_audio:
        silent = f"aevalsrc=0:c=mono:s={AUDIO_SAMPLE_RATE}"
        graph.append(f"[0:a]aresample={AUDIO_SAMPLE_RATE},aformat=channel_layouts=mono,atrim=0:{seconds:.6f},"
                     f"asetpts=PTS-STARTPTS,apad=whole_dur={seconds:.6f},"
                     f"afade=t=out:st={seconds - CUT_FADE:.6f}:d={CUT_FADE}[ta]" if teaser_audio
                     else f"{silent}:d={seconds:.6f}[ta]")
        graph.append(f"[1:a]aresample={AUDIO_SAMPLE_RATE},aformat=channel_layouts=mono,asetpts=PTS-STARTPTS[ba]"
                     if body_audio else f"{silent}:d={body_seconds:.6f}[ba]")
        graph.append("[tv][ta][bv][ba]concat=n=2:v=1:a=1[v][mixed]")
        track = (audio_polish.cue_track([(seconds, "sub_hit")], total, work_dir / "sub_hit.wav")
                 if audio_polish.enabled() else None)
        from core import textless

        if track is not None and textless.active():  # Round 57: a stem for the editor, not in the mix
            textless.keep_stem("sfx", track)
            track = None
        if track is not None:
            inputs += ["-i", str(track)]
            graph.append(f"[mixed][2:a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                         f"{audio_polish.SFX_LIMITER}[a]")
        else:
            graph.append(f"[mixed]{audio_polish.SFX_LIMITER}[a]")
        audio_map = ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", AUDIO_SAMPLE_RATE]
    else:
        graph.append("[tv][bv]concat=n=2:v=1:a=0[v]")
    encoder = list(_video_encoder_args(ffmpeg))
    from core import textless

    if "-crf" in encoder and not textless.active():  # the clip is encoded a second time here: a little more quality for it
        encoder[encoder.index("-crf") + 1] = "16"
    destination = output.with_name(f"{output.stem}.stitch{output.suffix}")
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex", ";".join(graph),
               "-map", "[v]", *audio_map, *encoder, "-pix_fmt", "yuv420p",
               "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
               "-fps_mode", "passthrough", "-movflags", "+faststart", str(destination)]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=600)
    except subprocess.CalledProcessError as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"The climax teaser stitch failed: {(exc.stderr or '').strip()[-600:]}") from exc
    destination.replace(output)
    return output
