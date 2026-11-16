"""Round 59: your voiceover + your script -> word timings, in the script's own spelling.

You already know every word you read, so the captions should be the script, not Whisper's
guess at it (no "Leonida" heard as "Leonita", no "GTA" heard as "G.T.A."). Two ways:

1. stable-ts forced alignment (pip install stable-ts), when installed: it places each script
   word on the audio directly.
2. Otherwise: Nav's own transcriber (faster-whisper / Groq, core.transcriber) hears the
   voiceover, and the script's words are matched onto the heard words (difflib). Matched
   words take the heard word's timing; the few unmatched ones (an ad-lib, a misheard name)
   are spread evenly between their matched neighbours.

Either way the result is core.transcriber.Word objects, so every caption and timing module
in Nav takes them as they are.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from core.narrate.script import Script
from core.transcriber import Word

logger = logging.getLogger(__name__)
_TOKEN = re.compile(r"[a-z0-9]+")


def norm(word: str) -> str:
    return "".join(_TOKEN.findall(word.lower()))


def script_words(script: Script) -> list[tuple[str, int]]:
    """(word as written, line index) for every spoken word."""
    return [(w, i) for i, line in enumerate(script.lines) for w in line.text.split()]


def map_onto(written: list[str], heard: list[Word]) -> list[Word]:
    """Give each written word a time from the heard transcript (see the module docstring)."""
    if not written:
        return []
    if not heard:
        raise ValueError("The voiceover transcript is empty: is the audio silent?")
    a = [norm(w) for w in written]
    b = [norm(w.text) for w in heard]
    times: list[tuple[float, float] | None] = [None] * len(written)
    for block in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            h = heard[block.b + k]
            times[block.a + k] = (h.start, h.end)
    # unmatched runs: share the gap between the matched words around them
    first_start, last_end = heard[0].start, heard[-1].end
    i = 0
    while i < len(times):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(times) and times[j] is None:
            j += 1
        left = times[i - 1][1] if i > 0 else first_start
        right = times[j][0] if j < len(times) else last_end
        right = max(right, left + 0.12 * (j - i))
        step = (right - left) / (j - i)
        for k in range(i, j):
            times[k] = (left + (k - i) * step, left + (k - i + 1) * step)
        i = j
    words: list[Word] = []
    prev_end = 0.0
    for text, (start, end) in zip(written, times):  # type: ignore[misc]
        start = max(start, prev_end)
        end = max(end, start + 0.05)
        words.append(Word(text=text, start=round(start, 3), end=round(end, 3)))
        prev_end = end
    return words


def _stable_ts(audio: Path, text: str, model_size: str) -> list[Word] | None:
    try:
        import stable_whisper
    except ImportError:
        return None
    try:
        model = stable_whisper.load_model(model_size)
        result = model.align(str(audio), text, language="en")
        return [Word(text=w.word.strip(), start=float(w.start), end=float(w.end))
                for w in result.all_words() if w.word.strip()]
    except Exception:  # noqa: BLE001 - fall back to transcribe-and-match
        logger.warning("stable-ts alignment failed; matching onto a transcript instead.", exc_info=True)
        return None


def align(audio: str | Path, script: Script, settings=None, heard: list[Word] | None = None) -> list[Word]:
    """Word timings for the script's words on this voiceover. ``heard`` (tests): a transcript
    to match onto instead of transcribing."""
    audio = Path(audio)
    written = [w for w, _ in script_words(script)]
    if heard is None:
        aligned = _stable_ts(audio, " ".join(written), getattr(settings, "whisper_model", "small"))
        if aligned and len(aligned) >= 0.8 * len(written):
            # stable-ts keeps the script's words; re-map so the spelling is exactly the script's
            return map_onto(written, aligned)
        from core.transcriber import transcribe_clip_audio

        heard = transcribe_clip_audio(audio, settings)
    return map_onto(written, heard)


@dataclass
class LineSpan:
    index: int
    start: float  # the line's first word
    end: float  # the next line's first word (the last line: its last word + tail)


def line_spans(script: Script, words: list[Word], tail: float = 0.35) -> list[LineSpan]:
    """When each script line is on screen. Line 0 starts at 0 so the picture starts with the audio."""
    owners = [i for _, i in script_words(script)]
    firsts: dict[int, float] = {}
    last_end = 0.0
    for word, line in zip(words, owners):
        firsts.setdefault(line, word.start)
        last_end = max(last_end, word.end)
    order = sorted(firsts)
    spans: list[LineSpan] = []
    for n, line in enumerate(order):
        start = 0.0 if n == 0 else firsts[line]
        end = firsts[order[n + 1]] if n + 1 < len(order) else last_end + tail
        spans.append(LineSpan(line, start, end))
    return spans
