from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from config import Settings, load_settings

ProgressCallback = Callable[[float, str], None]

# Pick the closest match to 1080p H.264 + M4A audio. Sorting (instead of the old
# filter chain) never falls through to "best": the old chain sometimes ended at
# 4K AV1 (587 MB for 14 minutes), which is far slower to decode on a MacBook.
VIDEO_FORMAT = "bv*+ba/b"
VIDEO_FORMAT_SORT = ["res:1080", "vcodec:h264", "ext:mp4:m4a"]

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
        "format_sort": VIDEO_FORMAT_SORT,
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
        "no_warnings": True,
        "ffmpeg_location": ffmpeg_path,
        "progress_hooks": [_yt_dlp_progress_hook(on_progress)],
    }
    try:
        with YoutubeDL(options) as downloader:
            downloader.download([url])
    except DownloadError as exc:
        raise RuntimeError(f"yt-dlp could not download the input URL: {exc}") from exc

    downloaded_files = list(settings.raw_dir.glob(f"{download_id}*.mp4"))
    if len(downloaded_files) != 1 or not downloaded_files[0].is_file():
        raise RuntimeError(
            "yt-dlp completed, but did not produce exactly one MP4 file in "
            f"{settings.raw_dir}."
        )
    if cached is not None:
        downloaded_files[0].replace(cached)
        return cached
    return downloaded_files[0]


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
        raise RuntimeError(f"yt-dlp could not download audio from the URL: {exc}") from exc
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
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg clip audio extraction failed: {detail}") from exc
    return destination
