"""Poster frames: the still a prospect sees on their clip page before pressing play.

It used to be the frame at 1.2 s, whatever it showed: a blink or a half-closed eye
was the clip's first impression. Now the frame is picked (eyes open, face sharp) from
the first seconds, and the Outbox lets you pick any other frame by hand.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np

from core.face_tracker import _face_detector

logger = logging.getLogger(__name__)

DEFAULT_POSTER_AT = 1.2  # used when no face is found (and before round 22)
AUTO_WINDOW = (0.6, 4.0)  # the title card is up; still the hook's first seconds
AUTO_FPS = 8
SAMPLE_WIDTH = 640
POSTER_WIDTH = 540
MIN_FACE_SCORE = 0.8
MIN_FACE_WIDTH = 0.06  # of the frame width: smaller faces (background people) don't count


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise RuntimeError("FFmpeg was not found.")
    return path


def eyes_open_score(frame: np.ndarray) -> float | None:
    """How open the eyes look, from the contrast around each eye against the face's
    own contrast (a closed lid is flat skin; an open eye has iris and white). For
    several people, the lowest one counts: one blink spoils the still. None: no face."""
    faces = [
        face for face in _face_detector().faces_with_eyes(frame)
        if face[4] >= MIN_FACE_SCORE and face[2] >= MIN_FACE_WIDTH * frame.shape[1]
    ]
    if not faces:
        return None
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    height, width = gray.shape
    scores = []
    for cx, cy, w, h, _score, right_eye, left_eye in faces:
        radius = max(3, int(0.09 * w))
        patches = [
            gray[max(0, int(ey) - radius):int(ey) + radius, max(0, int(ex) - radius):int(ex) + radius]
            for ex, ey in (right_eye, left_eye)
        ]
        face = gray[max(0, int(cy - h / 2)):min(height, int(cy + h / 2)), max(0, int(cx - w / 2)):min(width, int(cx + w / 2))]
        if face.size == 0 or any(patch.size == 0 for patch in patches):
            continue
        sharpness = float(cv2.Laplacian(face, cv2.CV_32F).var())
        eyes = float(np.mean([patch.std() for patch in patches])) / (float(face.std()) + 1.0)
        scores.append(eyes + 0.02 * float(np.log1p(sharpness)))  # sharpness only breaks near-ties
    return min(scores) if scores else None


def best_poster_time(clip: Path, window: tuple[float, float] = AUTO_WINDOW) -> float:
    """The best poster moment in ``window`` (seconds into the clip): eyes open, face
    sharp. DEFAULT_POSTER_AT when no face is found or the clip can't be read."""
    start, end = window
    width = SAMPLE_WIDTH
    try:
        size = subprocess.run(
            [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(clip)],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout.strip().split(",")
        height = int(round(width * int(size[1]) / int(size[0]) / 2) * 2)
        raw = subprocess.run(
            [_ffmpeg(), "-v", "error", "-ss", f"{start:.3f}", "-t", f"{max(0.1, end - start):.3f}", "-i", str(clip),
             "-vf", f"fps={AUTO_FPS},scale={width}:{height},format=bgr24", "-f", "rawvideo", "pipe:1"],
            capture_output=True, timeout=120, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, RuntimeError):
        logger.warning("Could not read %s for its poster; using %.1f s.", clip, DEFAULT_POSTER_AT, exc_info=True)
        return DEFAULT_POSTER_AT
    frame_bytes = width * height * 3
    frames = np.frombuffer(raw, np.uint8)
    frames = frames[: frames.size - frames.size % frame_bytes].reshape(-1, height, width, 3)
    scores = [eyes_open_score(np.ascontiguousarray(frame)) for frame in frames]
    best: tuple[float, float] | None = None
    for index, score in enumerate(scores):
        if score is None:
            continue
        # A frame counts as its weakest neighbor (1/8 s each side): the middle of an
        # eyes-open moment, never its edge, where a frame earlier is still a blink.
        around = [s for s in scores[max(0, index - 1):index + 2]]
        steady = min(s if s is not None else 0.0 for s in around)
        if best is None or steady > best[0]:
            best = (steady, start + index / AUTO_FPS)
    return round(best[1], 2) if best else DEFAULT_POSTER_AT


def poster_jpeg(clip: Path, at: float, width: int = POSTER_WIDTH, quality: int = 3) -> bytes:
    """The frame at ``at`` seconds as a JPEG (what the clip page shows). ``quality``: FFmpeg's
    -q:v (2 is near-lossless: the full-size cover image, Round 49)."""
    result = subprocess.run(
        [_ffmpeg(), "-v", "error", "-ss", f"{max(0.0, at):.3f}", "-i", str(clip), "-frames:v", "1",
         "-vf", f"scale={width}:-2:flags=lanczos,format=yuvj420p", "-q:v", str(int(quality)), "-f", "image2pipe",
         "-vcodec", "mjpeg", "pipe:1"],
        capture_output=True, timeout=60,
    )
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(f"Could not read the frame at {at:.1f} s: {result.stderr.decode(errors='ignore')[-300:]}")
    return result.stdout
