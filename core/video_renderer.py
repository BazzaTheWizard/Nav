from __future__ import annotations

import functools
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from core.face_tracker import CropBox, SplitView
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
# A stuck FFmpeg (damaged file, full disk) must not stall the worker forever.
RENDER_TIMEOUT_SEC = 10 * 60
# Two-speaker split view: two 1080x960 panels with a thin dark line on the seam.
SEAM_LINE = "drawbox=x=0:y=958:w=1080:h=4:color=black@0.85:t=fill"


def _enable_expression(spans: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in spans)


# Delivery encode. Tested on real clips: a slower, "higher quality" x264 encode
# scored almost the same as the fast one (SSIM 0.982 vs 0.983), because the source
# limits quality, not the encoder. So the fast encoder stays, at a bitrate in line
# with YouTube's upload guide for 1080p (8 Mbps at 24-30 fps, 12 Mbps at 48-60).
VIDEO_BITRATE, VIDEO_MAXRATE, VIDEO_BUFSIZE = "10M", "14M", "20M"
AUDIO_SAMPLE_RATE = "48000"  # YouTube's upload guide
# Loudness: every clip ends up at the same level (EBU R128 two-pass, linear).
# -14 LUFS is where YouTube normalizes; set AUDIO_TARGET_LUFS=off in .env to skip.
DEFAULT_TARGET_LUFS = -14.0
TRUE_PEAK_DB = -1.5  # headroom so AAC encoding can't clip
LOUDNESS_RANGE = 11.0
MAX_GAIN_DB = 20.0  # a nearly silent clip isn't boosted (that would only raise noise)
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


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
    except (OSError, subprocess.SubprocessError):
        encoders = ""
    rate = ("-maxrate", VIDEO_MAXRATE, "-bufsize", VIDEO_BUFSIZE, "-profile:v", "high")
    if "h264_videotoolbox" in encoders:
        return ("-c:v", "h264_videotoolbox", "-b:v", VIDEO_BITRATE, *rate)
    return ("-c:v", "libx264", "-preset", "veryfast", "-crf", "18", *rate)


def _sharpen(upscale: float) -> str:
    """Sharpening matched to how much the picture is enlarged: none for sharp
    sources (1440p/4K crops), the old 0.7 for a 1080p crop enlarged 1.78x."""
    amount = round(min(0.8, max(0.0, 0.9 * (upscale - 1.0))), 2)
    return f",unsharp=5:5:{amount}:5:5:0.0" if amount >= 0.05 else ""


def _source_color(source: Path) -> dict[str, str]:
    """The source's color tags (empty when ffprobe can't read them)."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=color_space,color_transfer,color_primaries,color_range",
                "-of", "json", str(source),
            ],
            check=True, capture_output=True, text=True, timeout=60,
        )
        return (json.loads(result.stdout).get("streams") or [{}])[0]
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def loudness_target() -> float | None:
    """AUDIO_TARGET_LUFS from .env (default -14); "off" turns normalization off."""
    value = os.getenv("AUDIO_TARGET_LUFS", "").strip().lower()
    if value in {"off", "none", "0", "false"}:
        return None
    try:
        return float(value) if value else DEFAULT_TARGET_LUFS
    except ValueError:
        return DEFAULT_TARGET_LUFS


def _measure_loudness(ffmpeg: str, source: Path, start: float, duration: float, target: float) -> dict[str, str] | None:
    """First loudnorm pass over the clip's audio; None if it can't be measured."""
    try:
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-nostats", "-ss", str(start), "-t", str(duration),
                "-i", str(source), "-map", "0:a:0", "-vn",
                "-af", f"loudnorm=I={target}:TP={TRUE_PEAK_DB}:LRA={LOUDNESS_RANGE}:print_format=json",
                "-f", "null", "-",
            ],
            check=True, capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.findall(r"\{[^{}]*\}", str(getattr(result, "stderr", "") or ""))
    try:
        values = json.loads(found[-1]) if found else {}
        keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
        measured = {key: str(values[key]) for key in keys}
        if any(not re.fullmatch(r"-?\d+(\.\d+)?", value) for value in measured.values()):
            return None  # silence ("-inf") or garbage
        return measured
    except (ValueError, KeyError, IndexError):
        return None


def _audio_filter(ffmpeg: str, source: Path, start: float, duration: float) -> str:
    """Audio chain: resync, then (when measurable) two-pass loudness normalization."""
    chain = "aresample=async=1:first_pts=0"
    target = loudness_target()
    measured = _measure_loudness(ffmpeg, source, start, duration, target) if target is not None else None
    if measured is None or float(measured["input_i"]) < target - MAX_GAIN_DB:
        return chain
    return (
        f"{chain},loudnorm=I={target}:TP={TRUE_PEAK_DB}:LRA={LOUDNESS_RANGE}"
        f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}:linear=true:print_format=none,"
        f"aresample={AUDIO_SAMPLE_RATE}"
    )


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
    split_view: SplitView | None = None,
) -> Path:
    """Render one vertical clip.

    ``crop_x_expression`` (from CropPlan.x_expression) lets the crop follow
    camera cuts; when omitted, ``crop.x`` is used for the whole clip.
    ``title`` (from core.title_card) is laid over the video, under the captions.
    ``full_frame_ranges`` (smart_crop only, from CropPlan.full_frame_ranges): spans
    where the whole frame is shown over a blurred background instead of the crop.
    ``split_view`` (smart_crop only, from CropPlan.split_view): two-person shots
    shown as two stacked panels, one speaker each.
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
    crop_sharpen = _sharpen(1920 / crop.height)
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
    elif layout_mode == "smart_crop" and (full_frame_ranges or (split_view and split_view.spans)):
        sources = ["[src_crop]"]
        parts: list[str] = []
        layers: list[tuple[str, list[tuple[float, float]]]] = []
        if full_frame_ranges:
            sources += ["[src_bg]", "[src_fg]"]
            parts.append(
                _BLURRED_BACKGROUND.replace("[0:v]", "[src_bg]")
                + "[src_fg]scale=1080:-2[fg];"
                f"[bg][fg]overlay=x=(W-w)/2:y='max(0,{FULL_FRAME_BOTTOM_Y}-h)'[full];"
            )
            layers.append(("[full]", full_frame_ranges))
        if split_view and split_view.spans:
            sources += ["[src_top]", "[src_bottom]"]
            panel = f"crop=w={split_view.width}:h={split_view.height}"
            panel_sharpen = _sharpen(1080 / split_view.width)
            parts.append(
                f"[src_top]{panel}:x='{split_view.top_x}':y='{split_view.top_y}',"
                f"scale=1080:960:flags=lanczos{panel_sharpen}[top];"
                f"[src_bottom]{panel}:x='{split_view.bottom_x}':y='{split_view.bottom_y}',"
                f"scale=1080:960:flags=lanczos{panel_sharpen}[bottom];"
                f"[top][bottom]vstack=inputs=2,{SEAM_LINE}[pair];"
            )
            layers.append(("[pair]", list(split_view.spans)))
        whole_clip_split = (
            not full_frame_ranges
            and len(layers) == 1
            and layers[0][1][0][0] <= 0.05
            and layers[0][1][-1][1] >= end_time - start_time - 0.05
            and len(layers[0][1]) == 1
        )
        if whole_clip_split:
            # The whole clip is the split view: skip the unused single-face crop.
            filter_complex = (
                "[0:v]split=2[src_top][src_bottom];" + "".join(parts) + "[pair]null[base]"
            )
            layers = []
        else:
            filter_complex = (
                f"[0:v]split={len(sources)}{''.join(sources)};"
                + "".join(parts)
                + f"[src_crop]{crop_filter},"
                f"scale=1080:1920:flags=lanczos{crop_sharpen}[layer0]"
            )
        for index, (label, spans) in enumerate(layers):
            target = "[base]" if index == len(layers) - 1 else f"[layer{index + 1}]"
            filter_complex += (
                f";[layer{index}]{label}overlay=0:0:enable='{_enable_expression(spans)}'{target}"
            )
    else:
        filter_complex = (
            f"[0:v]{crop_filter},"
            f"scale=1080:1920:flags=lanczos{crop_sharpen}[base]"
        )
    # Tag the picture as Rec. 709 (SDR HD) so captions, the title card and players
    # all use the same colors; some layouts dropped the source's tags.
    color = _source_color(source)
    color_args: list[str] = []
    if color.get("color_transfer") not in _HDR_TRANSFERS:
        props = "color_primaries=bt709:color_trc=bt709:colorspace=bt709"
        if color.get("color_range") != "pc":
            props += ":range=tv"
        filter_complex = filter_complex.replace("[base]", "[untagged]") + f";[untagged]setparams={props}[base]"
        color_args = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
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
        _audio_filter(ffmpeg, source, start_time, end_time - start_time),
        *_video_encoder_args(ffmpeg),
        "-pix_fmt",
        "yuv420p",
        *color_args,
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        AUDIO_SAMPLE_RATE,
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
            timeout=RENDER_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            f"FFmpeg clip rendering took longer than {RENDER_TIMEOUT_SEC // 60} minutes and was stopped."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        logger.error("FFmpeg clip rendering failed: %s", detail)
        raise RuntimeError(f"FFmpeg clip rendering failed: {detail}") from exc
    finally:
        staged_subtitle.unlink(missing_ok=True)
    return destination
