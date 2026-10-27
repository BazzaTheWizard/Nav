from __future__ import annotations

import functools
import logging
import shutil
import subprocess
import uuid
from pathlib import Path

from core.effects import ClipEffects
from core.face_tracker import CropBox


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


def _has_audio(source: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return bool(result.stdout.strip())


def render_clip(
    video_path: str | Path,
    subtitle_path: str | Path,
    crop: CropBox,
    start_time: float,
    end_time: float,
    output_path: str | Path,
    layout_mode: str = "blurred_background",
    crop_x_expression: str | None = None,
    effects: ClipEffects | None = None,
) -> Path:
    """Render one vertical clip.

    ``crop_x_expression`` (from CropPlan.x_expression) lets the crop follow
    camera cuts; when omitted, ``crop.x`` is used for the whole clip.
    ``effects`` adds punch-in zooms (a second, tighter crop switched on during
    each zoom interval) and a whoosh at the start of each one.
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
    zoom = effects.zoom if effects is not None else None
    zoom_crop = (
        f"crop=w='{zoom.width}':h='{zoom.height}':x='{zoom.x}':y='{zoom.y}'"
        if zoom is not None
        else ""
    )
    zoom_enable = f"enable='{zoom.enable_expression()}'" if zoom is not None else ""
    if layout_mode in {"blurred_background", "wide_blurred_background"}:
        fit_foreground = (
            "scale=1080:608:force_original_aspect_ratio=decrease,"
            "pad=1080:608:(ow-iw)/2:(oh-ih)/2"
            if layout_mode == "wide_blurred_background"
            else "scale=1080:-1"
        )
        if zoom is None:
            foreground_filter = f"[0:v]{fit_foreground}[fg];"
        else:
            foreground_filter = (
                f"[0:v]{fit_foreground}[fg_base];"
                f"[0:v]{zoom_crop},{fit_foreground}[fg_zoom];"
                f"[fg_base][fg_zoom]overlay=0:0:{zoom_enable}[fg];"
            )
        filter_complex = (
            _BLURRED_BACKGROUND
            + foreground_filter
            + f"[bg][fg]overlay=(W-w)/2:(1920-h)/2,{ass_filter}[v]"
        )
    elif layout_mode == "stacked":
        filter_complex = (
            "[0:v]scale=1080:960:force_original_aspect_ratio=decrease,"
            "pad=1080:960:(ow-iw)/2:(oh-ih)/2[top];"
            f"[0:v]{stacked_crop_filter},"
            f"scale=1080:960[bottom];[top][bottom]vstack=inputs=2,{ass_filter}[v]"
        )
    elif zoom is None:
        filter_complex = (
            f"[0:v]{crop_filter},"
            f"scale=1080:1920:flags=lanczos,"
            f"unsharp=5:5:0.7:5:5:0.0,{ass_filter}[v]"
        )
    else:
        filter_complex = (
            f"[0:v]{crop_filter},scale=1080:1920:flags=lanczos[base];"
            f"[0:v]{zoom_crop},scale=1080:1920:flags=lanczos[zoomed];"
            f"[base][zoomed]overlay=0:0:{zoom_enable},"
            f"unsharp=5:5:0.7:5:5:0.0,{ass_filter}[v]"
        )

    # Audio: the voice as before, plus a whoosh at each punch-in when requested.
    extra_inputs: list[str] = []
    audio_args = ["-map", "0:a:0?", "-af", "aresample=async=1:first_pts=0"]
    sfx_times = list(effects.sfx_times) if effects is not None else []
    if sfx_times and effects is not None and effects.sfx_path and _has_audio(source):
        count = len(sfx_times)
        labels = "".join(f"[s{i}]" for i in range(count))
        delayed = "".join(
            f"[s{i}]adelay={int(round(time * 1000))}:all=1[d{i}];"
            for i, time in enumerate(sfx_times)
        )
        mix_inputs = "".join(f"[d{i}]" for i in range(count))
        filter_complex += (
            ";[0:a:0]aresample=48000:async=1:first_pts=0[voice];"
            f"[1:a]aresample=48000,volume={effects.sfx_volume:.3f},asplit={count}{labels};"
            + delayed
            + f"[voice]{mix_inputs}amix=inputs={count + 1}:duration=first:"
            "normalize=0:dropout_transition=0[aout]"
        )
        extra_inputs = ["-i", str(effects.sfx_path)]
        audio_args = ["-map", "[aout]"]
    command = [
        ffmpeg,
        "-y",
        "-ss",
        str(start_time),
        "-t",
        str(end_time - start_time),
        "-i",
        str(source),
        *extra_inputs,
        "-map",
        "[v]",
        *audio_args,
        "-filter_complex",
        filter_complex,
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
