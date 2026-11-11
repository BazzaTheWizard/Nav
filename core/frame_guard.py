"""Round 48: never ship a blank screen.

The Apollo GraphQL pitch went out of the worker as 40 s of flat grey with captions (a crop
bug, fixed in core.pipeline). Whatever the cause next time, a render is now checked before
it's used: sampled twice a second, a frame of a crop layout (picture edge to edge) that is mostly
one flat colour while the source frame at that moment has a picture is "blank". core.pipeline re-renders such a clip with the whole frame (no crop can miss) and
logs an error. A source that is itself blank at that moment (a fade, a black slide) is fine.
FRAME_GUARD=0 turns the check off.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)
SAMPLE_FPS = 2
STRIP_ROWS = 12  # the frame (240 px tall when checked) in strips of this many rows
FLAT_STRIP_STD = 3.0  # a strip this uniform shows no picture
BLANK_SHARE = 0.6  # a frame with this share of flat strips is blank (captions and a title are the rest)
LOW_SOURCE_STD = 6.0
MIN_SPAN = 0.9  # seconds of blank frames in a row before it counts (a flash or a fade is fine)


def enabled() -> bool:
    return os.getenv("FRAME_GUARD", "1").strip().lower() not in {"0", "false", "no", "off"}


def flat_strip_share(gray: np.ndarray) -> float:
    """Share of the frame's horizontal strips that are one flat colour."""
    strips = [gray[r:r + STRIP_ROWS] for r in range(0, gray.shape[0] - STRIP_ROWS + 1, STRIP_ROWS)]
    return sum(float(strip.std()) < FLAT_STRIP_STD for strip in strips) / max(1, len(strips))


def is_blank(gray: np.ndarray) -> bool:
    """A crop layout's frame with no picture: most of it one flat colour."""
    return flat_strip_share(gray) >= BLANK_SHARE


def _frames(path: Path, start: float, duration: float, width: int, height: int) -> list[np.ndarray]:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(path),
         "-vf", f"fps={SAMPLE_FPS},scale={width}:{height},format=gray", "-f", "rawvideo", "-"],
        capture_output=True, timeout=max(300.0, 3.0 * float(duration))).stdout
    count = len(raw) // (width * height)
    return [np.frombuffer(raw, np.uint8, width * height, k * width * height).reshape(height, width) for k in range(count)]


def blank_spans(rendered: Path, source: Path, start: float, end: float,
                skip: list[tuple[float, float]] | None = None) -> list[tuple[float, float]]:
    """Clip seconds where a crop layout's render (smart_crop, stacked: picture edge to edge) is
    blank but the source (from ``start``) isn't. ``skip``: spans shown full-frame over a blur
    on purpose (their blurred background is flat by design)."""
    if not enabled():
        return []
    try:
        out = _frames(rendered, 0.0, end - start, 135, 240)
        if not out:
            return []
        flags = [is_blank(frame) for frame in out]
        if not any(flags):
            return []
        src = _frames(source, start, end - start, 160, 90)
        spans: list[list[float]] = []
        for k, blank in enumerate(flags):
            t = k / SAMPLE_FPS
            if not blank or any(a - 0.3 <= t <= b + 0.3 for a, b in skip or []):
                continue
            if k < len(src) and float(src[k].std()) < LOW_SOURCE_STD:
                continue  # the source itself shows nothing here (a fade, a black screen)
            if spans and t - spans[-1][1] <= 1.0 / SAMPLE_FPS + 1e-6:
                spans[-1][1] = t + 1.0 / SAMPLE_FPS
            else:
                spans.append([t, t + 1.0 / SAMPLE_FPS])
        return [(round(a, 2), round(b, 2)) for a, b in spans if b - a >= MIN_SPAN]
    except Exception:  # noqa: BLE001 - the check never costs a clip
        logger.warning("Blank-frame check failed for %s.", rendered.name, exc_info=True)
        return []
