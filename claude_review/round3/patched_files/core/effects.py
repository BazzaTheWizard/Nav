"""Punch-in zooms and sound effects.

A punch-in is a hard cut to a tighter crop (default 1.15x) at the start of a
sentence that lands a point, held until that sentence ends, with a short whoosh.
Moments come from the AI picker (``ClipResult.punch_ins``); when it gives none,
sentence starts are used every few seconds so every clip still gets movement.
"""
from __future__ import annotations

import math
import random
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from core.face_tracker import CropPlan
from core.transcriber import Word


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SFX_DIR = PROJECT_ROOT / "assets" / "sfx"
WHOOSH_FILE = SFX_DIR / "whoosh.wav"

FIRST_PUNCH_AFTER = 1.5  # leave the opening hook alone
MIN_PUNCH_GAP = 3.0  # seconds between punch-in starts
MIN_ZOOMED_OUT = 1.5  # seconds back at normal framing between punch-ins
MIN_HOLD = 1.2
MAX_HOLD = 3.5
HEURISTIC_SPACING = 7.0  # when the AI gave no moments
AI_SNAP_WINDOW = 1.5  # snap AI moments to a sentence start this close
FILL_GAP = 12.0  # fill AI gaps longer than this with sentence starts
SENTENCE_PAUSE = 0.5
ZOOM_LAYOUTS = frozenset({"smart_crop", "blurred_background", "wide_blurred_background"})


@dataclass(frozen=True)
class ZoomSpec:
    """Zoomed crop window (FFmpeg expressions) and when it is shown (clip seconds)."""

    intervals: tuple[tuple[float, float], ...]
    width: str
    height: str
    x: str
    y: str

    def enable_expression(self) -> str:
        return "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in self.intervals)


@dataclass(frozen=True)
class ClipEffects:
    zoom: ZoomSpec | None = None
    sfx_times: tuple[float, ...] = ()
    sfx_path: Path | None = None
    sfx_volume: float = 0.35


# ---------------------------------------------------------------------------
# Choosing punch-in moments
# ---------------------------------------------------------------------------

def _sentences(words: Sequence[Word]) -> list[tuple[float, float]]:
    """(start, end) of each sentence in clip-relative seconds."""
    ordered = sorted(words, key=lambda word: word.start)
    sentences: list[tuple[float, float]] = []
    start: float | None = None
    for index, word in enumerate(ordered):
        if start is None:
            start = word.start
        next_word = ordered[index + 1] if index + 1 < len(ordered) else None
        ends = word.text.rstrip("\"')]}»”’").endswith((".", "!", "?"))
        paused = next_word is None or next_word.start - word.end >= SENTENCE_PAUSE
        if ends or paused:
            sentences.append((start, word.end))
            start = None
    return sentences


def plan_punch_ins(
    words: Sequence[Word],
    duration: float,
    ai_moments: Sequence[float] = (),
) -> list[tuple[float, float]]:
    """Zoom intervals (clip-relative) aligned to sentence starts and ends."""
    sentences = _sentences(words)
    if not sentences or duration <= FIRST_PUNCH_AFTER + MIN_HOLD:
        return []
    starts = [start for start, _end in sentences]

    def sentence_end(moment: float) -> float:
        for start, end in sentences:
            if start <= moment + 0.05 and end > moment:
                return end
        return moment + 2.5

    ai_points: list[float] = []
    for moment in sorted(ai_moments):
        nearest = min(starts, key=lambda start: abs(start - moment))
        ai_points.append(nearest if abs(nearest - moment) <= AI_SNAP_WINDOW else moment)

    # Sentence starts fill long stretches with no AI moment (or the whole clip).
    fillers: list[float] = []
    anchors = [0.0, *ai_points, duration]
    for left, right in zip(anchors, anchors[1:]):
        if right - left <= (FILL_GAP if ai_points else HEURISTIC_SPACING):
            continue
        # The first heuristic punch-in can come ~3 s in; later ones every ~7 s.
        last = left if left > 0 else 3.0 - HEURISTIC_SPACING
        for start in starts:
            if last + HEURISTIC_SPACING <= start <= right - MIN_PUNCH_GAP:
                fillers.append(start)
                last = start

    def interval_for(moment: float) -> tuple[float, float] | None:
        if moment < FIRST_PUNCH_AFTER or moment > duration - MIN_HOLD - 0.3:
            return None
        end = min(max(sentence_end(moment), moment + MIN_HOLD), moment + MAX_HOLD, duration - 0.3)
        return (moment, end) if end - moment >= MIN_HOLD else None

    def fits(new: tuple[float, float], chosen: list[tuple[float, float]]) -> bool:
        for start, end in chosen:
            if abs(new[0] - start) < MIN_PUNCH_GAP:
                return False
            gap = start - new[1] if new[0] < start else new[0] - end
            if gap < MIN_ZOOMED_OUT:
                return False
        return True

    # AI moments are placed first; fillers only go where they don't crowd them.
    intervals: list[tuple[float, float]] = []
    for moment in [*ai_points, *fillers]:
        candidate = interval_for(moment)
        if candidate is not None and fits(candidate, intervals):
            intervals.append(candidate)
    return sorted(intervals)


# ---------------------------------------------------------------------------
# Where the zoomed window sits
# ---------------------------------------------------------------------------

def _piecewise(segments: list[tuple[float, int]]) -> str:
    """if(lt(t,b1),v1,if(lt(t,b2),v2,...)) from (boundary, value) pairs."""
    merged: list[tuple[float, int]] = []
    for boundary, value in segments:
        if merged and merged[-1][1] == value:
            merged[-1] = (boundary, value)
        else:
            merged.append((boundary, value))
    expression = str(merged[-1][1])
    for boundary, value in reversed(merged[:-1]):
        expression = f"if(lt(t,{boundary:.3f}),{value},{expression})"
    return expression


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def zoom_spec(
    layout: str,
    plan: CropPlan | None,
    clip_start: float,
    clip_end: float,
    factor: float,
    intervals: Sequence[tuple[float, float]],
) -> ZoomSpec | None:
    """Zoom about the speaker's face so it stays in the same spot on screen."""
    if not intervals or factor <= 1.0 or layout not in ZOOM_LAYOUTS:
        return None
    windows = tuple((round(a, 3), round(b, 3)) for a, b in intervals)
    keep = 1.0 - 1.0 / factor
    if plan is None:  # no face data: zoom about the frame center
        return ZoomSpec(
            intervals=windows,
            width=f"trunc(iw/{factor}/2)*2",
            height=f"trunc(ih/{factor}/2)*2",
            x="(iw-ow)/2",
            y="(ih-oh)/2",
        )
    if layout == "smart_crop":
        base_w, base_h = plan.crop_width, plan.crop_height
    else:
        base_w, base_h = plan.frame_width, plan.frame_height
    zoom_w, zoom_h = _even(base_w / factor), _even(base_h / factor)
    xs: list[tuple[float, int]] = []
    ys: list[tuple[float, int]] = []
    for shot in plan.shots:
        if shot.end <= clip_start or shot.start >= clip_end:
            continue
        base_x, base_y = (shot.x, plan.y) if layout == "smart_crop" else (0, 0)
        focus_x = shot.face_x if shot.face_x is not None else base_x + base_w / 2
        focus_y = shot.face_y if shot.face_y is not None else base_y + base_h / 2
        x = base_x + (focus_x - base_x) * keep
        y = base_y + (focus_y - base_y) * keep
        x = min(max(x, base_x), base_x + base_w - zoom_w)
        y = min(max(y, base_y), base_y + base_h - zoom_h)
        boundary = min(shot.end, clip_end) - clip_start
        xs.append((boundary, _even(x)))
        ys.append((boundary, _even(y)))
    if not xs:
        return None
    return ZoomSpec(
        intervals=windows,
        width=str(zoom_w),
        height=str(zoom_h),
        x=_piecewise(xs),
        y=_piecewise(ys),
    )


# ---------------------------------------------------------------------------
# Sound effect
# ---------------------------------------------------------------------------

def whoosh_path() -> Path:
    """A short whoosh, synthesized once into assets/sfx/whoosh.wav.

    It is generated from noise by this code, so there are no licence limits on
    sending it to creators. Drop your own whoosh.wav there to replace it.
    """
    if WHOOSH_FILE.is_file():
        return WHOOSH_FILE
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    rate, seconds = 48000, 0.42
    total = int(rate * seconds)
    rng = random.Random(7)
    low = band = 0.0
    samples: list[float] = []
    for index in range(total):
        progress = index / total
        cutoff = 350 + 3200 * math.sin(math.pi * min(progress * 1.15, 1.0)) ** 2
        f = 2 * math.sin(math.pi * cutoff / rate)
        noise = rng.uniform(-1.0, 1.0)
        high = noise - low - 0.55 * band
        band += f * high
        low += f * band
        attack = min(1.0, progress / 0.45)
        release = min(1.0, (1.0 - progress) / 0.55)
        samples.append(band * attack * attack * release)
    peak = max(abs(value) for value in samples) or 1.0
    frames = b"".join(struct.pack("<h", int(32767 * 0.9 * value / peak)) for value in samples)
    temporary = WHOOSH_FILE.with_suffix(".tmp")
    with wave.open(str(temporary), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    temporary.replace(WHOOSH_FILE)
    return WHOOSH_FILE


# ---------------------------------------------------------------------------
# Everything for one clip
# ---------------------------------------------------------------------------

def build_effects(
    clip_words: Sequence[Word],
    clip_start: float,
    clip_end: float,
    layout: str,
    plan: CropPlan | None,
    *,
    ai_moments: Sequence[float] = (),
    zoom_factor: float = 1.15,
    sfx_enabled: bool = True,
    sfx_volume: float = 0.35,
) -> ClipEffects:
    """Punch-in zooms and whooshes for one clip (ai_moments in source seconds)."""
    if zoom_factor <= 1.0 or layout not in ZOOM_LAYOUTS:
        return ClipEffects()
    duration = clip_end - clip_start
    moments = [moment - clip_start for moment in ai_moments if clip_start <= moment <= clip_end]
    intervals = plan_punch_ins(clip_words, duration, moments)
    zoom = zoom_spec(layout, plan, clip_start, clip_end, zoom_factor, intervals)
    if zoom is None:
        return ClipEffects()
    if not sfx_enabled or sfx_volume <= 0:
        return ClipEffects(zoom=zoom)
    return ClipEffects(
        zoom=zoom,
        sfx_times=tuple(start for start, _end in zoom.intervals),
        sfx_path=whoosh_path(),
        sfx_volume=sfx_volume,
    )
