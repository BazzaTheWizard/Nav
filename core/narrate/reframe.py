"""Round 60: an action-following crop for gameplay (Phase 2).

Gameplay has no face to track, so the crop follows what moves. For each shot:
1. a few small frames, and the optical flow between them (OpenCV Farneback);
2. the camera's own pan removed (the median flow), so a car driving past counts and a
   turning camera doesn't;
3. the columns where the remaining motion is, as a centre (0 = left edge, 1 = right edge);
4. pulled toward the frame centre (third-person games keep the player near it) and slowed
   to at most MAX_PAN of the frame per second, so the crop drifts instead of jumping.

The result is a start and end centre per shot; core.narrate.assemble pans linearly between
them. No new installs: OpenCV is already in requirements.txt.
"""
from __future__ import annotations

import functools
import subprocess
from dataclasses import dataclass

import numpy as np

FRAMES = {  # name -> (output width, output height); "wide" keeps the 16:9 picture
    "wide": (1920, 1080),
    "tall": (1080, 1350),  # 4:5, shown over a blurred copy: keeps more of the scene
    "vertical": (1080, 1920),  # full-screen 9:16
}
CENTER_PULL = 0.45  # 0 = follow the motion only, 1 = always the centre
MAX_PAN = 0.18  # the most the crop centre moves per second (share of the frame width)
SAMPLE_FPS = 4.0
KEEP_INSIDE = 0.3  # the action's centre stays within this share of the crop width from its middle
MIN_ENERGY = 0.35  # mean motion (px/frame at 320 px) below which the shot stays centred


@dataclass(frozen=True)
class Track:
    start: float  # crop centre at the shot's first frame (0-1 of the source width)
    end: float  # ... and at its last frame


@functools.lru_cache(maxsize=256)
def source_size(path: str) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    w, h = out.stdout.strip().split(",")[:2]
    return int(w), int(h)


def motion_centres(path: str, start: float, duration: float) -> list[tuple[float, float]]:
    """(centre 0-1, energy) per sampled frame pair."""
    import cv2

    cap = cv2.VideoCapture(path)
    grey: list[np.ndarray] = []
    try:
        for t in np.arange(start, start + duration, 1.0 / SAMPLE_FPS):
            cap.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000.0)
            ok, frame = cap.read()
            if ok:
                small = cv2.resize(frame, (320, int(320 * frame.shape[0] / frame.shape[1])))
                grey.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
    finally:
        cap.release()
    found: list[tuple[float, float]] = []
    for a, b in zip(grey, grey[1:]):
        flow = cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 3, 15, 3, 5, 1.2, 0)
        flow -= np.median(flow.reshape(-1, 2), axis=0)  # remove the camera's own pan
        mag = np.linalg.norm(flow, axis=2)
        h = mag.shape[0]
        mag[int(h * 0.72):, : int(mag.shape[1] * 0.28)] = 0  # ignore the minimap corner (bottom-left HUD)
        energy = float(mag.mean())
        cols = mag.sum(axis=0)
        cols = np.convolve(cols, np.ones(25) / 25, mode="same")
        if cols.sum() <= 0:
            found.append((0.5, 0.0))
            continue
        top = cols >= np.quantile(cols, 0.8)  # the busiest fifth of the columns
        xs = np.arange(len(cols))
        centre = float((xs[top] * cols[top]).sum() / cols[top].sum()) / len(cols)
        found.append((centre, energy))
    return found


def track(path: str, start: float, duration: float, out_aspect: float) -> Track:
    """Where to centre a crop of ``out_aspect`` (width / height) over this piece of a shot."""
    w, h = source_size(path)
    crop_share = min(1.0, h * out_aspect / w)  # the crop's width as a share of the source width
    if crop_share >= 0.999:
        return Track(0.5, 0.5)
    samples = motion_centres(path, start, duration)
    moving = [(c, e) for c, e in samples if e >= MIN_ENERGY]
    if not moving:
        return Track(0.5, 0.5)
    half = max(1, len(moving) // 2)
    first = np.average([c for c, _ in moving[:half]], weights=[e for _, e in moving[:half]])
    last = np.average([c for c, _ in moving[-half:]], weights=[e for _, e in moving[-half:]])
    def pull(c: float) -> float:
        # toward the centre, but the action always stays inside the crop's middle 60 %
        pulled = 0.5 + (1.0 - CENTER_PULL) * (c - 0.5)
        return float(np.clip(pulled, c - KEEP_INSIDE * crop_share, c + KEEP_INSIDE * crop_share))

    a, b = pull(float(first)), pull(float(last))
    limit = MAX_PAN * max(duration, 0.1)
    b = a + float(np.clip(b - a, -limit, limit))
    lo, hi = crop_share / 2, 1 - crop_share / 2  # the crop must stay inside the frame
    return Track(float(np.clip(a, lo, hi)), float(np.clip(b, lo, hi)))


def crop_filter(path: str, track_: Track, frame: str, duration: float, zoom: float = 1.0) -> str:
    """The FFmpeg crop+scale for one piece, panning from track.start to track.end."""
    out_w, out_h = FRAMES[frame]
    w, h = source_size(path)
    aspect = out_w / out_h
    ch = int(h / zoom) // 2 * 2
    cw = min(w, int(ch * aspect)) // 2 * 2
    ch = min(ch, int(cw / aspect)) // 2 * 2
    span = max(0, w - cw)
    left0 = track_.start * w - cw / 2
    left1 = track_.end * w - cw / 2
    d = max(duration, 0.01)
    x = f"max(0,min({span},{left0:.1f}+({left1 - left0:.1f})*t/{d:.3f}))"
    y = f"{(h - ch) // 2}"
    return f"crop={cw}:{ch}:'{x}':{y},scale={out_w}:{out_h}:flags=lanczos"
