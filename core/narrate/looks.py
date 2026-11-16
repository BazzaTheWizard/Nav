"""Round 63: caption-style rotation for Narrate mode.

A channel where every video has the same captions reads as templated, so each Narrate video
gets one of NARRATE_CAPTION_STYLES (core.subtitle_engine.CAPTION_STYLES):

- stable per video: a script keeps the look it was first given (a re-render looks the same);
- rotating: a new script gets the look used least recently, so consecutive videos differ;
- no clash with the pop-ups: a look whose spoken-word colour matches the video's pop-up box
  (core.narrate.finish.POPUP_STYLES) is skipped, so the [TEXT] pop-up still stands out.

The choices are kept in storage/narrate/caption_styles.json beside the footage library.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

NARRATE_CAPTION_STYLES = ("bold", "neon", "lime", "boxed")
# The colour family of each look's spoken word, and of each pop-up box (same order as POPUP_STYLES).
CAPTION_HUES = {"bold": "yellow", "neon": "cyan", "lime": "green", "boxed": "pink", "classic": "yellow"}
POPUP_HUES = ("yellow", "cyan", "pink", "green", "white")
ROTATE = "rotate"
CHOICES = (ROTATE, *NARRATE_CAPTION_STYLES)


def _clashes(style: str, popup_style: int | None) -> bool:
    if popup_style is None:
        return False
    return CAPTION_HUES.get(style) == POPUP_HUES[popup_style % len(POPUP_HUES)]


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        if isinstance(data.get("assigned"), dict) and isinstance(data.get("order"), list):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return {"assigned": {}, "order": []}


def caption_style_for(name: str, popup_style: int | None, history: Path, choice: str = ROTATE) -> str:
    """The caption look for the video ``name``. ``choice``: "rotate" (default) or a fixed look,
    which is used as it is (and not recorded, so it never skews the rotation)."""
    choice = (choice or ROTATE).strip().lower()
    if choice != ROTATE:
        if choice not in NARRATE_CAPTION_STYLES:
            raise ValueError(f"Unknown caption style {choice!r}: use one of {', '.join(CHOICES)}")
        return choice
    data = _load(history)
    kept = data["assigned"].get(name)
    if kept in NARRATE_CAPTION_STYLES and not _clashes(kept, popup_style):
        return kept
    candidates = [s for s in NARRATE_CAPTION_STYLES if not _clashes(s, popup_style)] or list(NARRATE_CAPTION_STYLES)
    recent = [data["assigned"].get(n) for n in data["order"] if n != name]

    def last_used(style: str) -> int:  # -1: never used; else its newest position in the history
        return max((i for i, s in enumerate(recent) if s == style), default=-1)

    chosen = min(candidates, key=lambda s: (last_used(s), NARRATE_CAPTION_STYLES.index(s)))
    data["assigned"][name] = chosen
    data["order"] = [n for n in data["order"] if n != name][-199:] + [name]
    history.parent.mkdir(parents=True, exist_ok=True)
    tmp = history.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, history)
    return chosen


@contextmanager
def caption_env(style: str) -> Iterator[None]:
    """CAPTION_STYLE=style while rendering (core.subtitle_engine reads it), restored after."""
    before = os.environ.get("CAPTION_STYLE")
    os.environ["CAPTION_STYLE"] = style
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("CAPTION_STYLE", None)
        else:
            os.environ["CAPTION_STYLE"] = before
