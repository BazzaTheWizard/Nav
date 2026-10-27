"""One clip-picking and rendering path shared by main.py, app.py and the worker.

Before this module the same steps were copied three times, so a fix in one
copy didn't reach the others.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Settings
from core.agent_brain import select_clips_and_draft_pitch
from core.clipper import _center_crop
from core.downloader import extract_audio_segment, fetch_video_info
from core.face_tracker import CropBox, CropPlan, analyze_crop_plan
from core.hook_selector import ClipResult, finalize_candidates, replay_peaks
from core.subtitle_engine import create_ass_subtitles
from core.title_card import TitleOverlay, prepare_title_overlay
from core.transcriber import (
    Word,
    clip_words_from_transcript,
    transcribe_clip_audio,
    transcript_segments,
)
from core.video_renderer import CROP_LAYOUTS, LAYOUT_MODES, render_clip


logger = logging.getLogger(__name__)

# "auto" uses a full-screen face crop only when it will look good:
MIN_SMART_CROP_WIDTH = 540  # 9:16 crop width in source pixels (needs ~1080p+)
AUTO_MIN_FACE_COVERAGE = 0.6  # share of the clip where a face is found
AUTO_MAX_GROUP_COVERAGE = 0.35  # share in wide shots with 2+ people far apart
AUTO_MAX_SHOTS = 40  # very fast cutting looks jumpy with a moving crop
_PLACEHOLDER_CROP = CropBox(x=0, y=0, width=2, height=2)


@dataclass(frozen=True)
class RenderedClip:
    path: Path
    clip: ClipResult
    layout_mode: str
    crop: CropBox
    crop_plan: CropPlan | None


def choose_layout(requested: str, plan: CropPlan | None) -> str:
    """Resolve "auto" to a concrete layout; explicit layouts are respected."""
    if requested != "auto":
        if requested not in LAYOUT_MODES:
            raise ValueError(f"Unknown layout mode {requested!r}.")
        return requested
    if (
        plan is not None
        and plan.crop_width >= MIN_SMART_CROP_WIDTH
        and plan.face_coverage >= AUTO_MIN_FACE_COVERAGE
        and plan.group_coverage <= AUTO_MAX_GROUP_COVERAGE
        and len(plan.shots) <= AUTO_MAX_SHOTS
    ):
        return "smart_crop"
    return "blurred_background"


def render_selected_clip(
    source_video: Path,
    clip: ClipResult,
    words: list[Word],
    settings: Settings,
    output_path: Path,
    *,
    layout_mode: str = "auto",
    work_dir: Path | None = None,
) -> RenderedClip:
    """Subtitles, framing and render for one selected clip."""
    work_dir = work_dir or settings.temp_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    subtitle_path = work_dir / f"{output_path.stem}.ass"
    clip_audio_path = work_dir / f"{output_path.stem}.wav"
    start, end = clip.start_time, clip.end_time
    title: TitleOverlay | None = None
    try:
        clip_words = clip_words_from_transcript(words, start, end)
        if clip_words is None:
            extract_audio_segment(source_video, start, end, clip_audio_path)
            clip_words = transcribe_clip_audio(clip_audio_path, settings)

        plan: CropPlan | None = None
        if layout_mode == "auto" or layout_mode in CROP_LAYOUTS:
            try:
                plan = analyze_crop_plan(source_video, start, end)
            except Exception:
                logger.warning("Framing analysis failed; using a fallback layout.", exc_info=True)
        layout = choose_layout(layout_mode, plan)

        if layout in CROP_LAYOUTS:
            crop = plan.crop if plan is not None else _center_crop(source_video, None)
            x_expression = plan.x_expression(start, end) if plan is not None else None
        else:
            crop, x_expression = _PLACEHOLDER_CROP, None

        try:
            title = prepare_title_overlay(
                clip.viral_title,
                settings.hook_title_seconds,
                layout=layout,
                plan=plan,
                crop=crop,
                source_video=source_video,
                clip_start=start,
                clip_duration=end - start,
                work_dir=work_dir,
                name=output_path.stem,
                style=settings.hook_title_style,
            )
        except Exception:
            logger.warning("Title card failed; using the plain subtitle title.", exc_info=True)
        create_ass_subtitles(
            clip_words,
            subtitle_path,
            clip_duration=end - start,
            highlight_active_word=settings.caption_highlight,
            # The card replaces the old subtitle-style title; that one is only a fallback.
            hook_title=None if title else clip.viral_title,
            hook_seconds=settings.hook_title_seconds,
        )
        rendered = render_clip(
            source_video,
            subtitle_path,
            crop,
            start,
            end,
            output_path,
            layout_mode=layout,
            crop_x_expression=x_expression,
            title=title,
        )
        logger.info(
            "Rendered %s (%s, %d shot(s))",
            rendered.name,
            layout,
            len(plan.shots) if plan else 1,
        )
        return RenderedClip(
            path=rendered,
            clip=clip,
            layout_mode=layout,
            crop=crop,
            crop_plan=plan if layout in CROP_LAYOUTS else None,
        )
    finally:
        subtitle_path.unlink(missing_ok=True)
        clip_audio_path.unlink(missing_ok=True)
        if title is not None:
            title.image_path.unlink(missing_ok=True)


def pick_clips_with_agent(
    words: list[Word],
    video_duration: float,
    count: int,
    channel_name: str,
    video_title: str,
    peaks: list[tuple[float, float, float]] | None = None,
) -> tuple[list[ClipResult], str]:
    """Gemini/OpenAI picks (ranked) + outreach email, snapped like every other clip.

    The model returns count + 2 ranked candidates; finalize_candidates snaps
    them to sentences and keeps the best non-overlapping ``count``.
    """
    result = select_clips_and_draft_pitch(
        transcript_segments=transcript_segments(words),
        channel_name=channel_name,
        video_title=video_title,
        drive_link="",
        candidate_count=count + 2,
        replay_peaks=peaks,
    )
    proposals = [
        ClipResult(
            start_time=float(item["start_time"]),
            end_time=float(item["end_time"]),
            viral_title=str(item["hook_title"]),
            caption=str(item["reason"]),
            hashtags=[],
            virality_score=max(1, 100 - 10 * rank),
        )
        for rank, item in enumerate(result["clips"])
    ]
    clips = finalize_candidates(proposals, words, video_duration, count)
    return clips, str(result["outreach_email"])


def video_info_and_peaks(url: str) -> tuple[dict[str, Any], list[tuple[float, float, float]]]:
    """Video metadata plus "Most replayed" peaks; never raises."""
    try:
        info = fetch_video_info(url)
    except Exception as exc:
        logger.warning("Could not read video metadata for %s: %s", url, exc)
        return {}, []
    peaks = replay_peaks(info.get("heatmap"))  # type: ignore[arg-type]
    if peaks:
        logger.info("Using %d 'Most replayed' peak(s) for clip picking.", len(peaks))
    return info, peaks
