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

# Prefer H.264 (avc1) up to 1080p. A plain "bestvideo[ext=mp4]" picks AV1 streams
# (often 4K), which are several times larger and much slower to decode for face
# tracking and rendering. Output is 1080x1920, so 1080p sources are enough.
VIDEO_FORMAT = (
    "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/"
    "bv*[height<=1080]+ba/"
    "b[height<=1080]/"
    "bv*+ba/b"
)

__all__ = [
    "download_audio_only",
    "download_source_video",
    "download_video_segment",
    "extract_audio_only",
    "extract_audio_segment",
    "ingest_video",
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
    return downloaded_files[0]


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


def download_video_segment(
    url: str,
    start_time: float,
    end_time: float,
    output_path: str | Path,
    on_progress: ProgressCallback | None = None,
) -> str:
    """Download and accurately trim a sentence-aligned source-video segment."""
    if (
        not math.isfinite(start_time)
        or not math.isfinite(end_time)
        or start_time < 0
        or end_time <= start_time
    ):
        raise ValueError("Video segment times must satisfy 0 <= start_time < end_time.")

    try:
        from yt_dlp import DownloadError, YoutubeDL
    except ImportError as exc:
        raise RuntimeError(
            "URL downloads require yt-dlp. Install dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _require_ffmpeg()
    full_video = destination.with_name(
        f"{destination.stem}_full_{uuid.uuid4().hex}.mp4"
    )
    output_template = str(full_video.with_suffix("")) + ".%(ext)s"
    homebrew_ffmpeg_dir = "/opt/homebrew/opt/ffmpeg-full/bin"
    ffmpeg_location = (
        homebrew_ffmpeg_dir
        if os.path.exists(f"{homebrew_ffmpeg_dir}/ffmpeg")
        else shutil.which("ffmpeg")
    )
    options = {
        "format": VIDEO_FORMAT,
        "outtmpl": output_template,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": ffmpeg_location,
        "progress_hooks": [_yt_dlp_progress_hook(on_progress)],
    }
    try:
        try:
            with YoutubeDL(options) as downloader:
                downloader.download([url])
        except DownloadError as exc:
            raise RuntimeError(f"yt-dlp could not download the source video: {exc}") from exc

        if not full_video.is_file():
            raise RuntimeError(
                "yt-dlp completed, but did not produce the expected temporary MP4: "
                f"{full_video}"
            )
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-ss",
                    str(start_time),
                    "-i",
                    str(full_video),
                    "-t",
                    f"{end_time - start_time:.6f}",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "18",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-avoid_negative_ts",
                    "make_zero",
                    str(destination),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"FFmpeg segment extraction failed: {detail}") from exc
        if not destination.is_file():
            raise RuntimeError(
                f"FFmpeg completed, but did not produce the expected segment: {destination}"
            )
    finally:
        for temporary_file in full_video.parent.glob(f"{full_video.stem}*"):
            if temporary_file.is_file() or temporary_file.is_symlink():
                temporary_file.unlink(missing_ok=True)
    return str(destination)


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


def ingest_video(
    input_path: str | Path,
    settings: Settings,
    on_progress: ProgressCallback | None = None,
) -> tuple[Path, Path]:
    """Download or copy a supported video into raw storage and extract mono 16 kHz audio."""
    input_value = str(input_path)
    if input_value.startswith(("http://", "https://")):
        parsed_url = urlparse(input_value)
        if not parsed_url.netloc:
            raise ValueError(f"Invalid video URL: {input_value}")
        raw_path = _download_url(input_value, settings, on_progress=on_progress)
    else:
        source = Path(input_path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"Input video is not a file: {source}")
        if source.suffix.lower() not in {".mp4", ".mov"}:
            raise ValueError(f"Expected a local .mp4 or .mov input, got: {source.name}")

        raw_path = (
            settings.raw_dir
            / f"{source.stem}_{uuid.uuid4().hex[:10]}{source.suffix.lower()}"
        )
        shutil.copy2(source, raw_path)

    ffmpeg = _require_ffmpeg()
    audio_path = settings.temp_dir / f"{raw_path.stem}.wav"

    try:
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(raw_path),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(audio_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        audio_path.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg audio extraction failed: {detail}") from exc
    return raw_path, audio_path
