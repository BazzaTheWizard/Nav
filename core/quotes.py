"""✂️ Quote grabber (Make clips): one exact line from a podcast, as a 16:9 MP4 for Keynote.

1. YouTube's own captions are read (nothing is downloaded) and searched for the line.
   No captions: type the time instead.
2. Only that part of the video is downloaded (the line plus SECTION_PADDING on each
   side), and only that part is transcribed by Groq: word-exact times for under a
   minute of audio, instead of transcribing a 2-hour podcast.
3. You pick the first and last word; the quote is cut there (with the same small
   lead-in and tail the Shorts use) and saved, picture and sound re-encoded so it
   starts exactly on the word.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from core.transcriber import Word

logger = logging.getLogger(__name__)

SECTION_PADDING = 10.0  # seconds downloaded before and after the line you picked
MAX_SECTION_SECONDS = 150.0
MAX_RESULTS = 20
JOIN_GAP = 1.5  # two caption lines are searched as one only when they follow each other
PRE_ROLL = 0.15  # same lead-in and tail as the Shorts (core.hook_selector)
POST_ROLL = 0.4
MAX_QUOTE_HEIGHT = 1080
_WORD = re.compile(r"[a-z0-9$%']+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("’", "'"))


def search_captions(lines: list[tuple[float, float, str]], query: str) -> list[tuple[float, float, str]]:
    """Caption lines (or two neighbouring lines: a sentence often spans both) that
    contain every word of ``query``, in order of time. At most MAX_RESULTS."""
    wanted = _words(query)
    if not wanted:
        return []

    def has_all(text: str) -> bool:
        words = _words(text)
        return all(any(word.startswith(part) for word in words) for part in wanted)

    alone = [has_all(text) for _start, _end, text in lines]
    found: list[tuple[float, float, str]] = []
    used_until = -1
    for index, (start, end, text) in enumerate(lines):
        if index <= used_until:
            continue
        if alone[index]:
            found.append((start, end, text))
        elif (
            index + 1 < len(lines)
            and not alone[index + 1]  # the next line has it all by itself: that one is listed
            and lines[index + 1][0] - end <= JOIN_GAP
            and has_all(f"{text} {lines[index + 1][2]}")
        ):
            found.append((start, lines[index + 1][1], f"{text} {lines[index + 1][2]}"))
            used_until = index + 1
        if len(found) >= MAX_RESULTS:
            break
    return found


def parse_time(text: str) -> float | None:
    """"12:05", "1:02:09" or "725" -> seconds; None if it isn't a time."""
    parts = text.strip().split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    if not 1 <= len(numbers) <= 3 or any(n < 0 for n in numbers) or any(n >= 60 for n in numbers[1:]):
        return None
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def section_for(start: float, end: float, duration: float | None = None) -> tuple[float, float]:
    """The part to download around a line: SECTION_PADDING each side, at most
    MAX_SECTION_SECONDS long, inside the video."""
    section_start = max(0.0, start - SECTION_PADDING)
    section_end = min(end + SECTION_PADDING, section_start + MAX_SECTION_SECONDS)
    if duration:
        section_end = min(section_end, float(duration))
    if section_end <= section_start:
        raise ValueError("That time is past the end of the video.")
    return section_start, section_end


WORD_SLACK = 0.35  # caption times are rough: a word this close to the line still counts


def words_near(words: list[Word], start: float, end: float) -> tuple[int, int]:
    """First and last word index inside ``start``-``end`` (section-relative), for the
    word picker's starting position."""
    inside = [i for i, word in enumerate(words) if word.end > start - WORD_SLACK and word.start < end + WORD_SLACK]
    if not inside:
        return 0, max(0, len(words) - 1)
    return inside[0], inside[-1]


def cut_quote(section: Path, start: float, end: float, output: Path) -> Path:
    """Cut ``start``-``end`` (section-relative seconds, padded with the lead-in and tail)
    out of the downloaded part, re-encoded so it starts exactly there. 16:9 stays 16:9,
    at most 1080 tall."""
    from core.video_renderer import _video_encoder_args

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg was not found. Install it with `brew install ffmpeg`.")
    begin = max(0.0, start - PRE_ROLL)
    length = end + POST_ROLL - begin
    if length <= 0.3:
        raise ValueError("Pick an end word after the start word.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.part.mp4")
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{begin:.3f}", "-i", str(section),
            "-t", f"{length:.3f}", "-vf", f"scale=-2:'min({MAX_QUOTE_HEIGHT},ih)':flags=lanczos",
            *_video_encoder_args(ffmpeg), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(temporary),
        ],
        check=True, capture_output=True, timeout=300,
    )
    temporary.replace(output)
    logger.info("Saved a %.1f s quote: %s", length, output.name)
    return output
