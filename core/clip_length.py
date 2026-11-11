"""Round 55: how long a clip may run, and why it ends where it does.

Default ("idea-complete"): every clip is 35 s to 180 s long. Inside that window the length
is set by the idea, never by a clock: a clip stays with the speaker until they have landed
their thesis, argument or story, and it ends on that concluding sentence or at a natural
topic change (a long pause, the other person's next question). 180 s is the YouTube Shorts
and Instagram Reels limit; LinkedIn takes much longer videos, so 180 s never blocks a post.

CLIP_LENGTH=legacy (in .env, or set by tests/run_all.py for the suites before Round 55)
restores Round 54's rules exactly: 30-60 s picks, the 59 s cap, a 59 s teaser limit.

Every module reads its limits from here, so the AI prompt, the sentence snapping, the
fallback picker, the judge, the teaser, the worker and the re-cut checks always agree.
"""
from __future__ import annotations

import os


def legacy() -> bool:
    """True when CLIP_LENGTH asks for the old 30-60 s rules."""
    return os.getenv("CLIP_LENGTH", "idea").strip().lower() in {"legacy", "60", "old", "0", "off"}


LEGACY = legacy()

# Hard limits a final clip must fall inside (sentence snapping, fallback picks).
MIN_SECONDS = 30.0 if LEGACY else 35.0
MAX_SECONDS = 60.0 if LEGACY else 180.0
# What the AI is told to aim for. The real length is wherever the thought ends.
TARGET_TEXT = "30-60 seconds" if LEGACY else "35 seconds to 3 minutes"
# A thought that needs less than this is usually a fragment; the fallback picker and the
# boundary scorer lean toward complete arcs of at least this length.
COMFORT_SECONDS = 30.0 if LEGACY else 40.0

# AI proposals outside this range are thrown away before snapping (a model a little off
# still gets snapped into MIN..MAX).
MIN_PROPOSED_SECONDS = 10.0 if LEGACY else 20.0
MAX_PROPOSED_SECONDS = 120.0 if LEGACY else 240.0

# The automatic length cap (EditProfile.max_seconds). None: no cap, the idea decides.
# Make clips' "59 s max" box still offers the old cap by hand.
CAP_SECONDS: float | None = 59.0 if LEGACY else None
MANUAL_CAP_SECONDS = 59.0

# Teaser + clip must stay within the platform limit (core.climax_hook / core.opening).
PLATFORM_LIMIT_SECONDS = 180.0
TEASER_MAX_TOTAL_SECONDS = 59.0 if LEGACY else PLATFORM_LIMIT_SECONDS

# Words of each clip the judge reads: 3 min at a fast 200 words a minute, plus room.
JUDGE_MAX_WORDS = 300 if LEGACY else 650

# Manual re-cuts (Make clips trims, pitch trims): any length a delivered clip can have.
RECUT_MIN_SECONDS = 15.0
RECUT_MAX_SECONDS = 90.0 if LEGACY else PLATFORM_LIMIT_SECONDS


def label() -> str:
    """For the app and logs, e.g. "35 s to 3 min"."""
    return "30-60 s" if LEGACY else "35 s to 3 min"


def scaled_timeout(clip_seconds: float, base: float, per_clip_second: float) -> float:
    """A subprocess timeout that grows with the clip: a 3-minute clip must not hit a limit
    sized for a 60 s one. Never below ``base``."""
    try:
        seconds = max(0.0, float(clip_seconds))
    except (TypeError, ValueError):
        seconds = 0.0
    return max(float(base), seconds * float(per_clip_second))
