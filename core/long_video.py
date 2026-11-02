"""🧾 Finish a long video (Make clips): the last steps before uploading a long video.

From the export of a narrated slide video (Keynote, Canva):
* the same video with cleaned-up sound at the usual -14 LUFS, -1 dBTP; the picture is
  copied untouched (no re-encode, so it takes seconds and loses nothing),
* a caption file (.srt) to upload to YouTube instead of its automatic captions,
* chapters (checked against YouTube's rules) and three title ideas, drafted by the AI
  from the transcript, in a text file to paste from.

The transcript is saved by the file's fingerprint, so making Shorts from the same file
afterwards doesn't pay Groq a second time.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config import PROJECT_ROOT
from core.transcriber import Word

logger = logging.getLogger(__name__)

TRANSCRIPTS_DIR = PROJECT_ROOT / "storage" / "transcripts" / "files"
FINGERPRINT_BYTES = 4 * 1024 * 1024
# YouTube's chapter rules: the first at 0:00, at least 3, each at least 10 s.
MIN_CHAPTERS = 3
MIN_CHAPTER_SECONDS = 10.0
FIRST_CHAPTER_SNAP = 30.0  # a first chapter this close to the start is moved to 0:00
MAX_TITLE_CHARS = 100  # YouTube's limit
# Caption cues (.srt): short enough to read, cut at sentence ends and pauses.
CUE_MAX_CHARS = 84  # two lines of 42
CUE_LINE_CHARS = 42
CUE_MAX_SECONDS = 6.0
CUE_PAUSE_SECONDS = 1.0


# --- Transcripts of files ------------------------------------------------------------
def file_fingerprint(path: Path) -> str:
    """Same file, same fingerprint (its size and its first and last 4 MB), so a copy
    or an upload of the same export is recognized."""
    size = path.stat().st_size
    digest = hashlib.sha1(str(size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(FINGERPRINT_BYTES))
        if size > FINGERPRINT_BYTES:
            handle.seek(max(FINGERPRINT_BYTES, size - FINGERPRINT_BYTES))
            digest.update(handle.read(FINGERPRINT_BYTES))
    return digest.hexdigest()[:20]


def load_file_transcript(fingerprint: str) -> list[Word] | None:
    path = TRANSCRIPTS_DIR / f"{fingerprint}.json"
    try:
        return [Word(*row) for row in json.loads(path.read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return None


def save_file_transcript(fingerprint: str, words: list[Word]) -> None:
    try:
        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        rows = [[w.text, w.start, w.end, w.segment_start, w.segment_end] for w in words]
        (TRANSCRIPTS_DIR / f"{fingerprint}.json").write_text(json.dumps(rows), encoding="utf-8")
    except OSError:
        logger.warning("Couldn't save the transcript (%s).", fingerprint, exc_info=True)


# --- Captions file ---------------------------------------------------------------------
def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def caption_cues(words: list[Word]) -> list[tuple[float, float, str]]:
    """Words -> (start, end, text) cues: at most two lines of 42 characters and 6 s,
    starting a new cue after a sentence end or a pause."""
    cues: list[tuple[float, float, str]] = []
    current: list[Word] = []

    def close() -> None:
        if current:
            cues.append((current[0].start, current[-1].end, " ".join(w.text.strip() for w in current)))
            current.clear()

    for word in words:
        if not word.text.strip():
            continue
        if current:
            text = " ".join(w.text.strip() for w in current)
            ends_sentence = current[-1].text.rstrip().endswith((".", "?", "!"))
            if (
                len(text) + 1 + len(word.text.strip()) > CUE_MAX_CHARS
                or word.end - current[0].start > CUE_MAX_SECONDS
                or word.start - current[-1].end > CUE_PAUSE_SECONDS
                or (ends_sentence and len(current) >= 3)
            ):
                close()
        current.append(word)
    close()
    # Each cue ends before the next one starts.
    return [
        (start, min(end, cues[index + 1][0]) if index + 1 < len(cues) else end, text)
        for index, (start, end, text) in enumerate(cues)
    ]


def words_to_srt(words: list[Word]) -> str:
    blocks = []
    for number, (start, end, text) in enumerate(caption_cues(words), start=1):
        lines = textwrap.wrap(text, CUE_LINE_CHARS) or [text]
        if len(lines) > 2:  # a long word pushed a third line: two balanced lines instead
            middle = len(text.split()) // 2
            lines = [" ".join(text.split()[:middle]), " ".join(text.split()[middle:])]
        blocks.append(f"{number}\n{_srt_time(start)} --> {_srt_time(max(end, start + 0.3))}\n" + "\n".join(lines))
    return "\n\n".join(blocks) + "\n"


# --- Chapters and title ideas ----------------------------------------------------------
def timestamp(seconds: float) -> str:
    """0:00, 12:05 or 1:02:09, as YouTube writes them."""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def valid_chapters(raw: list[dict[str, Any]], duration: float) -> list[tuple[float, str]]:
    """Chapters YouTube will show: the first at 0:00, each at least 10 s long, at least
    3. Out-of-order, too-close or empty ones are dropped. [] if fewer than 3 are left."""
    items: list[tuple[float, str]] = []
    for item in raw:
        try:
            start = float(item["start"])
        except (KeyError, TypeError, ValueError):
            continue
        title = " ".join(str(item.get("title") or "").split())[:MAX_TITLE_CHARS]
        if title and 0 <= start < duration:
            items.append((start, title))
    items.sort()
    if not items:
        return []
    if items[0][0] <= FIRST_CHAPTER_SNAP:
        items[0] = (0.0, items[0][1])
    else:
        items.insert(0, (0.0, "Intro"))
    kept: list[tuple[float, str]] = []
    for start, title in items:
        if kept and start - kept[-1][0] < MIN_CHAPTER_SECONDS:
            continue
        kept.append((start, title))
    while len(kept) > 1 and duration - kept[-1][0] < MIN_CHAPTER_SECONDS:
        kept.pop()
    return kept if len(kept) >= MIN_CHAPTERS else []


def chapters_text(chapters: list[tuple[float, str]]) -> str:
    return "\n".join(f"{timestamp(start)} {title}" for start, title in chapters)


def clean_titles(titles: list[str]) -> list[str]:
    cleaned = []
    for title in titles:
        title = " ".join(str(title).split()).strip("\"'“” ")
        if title and title not in cleaned:
            cleaned.append(title[:MAX_TITLE_CHARS])
    return cleaned[:3]


# --- The finished file -----------------------------------------------------------------
def finish_audio(source: Path, output: Path, duration: float) -> Path:
    """The same video with the clips' sound treatment (cleanup, -14 LUFS, -1 dBTP);
    the picture stream is copied as it is."""
    from core.video_renderer import (
        AAC_OVERSHOOT_DB, AUDIO_SAMPLE_RATE, LOUDNESS_RANGE, MAX_GAIN_DB, TRUE_PEAK_DB,
        _measure_loudness, audio_cleanup, loudness_target,
    )

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg was not found. Install it with `brew install ffmpeg`.")
    cleanup = audio_cleanup()
    chain = "aresample=async=1:first_pts=0" + (f",{cleanup}" if cleanup else "")
    target = loudness_target()
    measured = None
    if target is not None:
        measured = _measure_loudness(ffmpeg, source, 0.0, duration, target, cleanup, timeout=120 + duration)
    if measured is not None and float(measured["input_i"]) >= target - MAX_GAIN_DB:
        chain += (
            f",loudnorm=I={target}:TP={TRUE_PEAK_DB - AAC_OVERSHOOT_DB:g}:LRA={LOUDNESS_RANGE}"
            f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true:print_format=none,aresample={AUDIO_SAMPLE_RATE}"
        )
    else:
        logger.warning("Couldn't measure the loudness of %s; only the cleanup is applied.", source.name)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.part{output.suffix}")
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-map", "0:v:0", "-c:v", "copy", "-map", "0:a:0", "-af", chain,
            "-c:a", "aac", "-b:a", "192k", "-ar", AUDIO_SAMPLE_RATE, "-shortest", "-movflags", "+faststart",
            str(temporary),
        ],
        check=True, capture_output=True, timeout=600 + 2 * duration,
    )
    temporary.replace(output)
    return output


@dataclass
class FinishedVideo:
    video: Path
    captions: Path
    notes: Path
    chapters: list[tuple[float, str]] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def finish_long_video(
    source: Path,
    out_dir: Path,
    words: list[Word],
    duration: float,
    *,
    suggest: Callable[[list[Word]], dict[str, Any] | None] | None = None,
) -> FinishedVideo:
    """Write "<name> (finished).mp4", "<name>.srt" and "<name> - chapters and titles.txt"
    into ``out_dir``. ``suggest``: the AI drafting chapters and titles (default:
    core.agent_brain.suggest_chapters_and_titles). A failed suggestion is reported in
    ``problems``; the video and the captions are still made."""
    if suggest is None:
        from core.agent_brain import suggest_chapters_and_titles as suggest
    name = source.stem.removesuffix(" (finished)")
    out_dir.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    video = finish_audio(source, out_dir / f"{name} (finished).mp4", duration)
    captions = out_dir / f"{name}.srt"
    captions.write_text(words_to_srt(words), encoding="utf-8")
    chapters: list[tuple[float, str]] = []
    titles: list[str] = []
    try:
        suggestion = suggest(words)
    except Exception as exc:  # noqa: BLE001 - shown to you; the files above are fine
        logger.warning("Chapter suggestions failed: %s", exc)
        suggestion = None
    if suggestion is None:
        problems.append("The AI didn't draft chapters or titles this time (no answer). Try again, or write them yourself.")
    else:
        chapters = valid_chapters(suggestion.get("chapters") or [], duration)
        titles = clean_titles(suggestion.get("titles") or [])
        if not chapters:
            problems.append("The AI's chapters didn't meet YouTube's rules (3+, first at 0:00, 10 s each), so none are listed.")
    notes = out_dir / f"{name} - chapters and titles.txt"
    notes.write_text(
        "Title ideas (test them with Test & Compare in YouTube Studio):\n"
        + ("\n".join(f"{n}. {title}" for n, title in enumerate(titles, start=1)) or "(none)")
        + "\n\nChapters (paste into the description):\n" + (chapters_text(chapters) or "(none)") + "\n",
        encoding="utf-8",
    )
    logger.info("Finished %s: %d chapter(s), %d title idea(s).", name, len(chapters), len(titles))
    return FinishedVideo(video=video, captions=captions, notes=notes, chapters=chapters, titles=titles, problems=problems)
