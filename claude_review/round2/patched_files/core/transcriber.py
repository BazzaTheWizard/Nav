from __future__ import annotations

import bisect
import functools
import logging
import math
import os
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"

from faster_whisper import BatchedInferencePipeline, WhisperModel
from youtube_transcript_api import YouTubeTranscriptApi

from config import Settings


logger = logging.getLogger(__name__)
ProgressCallback = Callable[[float, str], None]
GROQ_TRANSCRIPTION_MODEL = "whisper-large-v3-turbo"
GROQ_MAX_UPLOAD_BYTES = 24 * 1024 * 1024
# MP3 bitrates (kbps) that are valid at a 16 kHz sample rate. The highest one that
# keeps the upload under GROQ_MAX_UPLOAD_BYTES is chosen, so long videos still fit.
_GROQ_MP3_BITRATES_KBPS = (64, 56, 48, 40, 32, 24, 16)
_WHISPER_MODEL_LOCK = threading.Lock()


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    segment_start: float | None = None
    segment_end: float | None = None


@functools.lru_cache(maxsize=2)
def _load_whisper_model(
    model_name: str,
    device: str,
    compute_type: str,
) -> WhisperModel:
    return WhisperModel(
        model_name,
        device=device,
        compute_type=compute_type,
        cpu_threads=4,
        num_workers=1,
    )


def _get_whisper_model(
    model_name: str,
    device: str,
    compute_type: str,
) -> WhisperModel:
    # Cached per process. Works the same in Streamlit, the CLI and the worker,
    # without importing Streamlit into core modules.
    with _WHISPER_MODEL_LOCK:
        return _load_whisper_model(model_name, device, compute_type)


def youtube_video_id(url: str) -> str | None:
    """Extract a video ID from a standard YouTube URL."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
        return video_id or None
    if hostname == "youtube.com" or hostname.endswith(".youtube.com"):
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
            return path_parts[1] or None
    return None


def transcribe_youtube_captions(video_id: str) -> list[Word] | None:
    """Return YouTube captions as timestamped transcript snippets when available."""
    try:
        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            snippets = YouTubeTranscriptApi.get_transcript(video_id)
        else:
            transcript = YouTubeTranscriptApi().fetch(video_id)
            snippets = transcript.snippets
    except Exception as exc:
        logger.info(
            "YouTube captions unavailable for %s; using local transcription: %s",
            video_id,
            exc,
        )
        return None

    words: list[Word] = []
    try:
        for snippet in snippets:
            if isinstance(snippet, dict):
                text = str(snippet.get("text", "")).strip()
                start = float(snippet["start"])
                end = start + float(snippet["duration"])
            else:
                text = str(snippet.text).strip()
                start = float(snippet.start)
                end = start + float(snippet.duration)
            if text and end > start:
                words.append(
                    Word(
                        text=text,
                        start=start,
                        end=end,
                        segment_start=start,
                        segment_end=end,
                    )
                )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "YouTube captions for %s were malformed; using local transcription: %s",
            video_id,
            exc,
        )
        return None
    return words or None


def transcribe_audio(
    audio_path: str | Path,
    settings: Settings,
    on_progress: ProgressCallback | None = None,
) -> list[Word]:
    """Transcribe audio and return timestamped transcript entries in seconds."""
    wav_path = Path(audio_path).expanduser().resolve(strict=True)
    if not wav_path.is_file():
        raise ValueError(f"Transcription audio is not a file: {wav_path}")
    if wav_path.suffix.lower() not in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}:
        raise ValueError(f"Unsupported transcription audio format: {wav_path.name}")

    groq_key = _groq_api_key()
    if groq_key:
        try:
            words = _transcribe_with_groq(
                wav_path,
                groq_key,
                settings.whisper_language,
            )
        except Exception as exc:
            logger.warning(
                "Groq transcription failed; using local faster-whisper: %s",
                exc,
            )
        else:
            if words:
                if on_progress is not None:
                    on_progress(1.0, "100% complete")
                return words
            logger.warning(
                "Groq transcription returned no valid timestamped segments; "
                "using local faster-whisper."
            )

    model = _get_whisper_model(
        settings.whisper_model,
        settings.whisper_device,
        settings.whisper_compute_type,
    )
    try:
        pipeline = BatchedInferencePipeline(model=model)
        segments, _info = pipeline.transcribe(
            str(wav_path),
            language=settings.whisper_language,
            beam_size=1,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
            batch_size=8,
        )
    except TypeError as exc:
        if "metadata_errors" in str(exc):
            raise RuntimeError(
                "The installed PyAV version is incompatible with Faster-Whisper. "
                'Run `python -m pip install -r requirements.txt` to install the '
                "supported PyAV version (av<19)."
            ) from exc
        raise

    total_duration = _audio_duration(wav_path)
    words = _words_from_segments(
        segments,
        total_duration=total_duration,
        on_progress=on_progress,
    )
    if not words:
        raise RuntimeError(
            "Transcription returned no word timestamps. Check that the input has "
            "audible speech and that the selected language/model is appropriate."
        )
    return words


def transcribe_clip_audio(audio_path: str | Path, settings: Settings) -> list[Word]:
    """Transcribe a short, already-trimmed clip using the configured provider."""
    audio_file = Path(audio_path).expanduser().resolve(strict=True)
    if not audio_file.is_file():
        raise ValueError(f"Transcription audio is not a file: {audio_file}")
    if audio_file.suffix.lower() not in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}:
        raise ValueError(f"Unsupported transcription audio format: {audio_file.name}")

    language = settings.whisper_language or "en"
    groq_key = _groq_api_key()
    if groq_key:
        try:
            words = _transcribe_with_groq(audio_file, groq_key, language)
        except Exception as exc:
            logger.warning(
                "Groq clip transcription failed; using local faster-whisper: %s",
                exc,
            )
        else:
            if words:
                return words
            logger.warning(
                "Groq clip transcription returned no valid timestamped segments; "
                "using local faster-whisper."
            )

    model = _get_whisper_model(
        settings.whisper_model,
        settings.whisper_device,
        settings.whisper_compute_type,
    )
    try:
        segments, _info = model.transcribe(
            str(audio_file),
            beam_size=1,
            word_timestamps=True,
            language=language,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        words = _words_from_segments(segments)
    except TypeError as exc:
        if "metadata_errors" in str(exc):
            raise RuntimeError(
                "The installed PyAV version is incompatible with Faster-Whisper. "
                'Run `python -m pip install -r requirements.txt` to install the '
                "supported PyAV version (av<19)."
            ) from exc
        raise
    if not words:
        raise RuntimeError(
            "Clip transcription returned no word timestamps. Check that the clip "
            f"contains audible speech in the configured language ({language})."
        )
    return words


def _groq_api_key() -> str:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    return os.getenv("GROQ_API_KEY", "").strip()


def _transcribe_with_groq(
    audio_path: Path,
    api_key: str,
    language: str | None,
) -> list[Word]:
    try:
        from groq import Groq
    except ImportError as exc:
        raise RuntimeError(
            "GROQ_API_KEY is configured, but the Groq SDK is not installed. "
            "Install it with `python -m pip install -r requirements.txt`."
        ) from exc

    client = Groq(api_key=api_key)
    with tempfile.TemporaryDirectory(prefix="groq-transcription-") as temp_dir:
        upload_path = audio_path
        if audio_path.stat().st_size > GROQ_MAX_UPLOAD_BYTES:
            upload_path = Path(temp_dir) / "transcription.mp3"
            bitrate_kbps = _groq_upload_bitrate_kbps(audio_path)
            try:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(audio_path),
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-b:a",
                        f"{bitrate_kbps}k",
                        str(upload_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                stderr = (
                    exc.stderr.strip()
                    if isinstance(exc, subprocess.CalledProcessError)
                    and exc.stderr
                    else str(exc)
                )
                raise RuntimeError(
                    f"Could not prepare audio for Groq transcription: {stderr}"
                ) from exc
            if upload_path.stat().st_size > GROQ_MAX_UPLOAD_BYTES:
                raise ValueError(
                    "Audio remains larger than Groq's 24 MiB upload limit after "
                    "compression."
                )

        request_options: dict[str, object] = {
            "model": GROQ_TRANSCRIPTION_MODEL,
            "response_format": "verbose_json",
            # Word timestamps are what the subtitles need. Segment timestamps are
            # kept so clip selection can still snap to sentence/segment ends.
            "timestamp_granularities": ["word", "segment"],
        }
        if language:
            request_options["language"] = language
        with upload_path.open("rb") as audio_file:
            response = client.audio.transcriptions.create(
                file=audio_file,
                **request_options,
            )

    segments = _parse_timed_items(_response_field(response, "segments"), "text")
    words = _parse_timed_items(_response_field(response, "words"), "word")
    if words:
        return _attach_segments(words, segments)
    return [
        Word(
            text=text,
            start=start,
            end=end,
            segment_start=start,
            segment_end=end,
        )
        for text, start, end in segments
    ]


def _groq_upload_bitrate_kbps(audio_path: Path) -> int:
    """Pick the highest MP3 bitrate that keeps the upload under Groq's limit."""
    try:
        duration = _audio_duration(audio_path)
    except Exception:
        return 48
    budget_bits = GROQ_MAX_UPLOAD_BYTES * 8 * 0.92  # headroom for MP3 framing
    for bitrate in _GROQ_MP3_BITRATES_KBPS:
        if bitrate * 1000 * duration <= budget_bits:
            return bitrate
    return _GROQ_MP3_BITRATES_KBPS[-1]


def _response_field(response: Any, name: str) -> Any:
    """Read a field from a Groq/OpenAI response object, dict or pydantic model."""
    if isinstance(response, dict):
        return response.get(name)
    value = getattr(response, name, None)
    if value is not None:
        return value
    for dump_name in ("model_dump", "to_dict"):
        dump = getattr(response, dump_name, None)
        if callable(dump):
            try:
                data = dump()
            except Exception:
                continue
            if isinstance(data, dict) and data.get(name) is not None:
                return data[name]
    try:
        return vars(response).get(name)
    except TypeError:
        return None


def _item_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    value = getattr(item, key, None)
    if value is None and hasattr(item, "get"):
        try:
            value = item.get(key)
        except Exception:
            value = None
    return value


def _parse_timed_items(
    raw_items: Any,
    text_key: str,
) -> list[tuple[str, float, float]]:
    if not isinstance(raw_items, (list, tuple)):
        return []
    parsed: list[tuple[str, float, float]] = []
    for item in raw_items:
        start_value = _item_value(item, "start")
        end_value = _item_value(item, "end")
        if start_value is None or end_value is None:
            continue
        try:
            start = float(start_value)
            end = float(end_value)
        except (OverflowError, TypeError, ValueError):
            continue
        text = str(_item_value(item, text_key) or "").strip()
        if text and math.isfinite(start) and math.isfinite(end) and end > start:
            parsed.append((text, start, end))
    return sorted(parsed, key=lambda item: (item[1], item[2]))


def _attach_segments(
    words: list[tuple[str, float, float]],
    segments: list[tuple[str, float, float]],
) -> list[Word]:
    """Give every word the start/end of the segment (sentence) that contains it."""
    segment_starts = [start for _text, start, _end in segments]
    result: list[Word] = []
    for text, start, end in words:
        segment_start: float | None = None
        segment_end: float | None = None
        if segments:
            midpoint = (start + end) / 2
            index = max(0, bisect.bisect_right(segment_starts, midpoint) - 1)
            _segment_text, candidate_start, candidate_end = segments[index]
            if candidate_start <= midpoint <= candidate_end:
                segment_start, segment_end = candidate_start, candidate_end
        result.append(
            Word(
                text=text,
                start=start,
                end=end,
                segment_start=segment_start if segment_start is not None else start,
                segment_end=segment_end if segment_end is not None else end,
            )
        )
    return result


def _audio_duration(audio_path: Path) -> float:
    if audio_path.suffix.lower() == ".wav":
        with wave.open(str(audio_path), "rb") as audio_file:
            frames = audio_file.getnframes()
            sample_rate = audio_file.getframerate()
        if sample_rate <= 0:
            raise ValueError(f"Audio file has an invalid sample rate: {audio_path}")
        return frames / sample_rate

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        duration = float(result.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise RuntimeError(
            f"Could not read audio duration metadata for {audio_path}."
        ) from exc
    if duration <= 0:
        raise ValueError(f"Audio file has an invalid duration: {audio_path}")
    return duration


def _words_from_segments(
    segments: Iterator[object],
    total_duration: float | None = None,
    on_progress: ProgressCallback | None = None,
) -> list[Word]:
    words: list[Word] = []
    for segment in segments:
        segment_start = float(getattr(segment, "start", 0.0))
        segment_end = float(getattr(segment, "end", segment_start))
        for item in getattr(segment, "words", None) or ():
            text = item.word.strip()
            if text and item.start is not None and item.end is not None:
                start = float(item.start)
                end = float(item.end)
                if end > start:
                    words.append(
                        Word(
                            text=text,
                            start=start,
                            end=end,
                            segment_start=segment_start,
                            segment_end=segment_end,
                        )
                    )
        if on_progress is not None and total_duration and total_duration > 0:
            progress = min(segment_end / total_duration, 1.0)
            on_progress(
                progress,
                f"{int(progress * 100)}% complete",
            )
    return words


def transcript_segments(words: list[Word]) -> list[dict[str, Any]]:
    """Group words back into their sentence/segment lines for the AI picker."""
    segments: list[dict[str, Any]] = []
    for word in words:
        start = word.segment_start if word.segment_start is not None else word.start
        end = word.segment_end if word.segment_end is not None else word.end
        if segments and segments[-1]["start"] == start and segments[-1]["end"] == end:
            segments[-1]["text"] = f'{segments[-1]["text"]} {word.text}'.strip()
        else:
            segments.append({"start": start, "end": end, "text": word.text})
    return segments


def has_word_level_timing(words: list[Word]) -> bool:
    """True when entries are single words (Whisper/Groq), not caption lines."""
    if not words:
        return False
    single_words = sum(1 for word in words if len(word.text.split()) <= 1)
    return single_words / len(words) >= 0.9


def clip_words_from_transcript(
    words: list[Word],
    clip_start: float,
    clip_end: float,
) -> list[Word] | None:
    """Return clip-relative words cut from the full transcript.

    Returns None when the transcript has no word-level timing (for example
    YouTube caption lines); callers should then transcribe the clip audio.
    """
    if clip_end <= clip_start or not has_word_level_timing(words):
        return None
    clip_duration = clip_end - clip_start
    clip_words: list[Word] = []
    for word in words:
        if word.end <= clip_start or word.start >= clip_end:
            continue
        relative_start = max(0.0, word.start - clip_start)
        relative_end = min(clip_duration, word.end - clip_start)
        if relative_end > relative_start:
            clip_words.append(
                Word(text=word.text, start=relative_start, end=relative_end)
            )
    return clip_words or None


_TIMELINE_MAX_LINE_SECONDS = 12.0
_TIMELINE_MAX_LINE_WORDS = 40
_TIMELINE_PAUSE_SECONDS = 1.0


def format_timeline(words: Iterator[Word] | list[Word]) -> str:
    """Format the transcript as timestamped sentence-sized lines for the LLM.

    One line per sentence (or per ~12 s when punctuation is missing) instead of
    one line per word. Roughly 5x fewer prompt tokens on long videos, and the
    model reads whole sentences, which helps it pick self-contained moments.
    """
    ordered = sorted(words, key=lambda word: (word.start, word.end))
    lines: list[str] = []
    current: list[Word] = []

    def flush() -> None:
        if current:
            text = " ".join(word.text for word in current)
            lines.append(f"{current[0].start:.1f}-{current[-1].end:.1f}: {text}")
            current.clear()

    for word in ordered:
        if current:
            previous = current[-1]
            if (
                word.start - previous.end >= _TIMELINE_PAUSE_SECONDS
                or word.end - current[0].start > _TIMELINE_MAX_LINE_SECONDS
                or len(current) >= _TIMELINE_MAX_LINE_WORDS
                or (
                    previous.segment_start is not None
                    and word.segment_start != previous.segment_start
                    and len(current) >= 4
                )
            ):
                flush()
        current.append(word)
        if word.text.rstrip("\"')]}»”’").endswith((".", "!", "?")):
            flush()
    flush()
    return "\n".join(lines)
