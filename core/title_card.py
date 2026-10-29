"""Hook title card: one clean rounded box, drawn with Pillow, placed off the face.

The old title was an ASS subtitle with BorderStyle 3. That draws a separate box
behind each line (different widths, overlapping corners) at a fixed height that
often sat on the speaker's face. Here the title becomes a PNG (balanced lines,
one box, soft shadow) that FFmpeg fades in over the first few seconds.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from core.face_tracker import CropBox, CropPlan, SplitView, _face_detector, _probe_display_size

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONT_PATH = PROJECT_ROOT / "assets" / "fonts" / "Montserrat-ExtraBold.ttf"

FRAME_WIDTH, FRAME_HEIGHT = 1080, 1920
# TikTok/Reels/Shorts draw tabs and the status bar over roughly the top 9%.
SAFE_TOP = 170
# Captions end at y=1240 and can wrap to two lines (from about y=1010).
CAPTION_ZONE_TOP = 1010
CAPTION_ZONE_BOTTOM = 1240
# During a two-speaker split view the captions sit on the seam instead.
SEAM_CAPTION_ZONE = (860, 1060)
SEAM_Y = 960
SEAM_PLACEMENT = "on the seam"
# When the face fills the top of the frame, the title may go under the captions,
# but no lower than this: TikTok/Reels/Shorts put the caption and buttons below.
LOWEST_TITLE_BOTTOM = 1500
GAP = 28  # space between the title and a face or the video band

MAX_CARD_WIDTH = 900
FONT_SIZES = (62, 56, 50)  # tried largest first; the smallest may use 3 lines
MAX_CHARACTERS = 70
GENERIC_TITLES = frozenset({"key highlight", "clip", ""})

STYLES = {
    # White card, near-black text: the native TikTok/Reels text look.
    "light": {"box": (255, 255, 255, 255), "text": (17, 17, 17, 255)},
    # Dark glass card, white text.
    "dark": {"box": (12, 12, 12, 222), "text": (255, 255, 255, 255)},
}
DEFAULT_STYLE = "light"

SHADOW_BLUR = 14
SHADOW_OFFSET = 6
SHADOW_ALPHA = 80
SHADOW_MARGIN = SHADOW_BLUR * 2 + SHADOW_OFFSET  # transparent edge kept for the shadow

FADE_IN = 0.18
FADE_OUT = 0.25
SLIDE_PX = 16  # the card rises this far while fading in


@dataclass(frozen=True)
class TitleOverlay:
    """A rendered title PNG and where FFmpeg should put it."""

    image_path: Path
    x: int
    y: int
    seconds: float
    card_top: int
    card_height: int
    placement: str


def clean_title(title: str | None) -> str:
    """Drop emoji and symbols the font can't draw, tidy spacing, cap the length."""
    if not title:
        return ""
    text = unicodedata.normalize("NFC", title)
    text = "".join(
        ch
        for ch in text
        if unicodedata.category(ch) not in {"So", "Sk", "Cs", "Co", "Cn", "Cc", "Cf"}
    )
    text = " ".join(text.split()).strip(" \"'“”‘’")
    if len(text) > MAX_CHARACTERS:
        text = text[:MAX_CHARACTERS].rsplit(" ", 1)[0].rstrip(",.;:-–—") + "…"
    return text


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def _text_width(text: str, font: ImageFont.FreeTypeFont) -> float:
    return font.getlength(text)


def _balanced_lines(
    words: list[str], font: ImageFont.FreeTypeFont, max_width: float, max_lines: int
) -> list[str] | None:
    """Fewest lines that fit; among those, the split whose widest line is narrowest."""
    for line_count in range(1, min(max_lines, len(words)) + 1):
        best: tuple[float, list[str]] | None = None
        for breaks in combinations(range(1, len(words)), line_count - 1):
            edges = (0, *breaks, len(words))
            lines = [" ".join(words[a:b]) for a, b in zip(edges, edges[1:])]
            widest = max(_text_width(line, font) for line in lines)
            if widest <= max_width and (best is None or widest < best[0]):
                best = (widest, lines)
        if best is not None:
            return best[1]
    return None


def _layout_text(text: str, max_font_size: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    words = text.split()
    sizes = [size for size in FONT_SIZES if size <= max_font_size] or [FONT_SIZES[-1]]
    for size in sizes:
        font = _font(size)
        pad_x = round(size * 0.6)
        max_lines = 3 if size == sizes[-1] else 2
        lines = _balanced_lines(words, font, MAX_CARD_WIDTH - 2 * pad_x, max_lines)
        if lines:
            return font, lines
    # A single word wider than the card: let it be wide rather than cut it.
    font = _font(sizes[-1])
    return font, _balanced_lines(words, font, 10_000, 3) or [text]


def render_title_card(
    text: str, output_path: Path, *, style: str = DEFAULT_STYLE, max_font_size: int = FONT_SIZES[0]
) -> tuple[Path, int, int]:
    """Draw the card; returns (path, card width, card height). The PNG has a
    transparent SHADOW_MARGIN on every side around the card."""
    colours = STYLES.get(style, STYLES[DEFAULT_STYLE])
    font, lines = _layout_text(text, max_font_size)
    size = font.size
    pad_x = round(size * 0.6)
    pad_top = round(size * 0.46)
    pad_bottom = round(size * 0.52)  # a little extra so descenders (g, y, p) don't look cramped
    line_pitch = round(size * 1.22)
    cap_height = -font.getbbox("H", anchor="ls")[1]

    text_width = max(_text_width(line, font) for line in lines)
    card_width = round(text_width + 2 * pad_x)
    card_height = round(pad_top + cap_height + (len(lines) - 1) * line_pitch + pad_bottom)
    radius = round(size * 0.42)

    margin = SHADOW_MARGIN
    image = Image.new("RGBA", (card_width + 2 * margin, card_height + 2 * margin), (0, 0, 0, 0))
    box = (margin, margin, margin + card_width, margin + card_height)

    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (box[0], box[1] + SHADOW_OFFSET, box[2], box[3] + SHADOW_OFFSET),
        radius=radius,
        fill=(0, 0, 0, SHADOW_ALPHA),
    )
    image = Image.alpha_composite(image, shadow.filter(ImageFilter.GaussianBlur(SHADOW_BLUR)))

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(box, radius=radius, fill=colours["box"])
    baseline = margin + pad_top + cap_height
    for line in lines:
        draw.text(
            (margin + card_width / 2, baseline), line, font=font, fill=colours["text"], anchor="ms"
        )
        baseline += line_pitch

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return output_path, card_width, card_height


FACE_SAMPLE_FPS = 3
FACE_SAMPLE_WIDTH = 640


def _sample_frames(
    source: Path, start: float, seconds: float, frame_size: tuple[int, int]
) -> np.ndarray:
    """A few small BGR frames from the seconds the title is on screen."""
    width, height = frame_size
    sample_height = max(2, round(FACE_SAMPLE_WIDTH * height / width / 2) * 2)
    result = subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{seconds:.3f}",
            "-i",
            str(source),
            "-an",
            "-vf",
            f"fps={FACE_SAMPLE_FPS},scale={FACE_SAMPLE_WIDTH}:{sample_height},format=bgr24",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    frame_bytes = FACE_SAMPLE_WIDTH * sample_height * 3
    data = np.frombuffer(result.stdout, dtype=np.uint8)
    return data[: data.size - data.size % frame_bytes].reshape(
        -1, sample_height, FACE_SAMPLE_WIDTH, 3
    )


def _face_zones(
    source: Path,
    plan: CropPlan | None,
    crop: CropBox | None,
    frame_size: tuple[int, int],
    clip_start: float,
    seconds: float,
    split: SplitView | None = None,
    full_frame: list[tuple[float, float]] | None = None,
) -> list[list[tuple[float, float]]]:
    """For each sampled frame, the vertical spans (output pixels, smart crop) the
    title must not cover: each face plus hair above it and neck below it.

    Frames in a two-speaker split view (``split``) or a full-frame span
    (``full_frame``, clip-relative) are mapped the way the renderer shows them."""
    detector = _face_detector()
    if not detector.available:
        return []
    width, height = frame_size
    if plan is not None:
        crop_width, crop_height, crop_y = plan.crop_width, plan.crop_height, plan.y
    elif crop is not None and crop.width > 2:
        crop_width, crop_height, crop_y = crop.width, crop.height, crop.y
    else:
        crop_width, crop_height = min(width, round(height * 9 / 16)), height
        crop_y = 0
    to_source = width / FACE_SAMPLE_WIDTH
    to_output = FRAME_HEIGHT / crop_height
    zones: list[list[tuple[float, float]]] = []
    full_scale = FRAME_WIDTH / width
    full_top = max(0, 1050 - round(height * full_scale))  # video_renderer.FULL_FRAME_BOTTOM_Y
    for index, frame in enumerate(_sample_frames(source, clip_start, seconds, frame_size)):
        relative = index / FACE_SAMPLE_FPS
        moment = clip_start + relative
        window = next((w for w in (split.windows if split else ()) if w[0] <= relative < w[1]), None)
        if window is not None:
            _a, _b, top_x, top_y, bottom_x, bottom_y = window
            panel_scale = 960 / split.height
            spans = []
            for cx, cy, _w, h, _score in detector.detect(frame):
                sx, sy, sh = cx * to_source, cy * to_source, h * to_source
                if top_x <= sx <= top_x + split.width:
                    center = (sy - top_y) * panel_scale
                elif bottom_x <= sx <= bottom_x + split.width:
                    center = 960 + (sy - bottom_y) * panel_scale
                else:
                    continue
                face = sh * panel_scale
                spans.append((center - face / 2 - 0.45 * face - GAP, center + face / 2 + 0.35 * face + GAP))
            zones.append(spans)
            continue
        if any(a <= relative < b for a, b in (full_frame or [])):
            spans = []
            for _cx, cy, _w, h, _score in detector.detect(frame):
                center = full_top + cy * to_source * full_scale
                face = h * to_source * full_scale
                spans.append((center - face / 2 - 0.45 * face - GAP, center + face / 2 + 0.35 * face + GAP))
            zones.append(spans)
            continue
        if plan is not None:
            shot = next((s for s in plan.shots if s.start <= moment < s.end), plan.shots[-1])
            crop_x = shot.x
        else:
            crop_x = crop.x if crop is not None and crop.width > 2 else (width - crop_width) // 2
        spans: list[tuple[float, float]] = []
        for cx, cy, _w, h, _score in detector.detect(frame):
            if not crop_x <= cx * to_source <= crop_x + crop_width:
                continue  # this face is cut out of the vertical frame
            top = ((cy - h / 2) * to_source - crop_y) * to_output
            chin = ((cy + h / 2) * to_source - crop_y) * to_output
            face = chin - top
            # Face boxes start around the eyebrows; add forehead and hair, and the neck.
            spans.append((top - 0.45 * face - GAP, chin + 0.35 * face + GAP))
        zones.append(spans)
    return zones


def _best_slot(
    card_height: int,
    zones: list[list[tuple[float, float]]],
    blocked: list[tuple[float, float]] | None = None,
) -> tuple[int, int]:
    """Highest card position that covers a face in the fewest sampled frames.

    ``blocked``: output y ranges the card must not touch (the captions). The
    search runs from the top of the safe area down to LOWEST_TITLE_BOTTOM, so a
    close-up whose face fills the top puts the title under the captions instead
    of over the face.
    """
    if blocked is None:
        blocked = [(CAPTION_ZONE_TOP, CAPTION_ZONE_BOTTOM)]
    best: tuple[int, int] | None = None
    for top in range(SAFE_TOP, max(SAFE_TOP, LOWEST_TITLE_BOTTOM - card_height) + 1, 10):
        bottom = top + card_height
        if any(top < b + GAP and bottom > a - GAP for a, b in blocked):
            continue
        covered = sum(any(top < b and bottom > a for a, b in spans) for spans in zones)
        if best is None or covered < best[1]:
            best = (top, covered)
            if covered == 0:
                break
    if best is None:  # every slot touches a caption zone: fall back to the top
        best = (SAFE_TOP, sum(bool(spans) for spans in zones))
    return best


def _video_band_top(layout: str, frame_size: tuple[int, int]) -> int:
    width, height = frame_size
    if layout == "wide_blurred_background":
        return (FRAME_HEIGHT - 608) // 2
    scaled_height = min(FRAME_HEIGHT, round(FRAME_WIDTH * height / max(1, width)))
    return (FRAME_HEIGHT - scaled_height) // 2


def _place(
    layout: str,
    card_height: int,
    frame_size: tuple[int, int],
    zones: list[list[tuple[float, float]]],
    blocked: list[tuple[float, float]] | None = None,
) -> tuple[int, str, int]:
    """(card top, description, sampled frames where it covers a face)."""
    if layout in {"blurred_background", "wide_blurred_background"}:
        band_top = _video_band_top(layout, frame_size)
        return max(SAFE_TOP, band_top - GAP - card_height), "above the video", 0
    if layout == "stacked":
        # Centre it in the dark bar between the wide shot and the face panel.
        width, height = frame_size
        content = min(960, round(FRAME_WIDTH * height / max(1, width)))
        bar_top = (960 + content) // 2
        return round((bar_top + 960) / 2 - card_height / 2), "between the panels", 0
    top, covered = _best_slot(card_height, zones, blocked)
    if not any(zones):
        return top, "top (no face found)", 0
    if covered:
        label = "least face overlap"
    elif top == SAFE_TOP:
        label = "top"
    elif top >= CAPTION_ZONE_BOTTOM:
        label = "under the captions"
    else:
        label = "under the face"
    return top, label, covered


def prepare_title_overlay(
    title: str | None,
    seconds: float,
    *,
    layout: str,
    plan: CropPlan | None,
    source_video: Path,
    crop: CropBox | None = None,
    clip_start: float,
    clip_duration: float,
    work_dir: Path,
    name: str,
    style: str | None = None,
) -> TitleOverlay | None:
    """Render the title card and decide where it goes. None means no title."""
    text = clean_title(title)
    seconds = min(seconds, clip_duration)
    if seconds <= 0 or text.casefold() in GENERIC_TITLES:
        return None
    if not FONT_PATH.is_file():
        raise FileNotFoundError(f"Title font missing: {FONT_PATH}")
    style = (style or os.getenv("HOOK_TITLE_STYLE") or DEFAULT_STYLE).strip().lower()
    if plan is not None:
        frame_size = (plan.frame_width, plan.frame_height)
    else:
        frame_size = _probe_display_size(source_video)

    zones: list[list[tuple[float, float]]] = []
    blocked: list[tuple[float, float]] | None = None
    if layout not in {"blurred_background", "wide_blurred_background", "stacked"}:
        split = full_frame = None
        if plan is not None and layout == "smart_crop":
            split = plan.split_view(clip_start, clip_start + clip_duration)
            full_frame = plan.full_frame_ranges(clip_start, clip_start + clip_duration)
        # Captions sit on the seam during the split view and low otherwise; the
        # title must miss whichever ones are on screen while it shows.
        split_time = sum(
            max(0.0, min(b, seconds) - a) for a, b in (split.spans if split else ())
        )
        if split_time >= seconds / 2:
            # Two-speaker split view: both panels are mostly face, so the title sits
            # on the seam between them (over the top speaker's chest and the space
            # above the lower speaker's head); the captions move just under it
            # while it shows (core.subtitle_engine, seam_title).
            image_path = work_dir / f"{name}_title_{FONT_SIZES[0]}.png"
            _path, card_width, card_height = render_title_card(
                text, image_path, style=style, max_font_size=FONT_SIZES[0]
            )
            card_top = SEAM_Y - card_height // 2
            logger.info("Title card: on the seam (split view) at y=%d (%d px tall)", card_top, card_height)
            return TitleOverlay(
                image_path=image_path,
                x=(FRAME_WIDTH - card_width) // 2 - SHADOW_MARGIN,
                y=card_top - SHADOW_MARGIN,
                seconds=seconds,
                card_top=card_top,
                card_height=card_height,
                placement=SEAM_PLACEMENT,
            )
        blocked = []
        if split_time > 0:
            blocked.append(SEAM_CAPTION_ZONE)
        if split_time < seconds - 0.05:
            blocked.append((CAPTION_ZONE_TOP, CAPTION_ZONE_BOTTOM))
        try:
            zones = _face_zones(
                source_video, plan, crop, frame_size, clip_start, seconds, split=split, full_frame=full_frame
            )
        except (subprocess.CalledProcessError, OSError, ValueError, subprocess.TimeoutExpired):
            logger.warning("Could not check the face position for the title.", exc_info=True)

    # Try the normal size, then a smaller one if the big card would cover the face.
    candidates: list[tuple[int, Path, int, int, int, str]] = []
    for max_size in (FONT_SIZES[0], FONT_SIZES[-1]):
        image_path = work_dir / f"{name}_title_{max_size}.png"
        _path, card_width, card_height = render_title_card(
            text, image_path, style=style, max_font_size=max_size
        )
        card_top, label, covered = _place(layout, card_height, frame_size, zones, blocked)
        candidates.append((covered, image_path, card_width, card_height, card_top, label))
        if covered == 0:
            break
    covered, image_path, card_width, card_height, card_top, label = min(
        candidates, key=lambda c: c[0]
    )
    for candidate in candidates:
        if candidate[1] != image_path:
            candidate[1].unlink(missing_ok=True)
    logger.info(
        "Title card: %s at y=%d (%d px tall); covers a face in %d of %d sampled frames",
        label,
        card_top,
        card_height,
        covered,
        len(zones),
    )
    return TitleOverlay(
        image_path=image_path,
        x=(FRAME_WIDTH - card_width) // 2 - SHADOW_MARGIN,
        y=card_top - SHADOW_MARGIN,
        seconds=seconds,
        card_top=card_top,
        card_height=card_height,
        placement=label,
    )


def overlay_filter(
    base_label: str, input_index: int, overlay: TitleOverlay, output_label: str
) -> str:
    """FFmpeg filter graph piece: fade the card in (rising slightly), then out."""
    fade_out_start = max(0.0, overlay.seconds - FADE_OUT)
    return (
        f"[{input_index}:v]format=rgba,"
        f"fade=t=in:st=0:d={FADE_IN}:alpha=1,"
        f"fade=t=out:st={fade_out_start:.3f}:d={FADE_OUT}:alpha=1[title];"
        f"[{base_label}][title]overlay=x={overlay.x}:"
        f"y='{overlay.y}+{SLIDE_PX}*max(0,1-t/{FADE_IN})':eof_action=pass[{output_label}]"
    )
