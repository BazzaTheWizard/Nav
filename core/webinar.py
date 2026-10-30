"""Webinar layout: the speaker's webcam on top, the slides below (9:16).

For slide decks, medical webinars and tutorials where a small webcam sits in a corner
of a big slide or screen share. A face crop would blow the tiny webcam up to full
height or cut the slides out; here both stay on screen:

    y 0-460     the speaker: the webcam window, sharp in the middle over a blurred
                copy of itself (no slide pixels)
    y 458-462   a thin dark line
    y 460-      the whole slide, fitted to the width (16:9 slides: 1080x608), then
                the title card and the captions under it, so neither covers a slide

One FFmpeg filter graph (split, crop, scale, overlay, vstack); encoding is unchanged,
so the Mac's VideoToolbox encoder is still used.
"""
from __future__ import annotations

import logging
import shutil
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from core.face_tracker import CropBox, _face_detector, _probe_display_size

logger = logging.getLogger(__name__)

OUTPUT_WIDTH, OUTPUT_HEIGHT = 1080, 1920
BAND_HEIGHT = 460  # the speaker band: 24% of the frame
CONTENT_MAX_HEIGHT = 640  # slides fit in 1080 x this (16:9 slides use the full width: 608 px)
DIVIDER = "drawbox=x=0:y={y}:w=1080:h=4:color=black@0.85:t=fill"
TITLE_GAP = 28  # slides -> title card
CAPTION_GAP = 36  # slides (or the title card) -> captions
TITLE_CAPTION_GAP = 20

CORNERS = ("auto", "top-right", "top-left", "bottom-right", "bottom-left")
SAMPLES = 12  # frames looked at across the clip
DETECT_WIDTH = 1280  # frames are decoded this wide; each quarter (640 wide) is searched on its own
MIN_FACE_WIDTH = 0.012  # of the frame width: smaller detections are noise
MIN_FACE_FRAMES = 3  # "auto" needs a face in this many sampled frames to trust a corner
# Finding the webcam window: a live camera changes between samples (noise, movement);
# slides stay exactly still between slide changes. The changing region around the face,
# inside its quarter of the frame, is the window (median over sample pairs, so a slide
# change doesn't count). Measured on test webinars: slides 0.00, webcam 6-7 on average.
MOTION_THRESHOLD = 0.75  # gray levels; raised when the slides themselves flicker
MAX_BOX_FACE_AREAS = 60  # a bigger "window" is a slide animation, not a webcam
BLUR = 9  # smoothing of the change map (sample pixels); it spreads the region by half of this
BOX_INSET = 0.015  # then trim this share off each side too (the window's border line)
# Fallback when no window is found: head and shoulders around the median face, never
# past the middle of the frame (the webcam is in one corner; beyond is slide).
CAMERA_ASPECT = 4 / 3
CAMERA_FACE_HEIGHTS = 2.4  # crop height in face heights
CAMERA_FACE_Y = 0.45  # the face's center sits this far down the crop
# No face found: a 16:9 box this share of the width in the corner (a common webcam size).
DEFAULT_WEBCAM_WIDTH = 0.25
DEFAULT_CORNER = "top-right"
DARK_BORDER = 28  # luminance: edge rows/columns darker than this in every sample are trimmed


@dataclass(frozen=True)
class WebinarLayout:
    """Where the speaker and the slides are in the source, and where they go."""

    frame_width: int
    frame_height: int
    corner: str  # "top-right"... (resolved, never "auto")
    found: str  # "webcam window", "face" or "no face: default corner box"
    camera: CropBox  # source pixels shown in the speaker band
    content: CropBox  # source pixels shown as the slides
    content_width: int  # output size of the slides
    content_height: int

    @property
    def content_bottom(self) -> int:
        return BAND_HEIGHT + self.content_height

    @property
    def title_top(self) -> int:
        return self.content_bottom + TITLE_GAP

    @property
    def caption_top(self) -> int:
        return self.content_bottom + CAPTION_GAP

    @property
    def camera_scale(self) -> float:
        return min(OUTPUT_WIDTH / self.camera.width, BAND_HEIGHT / self.camera.height)

    def filter_graph(self, sharpen: str = "") -> str:
        """FFmpeg filter_complex from input [0:v] to [base] (1080x1920)."""
        cam = self.camera
        cam_crop = f"crop=w={cam.width}:h={cam.height}:x={cam.x}:y={cam.y}"
        content = self.content
        content_crop = f"crop=w={content.width}:h={content.height}:x={content.x}:y={content.y}"
        band, rest = BAND_HEIGHT, OUTPUT_HEIGHT - BAND_HEIGHT
        return (
            "[0:v]split=3[web_cam][web_blur][web_slides];"
            f"[web_cam]{cam_crop},scale=w={OUTPUT_WIDTH}:h={band}:force_original_aspect_ratio=decrease:"
            f"force_divisible_by=2:flags=lanczos{sharpen}[web_face];"
            # A quarter-size blur scaled up, as in the blurred-background layouts.
            f"[web_blur]{cam_crop},scale=270:{band // 4}:force_original_aspect_ratio=increase,"
            f"crop=270:{band // 4},boxblur=5:2,eq=brightness=-0.15,scale={OUTPUT_WIDTH}:{band}[web_back];"
            "[web_back][web_face]overlay=x=(W-w)/2:y=(H-h)/2[web_top];"
            f"[web_slides]{content_crop},scale={self.content_width}:{self.content_height}:flags=lanczos,"
            f"pad={OUTPUT_WIDTH}:{rest}:(ow-iw)/2:0:color=black[web_bottom];"
            f"[web_top][web_bottom]vstack=inputs=2,{DIVIDER.format(y=band - 2)}[base]"
        )


def _even(value: float) -> int:
    """A size: even, at least 2."""
    return max(2, int(value) // 2 * 2)


def _even_position(value: float) -> int:
    return max(0, int(value) // 2 * 2)


def _sample_frames(source: Path, start: float, seconds: float, frame_size: tuple[int, int]) -> np.ndarray:
    """SAMPLES BGR frames spread over the clip, DETECT_WIDTH wide."""
    width, height = frame_size
    sample_width = min(DETECT_WIDTH, _even(width))
    sample_height = _even(sample_width * height / width)
    fps = SAMPLES / max(seconds, 0.5)
    result = subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg", "-v", "error",
            "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(source), "-an",
            "-vf", f"fps={fps:.5f},scale={sample_width}:{sample_height},format=bgr24",
            "-frames:v", str(SAMPLES), "-f", "rawvideo", "pipe:1",
        ],
        check=True, capture_output=True, timeout=180,
    )
    frame_bytes = sample_width * sample_height * 3
    data = np.frombuffer(result.stdout, dtype=np.uint8)
    return data[: data.size - data.size % frame_bytes].reshape(-1, sample_height, sample_width, 3)


def _quarter(corner: str, width: int, height: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) of the frame quarter a corner names."""
    half_w, half_h = width // 2, height // 2
    x = half_w if corner.endswith("right") else 0
    y = half_h if corner.startswith("bottom") else 0
    return x, y, width - half_w if x else half_w, height - half_h if y else half_h


def _faces_by_corner(frames: np.ndarray, corners: tuple[str, ...]) -> dict[str, list[tuple[float, float, float, float, float]]]:
    """For each corner: the best face per sampled frame, (cx, cy, w, h, score) as
    shares of the frame."""
    detector = _face_detector()
    found: dict[str, list[tuple[float, float, float, float, float]]] = {corner: [] for corner in corners}
    if not detector.available or frames.size == 0:
        return found
    height, width = frames.shape[1:3]
    for frame in frames:
        for corner in corners:
            qx, qy, qw, qh = _quarter(corner, width, height)
            faces = [
                ((qx + cx) / width, (qy + cy) / height, w / width, h / height, score)
                for cx, cy, w, h, score in detector.detect(np.ascontiguousarray(frame[qy:qy + qh, qx:qx + qw]))
                if w / width >= MIN_FACE_WIDTH
            ]
            if faces:
                found[corner].append(max(faces, key=lambda face: face[4]))
    return found


def _content_box(frames: np.ndarray, width: int, height: int, camera: CropBox | None = None) -> CropBox:
    """The whole frame without dark borders (black bars around a 4:3 slide, say). The
    webcam window doesn't count, so a webcam in a dark strip beside the slide (Zoom's
    side-by-side view) is left out and the slide shows bigger."""
    if frames.size == 0:
        return CropBox(0, 0, _even(width), _even(height))
    brightest = frames.max(axis=0).max(axis=2)  # per pixel, over every sample and channel
    sample_h, sample_w = brightest.shape
    if camera is not None:
        brightest = brightest.copy()
        margin = 0.02
        x0 = max(0, int((camera.x / width - margin) * sample_w))
        y0 = max(0, int((camera.y / height - margin) * sample_h))
        x1 = int(((camera.x + camera.width) / width + margin) * sample_w) + 1
        y1 = int(((camera.y + camera.height) / height + margin) * sample_h) + 1
        brightest[y0:y1, x0:x1] = 0
    columns = np.flatnonzero(brightest.max(axis=0) >= DARK_BORDER)
    rows = np.flatnonzero(brightest.max(axis=1) >= DARK_BORDER)
    if columns.size == 0 or rows.size == 0:
        return CropBox(0, 0, _even(width), _even(height))
    left, right = columns[0] / sample_w, (columns[-1] + 1) / sample_w
    top, bottom = rows[0] / sample_h, (rows[-1] + 1) / sample_h
    if right - left < 0.5 or bottom - top < 0.5:  # mostly dark: keep the whole frame
        return CropBox(0, 0, _even(width), _even(height))
    x, y = _even_position(left * width), _even_position(top * height)
    return CropBox(x, y, _even(min(width - x, (right - left) * width)), _even(min(height - y, (bottom - top) * height)))


def _camera_box(face: tuple[float, float, float, float], corner: str, width: int, height: int) -> CropBox:
    """Head and shoulders around the face, inside the face's quarter of the frame."""
    cx, cy, _fw, fh = face[0] * width, face[1] * height, face[2] * width, face[3] * height
    qx, qy, qw, qh = _quarter(corner, width, height)
    crop_h = min(CAMERA_FACE_HEIGHTS * fh, qh, qw / CAMERA_ASPECT)
    crop_w = crop_h * CAMERA_ASPECT
    x = min(max(cx - crop_w / 2, qx), qx + qw - crop_w)
    y = min(max(cy - CAMERA_FACE_Y * crop_h, qy), qy + qh - crop_h)
    return CropBox(_even_position(x), _even_position(y), _even(crop_w), _even(crop_h))


def _webcam_window(
    frames: np.ndarray, face: tuple[float, float, float, float], corner: str, width: int, height: int
) -> CropBox | None:
    """The webcam window around ``face`` (shares of the frame), in source pixels, or
    None when no believable window is found."""
    if len(frames) < 4:
        return None
    gray = frames.mean(axis=3, dtype=np.float32)
    change = cv2.blur(np.median(np.abs(np.diff(gray, axis=0)), axis=0), (BLUR, BLUR))
    sample_h, sample_w = change.shape
    qx, qy, qw, qh = _quarter(corner, sample_w, sample_h)
    outside = np.ones(change.shape, bool)
    outside[qy:qy + qh, qx:qx + qw] = False
    threshold = max(MOTION_THRESHOLD, 3 * float(np.median(change[outside])) + 0.5)
    mask = np.zeros(change.shape, np.uint8)
    mask[qy:qy + qh, qx:qx + qw] = change[qy:qy + qh, qx:qx + qw] > threshold
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    _count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask)
    fx, fy = face[0] * sample_w, face[1] * sample_h
    fw, fh = face[2] * sample_w, face[3] * sample_h
    face_labels = labels[
        max(0, int(fy - fh / 2)):int(fy + fh / 2) + 1, max(0, int(fx - fw / 2)):int(fx + fw / 2) + 1
    ]
    face_labels = face_labels[face_labels > 0]
    if face_labels.size < 0.25 * fw * fh:  # the face itself isn't "live": no window
        return None
    x, y, w, h, _area = stats[int(np.bincount(face_labels).argmax())]
    if (
        w * h > MAX_BOX_FACE_AREAS * fw * fh
        or w < 1.5 * fw
        or h < 1.5 * fh
        or not 0.5 <= w / h <= 3.0
    ):
        return None
    inset_x, inset_y = BLUR // 2 + w * BOX_INSET, BLUR // 2 + h * BOX_INSET
    to_source_x, to_source_y = width / sample_w, height / sample_h
    return CropBox(
        _even_position((x + inset_x) * to_source_x),
        _even_position((y + inset_y) * to_source_y),
        _even((w - 2 * inset_x) * to_source_x),
        _even((h - 2 * inset_y) * to_source_y),
    )


def _default_box(corner: str, width: int, height: int) -> CropBox:
    """No face found: a 16:9 webcam-sized box tucked into the corner."""
    box_w = _even(width * DEFAULT_WEBCAM_WIDTH)
    box_h = _even(box_w * 9 / 16)
    margin_x, margin_y = _even(width * 0.01), _even(height * 0.01)
    x = width - box_w - margin_x if corner.endswith("right") else margin_x
    y = height - box_h - margin_y if corner.startswith("bottom") else margin_y
    return CropBox(_even_position(x), _even_position(y), box_w, box_h)


def plan_webinar(source: Path, start: float, end: float, corner: str = "auto") -> WebinarLayout:
    """Find the speaker's webcam (in ``corner``, or the corner where a face shows up
    most) and the slide area for one clip."""
    if corner not in CORNERS:
        raise ValueError(f"Unknown webcam corner {corner!r}; choose one of {CORNERS}.")
    width, height = _probe_display_size(source)
    try:
        frames = _sample_frames(source, start, max(0.5, end - start), (width, height))
    except (OSError, subprocess.SubprocessError, ValueError):
        logger.warning("Could not sample frames for the webinar layout.", exc_info=True)
        frames = np.zeros((0, 2, 2, 3), dtype=np.uint8)
    candidates = CORNERS[1:] if corner == "auto" else (corner,)
    found = _faces_by_corner(frames, candidates)
    if corner == "auto":
        best = max(candidates, key=lambda c: (len(found[c]), sum(face[4] for face in found[c])))
        chosen = best if len(found[best]) >= MIN_FACE_FRAMES else DEFAULT_CORNER
    else:
        chosen = corner
    faces = found.get(chosen, [])
    if faces:
        face = tuple(statistics.median(face[i] for face in faces) for i in range(4))
        window = _webcam_window(frames, face, chosen, width, height)  # type: ignore[arg-type]
        if window is not None:
            camera, how = window, "webcam window"
        else:
            camera, how = _camera_box(face, chosen, width, height), "face"  # type: ignore[arg-type]
    else:
        camera, how = _default_box(chosen, width, height), "no face: default corner box"
    content = _content_box(frames, width, height, camera if how == "webcam window" else None)
    scale = min(OUTPUT_WIDTH / content.width, CONTENT_MAX_HEIGHT / content.height)
    layout = WebinarLayout(
        frame_width=width, frame_height=height, corner=chosen, found=how, camera=camera, content=content,
        content_width=_even(content.width * scale), content_height=_even(content.height * scale),
    )
    logger.info(
        "Webinar layout: speaker in the %s corner (%s, %d of %d frames), camera crop %dx%d at %d,%d; "
        "slides %dx%d shown at %dx%d",
        chosen, how, len(faces), len(frames), camera.width, camera.height, camera.x, camera.y,
        content.width, content.height, layout.content_width, layout.content_height,
    )
    return layout
