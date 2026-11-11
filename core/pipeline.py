"""One clip-picking and rendering path shared by main.py, app.py and the worker.

Before this module the same steps were copied three times, so a fix in one
copy didn't reach the others.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Settings
from core.agent_brain import judge_clips, select_clips_and_draft_pitch
from core.clipper import _center_crop
from core.downloader import extract_audio_segment, fetch_video_info
from core import explainer
from core.edl import ClipEdit
from core.face_tracker import CropBox, CropPlan, _probe_display_size, analyze_crop_plan
from core.graphics_director import VideoInfo
from core import audio_polish, climax_hook, clip_length, heatmap_scorer
from core.climax_hook import Teaser
from core.hook_selector import ClipResult, choose_clips, clip_text, prepare_candidates, replay_peaks
from core.subtitle_engine import create_ass_subtitles
from core.outreach import (
    build_outreach_email,
    build_subject,
    clean_format,
    clean_key_point,
    clean_short_topic,
    clean_topic,
)
from core.service_pause import ServicePause
from core.title_card import TitleOverlay, prepare_title_overlay, title_above_captions
from core.transcriber import (
    Word,
    clip_words_from_transcript,
    transcribe_clip_audio,
    transcript_segments,
)
from core.video_renderer import CROP_LAYOUTS, LAYOUT_MODES, pull_in_enabled, render_clip
from core.webinar import WebinarLayout, plan_webinar


logger = logging.getLogger(__name__)

# "auto" uses a full-screen face crop only when it will look good:
MIN_SMART_CROP_WIDTH = 540  # 9:16 crop width in source pixels (needs ~1080p+)
AUTO_MIN_FACE_COVERAGE = 0.6  # share of the clip where a face is found
AUTO_MAX_GROUP_COVERAGE = 0.35  # share in wide shots with 2+ people that can't be split
AUTO_MAX_SHOTS = 40  # very fast cutting looks jumpy with a moving crop
REEL_MIN_FACE = 0.15  # Round 43: camera shots in at least this share of a slide talk -> full-screen reel
_PLACEHOLDER_CROP = CropBox(x=0, y=0, width=2, height=2)


class _NoTitle(Exception):
    """No title card on this render: the creator pill is off in the executive look (Round 48,
    core.executive.title_card_on) and so is the executive topic header (EXEC_TOPIC_HEADER=0),
    or the topic header already showed over the cold open (Round 49)."""


@dataclass(frozen=True)
class RenderedClip:
    path: Path
    clip: ClipResult
    layout_mode: str
    crop: CropBox
    crop_plan: CropPlan | None
    webinar: WebinarLayout | None = None  # layout "webinar": reused when the clip is trimmed
    pin_title: bool = False  # the title card stayed on for the whole clip
    title_fade_in: bool = True  # False: the card was on screen from frame 0 (worker clips)
    graphics: ClipEdit | None = None  # the graphics rendered into the clip (Round 28)
    graphics_notes: tuple[str, ...] = ()  # what the graphics director added or left out, and why
    # Round 40: the climax teaser the clip opens on (None: none). Every time of the clip
    # after it (captions, crop, cards: all in ``graphics``' source seconds) is +teaser.offset
    # in the file: climax_hook.to_output / shift_edit give them on the file's timeline.
    teaser: Teaser | None = None
    palette: str = ""  # Round 40: the editorial palette its graphics wear (core.palettes)
    # Round 45 (core.executive / core.linkedin_post): the executive ID shown, and the
    # ready-to-post LinkedIn caption written next to the clip.
    lower_third: Any = None
    linkedin_post: Path | None = None
    # Round 46 (core.midclip): the mid-clip cuts (None: none). The clip was rendered from a
    # trimmed copy; to_trimmed() maps an original clip second onto the finished timeline.
    trim: Any = None
    captions_srt: Path | None = None  # the clip's captions as SRT (LinkedIn caption upload)
    closeups: Any = None  # Round 48: core.speaker_director.Direction (the close-ups cut in)
    # Round 57: the clean (textless) copy and its files (core.textless.CleanCopy; None: off or failed),
    # and the project file (core.project_state), next to the clip as <clip>.project.json.
    clean: Any = None
    project_file: Path | None = None

    def output_time(self, source_seconds: float) -> float:
        """Where a source second of the clip lands in the finished file."""
        if self.trim is not None:  # Round 46: the cut timeline, then the teaser in front of it
            return climax_hook.to_output(self.trim.to_trimmed(source_seconds - self.clip.start_time), 0.0, self.teaser)
        return climax_hook.to_output(source_seconds, self.clip.start_time, self.teaser)


@dataclass(frozen=True)
class EditProfile:
    """Round 32: the editing choices a clip is made with, the same for 🎬 Make clips and the
    autonomous worker: framing (layout, the explainer layout for slide videos), the title
    card, the length cap (none by default since Round 55), and the graphics director (1-2 Bento visuals and every speaker's
    name card, settings.auto_graphics). Captions, subtitle styling, speaker ID and the
    graphics engine all run inside render_selected_clip, which both call with these."""

    layout_mode: str = "auto"
    allow_explainer: bool = True  # "auto" may pick the explainer layout for a faceless slide video
    title_fade_in: bool = False  # False: the title card is on screen from the first frame
    pin_title: bool = False  # True: the title card stays for the whole clip
    # Round 55 (core.clip_length): None, clips run 35-180 s and end when the idea ends.
    # CLIP_LENGTH=legacy: 59.0 (YouTube blocks claimed Shorts over a minute).
    max_seconds: float | None = clip_length.CAP_SECONDS
    # Round 40: open on the climax teaser (core.climax_hook). None: settings.enable_climax_hook.
    climax_hook: bool | None = None

    def render_kwargs(self) -> dict[str, Any]:
        """The arguments render_selected_clip takes from this profile."""
        return {"layout_mode": self.layout_mode, "pin_title": self.pin_title, "title_fade_in": self.title_fade_in,
                "allow_explainer": self.allow_explainer and self.layout_mode == "auto", "climax_hook_on": self.climax_hook}


STANDARD_EDIT = EditProfile()  # Make clips' defaults, and what the worker always uses


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
        # Two-person shots become the two-speaker split view, so only wide shots
        # that can't be split (3+ people, faces too small) count against the crop.
        and plan.unsplit_group_coverage <= AUTO_MAX_GROUP_COVERAGE
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
    pin_title: bool = False,
    webinar_corner: str = "auto",
    webinar_options: dict[str, str] | None = None,
    webinar_layout: WebinarLayout | None = None,
    title_fade_in: bool = True,
    allow_explainer: bool = False,
    graphics: VideoInfo | None = None,
    climax_hook_on: bool | None = None,
) -> RenderedClip:
    """Subtitles, framing and render for one selected clip.

    ``pin_title``: the title card stays on for the whole clip (Make clips tab).
    Layout "webinar" only: ``webinar_corner`` is where the speaker's webcam is ("auto"
    finds it); ``webinar_options`` the slide shape and divider (core.webinar.plan_webinar's
    ``slides``, ``divider``, ``accent``); ``webinar_layout`` a layout found before (a
    trim reuses the first render's). ``title_fade_in``: False shows the title card fully
    from the first frame (the worker's clips). ``allow_explainer`` (Make clips): "auto"
    may choose the explainer layout for a faceless slide video (core.explainer).
    ``graphics`` (the video's title, channel and description): the graphics director picks
    and checks this clip's graphics and they're rendered in (settings.auto_graphics);
    RenderedClip.graphics holds what was added, RenderedClip.graphics_notes why.
    ``climax_hook_on`` (Round 40; None: settings.enable_climax_hook): a clip of 28 s+ opens
    on its climax teaser when one is found (core.climax_hook); RenderedClip.teaser."""
    work_dir = work_dir or settings.temp_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    subtitle_path = work_dir / f"{output_path.stem}.ass"
    clip_audio_path = work_dir / f"{output_path.stem}.wav"
    start, end = clip.start_time, clip.end_time
    try:
        from core import camera_path, opening

        if opening.enabled():  # Round 42: word one on frame one (no "um, so..." or silence first)
            tightened, opening_note = opening.tighten(start, end, words)
            if opening_note:
                clip = clip.model_copy(update={"start_time": tightened})
                start = tightened
                logger.info("Opening, %s: %s", output_path.name, opening_note)
        clip_words = clip_words_from_transcript(words, start, end)
        if clip_words is None:
            extract_audio_segment(source_video, start, end, clip_audio_path)
            clip_words = transcribe_clip_audio(clip_audio_path, settings)

        # Round 46 (core.midclip): cut mid-clip dead air and fillers into a trimmed copy; the rest
        # runs on that copy (start 0), so captions, camera, framing and teaser all follow the cuts.
        from core import midclip

        identity_source, identity_words = source_video, words  # the lower-third's face check: the whole video
        project_source, project_start = source_video, start  # Round 57: the project file's source clock
        trim_plan = None
        trimmed_path = work_dir / f"{output_path.stem}_trimmed.mp4"
        if midclip.enabled(settings) and clip_words:
            try:
                candidate = midclip.plan(clip_words, end - start, midclip.detect_silences(source_video, start, end))
                if candidate.cuts:
                    midclip.render_trimmed(source_video, start, candidate, trimmed_path)
                    clip_words = candidate.remap_words(clip_words)
                    source_video, words, start, end = trimmed_path, clip_words, 0.0, candidate.trimmed_duration
                    trim_plan = candidate
                    logger.info("Trim, %s: %s", output_path.name, midclip.describe(candidate))
            except Exception:  # noqa: BLE001 - an untrimmed clip is still a clip
                logger.warning("Mid-clip trim failed for %s; rendering it uncut.", output_path.name, exc_info=True)
                trim_plan = None

        plan: CropPlan | None = None
        if layout_mode == "auto" or layout_mode in CROP_LAYOUTS:
            try:
                plan = analyze_crop_plan(source_video, start, end)
            except Exception:
                logger.warning("Framing analysis failed; using a fallback layout.", exc_info=True)
        layout = choose_layout(layout_mode, plan)
        from core import reel_layout

        if layout_mode == "auto" and layout == "blurred_background" and reel_layout.enabled() and reel_fits(plan):
            layout = "smart_crop"  # Round 43: camera shots full-screen, slides composed for 9:16 (never a strip)
        if layout_mode == "auto" and allow_explainer and layout == "blurred_background":
            try:
                if explainer.explainer_fit(source_video, start, end):
                    layout = "explainer"
            except Exception:
                logger.warning("Explainer check failed; using the full frame.", exc_info=True)
        webinar = None
        if layout == "webinar":
            webinar = webinar_layout or plan_webinar(source_video, start, end, webinar_corner, **(webinar_options or {}))

        full_frame: list[tuple[float, float]] = []
        split = None
        holds = (0.0, 0.0)
        if layout in CROP_LAYOUTS:
            crop = plan.crop if plan is not None else _center_crop(source_video, None)
            x_expression = plan.x_expression(start, end) if plan is not None else None
            if plan is not None:
                holds = plan.holds(start, end)
            if layout == "smart_crop" and plan is not None:
                full_frame = plan.full_frame_ranges(start, end)
                split = plan.split_view(start, end)
        else:
            crop, x_expression = _PLACEHOLDER_CROP, None  # layouts that don't crop (the renderer ignores it)
        # Round 48: this `else` used to sit under the reel's `if` below (Round 43), so every
        # face-tracked clip without a full-frame span was cropped to the 2x2 placeholder: a
        # flat-colour screen with captions (the Apollo GraphQL pitch). test_round48 pins it.
        reel = None
        if layout == "smart_crop" and full_frame and reel_layout.enabled():
            reel = plan_reel(source_video, start, end, full_frame)
        closeups = None  # Round 48: cut from the two-shot to the speaker's close-up (core.speaker_director)
        if layout == "smart_crop" and split is not None and plan is not None and clip_words:
            from core import speaker_director

            if speaker_director.enabled(settings):
                try:
                    closeups = speaker_director.direct(source_video, start, end, plan, split, clip_words)
                    if closeups is not None:
                        logger.info("Director, %s: %s", output_path.name, closeups.describe())
                    if closeups is not None and not closeups.layers:
                        closeups = None
                except Exception:  # noqa: BLE001 - the two-shot is still a clip
                    logger.warning("Speaker close-ups failed for %s; staying on the two-shot.", output_path.name,
                                   exc_info=True)
                    closeups = None

        # The hook's subtle pull-in zoom (smart crop, a plain shot of one person at the start).
        pull_until = (plan.pull_in_until(start, end)
                      if layout == "smart_crop" and plan is not None and pull_in_enabled() else 0.0)
        flowing = camera_path.enabled() and layout not in {"webinar", "explainer"}
        if flowing:
            pull_until = 0.0  # Round 42: the hook's push is the first move of one smooth camera path
        loudness: list[float] = []
        try:
            from core.camera_energy import word_loudness

            loudness = word_loudness(source_video, start, end, clip_words) if clip_words else []
        except Exception:  # noqa: BLE001
            loudness = []
        direction = None
        # The graphics work in source seconds; the captions in clip seconds.
        source_words = [Word(text=w.text, start=w.start + start, end=w.end + start) for w in clip_words]
        teaser = find_teaser(source_video, source_words, start, end,
                             # an older or test settings object may not have the Round 40 field: on (the default)
                             getattr(settings, "enable_climax_hook", True) if climax_hook_on is None else climax_hook_on,
                             loudness=loudness)
        palette_note = ""
        from core import executive

        overlays = executive.overlay_mode(settings)
        lower_third = None
        if graphics is not None and overlays == "executive_minimal":
            # Round 45: B2B clean mode. No cards, tear-outs or scraped visuals; at most the ID.
            try:
                lower_third, note = executive.plan_lower_third(
                    source_video, plan if layout == "smart_crop" else None, start, end, graphics,
                    source_words, identity_words, split, identity_source=identity_source)
            except Exception as exc:  # noqa: BLE001 - the clip renders without the ID
                lower_third, note = None, f"No lower-third: {exc}"
            logger.info("Executive mode, %s: %s", output_path.name, note)
        if graphics is not None and settings.auto_graphics and overlays == "full":
            from core import palettes
            from core.edl import load_brand_kit
            from core.graphics_director import direct

            brand, palette_note = palettes.for_clip(load_brand_kit(), source_words, graphics.title, source_video, start, end)
            direction = direct(
                source_words, start, end, info=graphics, hook_seconds=settings.hook_title_seconds,
                plan=plan if layout == "smart_crop" else None, brand=brand, source=source_video,
                video_words=words,
            )
            direction.notes.append(palette_note)
            for note in direction.notes:
                logger.info("Graphics, %s: %s", output_path.name, note)
        with_graphics = direction is not None and bool(direction.edit.cards)
        moves: list[Any] = []
        emphasis: list[tuple[float, float]] = []
        steady = camera_path.steady(settings)
        if steady:  # Round 48: B2B: a still camera; at most one opening drift on a single speaker
            pull_until = 0.0
            if layout in {"smart_crop", "stacked"} and teaser is None:
                still = [tuple(s) for s in (split.spans if split else [])] + [tuple(s) for s in full_frame]
                moves = camera_path.opening_drift(end - start, still)
            if moves:
                logger.info("Camera, %s: steady, one opening drift 0.0-%.1f s (1.00x -> %.2fx), then held",
                            output_path.name, camera_path.OPENING_SECONDS, 1 + camera_path.OPENING_DRIFT)
            else:
                logger.info("Camera, %s: steady (no moves)", output_path.name)
        elif flowing:
            moves = camera_moves(clip_words, loudness, start, end, direction, teaser, plan, split, full_frame)
            if moves:
                logger.info("Camera, %s: %s", output_path.name,
                            ", ".join(f"{m.kind} {m.start:.1f}-{m.end:.1f} s (1.00x -> {1 + m.base + m.amount + m.creep:.3f}x)"
                                      for m in moves))
        else:
            emphasis = camera_punches(source_video, clip_words, start, end, direction, pull_until)
            if emphasis:
                logger.info("Camera energy, %s: punch-ins at %s", output_path.name,
                            ", ".join(f"{a:.1f}-{b:.1f} s" for a, b in emphasis))
        # With graphics the title shows for the opening only, so it never sits under one.
        pinned = pin_title and not with_graphics

        # Round 49: the creator pill (VISUAL_OVERLAYS=full / EXEC_TITLE_CARD=1) or the executive
        # topic header (the clip's topic for its first 3.5 s, then a fade). After a cold open
        # the header showed over the teaser (add_teaser), so the body doesn't repeat it.
        title_on, title_style, title_seconds, topic_header = executive.title_plan(settings)
        pill_on = title_on and not topic_header
        body_title = title_on and not (topic_header and teaser is not None)

        def render_version(caption_words: list[Word], destination: Path) -> Path:
            title: TitleOverlay | None = None
            try:
                try:
                    if not body_title:
                        raise _NoTitle
                    title = prepare_title_overlay(
                        clip.viral_title,
                        title_seconds,
                        layout=layout,
                        plan=plan,
                        crop=crop,
                        source_video=source_video,
                        clip_start=start,
                        clip_duration=end - start,
                        work_dir=work_dir,
                        name=destination.stem,
                        style=title_style,
                        pinned=pinned,
                        webinar=webinar,
                        # Round 40: after a teaser the title is already up (it showed over the teaser).
                        # Round 49: the topic header is up from frame 0 (it's on the cover frame at 0.5 s).
                        fade_in=title_fade_in and teaser is None and not topic_header,
                    )
                except _NoTitle:
                    title = None
                except Exception:
                    logger.warning("Title card failed; using the plain subtitle title.", exc_info=True)
                seams = list(split.spans) if split else None
                if seams and closeups is not None:
                    from core.speaker_director import subtract

                    seams = subtract(seams, closeups.windows()) or None  # captions leave the seam on close-ups
                create_ass_subtitles(
                    caption_words,
                    subtitle_path,
                    clip_duration=end - start,
                    highlight_active_word=settings.caption_highlight,
                    # The card replaces the old subtitle-style title; that one is only a fallback
                    # (the creator look's: the executive header has no boxy subtitle fallback).
                    hook_title=None if (title or not pill_on) else clip.viral_title,
                    hook_seconds=end - start if pinned else settings.hook_title_seconds,
                    seam_spans=seams,
                    seam_title=_seam_title(title),
                    caption_top=webinar.caption_top if webinar else _explainer_caption_top(layout, source_video),
                    lower_third=lower_third,
                )
                return render_clip(
                    source_video,
                    subtitle_path,
                    crop,
                    start,
                    end,
                    destination,
                    layout_mode=layout,
                    crop_x_expression=x_expression,
                    title=title,
                    full_frame_ranges=full_frame or None,
                    split_view=split,
                    webinar=webinar,
                    holds=holds,
                    pull_in_until=pull_until,
                    emphasis=emphasis,
                    camera=moves,
                    reel=reel,
                    closeups=closeups.layers if closeups is not None else None,
                )
            finally:
                if title is not None:
                    title.image_path.unlink(missing_ok=True)

        rendered: Path | None = None
        applied = None
        if with_graphics:
            from core.dynamic_layout import render_with_cards

            graphics_dir = work_dir / f"{output_path.stem}_graphics"
            try:
                graphics_dir.mkdir(parents=True, exist_ok=True)
                rendered, _checks = render_with_cards(
                    source_video, direction.edit, source_words, output_path, graphics_dir,
                    lambda kept: render_version(
                        [Word(text=w.text, start=w.start - start, end=w.end - start) for w in kept],
                        graphics_dir / f"{output_path.stem}_base.mp4",
                    ),
                    hook_seconds=settings.hook_title_seconds,
                    seams=list(split.spans) if split else None,
                    crop_at=((lambda t: plan.shown_rect_at(t, start, end, pull_until))
                             if layout == "smart_crop" and plan is not None else None),
                    layout=layout,
                )
                applied = direction.edit if rendered is not None else None
            except Exception as exc:  # noqa: BLE001 - a graphics failure never costs the clip
                logger.warning("Graphics failed for %s; rendering without them.", output_path.name, exc_info=True)
                direction.notes.append(f"Graphics left out: {exc}")
                rendered = None
            finally:
                shutil.rmtree(graphics_dir, ignore_errors=True)
        if rendered is None:
            pinned = pin_title
            rendered = render_version(clip_words, output_path)
        from core import frame_guard

        blank = (frame_guard.blank_spans(rendered, source_video, start, end, skip=list(full_frame))
                 if layout in {"smart_crop", "stacked"} else [])
        if blank:
            # Round 48: never ship a flat screen where the source has a picture. Render the whole
            # frame instead (no crop can miss), and say so loudly.
            logger.error("Blank frames in %s (%s) where the source has a picture; re-rendering it full-frame.",
                         rendered.name, ", ".join(f"{a:.1f}-{b:.1f} s" for a, b in blank))
            layout, crop, x_expression = "blurred_background", _PLACEHOLDER_CROP, None
            full_frame, split, closeups, reel, holds = [], None, None, None, (0.0, 0.0)
            moves, emphasis, pull_until, applied = [], [], 0.0, None
            rendered = render_version(clip_words, output_path)

        if teaser is not None:
            try:
                rendered = add_teaser(
                    source_video, rendered, teaser, source_words, clip.viral_title, settings, work_dir,
                    layout=layout, plan=plan, crop=crop, webinar=webinar,
                    caption_top=webinar.caption_top if webinar else _explainer_caption_top(layout, source_video),
                )
                logger.info("Climax teaser on %s: %.2f s (%s)", rendered.name, teaser.offset, teaser.reason)
            except Exception:  # noqa: BLE001 - a teaser failure never costs the clip
                logger.warning("Climax teaser failed for %s; the clip opens as before.", output_path.name, exc_info=True)
                teaser = None
        captions_srt = None
        try:  # Round 46: the captions as SRT, on the finished file's timeline (teaser first)
            from core import srt_export

            captions_srt = srt_export.write_for_clip(rendered, clip_words, teaser, source_words)
        except Exception:  # noqa: BLE001
            logger.warning("SRT captions failed for %s.", rendered.name, exc_info=True)
        clean = None
        from core import textless

        if textless.enabled(settings):  # Round 57: the clean (textless) copy, same decisions, frame for frame
            try:
                with textless.textless_pass() as clean_pass:
                    clean_video = render_version(clip_words, work_dir / f"{output_path.stem}_clean_render.mp4")
                    if teaser is not None:
                        clean_video = add_teaser(
                            source_video, clean_video, teaser, source_words, clip.viral_title, settings, work_dir,
                            layout=layout, plan=plan, crop=crop, webinar=webinar,
                            caption_top=webinar.caption_top if webinar else _explainer_caption_top(layout, source_video),
                        )
                    clean = textless.finish(clean_video, rendered, captions_srt, clean_pass)
                logger.info("Clean copy, %s: %s (%d frames, %s)", rendered.name, clean.video.name, clean.frames,
                            "matches the dirty copy" if clean.matches else "FRAME COUNT DIFFERS")
            except Exception:  # noqa: BLE001 - a clean-copy failure never costs the clip
                logger.warning("Clean copy failed for %s; the clip itself is fine.", rendered.name, exc_info=True)
                clean = None
        project_file = None
        try:  # Round 57: the project file (core.project_state), every time in source seconds
            from core import project_state

            smap = project_state.SourceMap(project_start, trim_plan.keep if trim_plan is not None else None)
            info = project_state.probe(rendered)
            frames = textless.frame_count(rendered)
            fps = info.get("fps") or 30.0
            files = {"dirty": rendered.name, **({"srt": captions_srt.name} if captions_srt else {}),
                     **(clean.files() if clean is not None else {})}
            project = project_state.build(
                clip_id=rendered.stem,
                source={"path": str(project_source), **project_state.probe(project_source)},
                output={**info, "frames": frames, "duration": round(frames / fps, 6) if frames > 0 else None},
                smap=smap, body=(start, end), teaser=teaser, layout=layout, crop=crop,
                full_frame=full_frame, split=list(split.spans) if split else [], camera=moves or [],
                x_expression=x_expression, words=clip_words,
                title={"text": clip.viral_title, "seconds": title_seconds, "shown": bool(body_title)},
                lower_third=lower_third, cards=applied.cards if applied is not None else [],
                deliverables=files, notes=direction.notes if direction else [],
            )
            if emphasis:
                project["framing"]["punch_ins"] = [[smap.point(start + a), smap.point(start + b)] for a, b in emphasis]
            if clean is not None and not clean.matches:
                project["notes"].append(f"clean copy has {clean.frames} frames, dirty {clean.dirty_frames}")
            problems = project_state.validate(project)
            if problems:
                logger.warning("Project file for %s: %s", rendered.name, "; ".join(problems))
                project["notes"] += [f"check: {p}" for p in problems]
            project_file = project_state.save(project, project_state.path_for(rendered))
        except Exception:  # noqa: BLE001 - a missing project file never costs the clip
            logger.warning("Project file failed for %s.", rendered.name, exc_info=True)
        post_file = None
        if graphics is not None and getattr(settings, "linkedin_posts", True):
            try:
                from core import linkedin_post

                post_file = linkedin_post.write_for_clip(
                    rendered, source_words, title=clip.viral_title, video_title=graphics.title,
                    theme=str(getattr(graphics, "theme", "") or ""),
                    speaker=(f"{lower_third.name}, {lower_third.role}".strip(", ") if lower_third else graphics.channel))
            except Exception:  # noqa: BLE001 - a missing caption never costs the clip
                logger.warning("LinkedIn post failed for %s.", rendered.name, exc_info=True)
            try:  # Round 50: the Shorts & Reels caption (YouTube Shorts, Instagram Reels, TikTok)
                from core import social_post

                if social_post.enabled():
                    social_post.write_for_clip(
                        rendered, source_words, title=clip.viral_title, video_title=graphics.title,
                        show=graphics.channel, theme=str(getattr(graphics, "theme", "") or ""),
                        kind=str(getattr(graphics, "content_format", "") or "episode"))
            except Exception:  # noqa: BLE001 - a missing caption never costs the clip
                logger.warning("Shorts & Reels caption failed for %s.", rendered.name, exc_info=True)
        logger.info(
            "Rendered %s (%s, %d shot(s), %d shown full-frame, %d as two-speaker split, %d graphic(s))",
            rendered.name,
            layout,
            len(plan.shots) if plan else 1,
            len(full_frame),
            len(split.spans) if split else 0,
            len(applied.cards) if applied else 0,
        )
        return RenderedClip(
            path=rendered,
            clip=clip,
            layout_mode=layout,
            crop=crop,
            crop_plan=plan if layout in CROP_LAYOUTS else None,
            webinar=webinar,
            pin_title=pinned,
            title_fade_in=title_fade_in,
            graphics=applied,
            graphics_notes=tuple(direction.notes) if direction else (),
            teaser=teaser,
            palette=(applied.brand.palette.get("name", "") if applied is not None else ""),
            lower_third=lower_third,
            linkedin_post=post_file,
            trim=trim_plan,
            captions_srt=captions_srt,
            closeups=closeups,
            clean=clean,
            project_file=project_file,
        )
    finally:
        subtitle_path.unlink(missing_ok=True)
        if "trimmed_path" in locals():
            trimmed_path.unlink(missing_ok=True)
        clip_audio_path.unlink(missing_ok=True)


def camera_punches(source_video: Path, clip_words: list[Word], start: float, end: float, direction: Any,
                   pull_until: float) -> list[tuple[float, float]]:
    """Round 41 (core.camera_energy): the clip's emphasis punch-ins (clip seconds), clear of
    the hook's pull-in, any split-screen card (and its moves) and a newspaper tear-out.
    None with SIGNATURE_EDIT=0."""
    from core import camera_energy, visual_proof
    from core.edl import MODES

    if not visual_proof.enabled() or not clip_words:
        return []
    try:
        exclude = [(0.0, pull_until + 0.3)] if pull_until > 0 else []
        thesis_at = None
        if direction is not None:
            for card in direction.edit.cards:
                if MODES.get(card.template) == "split" or card.template == "newspaper_tearout":
                    exclude.append((card.start - start - 0.8, card.end - start + 0.8))
        source_words = [Word(text=w.text, start=w.start + start, end=w.end + start) for w in clip_words]
        found = visual_proof.thesis(source_words, start + 3.0, end - 2.0)
        if found:
            thesis_at = float(found[0][0].start) - start
        loudness = camera_energy.word_loudness(source_video, start, end, clip_words)
        return camera_energy.emphasis_spans(clip_words, loudness, end - start, exclude, thesis_at)
    except Exception:  # noqa: BLE001 - a static camera is still a clip
        logger.warning("Camera energy failed; no punch-ins.", exc_info=True)
        return []


def reel_fits(plan: CropPlan | None) -> bool:
    """Round 43: a clip auto would show as a horizontal strip, but whose camera shots can be
    cropped full-screen (real faces in at least REEL_MIN_FACE of it, sharp enough, no
    unsplittable group shots): its slides get composed for 9:16 instead."""
    return (plan is not None and plan.crop_width >= MIN_SMART_CROP_WIDTH and plan.face_coverage >= REEL_MIN_FACE
            and plan.unsplit_group_coverage <= AUTO_MAX_GROUP_COVERAGE and len(plan.shots) <= AUTO_MAX_SHOTS)


def plan_reel(source_video: Path, start: float, end: float, full_frame: list[tuple[float, float]]) -> Any:
    """Round 43 (core.reel_layout): how the clip's slide spans are composed for 9:16."""
    from core import reel_layout
    from core.webinar import plan_webinar

    try:
        frame = _probe_display_size(source_video)
        camera = None
        try:
            found = plan_webinar(source_video, start, end, "auto")
            if not found.found.startswith("no face"):
                camera = found.camera
        except Exception:  # noqa: BLE001 - no webcam window: text cards only
            camera = None
        reel = reel_layout.analyze(source_video, start, full_frame, frame, camera)
        if reel is not None:
            logger.info("Reel layout: %d slide+webcam span(s), %d text span(s), %d kept as the whole frame",
                        len(reel.slide_cam), len(reel.text_spans), len(reel.kept))
        return reel
    except Exception:  # noqa: BLE001 - the strip is still a clip
        logger.warning("Reel layout failed; slides shown as the whole frame.", exc_info=True)
        return None


def find_teaser(source_video: Path, source_words: list[Word], start: float, end: float, on: bool,
                loudness: list[float] | None = None) -> Teaser | None:
    """Round 40: the clip's climax teaser (core.climax_hook.find_climax), at the source's own
    frame rate; Round 42: else, when the opening doesn't already pull, its cold open (the
    most gripping complete line, core.opening.cold_open). None when it's off (``on``, or
    ENABLE_CLIMAX_HOOK=0) or there's none."""
    from core import opening

    if not on or not climax_hook.enabled():
        return None
    from core.video_renderer import _frame_rate

    try:
        rate = _frame_rate(source_video)
        found = (climax_hook.find_climax(source_words, start, end, rate=rate)
                 if end - start >= climax_hook.MIN_CLIP_SECONDS else None)
        if found is None and opening.enabled():
            found = opening.cold_open(source_words, start, end, rate=rate, loudness=loudness)
        return found
    except Exception:  # noqa: BLE001
        logger.warning("Climax teaser search failed; no teaser.", exc_info=True)
        return None


def camera_moves(clip_words: list[Word], loudness: list[float], start: float, end: float, direction: Any,
                 teaser: Teaser | None, plan: CropPlan | None, split: Any,
                 still: list[tuple[float, float]] | None = None) -> list[Any]:
    """Round 42 (core.camera_path): the clip's flowing camera moves (clip seconds). Blocked:
    split-screen cards (and their moves), newspaper tear-outs, two-person split views.
    The payoff push lands where the cold open's line (or its answer) is finally said."""
    from core import camera_path, visual_proof
    from core.edl import MODES

    try:
        blocked: list[tuple[float, float]] = [tuple(s) for s in (split.spans if split else [])]
        blocked += [tuple(s) for s in (still or [])]  # slides and wide shots: the camera holds still there
        thesis_at = None
        if direction is not None:
            for card in direction.edit.cards:
                if MODES.get(card.template) == "split" or card.template == "newspaper_tearout":
                    blocked.append((card.start - start - 0.8, card.end - start + 0.8))
        tearout = direction is not None and any(c.template == "newspaper_tearout" for c in direction.edit.cards)
        if not tearout:
            source_words = [Word(text=w.text, start=w.start + start, end=w.end + start) for w in clip_words]
            found = visual_proof.thesis(source_words, start + 3.0, end - 2.0)
            thesis_at = float(found[1][0].start) - start if found else None
        payoff_at = None
        if teaser is not None:  # the open loop closes: the cliffhanger's answer, or the cold open's line in place
            payoff_at = (teaser.cut - start) if teaser.reason.startswith("\"") else (teaser.start - start + 0.2)
        cuts = [c - start for c in (plan.cuts if plan is not None else ())]
        return camera_path.plan(clip_words, loudness, end - start, cuts=cuts, blocked=blocked,
                                payoff_at=payoff_at, thesis_at=thesis_at)
    except Exception:  # noqa: BLE001 - a still camera is still a clip
        logger.warning("Camera path failed; a still camera.", exc_info=True)
        return []


def teaser_camera(seconds: float, layout: str) -> dict[str, Any]:
    """Round 42: the cold open drifts in (1.04x -> 1.10x, core.camera_path.teaser_move);
    Round 40's steady 1.05x punch-in without the flowing camera (or on slides).
    Round 48: in the B2B modes only a framed shot drifts; a letterbox or a slide holds still."""
    from core import camera_path

    if camera_path.steady():
        return {"camera": [camera_path.teaser_move(seconds)]} if layout in {"smart_crop", "stacked"} else {}

    if camera_path.enabled() and layout not in {"webinar", "explainer"}:
        return {"camera": [camera_path.teaser_move(seconds)]}
    return {"punch_in": climax_hook.PUNCH_IN}


def add_teaser(
    source_video: Path,
    body: Path,
    teaser: Teaser,
    source_words: list[Word],
    title_text: str | None,
    settings: Settings,
    work_dir: Path,
    *,
    layout: str,
    plan: CropPlan | None,
    crop: CropBox,
    webinar: WebinarLayout | None = None,
    caption_top: int | None = None,
) -> Path:
    """Round 40: render the teaser (the clip's own framing at a 1.05x punch-in, its captions,
    the title card banner up for all of it) and stitch it in front of ``body`` (the
    finished clip, replaced in place): see core.climax_hook. Make clips, the worker and
    the app's re-renders all call this."""
    work_dir.mkdir(parents=True, exist_ok=True)
    name = f"{body.stem}_teaser"
    seconds = teaser.offset
    t0, t1 = teaser.start, teaser.start + seconds + 1.0 / teaser.rate  # one spare frame: the stitch trims to exact
    ass = work_dir / f"{name}.ass"
    video = work_dir / f"{name}.mp4"
    title: TitleOverlay | None = None
    try:
        x_expression, full_frame, split = None, None, None
        if layout in CROP_LAYOUTS and plan is not None:
            x_expression = plan.x_expression(t0, t1)
            if layout == "smart_crop":
                full_frame = plan.full_frame_ranges(t0, t1) or None
                split = plan.split_view(t0, t1)
        from core import executive

        title_on, title_style, _seconds, topic_header = executive.title_plan(settings)
        try:
            if title_on:
                # Round 49: the executive header shows over the cold open from frame 0 and fades
                # out by 3.5 s or at the cut to the body, whichever comes first.
                title = prepare_title_overlay(
                    title_text, min(seconds, executive.TOPIC_SECONDS) if topic_header else seconds,
                    layout=layout, plan=plan, crop=crop, source_video=source_video,
                    clip_start=t0, clip_duration=t1 - t0, work_dir=work_dir, name=name,
                    style=title_style, pinned=not topic_header, webinar=webinar, fade_in=False,
                )
        except Exception:  # noqa: BLE001
            logger.warning("Teaser title card failed; the teaser shows the plain title.", exc_info=True)
        said = [Word(text=w.text, start=max(0.0, float(w.start) - t0), end=min(float(w.end), teaser.cut) - t0)
                for w in source_words if t0 - 1e-6 <= float(w.start) < teaser.cut]
        closeups = None  # Round 48: the cold open on a two-person shot cuts straight to whoever says the line
        if layout == "smart_crop" and split is not None and said:
            from core import speaker_director

            if speaker_director.enabled(settings):
                try:
                    closeups = speaker_director.direct(source_video, t0, t1, plan, split, said, establish=False)
                    closeups = closeups if closeups is not None and closeups.layers else None
                except Exception:  # noqa: BLE001
                    logger.warning("Teaser close-ups failed; the teaser stays on the two-shot.", exc_info=True)
        seams = list(split.spans) if split else None
        if seams and closeups is not None:
            from core.speaker_director import subtract

            seams = subtract(seams, closeups.windows()) or None
        create_ass_subtitles(
            said, ass, clip_duration=t1 - t0, highlight_active_word=settings.caption_highlight,
            hook_title=None if (title or not title_on or topic_header) else title_text, hook_seconds=seconds,
            seam_spans=seams, seam_title=_seam_title(title), caption_top=caption_top,
        )
        render_clip(
            source_video, ass, crop, t0, t1, video, layout_mode=layout, crop_x_expression=x_expression, title=title,
            full_frame_ranges=full_frame, split_view=split, webinar=webinar, holds=(0.0, 0.0), pull_in_until=0.0,
            closeups=closeups.layers if closeups is not None else None,
            **teaser_camera(seconds, layout),
        )
        return climax_hook.stitch(video, body, body, teaser, work_dir / f"{name}_stitch")
    finally:
        if title is not None:
            title.image_path.unlink(missing_ok=True)
        ass.unlink(missing_ok=True)
        video.unlink(missing_ok=True)
        shutil.rmtree(work_dir / f"{name}_stitch", ignore_errors=True)


def _explainer_caption_top(layout: str, source_video: Path) -> int | None:
    """Explainer layout: captions under the slide (None for every other layout)."""
    if layout != "explainer":
        return None
    return explainer.caption_top(explainer.safe_box(*_probe_display_size(source_video)))


def pick_clips_with_agent(
    words: list[Word],
    video_duration: float,
    count: int,
    channel_name: str,
    video_title: str,
    peaks: list[tuple[float, float, float]] | None = None,
    shorts_status: str = "",
    *,
    min_judge_score: int | None = None,
    report: dict[str, Any] | None = None,
    business: bool = False,
    heatmap: list[dict[str, Any]] | None = None,
    theme: str = "",
) -> tuple[list[ClipResult], str, str]:
    """Gemini/OpenAI picks (ranked) + outreach email and subject, snapped like
    every other clip. Returns (clips, email, subject).

    The model returns count + EXTRA_CANDIDATES ranked candidates. They're snapped
    to sentences, weak moments are fixed or dropped, a second AI pass scores
    each clip's exact words, and the best non-overlapping ``count`` are kept.
    The email opens on the best clip: its start time and what its point is.

    ``min_judge_score`` (the worker's quality gate): clips the second AI pass
    scored below this are dropped, and so are filler picks it never scored. With
    fewer than MIN_CLIPS left, WeakMoments is raised. With the gate on, every clip is
    scored (JudgeUnavailable if the judge can't answer), so evenly spaced filler picks
    can never be sent unscored. Without the gate (the app), no judge answer means
    nothing is dropped. ``report`` (optional dict) receives
    "judge_scores": one score (or None) per returned clip, "clip_points": the AI's
    point for each returned clip, and "pitch_facts": the moment the email names
    (time, point, topic, format), saved on the pitch for follow-ups and LinkedIn.
    ``business``: the creator sells a service (B2B lead), so the email mentions LinkedIn.
    ``heatmap`` (Round 36): yt-dlp's "Most replayed" buckets; a clip whose hook lands on a
    spike of 0.65+ ranks higher and carries its peak_retention_score (core.heatmap_scorer).
    Without it, the hot ``peaks`` are used.
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
    judged: dict[tuple[float, float], int] = {}
    gate = min_judge_score is not None
    candidates = rescore_with_judge(
        prepare_candidates(proposals, words, video_duration), words, count, judged, required=gate
    )
    spikes = heatmap_scorer.spikes(heatmap) if heatmap else list(peaks or [])
    candidates = heatmap_scorer.score_clips(candidates, spikes)  # a hook on a replay spike ranks higher
    clips = heatmap_scorer.score_clips(choose_clips(candidates, words, video_duration, count), spikes,
                                       rank=False, raise_scores=False)
    clips = audio_polish.flag_dead_air(clips, words)  # pauses over 0.65 s, flagged (never cut)
    if gate:
        good = [clip for clip in clips if judged.get(_clip_key(clip), -1) >= min_judge_score]
        if len(good) < MIN_CLIPS:
            raise WeakMoments(
                f"only {len(good)} clip(s) scored {min_judge_score}+ "
                f"(scores: {', '.join(str(judged.get(_clip_key(c), '-')) for c in clips)})"
            )
        if len(good) < len(clips):
            logger.info("Dropped %d weak clip(s); sending %d.", len(clips) - len(good), len(good))
        clips = good
    short_topic = str(result.get("short_topic") or "")
    content_format = str(result.get("format") or "")
    if report is not None:
        report["judge_scores"] = [judged.get(_clip_key(clip)) for clip in clips]
        report["format"] = clean_format(content_format)  # "episode" or "video", for the clip page
    if not clips:
        return clips, str(result["outreach_email"]), build_subject(short_topic, content_format, show=channel_name,
                                                                     business=business)
    # "Your point at 12:34 about ...": the best clip's real start time and its point
    # (if the AI gave none, its opening words are quoted instead).
    best = max(clips, key=lambda clip: clip.virality_score)
    picked = _ai_pick_for(best, result["clips"])
    if report is not None:
        report["clip_points"] = [clean_key_point(_ai_pick_for(clip, result["clips"]).get("point")) for clip in clips]
        report["pitch_facts"] = {
            "at": round(best.start_time, 1),
            "point": clean_key_point(picked.get("point")),
            "seconds": round(best.end_time - best.start_time, 1),
            "topic": clean_topic(result.get("video_topic")),
            "short_topic": clean_short_topic(short_topic),
            "format": clean_format(content_format),
            "business": bool(business),
            **({"theme": theme} if theme else {}),  # Round 46: the prospect's content-calendar angle
        }
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
        business=business,
        theme=theme,
    )
    return clips, email, build_subject(short_topic, content_format, len(clips), show=channel_name, business=business)


def _ai_pick_for(clip: ClipResult, picks: list[dict[str, Any]]) -> dict[str, Any]:
    """The AI's entry for a final clip: same hook title, else the nearest start
    (snapping to sentences can move a start by a few seconds)."""
    for pick in picks:
        if str(pick.get("hook_title")) == clip.viral_title:
            return pick
    nearby = [pick for pick in picks if abs(float(pick.get("start_time", -1e9)) - clip.start_time) <= 20]
    return min(nearby, key=lambda pick: abs(float(pick["start_time"]) - clip.start_time), default={})


def _seam_title(title: TitleOverlay | None) -> tuple[float, int] | None:
    """(seconds, bottom y) when the captions go just under the title card: on the
    split view's seam, or under the slides (webinar layout)."""
    return title_above_captions(title)


class JudgeUnavailable(ServicePause):
    """The quality gate is on but the judge gave no scores: try the creator again
    later rather than send clips nobody scored."""

    def __init__(self, message: str) -> None:
        super().__init__(message, JUDGE_RETRY_SEC, service="judge")


JUDGE_RETRY_SEC = 10 * 60


class WeakMoments(Exception):
    """Fewer than MIN_CLIPS clips passed the quality bar; the lead isn't worth sending."""


MIN_CLIPS = 2  # with fewer good clips than this, the lead is skipped


def _clip_key(clip: ClipResult) -> tuple[float, float]:
    return (round(clip.start_time, 2), round(clip.end_time, 2))


EXTRA_CANDIDATES = 5  # spare picks, so dropping weak moments rarely leaves a gap
JUDGE_WEIGHT = 0.7  # the judge reads the final clip text; the first pass only ranked moments


def rescore_with_judge(
    candidates: list[ClipResult],
    words: list[Word],
    count: int,
    scores_out: dict[tuple[float, float], int] | None = None,
    *,
    required: bool = False,
) -> list[ClipResult]:
    """Blend the judge's score into each candidate's rank score (best effort).

    ``scores_out`` collects each candidate's own judge score (0-100), keyed by
    (start_time, end_time), for the quality gate and the Outbox record.

    ``required`` (the worker's quality gate): always score, even with nothing to choose
    between, and raise JudgeUnavailable instead of returning unscored clips."""
    if not candidates or (len(candidates) <= count and not required):
        return candidates  # nothing to choose between
    scores = judge_clips([clip_text(words, clip.start_time, clip.end_time) for clip in candidates])
    if scores is None:
        if required:
            raise JudgeUnavailable("the clip judge gave no usable scores")
        return candidates
    if scores_out is not None:
        for clip, score in zip(candidates, scores):
            scores_out[_clip_key(clip)] = score
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
