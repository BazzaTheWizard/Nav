"""Character faces: cartoon, anime and drawn faces the human face model misses.

The worker skips videos with nobody on camera (core.face_tracker.camera_check). Its
face model (YuNet) is trained on photos of people: it finds many cartoon faces anyway,
but not big-eyed, dot-eyed or line-drawn characters, so animated explainer channels
could be skipped. This adds three cheap detectors, used only for that check:

* YuNet again with a lower bar (0.5 instead of 0.7): semi-realistic and 3D characters.
* lbpcascade_animeface (models/, MIT license, by nagadomi): anime and manga faces.
* A drawn-face test: two dark, rounded, look-alike eyes side by side, inside an
  enclosed, rounded head, with a mouth below. The enclosed head is what keeps text
  ("oo" in "book"), bar charts, tables ("$9.00" in a cell) and photos out: their
  "head" leaks into the background or its outline is a rectangle.

Measured on 336 cartoon characters in 14 illustration styles and 92 faceless frames
(slides, charts, tables, code and dashboards, infographics, photos): one-style
cartoon videos passing the check went from 61% to 90%; faceless decks, charts,
screens and B-roll stayed at 0-1%.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import cv2
import numpy as np

from core.face_tracker import _yunet_model_path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANIME_CASCADE_PATH = PROJECT_ROOT / "models" / "lbpcascade_animeface.xml"
LOW_FACE_SCORE = 0.5  # YuNet's bar for characters (people: 0.7)
MIN_FACE_WIDTH_RATIO = 0.025  # same as the people check

# The drawn-face test (frames about 640 px wide).
EYE_AREA = (0.00002, 0.01)  # of the frame
EYE_ASPECT = (0.3, 3.0)
EYE_SOLIDITY = 0.75  # rounded: area / convex hull
EYE_BOX_FILL = 0.9  # above this the "eye" is a rectangle (a bar, a cell)
EYE_SIZE_RATIO = 2.5
HEAD_FILL_TOLERANCE = 14  # flood fill (neighbor to neighbor) for the head's color
HEAD_AREA = (1.5, 30.0)  # in (eye distance)^2
HEAD_BOX_FILL = 0.9  # outline (holes filled) / box: above this it is a rectangle (a chart frame, a cell)
MAX_EYES = 400  # busy frames (dense text): stop looking
MAX_FILLS = 150  # head checks per frame (each is a flood fill)
_state = threading.local()


def _low_bar_detector():
    detector = getattr(_state, "yunet", None)
    if detector is None:
        model = _yunet_model_path()
        detector = False
        if model is not None and hasattr(cv2, "FaceDetectorYN"):
            try:
                detector = cv2.FaceDetectorYN.create(str(model), "", (320, 320), LOW_FACE_SCORE, 0.3, 5000)
            except cv2.error:
                logger.warning("Could not load YuNet for character faces.", exc_info=True)
        _state.yunet = detector
    return detector or None


def _anime_cascade():
    cascade = getattr(_state, "anime", None)
    if cascade is None:
        cascade = cv2.CascadeClassifier(str(ANIME_CASCADE_PATH)) if ANIME_CASCADE_PATH.is_file() else None
        if cascade is not None and cascade.empty():
            cascade = None
        if cascade is None:
            logger.warning("Anime face model missing: %s", ANIME_CASCADE_PATH)
        _state.anime = cascade or False
    return cascade or None


def low_bar_faces(frame: np.ndarray) -> int:
    detector = _low_bar_detector()
    if detector is None:
        return 0
    height, width = frame.shape[:2]
    detector.setInputSize((width, height))
    _count, faces = detector.detect(frame)
    return 0 if faces is None else sum(1 for face in faces if face[2] >= MIN_FACE_WIDTH_RATIO * width)


def anime_faces(frame: np.ndarray) -> int:
    cascade = _anime_cascade()
    if cascade is None:
        return 0
    gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    side = max(24, int(0.04 * frame.shape[1]))
    return len(cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(side, side)))


def drawn_faces(frame: np.ndarray) -> list[tuple[float, float, float]]:
    """Drawn faces as (center x, center y, eye distance), in frame pixels."""
    height, width = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    dark = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 25)
    contours, _ = cv2.findContours(dark, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    frame_area = width * height
    eyes: list[tuple[float, float, int, int, float]] = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if not EYE_AREA[0] * frame_area <= area <= EYE_AREA[1] * frame_area:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if x <= 1 or y <= 1 or x + w >= width - 1 or y + h >= height - 1 or not EYE_ASPECT[0] <= w / h <= EYE_ASPECT[1]:
            continue
        hull = cv2.contourArea(cv2.convexHull(contour))
        if hull <= 0 or area / hull < EYE_SOLIDITY or area / (w * h) > EYE_BOX_FILL:
            continue
        eyes.append((x + w / 2, y + h / 2, w, h, area))
        if len(eyes) > MAX_EYES:
            return []  # a wall of text, not a face
    eyes.sort()
    faces: list[tuple[float, float, float]] = []
    heads: list[tuple[int, int, int, int]] = []  # found heads: other eye pairs inside are the same face
    mask = np.zeros((height + 2, width + 2), np.uint8)
    tolerance = (HEAD_FILL_TOLERANCE,) * 3
    fills = 0
    for i, left in enumerate(eyes):
        for right in eyes[i + 1:]:
            dx, dy = right[0] - left[0], right[1] - left[1]
            size = (left[2] + right[2]) / 2
            if dx > 6 * size:
                break
            if dx < 1.4 * size or abs(dy) > 0.25 * dx or not 1 / EYE_SIZE_RATIO <= left[4] / right[4] <= EYE_SIZE_RATIO:
                continue
            cx, cy = (left[0] + right[0]) / 2, (left[1] + right[1]) / 2
            if any(hx0 <= cx <= hx1 and hy0 <= cy <= hy1 for hx0, hy0, hx1, hy1 in heads):
                continue
            mouth = dark[int(cy + 0.35 * dx):int(min(height, cy + 1.6 * dx)), int(max(0, cx - 0.6 * dx)):int(cx + 0.6 * dx)]
            if mouth.size == 0 or mouth.mean() < 255 * 0.02:
                continue  # no mouth below (cheap, so it's checked before the head)
            if fills >= MAX_FILLS:
                return faces
            fills += 1
            seed = (int(cx), int(min(height - 2, cy + 0.45 * dx)))
            mask[:] = 0
            area, _image, _mask, (x0, y0, box_w, box_h) = cv2.floodFill(
                frame, mask, seed, (0, 0, 0), tolerance, tolerance, cv2.FLOODFILL_MASK_ONLY | 4 | (255 << 8)
            )
            x1, y1 = x0 + box_w - 1, y0 + box_h - 1
            if area <= 0 or x0 <= 1 or y0 <= 1 or x1 >= width - 2 or y1 >= height - 2:
                continue  # leaks into the background: not an enclosed head
            if not HEAD_AREA[0] * dx * dx <= area <= HEAD_AREA[1] * dx * dx:
                continue
            # Its outline with the holes (eyes, mouth, text) filled in: a rectangle is a
            # chart frame, a table cell (e.g. "$9.00": two dark rings) or a button.
            outline, _ = cv2.findContours(mask[y0 + 1:y1 + 2, x0 + 1:x1 + 2], cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not outline or max(cv2.contourArea(c) for c in outline) / (box_w * box_h) > HEAD_BOX_FILL:
                continue
            if not (x0 - 0.3 * dx <= left[0] - left[2] / 2 and right[0] + right[2] / 2 <= x1 + 0.3 * dx):
                continue
            faces.append((cx, cy, dx))
            heads.append((x0, y0, x1, y1))
    return faces


def has_character_face(frame: np.ndarray) -> bool:
    """A cartoon, anime or drawn face in this BGR frame (~640 px wide)."""
    return bool(low_bar_faces(frame) or anime_faces(frame) or drawn_faces(frame))
