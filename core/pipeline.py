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
from core.agent_brain import judge_clips, select_clips_and_draft_pitch
from core.clipper import _center_crop
from core.downloader import extract_audio_segment, fetch_video_info
from core.face_tracker import CropBox, CropPlan, analyze_crop_plan
from core.hook_selector import ClipResult, choose_clips, clip_text, prepare_candidates, replay_peaks
from core.subtitle_engine import create_ass_subtitles
from core.outreach import build_outreach_email, build_subject
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

        full_frame: list[tuple[float, float]] = []
        if layout in CROP_LAYOUTS:
            crop = plan.crop if plan is not None else _center_crop(source_video, None)
            x_expression = plan.x_expression(start, end) if plan is not None else None
            if layout == "smart_crop" and plan is not None:
                full_frame = plan.full_frame_ranges(start, end)
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
            full_frame_ranges=full_frame or None,
        )
        logger.info(
            "Rendered %s (%s, %d shot(s), %d shown full-frame)",
            rendered.name,
            layout,
            len(plan.shots) if plan else 1,
            len(full_frame),
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
    shorts_status: str = "",
) -> tuple[list[ClipResult], str, str]:
    """Gemini/OpenAI picks (ranked) + outreach email and subject, snapped like
    every other clip. Returns (clips, email, subject).

    The model returns count + EXTRA_CANDIDATES ranked candidates. They're snapped
    to sentences, weak moments are fixed or dropped, a second AI pass scores
    each clip's exact words, and the best non-overlapping ``count`` are kept.
    The email opens on the best clip: its start time and what its point is.
    """
    result = select_clips_and_draft_pitch(
        transcript_segments=transcript_segments(words),
        channel_name=channel_name,
        video_title=video_title,
        drive_link="",
        candidate_count=count + EXTRA_CANDIDATES,
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
    candidates = rescore_with_judge(prepare_candidates(proposals, words, video_duration), words, count)
    clips = choose_clips(candidates, words, video_duration, count)
    short_topic = str(result.get("short_topic") or "")
    content_format = str(result.get("format") or "")
    if not clips:
        return clips, str(result["outreach_email"]), build_subject(short_topic, content_format)
    # "Your point at 12:34 about ...": the best clip's real start time and its point
    # (if the AI gave none, its opening words are quoted instead).
    best = max(clips, key=lambda clip: clip.virality_score)
    picked = _ai_pick_for(best, result["clips"])
    email = build_outreach_email(
        channel_name,
        video_title=video_title,
        video_topic=str(result.get("video_topic") or ""),
        clip_count=len(clips),
        opening_line=opening_words(words, best.start_time),
        opening_at=best.start_time,
        key_point=str(picked.get("point") or ""),
        shorts_status=shorts_status,
        greeting_name=str(result.get("host_first_name") or ""),
        usual_take=str(picked.get("usual_take") or ""),
        content_format=content_format,
    )
    return clips, email, build_subject(short_topic, content_format, len(clips))


def _ai_pick_for(clip: ClipResult, picks: list[dict[str, Any]]) -> dict[str, Any]:
    """The AI's entry for a final clip: same hook title, else the nearest start
    (snapping to sentences can move a start by a few seconds)."""
    for pick in picks:
        if str(pick.get("hook_title")) == clip.viral_title:
            return pick
    nearby = [pick for pick in picks if abs(float(pick.get("start_time", -1e9)) - clip.start_time) <= 20]
    return min(nearby, key=lambda pick: abs(float(pick["start_time"]) - clip.start_time), default={})


EXTRA_CANDIDATES = 5  # spare picks, so dropping weak moments rarely leaves a gap
JUDGE_WEIGHT = 0.7  # the judge reads the final clip text; the first pass only ranked moments


def rescore_with_judge(candidates: list[ClipResult], words: list[Word], count: int) -> list[ClipResult]:
    """Blend the judge's score into each candidate's rank score (best effort)."""
    if len(candidates) <= count:
        return candidates  # nothing to choose between
    scores = judge_clips([clip_text(words, clip.start_time, clip.end_time) for clip in candidates])
    if scores is None:
        return candidates
    rescored = [
        clip.model_copy(
            update={"virality_score": round(JUDGE_WEIGHT * score + (1 - JUDGE_WEIGHT) * clip.virality_score)}
        )
        for clip, score in zip(candidates, scores)
    ]
    logger.info("Clip judge scores: %s", ", ".join(str(score) for score in scores))
    return sorted(rescored, key=lambda clip: clip.virality_score, reverse=True)


def opening_words(words: list[Word], start: float, seconds: float = 15.0) -> str:
    """The words spoken in the first seconds after ``start`` (whole words only)."""
    return " ".join(
        word.text.strip()
        for word in sorted(words, key=lambda w: w.start)
        if start - 0.3 <= word.start < start + seconds
    )


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
