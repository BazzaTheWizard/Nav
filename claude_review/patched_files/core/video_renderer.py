from __future__ import annotations

import functools
import logging
import shutil
import subprocess
import uuid
from pathlib import Path

from core.face_tracker import CropBox


logger = logging.getLogger(__name__)


LAYOUT_MODES = (
    "stacked",
    "blurred_background",
    "smart_crop",
    "wide_blurred_background",
)


@functools.lru_cache(maxsize=4)
def _video_encoder_args(ffmpeg: str) -> tuple[str, ...]:
    """Use the Mac hardware encoder when present, otherwise libx264."""
    try:
        encoders = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        encoders = ""
    if "h264_videotoolbox" in encoders:
        return ("-c:v", "h264_videotoolbox", "-b:v", "6000k")
    return ("-c:v", "libx264", "-preset", "veryfast", "-crf", "20")


def render_clip(
    video_path: str | Path,
    subtitle_path: str | Path,
    crop: CropBox,
    start_time: float,
    end_time: float,
    output_path: str | Path,
    layout_mode: str = "blurred_background",
) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg was not found. Install it with `brew install ffmpeg`.")
    if end_time <= start_time:
        raise ValueError("Clip end time must be later than its start time.")
    if crop.width <= 0 or crop.height <= 0:
        raise ValueError("Crop width and height must be positive.")
    if layout_mode not in LAYOUT_MODES:
        raise ValueError(
            f"Unsupported layout mode {layout_mode!r}; choose one of {LAYOUT_MODES}."
        )

    subtitle_file = Path(subtitle_path).expanduser().resolve(strict=True)
    if not subtitle_file.is_file():
        raise ValueError(f"Subtitle file is not a file: {subtitle_file}")

    source = Path(video_path).expanduser().resolve(strict=True)
    destination = Path(output_path)
    output_dir = destination.parent.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / destination.name

    staged_subtitle = output_dir / f"subtitle_{uuid.uuid4().hex}.ass"
    shutil.copy2(subtitle_file, staged_subtitle)

    ass_path = staged_subtitle.name.replace("\\", "/").replace(":", "\\:")
    ass_filter = f"ass='{ass_path}'"
    if layout_mode in {"blurred_background", "wide_blurred_background"}:
        foreground_filter = (
            "[0:v]scale=1080:608:force_original_aspect_ratio=decrease,"
            "pad=1080:608:(ow-iw)/2:(oh-ih)/2[fg];"
            if layout_mode == "wide_blurred_background"
            else "[0:v]scale=1080:-1[fg];"
        )
        filter_complex = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=20:5,eq=brightness=-0.15[bg];"
            + foreground_filter
            + f"[bg][fg]overlay=(W-w)/2:(1920-h)/2,{ass_filter}[v]"
        )
    elif layout_mode == "stacked":
        filter_complex = (
            "[0:v]scale=1080:960:force_original_aspect_ratio=decrease,"
            "pad=1080:960:(ow-iw)/2:(oh-ih)/2[top];"
            f"[0:v]crop={crop.width}:{crop.height}:{crop.x}:{crop.y},"
            f"scale=1080:960[bottom];[top][bottom]vstack=inputs=2,{ass_filter}[v]"
        )
    else:
        filter_complex = (
            f"[0:v]crop={crop.width}:{crop.height}:{crop.x}:{crop.y},"
            f"scale=1080:1920:flags=lanczos,"
            f"unsharp=5:5:0.7:5:5:0.0,{ass_filter}[v]"
        )
    command = [
        ffmpeg,
        "-y",
        "-ss",
        str(start_time),
        "-t",
        str(end_time - start_time),
        "-i",
        str(source),
        "-map",
        "[v]",
        "-map",
        "0:a:0?",
        "-filter_complex",
        filter_complex,
        "-af",
        "aresample=async=1:first_pts=0",
        *_video_encoder_args(ffmpeg),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        destination.name,
    ]
    try:
        subprocess.run(
            command,
            cwd=str(output_dir),
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        logger.error("FFmpeg clip rendering failed: %s", detail)
        raise RuntimeError(f"FFmpeg clip rendering failed: {detail}") from exc
    finally:
        staged_subtitle.unlink(missing_ok=True)
    return destination
