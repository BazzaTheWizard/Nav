from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from config import Settings, load_settings

ProgressCallback = Callable[[float, str], None]
logger = logging.getLogger(__name__)

# Source quality. YouTube's H.264 1080p stream is its lowest-quality 1080p (the
# downloads measured about 1.1-1.25 Mbps), and a 9:16 crop of it is only 608 px
# wide, enlarged 1.78x. So: the best VP9 stream up to 1440p (a 810 px wide crop,
# enlarged 1.33x), falling back to H.264. AV1 stays last (slow to decode on a
# MacBook; an earlier 4K AV1 pick was 587 MB for 14 minutes). Sorting never falls
# through to "best". SOURCE_MAX_HEIGHT in .env: 1080, 1440 (default) or 2160.
# "proto": regular downloads before YouTube's chunked HLS streams (616 "Premium"
# 1080p, 620 at 1440p). Round 12 picked those because they are MP4: one 50-minute
# video was 1.23 GB and took 7.5 minutes, in hundreds of small pieces.
VIDEO_FORMAT = "bv*+ba/b"
FFMPEG_TIMEOUT_SEC = 15 * 60  # audio extraction from a 90-min video takes about a minute
DEFAULT_SOURCE_MAX_HEIGHT = 1440
VIDEO_FORMAT_SORT = [f"res:{DEFAULT_SOURCE_MAX_HEIGHT}", "vcodec:vp9", "proto", "ext:mp4:m4a"]


def video_format_sort() -> list[str]:
    """VIDEO_FORMAT_SORT with SOURCE_MAX_HEIGHT from .env (720-2160) applied."""
    try:
        height = int(os.getenv("SOURCE_MAX_HEIGHT", "") or DEFAULT_SOURCE_MAX_HEIGHT)
    except ValueError:
        height = DEFAULT_SOURCE_MAX_HEIGHT
    height = min(2160, max(720, height))
    return [f"res:{height}", *VIDEO_FORMAT_SORT[1:]]

# YouTube refusing the worker for a while (bot check, rate limit): pause the worker
# instead of using up creators' attempts. A plain 403 is often just one video or an
# outdated yt-dlp, so it's reported separately (DownloadForbidden).
_YOUTUBE_BLOCKED = re.compile(r"not a bot|HTTP Error 429|Too Many Requests", re.IGNORECASE)
YOUTUBE_PAUSE_SEC = 30 * 60
UPDATE_HINT = "If it keeps happening, update yt-dlp: .venv/bin/pip install -U yt-dlp"


class DownloadForbidden(RuntimeError):
    """YouTube answered HTTP 403. Two in a row and the worker pauses (see the worker)."""


# What yt-dlp learned while downloading (title, heatmap, ...), so the worker doesn't
# ask YouTube a second time for the "Most replayed" heatmap. url -> info.
_DOWNLOAD_INFO: dict[str, dict[str, object]] = {}


def downloaded_video_info(url: str) -> dict[str, object] | None:
    """Title, channel, duration and heatmap from this URL's download (once), or None
    when the file came from the cache or the download didn't report them."""
    return _DOWNLOAD_INFO.pop(url, None)


def yt_dlp_version() -> str:
    try:
        from yt_dlp.version import __version__
    except ImportError:
        return "not installed"
    return str(__version__)


def _download_error(exc: Exception, what: str) -> Exception:
    """The exception to raise for a yt-dlp DownloadError."""
    text = str(exc)
    if _YOUTUBE_BLOCKED.search(text):
        from core.service_pause import ServicePause

        return ServicePause(
            "YouTube is blocking downloads for now (bot check or too many requests)",
            YOUTUBE_PAUSE_SEC, service="youtube", hint=UPDATE_HINT,
        )
    if "HTTP Error 403" in text:
        return DownloadForbidden(f"yt-dlp could not download {what}: {exc}")
    return RuntimeError(f"yt-dlp could not download {what}: {exc}")


def _audio_language(info: object) -> str | None:
    """The language of the audio track yt-dlp picked (a video can have dubbed tracks)."""
    if not isinstance(info, dict):
        return None
    for item in info.get("requested_formats") or []:
        if isinstance(item, dict) and item.get("acodec") not in (None, "none") and item.get("language"):
            return str(item["language"])
    return str(info["language"]) if info.get("language") else None


__all__ = [
    "fetch_video_info",
    "download_audio_only",
    "download_source_video",
    "extract_audio_only",
    "extract_audio_segment",
]


def _yt_dlp_progress_hook(
    on_progress: ProgressCallback | None,
) -> Callable[[dict[str, object]], None]:
    def report_download_progress(data: dict[str, object]) -> None:
        if on_progress is None:
            return
        if data.get("status") == "finished":
            on_progress(1.0, "100% complete")
            return
        percent_text = str(data.get("_percent_str", ""))
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", percent_text)
        if match is None:
            return
        percent = min(max(float(match.group(1)), 0.0), 100.0)
        progress = percent / 100.0
        on_progress(
            progress,
            f"{int(progress * 100)}% complete",
        )

    return report_download_progress


def _download_url(
    url: str,
    settings: Settings,
    on_progress: ProgressCallback | None = None,
) -> Path:
    try:
        from yt_dlp import DownloadError, YoutubeDL
    except ImportError as exc:
        raise RuntimeError(
            "URL downloads require yt-dlp. Install dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    # Reuse a finished download of the same YouTube video instead of fetching it
    # again (one 38-minute video was stored 5 times).
    youtube_id = _youtube_id(url)
    cached = settings.raw_dir / f"yt_{youtube_id}.mp4" if youtube_id else None
    if cached is not None and cached.is_file() and cached.stat().st_size > 0:
        if on_progress is not None:
            on_progress(1.0, "100% complete")
        return cached

    download_id = uuid.uuid4().hex
    output_template = str(settings.raw_dir / f"{download_id}.%(ext)s")
    homebrew_ffmpeg_dir = "/opt/homebrew/opt/ffmpeg-full/bin"
    ffmpeg_path = (
        homebrew_ffmpeg_dir
        if os.path.exists(f"{homebrew_ffmpeg_dir}/ffmpeg")
        else shutil.which("ffmpeg")
    )
    options = {
        "format": VIDEO_FORMAT,
        "format_sort": video_format_sort(),
        "outtmpl": output_template,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
        "quiet": True,
        "noprogress": True,  # the progress hook still runs; this only drops the bars in the log
        "no_warnings": True,
        "ffmpeg_location": ffmpeg_path,
        "progress_hooks": [_yt_dlp_progress_hook(on_progress)],
    }
    started = time.monotonic()
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
    except DownloadError as exc:
        raise _download_error(exc, "the input URL") from exc

    if isinstance(info, dict):
        _DOWNLOAD_INFO[url] = {
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "duration": info.get("duration"),
            "heatmap": info.get("heatmap") if isinstance(info.get("heatmap"), list) else None,
        }
    downloaded_files = list(settings.raw_dir.glob(f"{download_id}*.mp4"))
    if len(downloaded_files) != 1 or not downloaded_files[0].is_file():
        raise RuntimeError(
            "yt-dlp completed, but did not produce exactly one MP4 file in "
            f"{settings.raw_dir}."
        )
    logger.info(
        "%s",
        download_summary(
            (info or {}).get("format_id") if isinstance(info, dict) else None,
            downloaded_files[0].stat().st_size,
            time.monotonic() - started,
            _audio_language(info),
        ),
    )
    if cached is not None:
        downloaded_files[0].replace(cached)
        return cached
    return downloaded_files[0]


def download_summary(format_id: object, size_bytes: int, seconds: float, audio_language: str | None = None) -> str:
    """One log line per download, so slow or oversized downloads show up in the log."""
    megabytes = size_bytes / 1_000_000
    return (
        f"Downloaded format {format_id or '?'}: {megabytes:,.0f} MB in {seconds:,.0f} s "
        f"({megabytes / max(seconds, 0.001):.1f} MB/s)"
        + (f", audio language {audio_language}" if audio_language else "")
    )


def _youtube_id(url: str) -> str | None:
    from core.transcriber import youtube_video_id

    video_id = youtube_video_id(url)
    return video_id if video_id and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id) else None


def download_source_video(
    url: str,
    output_path: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> str:
    """Download one full source video for rendering and later trim adjustments."""
    if not url.startswith(("http://", "https://")) or not urlparse(url).netloc:
        raise ValueError("A valid http:// or https:// video URL is required.")
    downloaded_path = _download_url(url, load_settings(), on_progress=on_progress)
    if output_path is None:
        return str(downloaded_path.resolve())

    destination = Path(output_path).expanduser().resolve()
    if destination != downloaded_path.resolve():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(downloaded_path), str(destination))
    if not destination.is_file():
        raise RuntimeError(
            f"The downloaded source video is missing from the expected path: {destination}"
        )
    return str(destination)


def fetch_video_info(url: str) -> dict[str, object]:
    """Title, channel and YouTube "Most replayed" heatmap, without downloading.

    The heatmap is a list of {"start_time", "end_time", "value"} dicts, or None
    when YouTube has none (new or low-view videos).
    """
    from yt_dlp import YoutubeDL

    with YoutubeDL(
        {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    ) as downloader:
        info = downloader.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError(f"Could not read video metadata for {url}")
    heatmap = info.get("heatmap")
    return {
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
        "heatmap": heatmap if isinstance(heatmap, list) else None,
    }


def _require_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RuntimeError(
            "FFmpeg was not found. Install it with `brew install ffmpeg` and "
            "ensure `ffmpeg` is on your PATH."
        )
    return executable


def download_audio_only(
    url: str,
    output_dir: str | Path = "storage/temp",
    on_progress: ProgressCallback | None = None,
) -> str:
    """Download audio only and return its absolute M4A or WAV path.

    An explicit ``.wav`` output path remains supported for existing callers.
    """
    if not url.startswith(("http://", "https://")) or not urlparse(url).netloc:
        raise ValueError("A valid http:// or https:// video URL is required.")

    try:
        from yt_dlp import DownloadError, YoutubeDL
    except ImportError as exc:
        raise RuntimeError(
            "URL downloads require yt-dlp. Install dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    requested_path = Path(output_dir).expanduser()
    wav_destination = (
        requested_path.resolve()
        if requested_path.suffix.lower() == ".wav"
        else None
    )
    destination_dir = (
        wav_destination.parent
        if wav_destination is not None
        else requested_path.resolve()
    )
    destination_dir.mkdir(parents=True, exist_ok=True)
    download_id = uuid.uuid4().hex
    output_template = str(destination_dir / f"{download_id}.%(ext)s")
    options = {
        "format": "ba[ext=m4a]/ba",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
            }
        ],
        "progress_hooks": [_yt_dlp_progress_hook(on_progress)],
    }
    temporary_files: list[Path] = []
    final_audio: Path | None = None
    try:
        with YoutubeDL(options) as downloader:
            downloader.download([url])
        temporary_files = list(destination_dir.glob(f"{download_id}.*"))
        source_audio = next(
            (
                path
                for path in temporary_files
                if path.is_file() and path.suffix.lower() == ".m4a"
            ),
            None,
        )
        if source_audio is None:
            raise RuntimeError(
                "yt-dlp completed, but did not produce an M4A audio file in "
                f"{destination_dir}."
            )
        if wav_destination is None:
            final_audio = destination_dir / f"audio_{download_id}.m4a"
            source_audio.replace(final_audio)
        else:
            ffmpeg = _require_ffmpeg()
            try:
                subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(source_audio),
                        "-vn",
                        "-acodec",
                        "pcm_s16le",
                        "-ar",
                        "16000",
                        "-ac",
                        "1",
                        str(wav_destination),
                    ],
                    check=True,
                    timeout=FFMPEG_TIMEOUT_SEC,  # a stuck FFmpeg must not stall the worker
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or "").strip()
                raise RuntimeError(f"FFmpeg WAV conversion failed: {detail}") from exc
            final_audio = wav_destination
        if final_audio is None or not final_audio.is_file():
            raise RuntimeError(
                "Audio download completed, but the expected output file is missing."
            )
    except DownloadError as exc:
        raise _download_error(exc, "audio from the URL") from exc
    finally:
        for temporary_file in set(temporary_files) | set(destination_dir.glob(f"{download_id}.*")):
            if temporary_file.is_file() or temporary_file.is_symlink():
                temporary_file.unlink(missing_ok=True)
        if wav_destination is not None and (
            final_audio is None or not final_audio.is_file()
        ):
            wav_destination.unlink(missing_ok=True)
    return str(final_audio)


def extract_audio_only(video_path: str | Path, output_path: str | Path) -> Path:
    """Extract a local video's audio to 16 kHz mono PCM WAV."""
    source = Path(video_path).expanduser().resolve(strict=True)
    destination = Path(output_path).expanduser().resolve()
    if destination.suffix.lower() != ".wav":
        raise ValueError("Audio output path must have a .wav extension.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _require_ffmpeg()
    try:
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(destination),
            ],
            check=True,
            timeout=FFMPEG_TIMEOUT_SEC,  # a stuck FFmpeg must not stall the worker
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg audio extraction failed: {detail}") from exc
    return destination


def extract_audio_segment(
    video_path: str | Path,
    start_time: float,
    end_time: float,
    output_path: str | Path,
) -> Path:
    """Extract one video's audio segment as 16 kHz mono PCM WAV."""
    if (
        not math.isfinite(start_time)
        or not math.isfinite(end_time)
        or start_time < 0
        or end_time <= start_time
    ):
        raise ValueError("Audio segment times must satisfy 0 <= start_time < end_time.")

    source = Path(video_path).expanduser().resolve(strict=True)
    destination = Path(output_path).expanduser().resolve()
    if destination.suffix.lower() != ".wav":
        raise ValueError("Clip audio output path must have a .wav extension.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _require_ffmpeg()
    try:
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(start_time),
                "-i",
                str(source),
                "-t",
                f"{end_time - start_time:.6f}",
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(destination),
            ],
            check=True,
            timeout=FFMPEG_TIMEOUT_SEC,  # a stuck FFmpeg must not stall the worker
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg clip audio extraction failed: {detail}") from exc
    return destination
