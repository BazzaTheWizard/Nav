"""Round 36: YouTube "Most replayed" retention scoring.

yt-dlp gives a video's heatmap as buckets {"start_time", "end_time", "value"} (value 0-1:
how much that moment is rewatched, 1 = the most). A clip whose HOOK (its first
HOOK_WINDOW seconds) lands on a spike of HOT_VALUE or more opens on a moment viewers
already chose to rewatch: it is ranked higher (boost()) and badged "🔥 High Replay Moment"
in the app. Each clip carries ``peak_retention_score`` (0-1) in its metadata:

  the value of the hottest spike the hook overlaps, else half the value of a spike later
  in the clip (so only a hook on a spike reaches HOT_VALUE); 0 with no data.

Used the same way by the app (core.pipeline.pick_clips_with_agent and the fallback
selector) and the autonomous worker, so both rank the same clips the same way.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

HOT_VALUE = 0.65  # a replay spike: the heatmap value at or above which a moment counts
HOOK_WINDOW = 8.0  # seconds: the opening a viewer decides on
BOOST_POINTS = 12  # virality points added for a hook on a spike (scaled by the spike's value)
SKIP_OPENING = 20.0  # the video's first seconds always look "replayed" (playback starts there)
BADGE = "🔥 High Replay Moment"

Peak = tuple[float, float, float]  # (start, end, value)


def spikes(heatmap: Iterable[dict[str, Any]] | None, threshold: float = HOT_VALUE) -> list[Peak]:
    """The heatmap's hot regions: adjacent buckets at ``threshold`` or more merged, with
    their highest value, in time order. The video's opening is skipped."""
    points: list[Peak] = []
    for item in heatmap or []:
        try:
            start, end, value = float(item["start_time"]), float(item["end_time"]), float(item["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(v) for v in (start, end, value)) and end > start:
            points.append((start, end, value))
    regions: list[Peak] = []
    for start, end, value in sorted(points):
        if value < threshold or end <= SKIP_OPENING:
            continue
        if regions and start - regions[-1][1] <= 1.0:
            first, _end, top = regions[-1]
            regions[-1] = (first, end, max(top, value))
        else:
            regions.append((start, end, value))
    return regions


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def peak_retention_score(start: float, end: float, peaks: Iterable[Peak] | None) -> float:
    """0-1: how strongly a clip from ``start`` to ``end`` sits on a replay spike (see the
    module docstring). ``peaks``: spikes() or core.hook_selector.replay_peaks()."""
    hook_end = min(end, start + HOOK_WINDOW)
    best = 0.0
    for p0, p1, value in peaks or []:
        if value < HOT_VALUE:
            continue
        if _overlap(start, hook_end, p0, p1) > 0:
            best = max(best, value)
        elif _overlap(start, end, p0, p1) > 0:
            best = max(best, 0.5 * value)
    return round(min(1.0, best), 3)


def hook_on_spike(start: float, end: float, peaks: Iterable[Peak] | None) -> bool:
    """True when the clip's hook overlaps a spike of HOT_VALUE or more."""
    hook_end = min(end, start + HOOK_WINDOW)
    return any(value >= HOT_VALUE and _overlap(start, hook_end, p0, p1) > 0 for p0, p1, value in peaks or [])


def boost(virality: int, start: float, end: float, peaks: Iterable[Peak] | None) -> int:
    """``virality`` raised for a hook on a spike (up to BOOST_POINTS), capped at 100."""
    peaks = list(peaks or [])
    if not hook_on_spike(start, end, peaks):
        return int(virality)
    return int(min(100, virality + round(BOOST_POINTS * peak_retention_score(start, end, peaks))))


def score_clips(clips: list[Any], peaks: Iterable[Peak] | None, *, rank: bool = True,
                raise_scores: bool = True) -> list[Any]:
    """Each clip (core.hook_selector.ClipResult) with ``peak_retention_score`` set and, when
    ``raise_scores``, its ``virality_score`` boosted for a hook on a spike; best first when
    ``rank``. Boost once, before choosing; re-score the final (snapped) times with
    ``raise_scores=False`` so the badge matches the clip as cut."""
    peaks = list(peaks or [])
    scored = []
    for clip in clips:
        update: dict[str, Any] = {"peak_retention_score": peak_retention_score(clip.start_time, clip.end_time, peaks)}
        if raise_scores:
            update["virality_score"] = boost(clip.virality_score, clip.start_time, clip.end_time, peaks)
        scored.append(clip.model_copy(update=update))
    return sorted(scored, key=lambda c: c.virality_score, reverse=True) if rank else scored


def high_replay(clip: Any) -> bool:
    """The app's badge: the clip's hook is on a replay spike."""
    return float(getattr(clip, "peak_retention_score", 0.0) or 0.0) >= HOT_VALUE
