from __future__ import annotations

import functools
import logging
import shutil
import subprocess
import uuid
from pathlib import Path

from core.face_tracker import CropBox
from core.title_card import TitleOverlay, overlay_filter


logger = logging.getLogger(__name__)


LAYOUT_MODES = (
    "stacked",
    "blurred_background",
    "smart_crop",
    "wide_blurred_background",
)
# "auto" is resolved to one of LAYOUT_MODES by core.pipeline before rendering.
SELECTABLE_LAYOUTS = ("auto", *LAYOUT_MODES)
CROP_LAYOUTS = frozenset({"stacked", "smart_crop"})
# Blur a quarter-size copy and scale it up: looks the same as blurring at full
# size but renders the background ~2.4x faster.
_BLURRED_BACKGROUND = (
    "[0:v]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
    "boxblur=5:2,eq=brightness=-0.15,scale=1080:1920[bg];"
)
# Full-frame shots inside a cropped clip (slides, wide shots): the whole frame sits
# over a blurred copy, with its bottom edge here so captions (which start around
# y=1080) sit below it instead of covering the slide.
FULL_FRAME_BOTTOM_Y = 1050


def _enable_expression(spans: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in spans)


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
    crop_x_expression: str | None = None,
    title: TitleOverlay | None = None,
    full_frame_ranges: list[tuple[float, float]] | None = None,
) -> Path:
    """Render one vertical clip.

    ``crop_x_expression`` (from CropPlan.x_expression) lets the crop follow
    camera cuts; when omitted, ``crop.x`` is used for the whole clip.
    ``title`` (from core.title_card) is laid over the video, under the captions.
    ``full_frame_ranges`` (smart_crop only, from CropPlan.full_frame_ranges): spans
    where the whole frame is shown over a blurred background instead of the crop.
    """
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
    crop_x = f"'{crop_x_expression}'" if crop_x_expression else str(crop.x)
    crop_filter = f"crop=w={crop.width}:h={crop.height}:x={crop_x}:y={crop.y}"
    # Stacked layout: the bottom panel is 1080x960 (9:8), so crop a 9:8 window
    # centred on the same face instead of squashing the 9:16 crop into it.
    face_center = f"({crop_x_expression or crop.x})+{crop.width / 2:.1f}"
    stacked_crop_filter = (
        f"crop=w='trunc(min(iw,{crop.height}*9/8)/2)*2':h={crop.height}:"
        f"x='max(0,min(iw-ow,{face_center}-ow/2))':y={crop.y}"
    )
    if layout_mode in {"blurred_background", "wide_blurred_background"}:
        foreground_filter = (
            "[0:v]scale=1080:608:force_original_aspect_ratio=decrease,"
            "pad=1080:608:(ow-iw)/2:(oh-ih)/2[fg];"
            if layout_mode == "wide_blurred_background"
            else "[0:v]scale=1080:-1[fg];"
        )
        filter_complex = (
            _BLURRED_BACKGROUND
            + foreground_filter
            + "[bg][fg]overlay=(W-w)/2:(1920-h)/2[base]"
        )
    elif layout_mode == "stacked":
        filter_complex = (
            "[0:v]scale=1080:960:force_original_aspect_ratio=decrease,"
            "pad=1080:960:(ow-iw)/2:(oh-ih)/2[top];"
            f"[0:v]{stacked_crop_filter},"
            "scale=1080:960[bottom];[top][bottom]vstack=inputs=2[base]"
        )
    elif layout_mode == "smart_crop" and full_frame_ranges:
        filter_complex = (
            "[0:v]split=3[src_crop][src_bg][src_fg];"
            + _BLURRED_BACKGROUND.replace("[0:v]", "[src_bg]")
            + "[src_fg]scale=1080:-2[fg];"
            f"[bg][fg]overlay=x=(W-w)/2:y='max(0,{FULL_FRAME_BOTTOM_Y}-h)'[full];"
            f"[src_crop]{crop_filter},"
            "scale=1080:1920:flags=lanczos,"
            "unsharp=5:5:0.7:5:5:0.0[cropped];"
            f"[cropped][full]overlay=0:0:enable='{_enable_expression(full_frame_ranges)}'[base]"
        )
    else:
        filter_complex = (
            f"[0:v]{crop_filter},"
            f"scale=1080:1920:flags=lanczos,"
            "unsharp=5:5:0.7:5:5:0.0[base]"
        )
    title_input: list[str] = []
    if title is not None:
        title_input = [
            "-loop", "1", "-framerate", "30", "-t", f"{title.seconds:.3f}",
            "-i", str(Path(title.image_path).resolve()),
        ]
        filter_complex += ";" + overlay_filter("base", 1, title, "titled")
        filter_complex += f";[titled]{ass_filter}[v]"
    else:
        filter_complex += f";[base]{ass_filter}[v]"
    command = [
        ffmpeg,
        "-y",
        "-ss",
        str(start_time),
        "-t",
        str(end_time - start_time),
        "-i",
        str(source),
        *title_input,
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
