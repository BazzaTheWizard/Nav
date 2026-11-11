from __future__ import annotations

import logging
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from core import clip_length, explainer
from core.face_tracker import CropBox, CropPlan, _probe_display_size
from core.subtitle_engine import create_ass_subtitles
from core.title_card import TitleOverlay, prepare_title_overlay, title_above_captions
from core.transcriber import Word
from core.video_renderer import CROP_LAYOUTS, pull_in_enabled, render_clip
from core.webinar import WebinarLayout, plan_webinar

logger = logging.getLogger(__name__)


# Manual re-cuts. Round 55 (core.clip_length): up to 180 s, any length a delivered clip has.
MIN_CLIP_DURATION = clip_length.RECUT_MIN_SECONDS
MAX_CLIP_DURATION = clip_length.RECUT_MAX_SECONDS


def _word_value(word: Word | dict[str, Any], key: str) -> Any:
    if isinstance(word, dict):
        return word[key]
    return getattr(word, key)


# Re-render names: "clip_<run>_<n>_edit_<8 hex>.mp4". Earlier edits' suffixes are
# dropped first, so the name stays the same length however often a clip is
# trimmed (chaining "_adjusted_..." each time used to pass macOS's 255-char limit).
_EDIT_SUFFIXES = re.compile(r"(?:_(?:adjusted|rerender|edit)_[0-9a-f]{6,32})+$")
MAX_CLIP_STEM = 100


def rerender_output_path(clip_path: str | Path) -> Path:
    """A fresh, short output path for a re-rendered clip, next to the original."""
    path = Path(clip_path)
    stem = _EDIT_SUFFIXES.sub("", path.stem)[:MAX_CLIP_STEM].rstrip("_") or "clip"
    return path.with_name(f"{stem}_edit_{uuid.uuid4().hex[:8]}.mp4")


def _center_crop(source_path: Path, cached_crop_x: int | None) -> CropBox:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=s=x:p=0",
                str(source_path),
            ],
            check=True,
            capture_output=True,
            timeout=60,
            text=True,
        )
        width_text, height_text = result.stdout.strip().split("x", maxsplit=1)
        input_width, input_height = int(width_text), int(height_text)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError(
            f"Could not read source video dimensions for re-rendering: {source_path}"
        ) from exc

    if input_width <= 0 or input_height <= 0:
        raise ValueError(f"Source video has invalid dimensions: {source_path}")
    if input_width / input_height >= 9 / 16:
        crop_width = min(input_width, round(input_height * 9 / 16))
        crop_height = input_height
    else:
        crop_width = input_width
        crop_height = min(input_height, round(input_width * 16 / 9))
    crop_x = (
        (input_width - crop_width) // 2
        if cached_crop_x is None
        else min(max(cached_crop_x, 0), input_width - crop_width)
    )
    crop_y = (input_height - crop_height) // 2
    return CropBox(crop_x, crop_y, crop_width, crop_height)


class _NoTitleHere(Exception):
    """No title card on this re-render (core.executive.title_plan)."""


def rerender_clip(
    source_path: str | Path,
    new_start: float,
    new_end: float,
    transcript_words: list[Word] | list[dict[str, Any]],
    output_path: str | Path,
    cached_crop_x: int | None = None,
    *,
    cached_crop: CropBox | None = None,
    layout_mode: str = "smart_crop",
    source_duration: float | None = None,
    crop_plan: CropPlan | None = None,
    hook_title: str | None = None,
    hook_seconds: float = 0.0,
    highlight_active_word: bool = True,
    hook_style: str | None = None,
    pin_title: bool = False,
    webinar: WebinarLayout | None = None,
    title_fade_in: bool = True,
) -> Path:
    """``pin_title``: keep the title card on for the whole clip. ``webinar``: the
    first render's webinar layout (layout "webinar"; found again if missing).
    ``title_fade_in``: False shows the title card fully from the first frame."""
    duration = new_end - new_start
    if not MIN_CLIP_DURATION <= duration <= MAX_CLIP_DURATION:
        raise ValueError(f"Adjusted clip duration must remain between {MIN_CLIP_DURATION:g} and "
                         f"{MAX_CLIP_DURATION:g} seconds.")
    if new_start < 0:
        raise ValueError("Adjusted clip start must be non-negative.")
    if source_duration is not None and new_end > source_duration:
        raise ValueError("Adjusted clip boundaries must stay within the source video.")

    source = Path(source_path).expanduser().resolve(strict=True)
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Short temporary name, independent of the clip's own name.
    temporary_output = destination.with_name(f"tmp_rerender_{uuid.uuid4().hex[:12]}.mp4")
    temp_dir = PROJECT_ROOT / "storage" / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    subtitle_path = temp_dir / (
        f"sub_{int(new_start * 100)}_{int(new_end * 100)}_{uuid.uuid4().hex}.ass"
    )
    title: TitleOverlay | None = None
    try:
        clip_words: list[Word] = []
        for word in transcript_words:
            word_start = float(_word_value(word, "start"))
            word_end = float(_word_value(word, "end"))
            if word_end <= new_start or word_start >= new_end:
                continue
            relative_start = max(0.0, word_start - new_start)
            relative_end = max(
                relative_start + 0.05,
                min(duration, word_end - new_start),
            )
            clip_words.append(
                Word(
                    text=str(_word_value(word, "text")),
                    start=relative_start,
                    end=relative_end,
                )
            )
        crop = cached_crop
        if crop is None:
            crop = _center_crop(source, cached_crop_x)
        elif cached_crop_x is not None:
            crop = CropBox(
                x=cached_crop_x,
                y=crop.y,
                width=crop.width,
                height=crop.height,
            )

        x_expression = None
        full_frame: list[tuple[float, float]] = []
        split = None
        holds = (0.0, 0.0)
        if crop_plan is not None and layout_mode in CROP_LAYOUTS:
            crop = crop_plan.crop
            x_expression = crop_plan.x_expression(new_start, new_end)
            holds = crop_plan.holds(new_start, new_end)
            if layout_mode == "smart_crop":
                # Same as first renders: slides and wide shots show the whole frame,
                # two-person shots the two-speaker split view.
                full_frame = crop_plan.full_frame_ranges(new_start, new_end)
                split = crop_plan.split_view(new_start, new_end)
        if layout_mode == "webinar" and webinar is None:
            webinar = plan_webinar(source, new_start, new_end)

        # Round 49: the same title rule as first renders (core.executive.title_plan): the creator
        # pill only in the creator look, else the executive topic header for 3.5 s (or none).
        from core import executive

        title_on, plan_style, plan_seconds, topic_header = executive.title_plan(None)
        pill = title_on and not topic_header
        if topic_header:
            hook_style, hook_seconds, title_fade_in = plan_style, plan_seconds, False
        try:
            if not title_on:
                raise _NoTitleHere
            title = prepare_title_overlay(
                hook_title,
                hook_seconds,
                layout=layout_mode,
                plan=crop_plan,
                crop=crop,
                source_video=source,
                clip_start=new_start,
                clip_duration=duration,
                work_dir=temp_dir,
                name=subtitle_path.stem,
                style=hook_style,
                pinned=pin_title,
                webinar=webinar,
                fade_in=title_fade_in,
            )
        except _NoTitleHere:
            title = None
        except Exception:
            logger.warning("Title card failed; using the plain subtitle title.", exc_info=True)
        create_ass_subtitles(
            clip_words,
            subtitle_path,
            clip_duration=duration,
            highlight_active_word=highlight_active_word,
            # The card replaces the old subtitle-style title; that one is only a fallback.
            hook_title=None if (title or not pill) else hook_title,
            hook_seconds=duration if pin_title else hook_seconds,
            seam_spans=split.spans if split else None,
            seam_title=title_above_captions(title),
            caption_top=(
                webinar.caption_top if layout_mode == "webinar" and webinar
                else explainer.caption_top(explainer.safe_box(*_probe_display_size(source)))
                if layout_mode == "explainer" else None
            ),
        )
        render_clip(
            source,
            subtitle_path,
            crop,
            new_start,
            new_end,
            temporary_output,
            layout_mode=layout_mode,
            crop_x_expression=x_expression,
            title=title,
            full_frame_ranges=full_frame or None,
            split_view=split,
            webinar=webinar if layout_mode == "webinar" else None,
            holds=holds,
            pull_in_until=(crop_plan.pull_in_until(new_start, new_end)
                           if layout_mode == "smart_crop" and crop_plan is not None and pull_in_enabled() else 0.0),
        )
        temporary_output.replace(destination)
    finally:
        subtitle_path.unlink(missing_ok=True)
        temporary_output.unlink(missing_ok=True)
        if title is not None:
            title.image_path.unlink(missing_ok=True)
    return destination
