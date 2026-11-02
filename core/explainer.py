"""Explainer layout: a faceless slide video (Keynote, Canva) as a vertical Short.

The long video is designed at 16:9 with everything important inside the center
4:3 box (1440x1080 of a 1920x1080 slide). For the Short, that box fills the phone's
width:

    y 0-440      blurred, darkened copy of the slide (the title card sits at the
                 bottom of this band, just above the slide)
    y 440-1250   the center 4:3 box at 1080x810 (a third larger than the whole
                 16:9 frame at 1080x608 in "full frame over a blurred background")
    y 1280-      the captions, above where TikTok's caption and buttons start

Make clips picks it by itself ("Auto") when no face fills the clip and the left and
right margins of the frame are nearly empty, which is how such videos are designed.
"""
from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

from core.face_tracker import CropBox

logger = logging.getLogger(__name__)

OUTPUT_WIDTH, OUTPUT_HEIGHT = 1080, 1920
SAFE_ASPECT = 4 / 3  # the center box that carries everything important
SLIDE_TOP = 440
SLIDE_BOTTOM_MAX = 1250  # Reels and Shorts start covering the frame about here
TITLE_GAP = 24  # title card -> slide
CAPTION_GAP = 30  # slide -> captions
EXPLAINER_PLACEMENT = "above the slide"

# Auto: the margins outside the center box must be this quiet compared with it.
SAMPLES = 8
SAMPLE_WIDTH = 640
WIDE_ASPECT = (1.6, 1.85)  # 16:9 sources only (1.78); 4:3 or vertical sources have no margins
MAX_MARGIN_RATIO = 0.3  # edge density outside the box / inside it
MIN_CENTER_EDGES = 0.004  # a nearly empty center (a black frame) says nothing


def safe_box(width: int, height: int) -> CropBox:
    """The center 4:3 box of a ``width`` x ``height`` frame (the whole frame if it's
    already 4:3 or narrower). Even sizes, so FFmpeg's crop and scale agree."""
    box_width = min(width, round(height * SAFE_ASPECT)) // 2 * 2
    box_height = height // 2 * 2
    return CropBox(x=(width - box_width) // 2, y=(height - box_height) // 2, width=box_width, height=box_height)


def slide_size(box: CropBox) -> tuple[int, int]:
    """The box's size on screen: the full 1080 width, at most down to SLIDE_BOTTOM_MAX."""
    height = round(OUTPUT_WIDTH * box.height / box.width)
    if SLIDE_TOP + height <= SLIDE_BOTTOM_MAX:
        return OUTPUT_WIDTH, height // 2 * 2
    height = SLIDE_BOTTOM_MAX - SLIDE_TOP
    return round(height * box.width / box.height) // 2 * 2, height // 2 * 2


def caption_top(box: CropBox) -> int:
    return SLIDE_TOP + slide_size(box)[1] + CAPTION_GAP


def title_top(card_height: int) -> int:
    return SLIDE_TOP - TITLE_GAP - card_height


def filter_graph(frame: tuple[int, int], sharpen: str = "") -> str:
    """FFmpeg filter_complex from input [0:v] to [base] (1080x1920)."""
    box = safe_box(*frame)
    width, height = slide_size(box)
    return (
        "[0:v]split=2[exp_bg][exp_slide];"
        "[exp_bg]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
        "boxblur=5:2,eq=brightness=-0.22,scale=1080:1920[exp_back];"
        f"[exp_slide]crop=w={box.width}:h={box.height}:x={box.x}:y={box.y},"
        f"scale={width}:{height}:flags=lanczos{sharpen}[exp_front];"
        f"[exp_back][exp_front]overlay=x=(W-w)/2:y={SLIDE_TOP}[base]"
    )


def _margin_ratio(frame: np.ndarray) -> tuple[float, float]:
    """(edge density outside the center box / inside it, inside density)."""
    height, width = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 60, 160) > 0
    box = safe_box(width, height)
    inside = edges[:, box.x:box.x + box.width].mean()
    outside = np.concatenate([edges[:, :box.x], edges[:, box.x + box.width:]], axis=1)
    margin = float(outside.mean()) if outside.size else 0.0
    return (margin / inside if inside > 0 else float("inf")), float(inside)


def explainer_fit(source: Path, start: float, end: float) -> bool:
    """Does this clip look like a designed slide video: a 16:9 frame whose left and
    right margins (outside the center 4:3 box) are nearly empty in most frames?"""
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        return False
    try:
        frames: list[np.ndarray] = []
        for index in range(SAMPLES):
            seconds = start + (end - start) * (index + 0.5) / SAMPLES
            capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000.0)
            ok, frame = capture.read()
            if ok and frame is not None:
                frames.append(frame)
    finally:
        capture.release()
    if len(frames) < SAMPLES // 2:
        return False
    height, width = frames[0].shape[:2]
    if not WIDE_ASPECT[0] <= width / max(1, height) <= WIDE_ASPECT[1]:
        return False
    scale = SAMPLE_WIDTH / width
    measured = [_margin_ratio(cv2.resize(frame, (SAMPLE_WIDTH, round(height * scale)))) for frame in frames]
    quiet = sum(1 for ratio, inside in measured if inside >= MIN_CENTER_EDGES and ratio <= MAX_MARGIN_RATIO)
    fit = quiet >= len(measured) * 0.75
    logger.info(
        "Explainer check: %d of %d sampled frames have quiet margins (%s)", quiet, len(measured),
        "using the explainer layout" if fit else "not a slide video",
    )
    return fit
