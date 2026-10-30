"""Series mode (🎬 Make clips): a whole video as Part 1, Part 2, … in order.

The parts are contiguous: each one ends at the first sentence end after the shorter
length (never later than the longer one), and the next one starts right there, so
nothing that's said is left out. Long silences between two parts are trimmed to a
short breath on each side.

The plan covers the whole video and is saved with its transcript in
storage/series/<video>.json. So every batch numbers its parts the same way (batch 1 =
Parts 1–3, batch 2 = Parts 4–6, …), and a later batch of the same video doesn't
download the audio or call Groq again.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.hook_selector import _sentence_spans
from core.transcriber import Word, youtube_video_id

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERIES_DIR = PROJECT_ROOT / "storage" / "series"
BATCH_SIZE = 3
MIN_PART_SECONDS = 60
MAX_PART_SECONDS = 180  # YouTube Shorts' limit
DEFAULT_LENGTHS = (60, 90)
SHORT_LAST_PART = 20.0  # a last part shorter than this joins the part before, if that fits
BREATH = 0.4  # seconds of the pause kept at the end and start of two parts in a row
MAX_TITLE_CHARS = 45  # "Part 12: " plus this still fits the title card on two lines


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------
def _split(end_of_speech: float, next_speech: float) -> tuple[float, float]:
    """Where one part ends and the next starts, in the pause between two words.
    Short pause: both at its middle. Long pause: a breath each side, silence dropped."""
    gap = max(0.0, next_speech - end_of_speech)
    if gap <= 2 * BREATH:
        middle = end_of_speech + gap / 2
        return middle, middle
    return end_of_speech + BREATH, next_speech - BREATH


def plan_parts(
    words: list[Word],
    duration: float,
    shortest: float = DEFAULT_LENGTHS[0],
    longest: float = DEFAULT_LENGTHS[1],
) -> list[tuple[float, float]]:
    """The whole video as (start, end) parts, in order, in source seconds.

    Each part is at least ``shortest`` and at most ``longest`` seconds, ending at the
    first sentence end after ``shortest``. Without one (a long run-on sentence, no
    punctuation): the longest pause between words in that window; without any words
    (music, silence): exactly ``longest``. Only the last part can be shorter.
    """
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("The video needs a length.")
    shortest = max(1.0, float(shortest))
    longest = max(shortest, float(longest))
    ordered = sorted((w for w in words if w.end > w.start), key=lambda w: (w.start, w.end))

    sentence_cuts: list[tuple[float, float]] = []
    for _start, end, index in _sentence_spans(ordered):
        next_speech = ordered[index + 1].start if index + 1 < len(ordered) else duration
        sentence_cuts.append(_split(end, next_speech))

    parts: list[tuple[float, float]] = []
    start = 0.0
    while duration - start > longest:
        low, high = start + shortest, start + longest
        cut = next((c for c in sentence_cuts if low <= c[0] <= high), None)
        if cut is None:
            pauses = [
                (ordered[i + 1].start - word.end, word.end, ordered[i + 1].start)
                for i, word in enumerate(ordered[:-1])
                if low <= word.end <= high - BREATH and ordered[i + 1].start > word.end
            ]
            if pauses:
                _gap, end_of_speech, next_speech = max(pauses)
                cut = _split(end_of_speech, next_speech)
            else:
                # No pause to use: at ``longest``, moved to the end of a word said across it.
                at = high
                for word in ordered:
                    if word.start < at < word.end:
                        at = word.end if word.end - start <= MAX_PART_SECONDS else word.start
                        break
                cut = (at, at)
        parts.append((start, cut[0]))
        start = cut[1]
    if duration - start > 0.05:
        if parts and duration - start < SHORT_LAST_PART and duration - parts[-1][0] <= MAX_PART_SECONDS:
            parts[-1] = (parts[-1][0], duration)
        else:
            parts.append((start, duration))
    return [(round(a, 3), round(b, 3)) for a, b in parts]


def batch_count(parts: list[tuple[float, float]]) -> int:
    return max(1, math.ceil(len(parts) / BATCH_SIZE))


def batch_parts(parts: list[tuple[float, float]], batch: int) -> list[tuple[int, float, float]]:
    """Batch 1 = Parts 1–3, batch 2 = Parts 4–6, …: (part number, start, end)."""
    if batch < 1 or batch > batch_count(parts):
        raise ValueError(f"This video has {len(parts)} parts: batches 1 to {batch_count(parts)}.")
    first = (batch - 1) * BATCH_SIZE
    return [(first + i + 1, start, end) for i, (start, end) in enumerate(parts[first:first + BATCH_SIZE])]


def clean_series_title(title: str) -> str:
    """The video title, shortened for the title card (at most MAX_TITLE_CHARS, whole words)."""
    text = " ".join(str(title or "").replace("_", " ").split()).strip(" -–—|:")
    text = re.sub(r"\s*[|(\[].*$", "", text) or text  # "Title | Podcast Ep 12" -> "Title"
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS].rsplit(" ", 1)[0].rstrip(",.;:-–—")
    return text


def part_title(number: int, title: str) -> str:
    """'Part 4: The British Are Coming' (the title card and the fallback title)."""
    title = clean_series_title(title)
    return f"Part {number}: {title}" if title else f"Part {number}"


# ---------------------------------------------------------------------------
# What's been made (storage/series/<key>.json)
# ---------------------------------------------------------------------------
def series_key(url: str = "", file_name: str = "", file_head: bytes = b"", file_size: int = 0) -> str:
    """'yt-<video id>' for a YouTube link; for a file, a fingerprint of its name, size
    and first bytes (the same file uploaded again gets the same series)."""
    video_id = youtube_video_id(url) if url else None
    if video_id and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
        return f"yt-{video_id}"
    if url:
        return "url-" + hashlib.sha1(url.strip().encode()).hexdigest()[:12]
    digest = hashlib.sha1(f"{file_name}|{file_size}|".encode() + file_head[:1_000_000]).hexdigest()[:12]
    return f"file-{digest}"


def _path(key: str) -> Path:
    if not re.fullmatch(r"[a-z]+-[A-Za-z0-9_-]{6,40}", key or ""):
        raise ValueError(f"Not a series key: {key!r}")
    return SERIES_DIR / f"{key}.json"


def load_series(key: str) -> dict[str, Any] | None:
    try:
        path = _path(key)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None


def save_series(key: str, state: dict[str, Any]) -> None:
    path = _path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(path)


def words_to_rows(words: list[Word]) -> list[list[Any]]:
    return [[w.text, w.start, w.end, w.segment_start, w.segment_end] for w in words]


def rows_to_words(rows: list[list[Any]]) -> list[Word]:
    return [Word(*row) for row in rows]


def next_batch(state: dict[str, Any] | None, shortest: float, longest: float) -> int:
    """The batch after the last one made with these part lengths (1 if none)."""
    if not state or (state.get("shortest"), state.get("longest")) != (shortest, longest):
        return 1
    made = [int(n) for n in state.get("made") or []]
    if not made:
        return 1
    parts = plan_parts(rows_to_words(state.get("words") or []), float(state["duration"]), shortest, longest)
    return min(batch_count(parts), max(made) // BATCH_SIZE + 1)
