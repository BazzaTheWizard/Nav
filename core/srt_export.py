"""Round 46: each clip's captions as an SRT file (<clip>.srt) on the finished file's timeline.

LinkedIn lets a company upload a captions file with a native video, which marketing teams
need for accessibility and brand compliance. Same lines as the burned-in executive captions
(core.executive.chunks): sentence case, at most two lines; the cold-open teaser comes first.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _stamp(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def cues(words: list[Any], offset: float = 0.0) -> list[tuple[float, float, str]]:
    """(start, end, text) cues for ``words`` (seconds), every time + ``offset``."""
    from core import executive

    out: list[tuple[float, float, str]] = []
    sentence_start = True
    groups = executive.chunks(sorted(words, key=lambda w: float(w.start)))
    for index, group in enumerate(groups):
        tokens = [executive._caption_token(w.text).replace("（", "{").replace("）", "}") for w in group]
        if sentence_start and tokens[0][:1].islower():
            tokens[0] = tokens[0][:1].upper() + tokens[0][1:]
        sentence_start = group[-1].text.rstrip("\"'”’)").endswith((".", "?", "!"))
        cut = executive.line_break(tokens)
        text = " ".join(tokens) if cut is None else " ".join(tokens[:cut]) + "\n" + " ".join(tokens[cut:])
        start = float(group[0].start)
        end = float(group[-1].end) + executive.HOLD
        if index + 1 < len(groups):
            end = min(end, max(float(group[-1].end), float(groups[index + 1][0].start)))
        out.append((round(start + offset, 3), round(end + offset, 3), text))
    return out


def _duration(clip: Path) -> float:
    import subprocess

    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(clip)],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out) if out else 0.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def to_srt(cue_list: list[tuple[float, float, str]]) -> str:
    return "".join(f"{n}\n{_stamp(a)} --> {_stamp(b)}\n{text}\n\n" for n, (a, b, text) in enumerate(cue_list, 1))


def write_for_clip(clip: Path, clip_words: list[Any], teaser: Any = None, source_words: list[Any] | None = None) -> Path:
    """Write <clip>.srt next to the clip. ``clip_words``: clip seconds (after any trim);
    ``source_words``: the same words in the render's source seconds, for the teaser's line."""
    all_cues: list[tuple[float, float, str]] = []
    offset = 0.0
    if teaser is not None:
        offset = float(teaser.offset)
        said = [w for w in (source_words or []) if float(w.start) >= teaser.start - 0.01 and float(w.end) <= teaser.cut + 0.05]
        shifted = [type(w)(text=w.text, start=float(w.start) - teaser.start, end=min(float(w.end) - teaser.start, offset))
                   for w in said]
        all_cues += [(a, min(b, offset), t) for a, b, t in cues(shifted) if a < offset]
    all_cues += cues(clip_words, offset)
    length = _duration(Path(clip))
    if length:  # Round 52: a caption's hold never runs past the end of the video
        all_cues = [(a, round(min(b, length), 3), t) for a, b, t in all_cues if a < length - 0.05]
    path = Path(clip).with_suffix(".srt")
    path.write_text(to_srt(all_cues), encoding="utf-8")
    return path
