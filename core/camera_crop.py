"""Round 36: broadcast camera physics for the 9:16 smart crop (used by core.face_tracker).

A TV operator never chases a head: the frame is locked while the subject stays inside a
dead band, moves only when they really move (and stay moved), and every move eases in and
settles like a physical head on a fluid tripod. Across a camera cut the crop never pans:
it snaps to the new angle's position on the cut frame itself.

- DEAD_BAND: the face may drift this share of the FRAME width (8%) from the crop's centre
  without the crop moving.
- Hysteresis: once the face is out of the dead band, it has to come back inside the
  tighter RELEASE_BAND before the "it's moved" timer resets, so a face hovering on the
  edge can't make the decision flicker; a move needs HOLD_SECONDS outside.
- spring(): a critically damped spring (no overshoot, no bounce) from rest to rest over
  MOVE_SECONDS, normalised to land exactly on the target; spring_expression() is the same
  curve as an FFmpeg expression, so the preview (x_at) and the render agree.
- cut_boundary(): a cut found between two analysed frames is placed exactly between them,
  so the frame before the cut keeps the old crop and the first frame of the new angle
  already has the new one.
"""
from __future__ import annotations

import math
import statistics

DEAD_BAND = 0.08  # share of the frame width the face may sit off the crop's centre
RELEASE_BAND = 0.05  # ...and the share it must come back within to reset the move timer (hysteresis)
HOLD_SECONDS = 2.0  # outside the dead band this long = a real move (a lean or a gesture never is)
MOVE_SECONDS = 0.8  # one re-frame's length
SPRING_K = 7.0  # stiffness of the critically damped spring over a move (higher = settles sooner)
SAME_X_PX = 8  # moves smaller than this are not worth making
REFRAME_COOLDOWN = 4.0  # Round 42: seconds the camera rests after a re-frame (with the flowing camera)


def spring(u: float, k: float = SPRING_K) -> float:
    """Critically damped spring progress 0..1 for normalised time ``u`` in 0..1: starts at
    rest, never overshoots, lands on 1 at u = 1."""
    u = min(1.0, max(0.0, u))
    return (1 - (1 + k * u) * math.exp(-k * u)) / (1 - (1 + k) * math.exp(-k))


def spring_expression(progress: str, k: float = SPRING_K) -> str:
    """spring() as an FFmpeg expression of ``progress`` (an expression in 0..1)."""
    norm = 1 - (1 + k) * math.exp(-k)
    return f"((1-(1+{k:g}*{progress})*exp(-{k:g}*{progress}))/{norm:.6f})"


def eased_x(t: float, begin: float, end: float, start_x: float, end_x: float) -> float:
    """The crop's x during a move from ``start_x`` (at ``begin``) to ``end_x`` (at ``end``)."""
    return start_x + (end_x - start_x) * spring((t - begin) / max(1e-6, end - begin))


def eased_x_expression(begin: float, end: float, start_x: int, end_x: int) -> str:
    progress = f"(t-{begin:.3f})/{end - begin:.3f}"
    return f"{start_x}+({end_x - start_x})*{spring_expression(progress)}"


def cut_boundary(before: float, after: float) -> float:
    """Where the crop switches for a cut between frames at ``before`` and ``after``
    (seconds): exactly between them, never on either frame."""
    return (before + after) / 2


def reframes(track: list[tuple[float, float]], x: int, crop_width: int, frame_width: int,
             start_time: float) -> tuple[tuple[float, int], ...]:
    """Dead-band re-framing for one shot. ``track``: (shot-relative second, face centre x)
    samples; ``x``: the shot's crop x. Returns (absolute start second, new x) moves: the
    crop stays put while the face is within DEAD_BAND of its centre; a face out of the band
    for HOLD_SECONDS (back inside RELEASE_BAND resets the timer) gets one eased re-centre,
    starting when it left."""
    moves: list[tuple[float, int]] = []
    from core import camera_path, visual_proof

    if visual_proof.enabled():  # Round 41: One-Euro smoothing first (micro-nods never reach the crop)
        from core.camera_energy import one_euro

        track = one_euro(track)
    # Round 42: side-to-side re-frames stay rare and unforced: after one, the camera rests
    # REFRAME_COOLDOWN seconds before it may move again.
    rest_until = -1e9
    cooldown = REFRAME_COOLDOWN if camera_path.enabled() else 0.0
    center = x + crop_width / 2
    enter, release = DEAD_BAND * frame_width, RELEASE_BAND * frame_width
    outside: list[tuple[float, float]] = []
    for t, face_x in track:
        if t < rest_until:
            outside.clear()
            continue
        offset = abs(face_x - center)
        if offset <= release or (not outside and offset <= enter):
            outside.clear()
            continue
        outside.append((t, face_x))
        if outside[-1][0] - outside[0][0] >= HOLD_SECONDS:
            far = [value for _t, value in outside if abs(value - center) > enter] or [v for _t, v in outside]
            target_center = statistics.median(far)
            new_x = int(max(0, min(frame_width - crop_width, target_center - crop_width / 2))) // 2 * 2
            if abs(new_x - (center - crop_width / 2)) > SAME_X_PX:
                moves.append((start_time + outside[0][0], new_x))
                center = new_x + crop_width / 2
                rest_until = outside[0][0] + MOVE_SECONDS + cooldown
            outside.clear()
    return tuple(moves)
