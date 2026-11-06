from __future__ import annotations

import base64
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st
from streamlit.errors import StreamlitAPIException
from pydantic import BaseModel

from autonomous_worker import (
    LOG_PATH as WORKER_LOG_PATH,
    MAX_PENDING_LEADS,
    MIN_CLIP_SCORE,
    CLEARABLE_GROUPS,
    FORCEABLE_GROUPS,
    PASS_REASONS,
    clear_forceable_group,
    force_lead_to_queue,
    REPLY_OUTCOMES,
    get_awaiting_reply,
    get_replies,
    get_forceable_leads,
    get_cached_leads,
    get_recently_passed,
    get_worker_status,
    mark_follow_up,
    prioritize_queued_lead,
    record_clip_trim,
    remove_pitch_clip,
    drive_backup_on,
    restore_forceable_group,
    restore_passed_as_sent,
    update_pending_draft,
    get_lead_queue,
    get_pending_outreach,
    is_youtube_quota_exhausted,
    mark_outreach_sent,
    pass_summary,
    skip_lead,
    skip_queued_lead,
    start_worker,
    stop_worker,
    undo_reply,
)
from config import load_settings
from core.downloader import (
    download_audio_only,
    download_source_video,
    extract_audio_only,
    fetch_video_info,
    sign_in_summary,
    video_format_sort,
)
from core import series
from core import long_video, quotes
import core.downloader as youtube
from core.lead_harvester import lead_profile, subscriber_range
from core.card_checks import LIMITS as CARD_LIMITS, check_edit
from core.cards import CANVAS as CARD_CANVAS, MIN_CONTRAST, contrast
from core.dynamic_layout import render_with_cards
from core.edl import (
    BRAND_KIT_PATH,
    MODES,
    TEMPLATE_LABELS,
    TEMPLATES,
    BrandKit,
    Card,
    CardRow,
    ClipEdit,
    load_brand_kit,
    save_brand_kit,
    sentences as edl_sentences,
)
from core import webinar as webinar_defaults
from core.clipper import MIN_CLIP_DURATION, rerender_clip, rerender_output_path
from core.face_tracker import CropBox
from core.drive_uploader import replace_clip_in_drive, upload_clips_to_drive
from core.outreach import (
    SUBJECT,
    build_linkedin_messages,
    build_outreach_email,
    build_subject,
    fill_drive_link,
    greeting_name_of,
    is_shared_inbox,
    set_greeting,
    subject_for,
)
from core.hook_selector import ClipResult, cap_clip_length, select_clips
from core.pipeline import (
    MIN_CLIPS,
    pick_clips_with_agent,
    render_selected_clip,
    video_info_and_peaks,
)
from core import clip_pages
from core.pitch_files import (
    accept_trim,
    cut_report,
    discard_trim,
    load_pitch,
    make_preview,
    pitch_files_bytes,
    poster_after_trim,
    rerender_trim,
    set_poster_time,
    trim_limits,
)
from core.poster import best_poster_time, poster_jpeg
from core.storage_manager import (
    prune_output_folder,
    prune_stale_files,
    purge_storage,
    render_output_dir,
    storage_usage_bytes,
)
from core.transcriber import (
    Word,
    local_whisper_allowed,
    transcribe_audio,
    youtube_video_id,
)
from core.subtitle_engine import caption_style
from core.video_renderer import loudness_target
from main import _audio_duration, _video_duration


class OutreachPitch(BaseModel):
    subject: str
    body: str


# Leads with these statuses are done, so the Lead Finder tab hides them. They stay
# saved in storage/processed_creators.json so the worker never contacts them again.
# Add "skipped" here to hide creators you passed on as well.
FINISHED_LEAD_STATUSES = frozenset({"sent", "completed", "skipped"})


QUEUE_PREVIEW_SIZE = 15


WORKER_COMMAND = (
    'cd "/Users/barrynavan/Short form content project" && '
    ".venv/bin/python autonomous_worker.py --continuous"
)
_STATE_TEXT = {
    "starting": "starting up",
    "processing": "making clips for {detail}",
    "outbox_full": "waiting: the Outbox is full ({detail}). It makes the next pitch as soon as you send or pass one",
    "discovering": "looking for new creators",
    "idle": "waiting: {detail}",
    "quota_pause": "paused: {detail}",
    "drive_sign_in": "paused: {detail}",
}


def _render_worker_status() -> None:
    """Is the worker actually running, and what is it doing?"""
    try:
        status = get_worker_status()
    except Exception as exc:
        st.warning(f"Could not read the worker status: {exc}")
        return
    if status["running"] and status.get("stopping"):
        st.info(f"**Worker stopping:** it finishes {status['detail'] or 'the current creator'}, then stops.", icon="⏹️")
    elif status["running"]:
        text = _STATE_TEXT.get(status["state"], status["state"] or "running").format(detail=status["detail"])
        try:
            updated = datetime.fromisoformat(status["updated_at"]).astimezone().strftime("%-I:%M %p")
        except ValueError:
            updated = ""
        message = f"**Worker running:** {text}." + (f" (updated {updated})" if updated else "")
        if status["state"] in {"outbox_full", "quota_pause", "drive_sign_in"}:
            st.info(message, icon="⏸️")
        else:
            st.success(message, icon="🟢")
    else:
        st.warning(
            "**The worker isn't running**, so the queue won't move and no new pitches are made.",
            icon="🔴",
        )
        _render_start_button("start_worker_header")
        st.caption("Or start it in Terminal:")
        st.code(WORKER_COMMAND, language="bash")


def _render_start_button(key: str) -> None:
    if not st.button("▶ Start the worker", key=key, type="primary"):
        return
    try:
        started = start_worker()
    except Exception as exc:
        logging.exception("Could not start the worker")
        st.error(f"Could not start the worker: {exc}")
        return
    if started:
        with st.spinner("Starting the worker…"):
            for _ in range(30):  # it takes a few seconds to load
                if get_worker_status()["running"]:
                    break
                time.sleep(0.5)
    st.rerun()


def _render_stop_button() -> None:
    if not st.button("⏹ Stop the worker", key="stop_worker"):
        return
    try:
        result = stop_worker()
    except Exception as exc:
        logging.exception("Could not stop the worker")
        st.error(f"Could not stop the worker: {exc}")
        return
    if result == "stopped":
        time.sleep(1.5)  # let it save its status
    st.rerun()


def _render_lead_queue() -> None:
    """Up-next list: the worker takes these top-down; one click skips a creator."""
    try:
        queue = get_lead_queue()
    except Exception as exc:
        st.error(f"Could not read the lead queue: {exc}")
        return
    st.subheader(f"Up next ({len(queue)} in queue)")
    if not queue:
        st.caption("The queue is empty. The worker will look for new creators when it needs one.")
        return
    st.caption(
        "There's no approve step here: while the worker is running and the Outbox has room "
        "(up to 5 pitches), it takes creators from the top and makes their clips and email. "
        "**Do next** moves a creator to the top; **Skip** removes them before any work is done."
    )
    for index, lead in enumerate(queue[:QUEUE_PREVIEW_SIZE]):
        channel_id = str(lead.get("channel_id", ""))
        title = lead.get("channel_title") or channel_id or "Creator"
        channel_url = lead.get("channel_url") or f"https://www.youtube.com/channel/{channel_id}"
        subscribers = lead.get("subscriber_count")
        subscriber_text = f"{int(subscribers):,} subs" if isinstance(subscribers, int) else "subs hidden"
        email = lead.get("contact_email")
        email_text = f"✉️ {email}" if email else "no email found"
        seconds = lead.get("duration_seconds")
        length_text = f"{int(seconds) // 60} min" if isinstance(seconds, int) else "length unknown"
        video_url = lead.get("latest_video_url") or ""
        video_title = lead.get("latest_video_title") or "Target video"
        forced = bool(lead.get("forced"))
        shorts_text = lead.get("shorts_summary") or (
            "Shorts: not checked" if forced else "Shorts: not checked (queued before this update)"
        )
        forced_text = " · :violet[⏩ forced: automatic checks off]" if forced else ""
        with st.container(border=True):
            info, next_col, action = st.columns([6, 1, 1])
            with info:
                st.markdown(f"**{index + 1}. [{title}]({channel_url})** · {subscriber_text} · {email_text}{forced_text}")
                st.caption(f"{shorts_text} · [{video_title}]({video_url}) ({length_text})")
            with next_col:
                if index > 0 and st.button("Do next", key=f"do_next_{channel_id}_{index}", use_container_width=True):
                    try:
                        prioritize_queued_lead(channel_id)
                    except Exception as exc:
                        st.error(f"Could not move it: {exc}")
                    else:
                        st.rerun()
            with action:
                if st.button("Skip", key=f"skip_queued_{channel_id}_{index}", use_container_width=True):
                    try:
                        skip_queued_lead(channel_id)
                    except Exception as exc:
                        st.error(f"Could not skip: {exc}")
                    else:
                        st.rerun()
    if len(queue) > QUEUE_PREVIEW_SIZE:
        st.caption(f"…and {len(queue) - QUEUE_PREVIEW_SIZE} more.")


def _latest_lead_records() -> list[dict[str, Any]]:
    """One record per creator: the newest one (a retried lead has several)."""
    latest: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(get_cached_leads()):
        key = str(record.get("channel_id") or f"record-{index}")
        latest.pop(key, None)  # keep the newest record, in its newest position
        latest[key] = record
    return list(latest.values())


def _is_finished(record: dict[str, Any]) -> bool:
    return str(record.get("status", "")).strip().lower() in FINISHED_LEAD_STATUSES


def _active_cached_leads() -> list[dict[str, Any]]:
    """Leads still worth seeing in the app (not sent or completed)."""
    return [record for record in _latest_lead_records() if not _is_finished(record)]


FORCEABLE_PREVIEW = 30  # per group


def _render_forceable_leads() -> None:
    """Creators that didn't finish, that the worker skipped, or that you passed or
    skipped, each with ⏩ Force to queue."""
    try:
        rows = get_forceable_leads()
    except Exception as exc:
        st.error(f"Could not read skipped and unfinished leads: {exc}")
        return
    if not rows:
        return
    cleared = [row for row in rows if row.get("cleared")]
    rows = [row for row in rows if not row.get("cleared")]
    toast = st.session_state.pop("forceable_toast", None)
    if toast:
        st.toast(toast, icon="🧹")
    st.subheader(f"Skipped or unfinished ({len(rows)})")
    st.caption(
        "**⏩ Force to queue** puts a creator at the back of the queue (after everyone already in "
        "it) and clears why they were skipped. The worker then makes their clips without the automatic checks (on camera, "
        "screen share, AI score bar); every clip still gets an AI score, so you can remove weak "
        "ones in the Outbox."
    )
    for group, label in FORCEABLE_GROUPS.items():
        members = [row for row in rows if row["group"] == group]
        hidden = sum(1 for row in cleared if row["group"] == group)
        if not members and not hidden:
            continue
        with st.expander(f"{label} ({len(members)})"):
            if group in CLEARABLE_GROUPS:
                _render_clear_controls(group, label, len(members), hidden)
            for row in members[:FORCEABLE_PREVIEW]:
                channel_id = row["channel_id"]
                info, action = st.columns([6, 2], vertical_alignment="center")
                with info:
                    video = f" · [video]({row['video_url']})" if row["video_url"] else ""
                    st.markdown(f"**[{row['channel_title']}](https://www.youtube.com/channel/{channel_id})**{video}")
                    reason = f": {row['reason']}" if row["reason"] else ""
                    st.caption(f"{row['status']}{reason}"[:220])
                with action:
                    if st.button("⏩ Force to queue", key=f"force_{channel_id}", width="stretch"):
                        with st.spinner("Adding them to the queue…"):
                            try:
                                lead = force_lead_to_queue(channel_id)
                            except Exception as exc:
                                logging.exception("Could not force the lead")
                                st.error(f"Couldn't force them: {exc}")
                            else:
                                st.session_state["lead_forced"] = lead.get("channel_title") or channel_id
                                st.rerun()
            if len(members) > FORCEABLE_PREVIEW:
                st.caption(f"…and {len(members) - FORCEABLE_PREVIEW} older ones.")


def _render_clear_controls(group: str, label: str, count: int, hidden: int) -> None:
    """🧹 Clear (confirmed in a pop-over) and ↩ Restore for one list. Clearing only hides
    them: the queue, the Outbox and sent pitches don't change, and they stay "done", so
    the worker never finds them again."""
    clear_column, restore_column = st.columns(2)
    with clear_column:
        if count:
            with st.popover(f"🧹 Clear ({count})", width="stretch"):
                st.markdown(
                    f"Hide all {count} from **{label}**? Nothing is deleted: the queue, the Outbox and sent "
                    "pitches don't change, they won't be found again, and ↩ Restore brings the list back."
                )
                if st.button(f"Yes, clear {count}", key=f"clear_go_{group}", type="primary", width="stretch"):
                    try:
                        cleared = clear_forceable_group(group)
                    except Exception as exc:
                        logging.exception("Could not clear the list")
                        st.error(f"Couldn't clear it: {exc}")
                        return
                    st.session_state["forceable_toast"] = f"Cleared {cleared} from {label}."
                    st.rerun()
    with restore_column:
        if hidden and st.button(f"↩ Restore {hidden} cleared", key=f"clear_undo_{group}", width="stretch"):
            restore_forceable_group(group)
            st.session_state["forceable_toast"] = f"{label}: {hidden} back in the list."
            st.rerun()


LAYOUT_LABELS = {
    "Auto (recommended): face-tracked, split view for two people, explainer for slide videos": "auto",
    "Face-tracked crop": "smart_crop",
    "Full frame over a blurred background": "blurred_background",
    "Wide video, letterboxed": "wide_blurred_background",
    "Wide shot on top, face below": "stacked",
    "Webinar / screen + speaker: webcam on top, slides below": "webinar",
    "Explainer: your slide video, its center 4:3 at full width": "explainer",
}
# Webinar layout: where the speaker's webcam is ("auto" finds the window by itself).
WEBCAM_CORNERS = {
    "Find it automatically": "auto",
    "Top right": "top-right",
    "Top left": "top-left",
    "Bottom right": "bottom-right",
    "Bottom left": "bottom-left",
}
SLIDE_SHAPE_LABELS = {"Auto (trims dark borders)": "auto", "16:9": "16:9", "4:3 (most medical and academic decks)": "4:3"}
DIVIDER_LABELS = {"Thin dark line": "line", "Accent color line": "accent", "Soft shadow": "shadow", "Dark gap": "gap"}
PIN_TITLE_LABEL = "📌 Pin title card for full video duration"
PIN_TITLE_HELP = "Off: the title card shows for the first few seconds. On: it stays on screen until the clip's last frame."
WEBINAR_HELP = (
    "Webinar: for slides, webinars and tutorials with a small webcam in a corner: the webcam goes on "
    "top (about a quarter of the screen), the whole slide under it, then the title and captions.\n\n"
    "Explainer: for your own 16:9 slide videos (Keynote, Canva) with nobody on camera. The center "
    "4:3 of each slide (1440×1080 of 1920×1080) fills the phone's width, the title sits above it and "
    "the captions under it. Auto picks it when the slide margins are nearly empty."
)


def _get_video_metadata(
    input_path: str | Path,
    channel_name_override: str = "",
    video_title_override: str = "",
) -> tuple[str, str, list[tuple[float, float, float]]]:
    """Title, channel and YouTube "Most replayed" peaks (empty for local files)."""
    source_value = str(input_path)
    if not source_value.startswith(("http://", "https://")):
        title = video_title_override.strip() or Path(input_path).stem
        return title, channel_name_override.strip() or "your channel", []

    metadata, peaks = video_info_and_peaks(source_value)
    title = video_title_override.strip() or metadata.get("title")
    channel = channel_name_override.strip() or metadata.get("channel")
    if not isinstance(title, str) or not title.strip():
        raise RuntimeError("The video title is unavailable for the outreach pitch.")
    if not isinstance(channel, str) or not channel.strip():
        raise RuntimeError("The channel name is unavailable for the outreach pitch.")
    return title.strip(), channel.strip(), peaks


def _sanitize_outreach_video_title(video_title: str) -> str:
    title = re.sub(
        r"\.(?:mp4|mov|mkv|webm|avi|m4v|mp3|wav|m4a|aac|flac|ogg)$",
        "",
        video_title.strip(),
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"(?:[\s._-]*\d{4}[-/.]\d{1,2}[-/.]\d{1,2})?"
        r"(?:[\s._-]*[\[(]?\d{1,2}[:.]\d{2}(?::\d{2})?[\])]?)$",
        "",
        title,
    )
    title = title.replace("_", " ")
    title = re.sub(r"[^\w\s'’&,.!?()-]", " ", title, flags=re.UNICODE)
    title = re.sub(r"([!?.,])\1+", r"\1", title)
    title = re.sub(r"\s+", " ", title)
    return title.strip(" \t\r\n-–—_.,:;!?()")


def _generate_outreach_pitch(
    video_title: str,
    drive_link: str,
    clip_count: int,
    channel_name: str = "",
) -> OutreachPitch:
    """Email for when the AI step didn't produce one (same template as the worker)."""
    clean_title = _sanitize_outreach_video_title(video_title)
    if not clean_title:
        raise ValueError("The video title is empty after cleaning.")
    if not drive_link.strip():
        raise ValueError("A Google Drive folder link is required for the outreach email.")
    if clip_count < 1:
        raise ValueError("At least one uploaded clip is required for the outreach email.")

    return OutreachPitch(
        subject=build_subject("", "video", clip_count),
        body=build_outreach_email(
            channel_name,
            video_title=clean_title,
            drive_link=drive_link,
            clip_count=clip_count,
        ),
    )


def _render_copy_email_button(email_body: str, label: str = "📋 Copy email") -> None:
    encoded_email = base64.b64encode(email_body.encode("utf-8")).decode("ascii")
    st.iframe(
        f"""
        <style>html, body {{ margin: 0; padding: 0; }}</style>
        <button id="copy-email" aria-live="polite" style="width: 100%; height: 40px; cursor: pointer;
          border: 1px solid rgba(49, 51, 63, 0.2); border-radius: 8px; background: #fff;
          color: #31333f; font: 400 14px 'Source Sans Pro', 'Source Sans 3', sans-serif;">
          {label}
        </button>
        <script>
          const bytes = Uint8Array.from(atob("{encoded_email}"), c => c.charCodeAt(0));
          const email = new TextDecoder().decode(bytes);
          const button = document.getElementById("copy-email");
          button.addEventListener("click", async () => {{
            try {{
              if (navigator.clipboard && window.isSecureContext) {{
                await navigator.clipboard.writeText(email);
              }} else {{
                const field = document.createElement("textarea");
                field.value = email;
                document.body.appendChild(field);
                field.select();
                const copied = document.execCommand("copy");
                field.remove();
                if (!copied) {{
                  throw new Error("Clipboard copy was rejected.");
                }}
              }}
              button.textContent = "✓ Copied";
            }} catch (error) {{
              button.textContent = "Copy failed: select the text";
            }}
            setTimeout(() => {{ button.textContent = {json.dumps(label)}; }}, 2500);
          }});
        </script>
        """,
        height=42,
    )


def _discard_generated_clips() -> None:
    generated_clips = st.session_state.get("generated_clips", [])
    output_dir = render_output_dir().expanduser().resolve()
    settings = load_settings()
    raw_dir = settings.raw_dir.expanduser().resolve()
    clip_paths: list[Path] = []
    for clip in generated_clips:
        clip_path = Path(clip[0]).expanduser()
        if clip_path.is_symlink():
            raise ValueError(f"Refusing to delete a symlinked clip: {clip_path}")
        resolved_path = clip_path.resolve()
        if resolved_path.parent != output_dir or resolved_path.suffix.lower() != ".mp4":
            raise ValueError(
                f"Refusing to delete a clip outside the render folder: {clip_path}"
            )
        clip_paths.append(resolved_path)

    for clip_path_text in st.session_state.get("clip_paths", []):
        path = Path(clip_path_text).expanduser()
        if path.is_symlink():
            raise ValueError(f"Refusing to delete a symlinked clip: {path}")
        resolved_path = path.resolve()
        if resolved_path.parent != output_dir or resolved_path.suffix.lower() != ".mp4":
            raise ValueError(f"Refusing to delete a clip outside the render folder: {path}")
        if resolved_path not in clip_paths:
            clip_paths.append(resolved_path)

    source_path_value = st.session_state.get("clip_source_path")
    source_path: Path | None = None
    if source_path_value:
        source_path = Path(source_path_value).expanduser()
        if source_path.is_symlink():
            raise ValueError(f"Refusing to delete a symlinked source video: {source_path}")
        source_path = source_path.resolve()
        if source_path.parent != raw_dir:
            raise ValueError(
                f"Refusing to delete a source video outside raw storage: {source_path}"
            )

    active_run_id = st.session_state.get("active_clip_run_id")
    if active_run_id:
        for suffix in (".ass", ".wav"):
            for temporary_path in settings.temp_dir.glob(
                f"clip_{active_run_id}_*{suffix}"
            ):
                if temporary_path.is_symlink():
                    raise ValueError(
                        f"Refusing to delete a symlinked temporary file: {temporary_path}"
                    )
                if temporary_path.is_file():
                    temporary_path.unlink()
        for run_clip in output_dir.glob(f"clip_{active_run_id}_*.mp4"):
            if run_clip.is_symlink():
                raise ValueError(f"Refusing to delete a symlinked clip: {run_clip}")
            if run_clip.is_file():
                run_clip.unlink()
    else:
        for clip_path in clip_paths:
            clip_path.unlink(missing_ok=True)

    for clip_path in clip_paths:
        clip_path.unlink(missing_ok=True)
        for suffix in (".ass", ".wav"):
            (settings.temp_dir / f"{clip_path.stem}{suffix}").unlink(
                missing_ok=True
            )
    if source_path is not None:
        source_path.unlink(missing_ok=True)

    for key in (
        "generated_clips",
        "published_page",
        "card_drafts",
        "card_clips",
        "clip_paths",
        "active_clip_run_id",
        "clip_render_contexts",
        "clip_source_path",
        "drive_folder_link",
        "outreach_pitch",
        "outreach_email_subject",
        "outreach_email_body",
        "outreach_video_title",
        "outreach_channel_name",
        "outreach_input_path",
        "outreach_channel_override",
        "outreach_title_override",
        "transcript",
        "transcript_words",
        "clip_transcript",
        "ai_clip_selection_error",
        "ai_outreach_email",
        "ai_outreach_subject",
        "agent_video_title",
        "agent_channel_name",
    ):
        st.session_state.pop(key, None)
    st.session_state["_reset_clip_inputs_pending"] = True


def _delete_previous_source_video() -> None:
    """Remove the last run's downloaded source so raw storage doesn't pile up."""
    source_value = st.session_state.get("clip_source_path")
    if not source_value:
        return
    raw_dir = load_settings().raw_dir.expanduser().resolve()
    source_path = Path(source_value).expanduser()
    if source_path.is_symlink():
        return
    source_path = source_path.resolve()
    if source_path.parent == raw_dir:
        source_path.unlink(missing_ok=True)


def _generate_clips(
    input_path: str | Path,
    clip_count: int,
    layout_mode: str,
    *,
    webcam_corner: str = "auto",
    webinar_options: dict[str, str] | None = None,
    pin_title: bool = False,
    max_seconds: float | None = None,
    title_fade_in: bool = True,
) -> list[tuple[Path, ClipResult]]:
    """``max_seconds``: no clip longer than this (cut at a sentence end). ``title_fade_in``:
    False puts the title card fully on screen from the first frame. Layout "auto" may
    choose the explainer layout for a faceless slide video."""
    settings = load_settings()
    output_dir = render_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    prune_output_folder(output_dir=output_dir)
    prune_stale_files(settings.raw_dir)
    prune_stale_files(settings.temp_dir)
    progress = st.progress(0, text="0% complete")

    def update_progress(value: float, status_text: str) -> None:
        bounded_value = min(max(value, 0.0), 1.0)
        status_text = f"{int(bounded_value * 100)}% complete"
        progress.progress(
            bounded_value,
            text=status_text,
        )

    def stage_progress(
        start: float,
        end: float,
    ) -> Callable[[float, str], None]:
        def update_stage(value: float, status_text: str) -> None:
            bounded_value = min(max(value, 0.0), 1.0)
            overall_value = start + (end - start) * bounded_value
            status_text = f"{int(overall_value * 100)}% complete"
            update_progress(overall_value, status_text)

        return update_stage

    source_value = str(input_path)
    is_url = source_value.startswith(("http://", "https://"))
    source_video = None if is_url else Path(input_path).expanduser().resolve(strict=True)
    audio_path = settings.temp_dir / f"audio_{uuid.uuid4().hex}.wav"

    try:
        st.write("**Step 1 of 4: Preparing audio**")
        if is_url:
            download_audio_only(
                source_value,
                audio_path,
                on_progress=stage_progress(0.0, 0.20),
            )
            duration = _audio_duration(audio_path)
        else:
            if source_video is None or not source_video.is_file():
                raise ValueError(f"Input video is not a file: {input_path}")
            duration = _video_duration(source_video)
        fingerprint = None if is_url or source_video is None else long_video.file_fingerprint(source_video)
        words = long_video.load_file_transcript(fingerprint) if fingerprint else None
        if words is None and source_video is not None and not is_url:
            extract_audio_only(source_video, audio_path)
        update_progress(0.20, "")

        if words is not None:
            st.write("**Step 2 of 4: Reusing this file's saved transcript (no Groq call)**")
        else:
            st.write("**Step 2 of 4: Transcribing speech with Groq**")
            words = transcribe_audio(
                audio_path,
                settings,
                on_progress=stage_progress(0.20, 0.45),
            )
            if fingerprint:
                long_video.save_file_transcript(fingerprint, words)
        update_progress(0.45, "")

        st.write("**Step 3 of 4: AI picking the best moments**")
        peaks: list[tuple[float, float, float]] = []
        try:
            video_title, resolved_channel_name, peaks = _get_video_metadata(
                input_path,
                st.session_state.get("outreach_channel_override", ""),
                st.session_state.get("outreach_title_override", ""),
            )
        except Exception as exc:
            logging.warning("Video metadata lookup failed; using fallbacks: %s", exc)
            video_title = (
                st.session_state.get("outreach_title_override", "").strip()
                or (
                    youtube_video_id(source_value)
                    if is_url
                    else Path(input_path).stem
                )
                or "your video"
            )
            resolved_channel_name = (
                st.session_state.get("outreach_channel_override", "").strip()
                or "your channel"
            )
            st.warning(
                "Could not read all video metadata; using fallback title/channel "
                f"for the AI request. Details: {exc}"
            )

        try:
            with st.spinner("Analyzing the transcript and drafting outreach..."):
                clips, outreach_email, outreach_subject = pick_clips_with_agent(
                    words,
                    duration,
                    clip_count,
                    resolved_channel_name,
                    video_title,
                    peaks,
                )
            st.session_state["ai_outreach_email"] = outreach_email
            st.session_state["ai_outreach_subject"] = outreach_subject
            st.session_state.pop("ai_clip_selection_error", None)
        except Exception as exc:
            logging.exception("AI clip selection failed; using the existing selector")
            st.session_state["ai_outreach_email"] = ""
            st.session_state.pop("ai_outreach_subject", None)
            st.session_state["ai_clip_selection_error"] = str(exc)
            st.warning(
                "AI clip selection/outreach generation failed; using the existing "
                f"clip selector instead. Details: {exc}"
            )
            clips = select_clips(
                words, duration, clip_count, settings, replay_peaks_hint=peaks
            )[:clip_count]
        if max_seconds:
            clips = cap_clip_length(clips, words, max_seconds)

        st.session_state["agent_video_title"] = video_title
        st.session_state["agent_channel_name"] = resolved_channel_name
        if st.session_state.get("ai_outreach_email"):
            st.session_state["outreach_email_body"] = st.session_state[
                "ai_outreach_email"
            ]
            st.session_state["outreach_email_subject"] = st.session_state.get(
                "ai_outreach_subject"
            ) or subject_for(st.session_state["ai_outreach_email"])
        else:
            st.session_state.pop("outreach_email_body", None)
        st.session_state["outreach_video_title"] = video_title
        st.session_state["outreach_channel_name"] = resolved_channel_name
        audio_path.unlink(missing_ok=True)

        output_dir.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6]
        if is_url:
            source_video = Path(
                download_source_video(
                    source_value,
                    on_progress=stage_progress(0.45, 0.60),
                )
            )
        else:
            if source_video is None:
                raise RuntimeError("No source video is available for rendering.")
            cached_source = settings.raw_dir / f"source_{run_id}{source_video.suffix.lower()}"
            cached_source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_video, cached_source)
            source_video = cached_source.resolve(strict=True)
        update_progress(0.60, "")

        rendered_by_index: dict[int, tuple[Path, ClipResult]] = {}
        render_contexts: dict[str, dict[str, object]] = {}

        st.write("**Step 4 of 4: Rendering selected clips**")

        def render_one(index: int, clip: ClipResult) -> tuple[Path, ClipResult]:
            output_path = output_dir / f"clip_{run_id}_{index:02d}.mp4"
            if source_video is None:
                raise RuntimeError("No source video is available for rendering.")
            try:
                result = render_selected_clip(
                    source_video,
                    clip,
                    words,
                    settings,
                    output_path,
                    layout_mode=layout_mode,
                    pin_title=pin_title,
                    webinar_corner=webcam_corner,
                    webinar_options=webinar_options,
                    title_fade_in=title_fade_in,
                    allow_explainer=layout_mode == "auto",
                )
            except Exception:
                output_path.unlink(missing_ok=True)
                raise
            render_contexts[_clip_key(result.path)] = {
                "source_path": str(source_video),
                "words": words,
                "layout_mode": result.layout_mode,
                "crop": result.crop,
                "crop_plan": result.crop_plan,
                "webinar": result.webinar,
                "pin_title": pin_title,
                "title_fade_in": title_fade_in,
                "start_time": clip.start_time,
                "end_time": clip.end_time,
                "source_duration": duration,
            }
            return result.path, clip

        if len(clips) > 1:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {
                    executor.submit(render_one, index, clip): index
                    for index, clip in enumerate(clips, start=1)
                }
                for completed, future in enumerate(as_completed(futures), start=1):
                    index = futures[future]
                    try:
                        rendered_by_index[index] = future.result()
                    except Exception as exc:
                        logging.exception("Parallel clip %d failed: %s", index, exc)
                        st.warning(f"Clip {index} failed to render: {exc}")
                    progress.progress(
                        60 + int(completed / len(clips) * 40),
                        text=f"{60 + int(completed / len(clips) * 40)}% complete",
                    )
        else:
            for index, clip in enumerate(clips, start=1):
                try:
                    rendered_by_index[index] = render_one(index, clip)
                except Exception as exc:
                    logging.exception("Clip %d failed: %s", index, exc)
                    st.warning(f"Clip {index} failed to render: {exc}")
                progress.progress(
                    60 + int(index / len(clips) * 40),
                    text=f"{60 + int(index / len(clips) * 40)}% complete",
                )

        progress.progress(100, text="100% complete")
        rendered_clips = [
            rendered_by_index[index] for index in sorted(rendered_by_index)
        ]
        st.session_state["clip_render_contexts"] = render_contexts
        st.session_state["clip_source_path"] = str(source_video)
        st.session_state["active_clip_run_id"] = run_id
        return rendered_clips
    finally:
        audio_path.unlink(missing_ok=True)


TAB_LABELS = ("📬 Outbox", "📨 Sent", "🧭 Pipeline", "🎬 Make clips", "🩺 System")
# Page styles. Outbox players keep a 9:16 box while a new preview loads, so swapping
# a clip after a re-render never collapses the column and moves the page. The About
# page button is the big blue one: it's where the creator's email usually is.
PAGE_CSS = """
<style>
[class*="st-key-preview_"] video { aspect-ratio: 9 / 16; height: auto; background: #000; object-fit: contain; }
[class*="st-key-about_"] a {
  background-color: #1a73e8 !important; border-color: #1a73e8 !important; color: #ffffff !important;
  min-height: 3.1rem; font-size: 1.08rem; font-weight: 600;
}
[class*="st-key-about_"] a:hover { background-color: #1558b0 !important; border-color: #1558b0 !important; }
[class*="st-key-about_"] a p { font-size: 1.08rem; font-weight: 600; color: #ffffff !important; }
</style>
"""
LAYOUT_NAMES = {
    "smart_crop": "face-tracked",
    "blurred_background": "full frame",
    "wide_blurred_background": "full frame",
    "stacked": "stacked",
    "explainer": "explainer",
}


def main() -> None:
    st.set_page_config(page_title="Nav Studios", page_icon="🎬", layout="wide")
    st.html(PAGE_CSS)
    if st.session_state.pop("_reset_clip_inputs_pending", False):
        st.session_state["video_url_input"] = ""
        st.session_state["channel_name_input"] = ""
        st.session_state["video_upload_epoch"] = (
            int(st.session_state.get("video_upload_epoch", 0)) + 1
        )

    try:
        pending = get_pending_outreach()
    except Exception as exc:
        logging.exception("Could not load pending outreach")
        st.error(f"Could not load the Outbox: {exc}")
        pending = []
    _render_header(pending)

    outbox_tab, sent_tab, pipeline_tab, clips_tab, system_tab = st.tabs(TAB_LABELS)
    with outbox_tab:
        _render_outbox(pending)
    with sent_tab:
        _render_sent_pitches()
        _render_recently_passed()
    with pipeline_tab:
        _render_pipeline(pending)
    with clips_tab:
        _render_clip_generator()
    with system_tab:
        _render_system()


# --- Header: worker status and the four numbers that matter -------------------------
def _render_header(pending: list[dict[str, Any]]) -> None:
    st.title("Nav Studios")
    _render_worker_status()
    if is_youtube_quota_exhausted():
        st.warning(
            "YouTube's daily search quota is used up, so the worker isn't finding new creators "
            "right now. Pitches already in the Outbox and the queue are unaffected.",
            icon="⚠️",
        )
    try:
        waiting = get_awaiting_reply()
        queue = get_lead_queue()
        passed = pass_summary()
    except Exception:
        logging.exception("Could not load the status numbers")
        waiting, queue, passed = [], [], {}
    due = sum(1 for record in waiting if record.get("follow_up_due"))
    you_passed = sum(count for code, count in passed.items() if code in PASS_REASONS)
    outbox, sent, up_next, passes = st.columns(4)
    outbox.metric(
        "Ready to send", f"{len(pending)} / {MAX_PENDING_LEADS}", border=True,
        help="Pitches in the Outbox. The worker stops making new ones when it's full.",
    )
    sent.metric(
        "Waiting for a reply", len(waiting), border=True,
        delta=f"{due} follow-up{'s' if due != 1 else ''} due" if due else None,
        delta_color="off", delta_arrow="off",
    )
    up_next.metric("Up next", len(queue), border=True, help="Creators the worker will make clips for next.")
    passes.metric("Passed (14 days)", you_passed, border=True, help="Reasons are in the 📨 Sent tab.")


# --- 📬 Outbox: review and send ---------------------------------------------------------
def _render_outbox(pending: list[dict[str, Any]]) -> None:
    if st.session_state.pop("outreach_marked_sent", False):
        st.success("Marked as sent. It's in the 📨 Sent tab, and the worker will fill the free spot.")
    if st.session_state.pop("lead_skipped", False):
        st.info("Passed. If you actually sent it, use 'Passed recently' in the 📨 Sent tab.")
    if not pending:
        st.info("Nothing to send right now. New pitches appear here as the worker makes them.")
        return
    st.caption(
        "For each pitch: check the name and email → **Copy email** → send it from Gmail → "
        "**✅ Mark as Sent**. Not pitching someone? **❌ Pass** and pick why."
    )
    for record in pending:
        _render_pitch_card(record)


def _render_pitch_card(record: dict[str, Any]) -> None:
    record_id = str(record["record_id"])
    channel_id = str(record.get("channel_id") or record_id)
    channel_title = str(record.get("channel_title") or "Unknown creator")
    with st.container(border=True):
        heading, links = st.columns([3, 2], vertical_alignment="center")
        with heading:
            st.subheader(channel_title)
            _render_pitch_badges(record)
        with links:
            _render_contact(record)
            drive_link = str(record.get("drive_link") or "")
            page_url = str(record.get("page_url") or "")
            small = []
            if page_url:
                small.append(("🌐 Their clip page", page_url, "What they'll see when they open the link in your email."))
            if drive_link:
                small.append(("📁 Drive backup" if page_url else "📁 Open the clips (Drive)", drive_link, None))
            for column, (label, url, help_text) in zip(st.columns(len(small) or 1), small):
                column.link_button(label, url, help=help_text, width="stretch")

        _render_pitch_clips(record)

        subject_key = f"pending_subject_{record_id}"
        body_key = f"pending_pitch_{record_id}"
        name_key = f"pending_name_{record_id}"
        email_body = str(record.get("full_email_body") or "")
        if st.session_state.pop(f"draft_refresh_{record_id}", False):  # a clip was removed: new count
            st.session_state[subject_key] = str(record.get("subject") or SUBJECT)
            st.session_state[body_key] = email_body
        note = st.session_state.pop(f"pitch_toast_{record_id}", None)
        if note:
            st.toast(note, icon="🗑️")
        # Widgets start from the saved draft; edits are saved as you make them.
        st.session_state.setdefault(subject_key, str(record.get("subject") or SUBJECT))
        st.session_state.setdefault(body_key, email_body)
        st.session_state.setdefault(
            name_key, str(record.get("greeting_name") or greeting_name_of(email_body))
        )
        name_column, subject_column = st.columns([1, 2])
        with name_column:
            st.text_input(
                "First name",
                key=name_key,
                on_change=_on_greeting_change,
                args=(record_id, name_key, body_key),
                help="Who you're writing to, e.g. Marcela. The email opens with it: 'Hi Marcela,'. "
                "Until you type one it says 'Hi there'.",
            )
            if not str(st.session_state.get(name_key) or "").strip():
                st.caption(
                    "⚠️ No first name yet. 'Hi there' reads like a mass email: find the host's name "
                    "on their About page, in the video, or on LinkedIn."
                )
        with subject_column:
            st.text_input(
                "Subject",
                key=subject_key,
                on_change=_on_draft_edit,
                args=(record_id, subject_key, "subject"),
            )
        st.text_area(
            "Email",
            height=300,
            key=body_key,
            on_change=_on_draft_edit,
            args=(record_id, body_key, "body"),
        )
        copy_column, send_column, pass_column = st.columns(3, vertical_alignment="center")
        with copy_column:
            _render_copy_email_button(st.session_state[body_key])
        with send_column:
            if st.button(
                "✅ Mark as Sent",
                type="primary",
                key=f"mark_sent_{record_id}",
                use_container_width=True,
                help="Click after you've sent it from Gmail. It moves to the 📨 Sent tab.",
            ):
                try:
                    marked = mark_outreach_sent(
                        record_id,
                        subject=st.session_state.get(subject_key),
                        body=st.session_state.get(body_key),
                    )
                except Exception as exc:
                    logging.exception("Could not mark outreach as sent")
                    st.error(f"Could not update this pitch: {exc}")
                else:
                    if marked:
                        st.session_state["outreach_marked_sent"] = True
                        st.rerun()
                    else:
                        st.warning("This pitch is no longer pending. Refresh the page.")
        with pass_column:
            _render_pass_menu(channel_id, f"skip_lead_{record_id}")
        _render_linkedin_messages(record, str(st.session_state.get(name_key) or ""))


def _render_linkedin_messages(record: dict[str, Any], first_name: str) -> None:
    """The same pitch as three LinkedIn messages, for hosts who live on LinkedIn."""
    facts = record.get("pitch") if isinstance(record.get("pitch"), dict) else {}
    clips = [clip for clip in record.get("clips") or [] if isinstance(clip, dict)]
    messages = build_linkedin_messages(
        first_name,
        short_topic=str(facts.get("short_topic") or ""),
        key_point=str(facts.get("point") or ""),
        clip_seconds=facts.get("seconds") if isinstance(facts.get("seconds"), (int, float)) else None,
        clip_count=len(clips) or 3,
        content_format=str(facts.get("format") or ""),
        business=bool(facts.get("business") or record.get("business_offer")),
    )
    linkedin = str(record.get("linkedin_url") or "")
    with st.expander("💼 LinkedIn messages (the same pitch, for hosts active on LinkedIn)"):
        if linkedin:
            st.link_button("Open their LinkedIn", linkedin, width="stretch")
        st.caption("1. Connection note (fits the 200-character limit). Copy with the icon in the corner.")
        st.code(messages["connect"], language=None, wrap_lines=True)
        st.caption("2. Once they accept:")
        st.code(messages["after_accept"], language=None, wrap_lines=True)
        st.caption("3. After they say yes: send the clip file, then:")
        st.code(messages["close"], language=None, wrap_lines=True)
        st.caption(
            "Sent it on LinkedIn instead of email? Still click **✅ Mark as Sent**, so the follow-ups "
            "and numbers in 📨 Sent include it."
        )


def _render_pitch_clips(record: dict[str, Any]) -> None:
    """The pitch's clips, playable here, each with a quick trim."""
    record_id = str(record["record_id"])
    pitch = load_pitch(record_id)
    if pitch is None or not pitch.get("clips"):
        st.caption("No preview on this Mac for this pitch (it was made before previews existed). Use the Drive link.")
        return
    columns = st.columns(3)
    for column, item in zip(columns, pitch["clips"]):
        with column:
            _render_pitch_clip(record, int(item["index"]))


@st.fragment
def _render_pitch_clip(record: dict[str, Any], index: int) -> None:
    """One clip: player, times and trim. A fragment, so − / +, Re-render and the
    new-cut buttons rerun only this clip: the rest of the page doesn't move. The layout
    above the trim controls is the same in every state, and the trim panel remembers
    whether it's open, so nothing jumps after a re-render either."""
    record_id = str(record["record_id"])
    pitch = load_pitch(record_id)  # fresh from disk: a fragment rerun keeps the old arguments
    item = next((clip for clip in (pitch or {}).get("clips", []) if int(clip["index"]) == index), None)
    if item is None:
        st.caption("This clip's files are gone (the pitch was sent or passed).")
        return
    start_key, end_key = f"trim_start_{record_id}_{index}", f"trim_end_{record_id}_{index}"
    toast = st.session_state.pop(f"trim_toast_{record_id}_{index}", None)
    if toast:
        st.toast(toast, icon="✅")  # floats over the page, so nothing moves
    for key in st.session_state.pop(f"trim_reset_{record_id}_{index}", []):  # before the inputs exist
        st.session_state[key] = 0.0  # setting (not removing) also resets the number shown
    snap = st.session_state.pop(f"trim_snap_{record_id}_{index}", None)  # "Snap to whole words"
    if snap is not None:
        st.session_state[start_key], st.session_state[end_key] = snap
    waiting = bool(item.get("trim_waiting")) and "trim" in item
    preview = Path(item["trim_preview_path"] if waiting else item["preview_path"])
    with st.container(key=f"preview_{record_id}_{index}"):
        if preview.is_file():
            st.video(str(preview), width=PREVIEW_PLAYER_WIDTH)
        else:
            st.caption("Preview file missing. Use the Drive link.")
    start, end = float(item["start"]), float(item["end"])
    saved = [clip for clip in record.get("clips") or [] if isinstance(clip, dict)]
    score = saved[index - 1].get("judge") if len(saved) >= index else None
    st.markdown(
        f"**Clip {index}** · {_clock(start)}–{_clock(end)} · {end - start:.0f} s"
        + (f" · AI {int(score)}" if isinstance(score, (int, float)) else "")
    )
    # Keyed, so it stays open across reruns; it opens by itself when a new cut is
    # waiting (after the app restarts, say). Its arguments never change, so Streamlit
    # never treats it as a new, closed panel.
    open_key = f"trim_open_{record_id}_{index}"
    if st.session_state.pop(f"trim_close_{record_id}_{index}", False):  # a clip before it was removed
        st.session_state[open_key] = False
    if waiting and open_key not in st.session_state:
        st.session_state[open_key] = True
    with st.expander("✂️ Trim", key=open_key, on_change=_ignore):
        earliest, latest = trim_limits(item)
        # The offsets live in session state (no value=): one source of truth, so a reset
        # after "Use this cut" also resets the number on screen.
        st.session_state.setdefault(start_key, 0.0)
        st.session_state.setdefault(end_key, 0.0)
        st.number_input(
            "Start: move by (seconds)", key=start_key, step=0.5, format="%.2f",
            min_value=min(0.0, round(earliest - start, 1)),
            max_value=max(0.0, round(end - MIN_CLIP_DURATION - start, 1)),
            help="Negative starts earlier. − / + nudge by half a second.",
        )
        st.number_input(
            "End: move by (seconds)", key=end_key, step=0.5, format="%.2f",
            min_value=min(0.0, round(start + MIN_CLIP_DURATION - end, 1)),
            max_value=max(0.0, round(latest - end, 1)),
            help="Positive ends later. − / + nudge by half a second.",
        )
        new_start = start + float(st.session_state.get(start_key) or 0.0)
        new_end = end + float(st.session_state.get(end_key) or 0.0)
        st.caption(f"New cut: {_clock(new_start)}–{_clock(new_end)} ({new_end - new_start:.1f} s)")
        _render_cut_words(record_id, index, item, start, end, new_start, new_end)
        if st.button(
            "🔄 Re-render", key=f"trim_go_{record_id}_{index}", use_container_width=True,
            disabled=new_start == start and new_end == end,
        ):
            with st.spinner("Re-rendering this clip (about 10–30 s)…"):
                try:
                    rerender_trim(record_id, index, new_start, new_end, load_settings())
                except Exception as exc:
                    logging.exception("Could not re-render the trimmed clip")
                    st.error(f"Couldn't re-render: {exc}")
                    return
            _rerun_this_clip()
        if waiting:
            # Below the controls, so it never pushes them (or the player) around.
            new = item["trim"]
            st.markdown(
                f":orange-badge[New cut, not used yet] {_clock(new['start'])}–{_clock(new['end'])} "
                f"({new['end'] - new['start']:.1f} s). The player shows it."
            )
            use_column, keep_column = st.columns(2)
            use_clicked = use_column.button(
                "✅ Use this cut", key=f"trim_use_{record_id}_{index}", type="primary", use_container_width=True
            )
            keep_clicked = keep_column.button(
                "↩️ Keep the old cut", key=f"trim_keep_{record_id}_{index}", use_container_width=True
            )
            if use_clicked:
                with st.spinner("Replacing the clip on their page and in Drive…"):
                    try:
                        times = accept_trim(record_id, index, _clip_replacer(record, new["start"], new["end"]))
                        record_clip_trim(record_id, index, times["start"], times["end"])
                    except Exception as exc:
                        logging.exception("Could not use the trimmed clip")
                        st.error(f"Couldn't use the new cut: {exc}")  # both buttons stay
                        return
                st.session_state[f"trim_reset_{record_id}_{index}"] = [start_key, end_key]
                st.session_state.pop(f"poster_at_{record_id}_{index}", None)  # the poster slider follows the new clip
                st.session_state[f"trim_toast_{record_id}_{index}"] = f"Clip {index} updated. The link in the email stays the same."
                _rerun_this_clip()
            if keep_clicked:
                discard_trim(record_id, index)
                st.session_state[f"trim_reset_{record_id}_{index}"] = [start_key, end_key]
                _rerun_this_clip()
    _render_poster_picker(record, item)
    _render_remove_clip(record, index, len(pitch.get("clips", [])))


def _render_cut_words(
    record_id: str, index: int, item: dict[str, Any], start: float, end: float, new_start: float, new_end: float
) -> None:
    """The words at each edge of the cut, a warning when an edge lands inside a word,
    and a button that moves it out to take the whole word."""
    try:
        report = cut_report(item, new_start, new_end)
    except (KeyError, TypeError, ValueError):
        return
    if report["opens"]:
        st.caption(f"Opens on “{report['opens']}…” · ends on “…{report['closes']}”")
    cuts = [f"the start cuts into “{report['start_word']}”" if report["start_word"] else "",
            f"the end cuts into “{report['end_word']}”" if report["end_word"] else ""]
    cuts = [cut for cut in cuts if cut]
    if not cuts:
        return
    st.markdown(f":orange[✂ {' and '.join(cuts).capitalize()}.]")
    if st.button("↔ Snap to whole words", key=f"trim_snapgo_{record_id}_{index}", use_container_width=True,
                 help="Moves each cut that lands inside a word out to just before (or after) that word."):
        st.session_state[f"trim_snap_{record_id}_{index}"] = (
            round(report["snap_start"] - start, 2), round(report["snap_end"] - end, 2)
        )
        _rerun_this_clip()


@st.cache_data(show_spinner=False, max_entries=64)
def _auto_poster_time(path: str, modified: float) -> float:
    return best_poster_time(Path(path))


@st.cache_data(show_spinner=False, max_entries=256)
def _poster_image(path: str, modified: float, at: float) -> bytes:
    return poster_jpeg(Path(path), at)


def _render_poster_picker(record: dict[str, Any], item: dict[str, Any]) -> None:
    """🖼 Poster frame: the still on their clip page before they press play."""
    record_id, index = str(record["record_id"]), int(item["index"])
    open_key = f"poster_open_{record_id}_{index}"
    with st.expander("🖼 Poster frame", key=open_key, on_change=_ignore):
        if not st.session_state.get(open_key):
            return  # nothing to read until it's opened (the automatic pick takes ~2 s a clip)
        page_name = str(record.get("page_name") or "")
        clip_path = Path(item["path"])
        if not page_name:
            st.caption("The poster is the still on their clip page before they press play. "
                       "This pitch has no clip page (Drive shows its own).")
            return
        if not clip_path.is_file():
            st.caption("This clip's file is missing on this Mac.")
            return
        modified = clip_path.stat().st_mtime
        length = float(item["end"]) - float(item["start"])
        latest = max(0.5, round(length - 0.1, 1))
        saved = item.get("poster_at")
        current = float(saved) if saved is not None else _auto_poster_time(str(clip_path), modified)
        at_key = f"poster_at_{record_id}_{index}"
        pending = st.session_state.pop(f"poster_set_{record_id}_{index}", None)  # before the slider exists
        if pending is not None:
            st.session_state[at_key] = pending
        if at_key not in st.session_state:
            st.session_state[at_key] = min(latest, round(current, 1))
        st.slider("Frame (seconds into the clip)", 0.0, latest, step=0.1, key=at_key, format="%.1f s")
        at = round(float(st.session_state[at_key]), 1)
        try:
            st.image(_poster_image(str(clip_path), modified, at), width=PREVIEW_PLAYER_WIDTH)
        except Exception as exc:
            st.caption(f"Couldn't show that frame: {exc}")
        st.caption(f"Their page shows the frame at {current:.1f} s "
                   + ("(picked by you)." if saved is not None else "(picked automatically: eyes open, face sharp)."))
        auto_column, use_column = st.columns(2)
        if auto_column.button("✨ Auto", key=f"poster_auto_{record_id}_{index}", use_container_width=True,
                              help="Jump to the best frame of the first seconds: eyes open, face sharp."):
            st.session_state[f"poster_set_{record_id}_{index}"] = round(_auto_poster_time(str(clip_path), modified), 1)
            _rerun_this_clip()
        if use_column.button("✅ Use this frame", key=f"poster_use_{record_id}_{index}", type="primary",
                             use_container_width=True, disabled=abs(at - current) < 0.05):
            with st.spinner("Updating the poster on their page…"):
                try:
                    used = clip_pages.replace_poster(page_name, index, clip_path, at)
                    set_poster_time(record_id, index, used)
                except Exception as exc:
                    logging.exception("Could not update the poster")
                    st.error(f"Couldn't update the poster: {exc}")
                    return
            st.session_state[f"trim_toast_{record_id}_{index}"] = f"Clip {index}'s poster updated on their page."
            _rerun_this_clip()


def _render_remove_clip(record: dict[str, Any], index: int, count: int) -> None:
    """🗑 Remove clip: confirm in a pop-over (floats, so nothing moves until you do)."""
    record_id = str(record["record_id"])
    if count <= MIN_CLIPS:
        st.button(
            "🗑 Remove clip", key=f"remove_off_{record_id}_{index}", disabled=True, width="stretch",
            help=f"A pitch keeps at least {MIN_CLIPS} clips. To send fewer, Pass this creator.",
        )
        return
    places = [p for p in (
        "their clip page" if record.get("page_name") else "",
        "the Drive backup" if record.get("drive_link") else "",
        "this Mac",
    ) if p]
    where = ", ".join(places[:-1]) + (" and " if len(places) > 1 else "") + places[-1]
    with st.popover("🗑 Remove clip", width="stretch"):
        st.markdown(
            f"Remove clip {index}? It's deleted from {where}. The clips after it move up a number, "
            f"and the email and subject change to {count - 1}."
        )
        if not st.button(f"Yes, remove clip {index}", key=f"remove_clip_{record_id}_{index}", type="primary", width="stretch"):
            return
        with st.spinner("Removing…"):
            try:
                result = remove_pitch_clip(record_id, index)
            except Exception as exc:
                logging.exception("Could not remove the clip")
                st.error(f"Couldn't remove it: {exc}")
                return
    # The clips after it changed numbers: their trim numbers start at 0 and their panels closed.
    for number in range(index, count + 1):
        st.session_state[f"trim_reset_{record_id}_{number}"] = [f"trim_start_{record_id}_{number}", f"trim_end_{record_id}_{number}"]
        st.session_state[f"trim_close_{record_id}_{number}"] = True
        st.session_state.pop(f"poster_at_{record_id}_{number}", None)
    st.session_state[f"draft_refresh_{record_id}"] = True
    note = f"Clip {index} removed. {result['count']} clips left; the email and subject say {result['count']}."
    if result.get("named_clip_removed"):
        note = (
            f"Clip {index} removed. Your email still names that moment ('Your point at ...'): "
            "change that line to one of the clips you kept."
        )
    elif not result["email_updated"]:
        note = f"Clip {index} removed. You rewrote the email, so check the number of clips in it."
    if result["drive_failed"]:
        note += " The Drive backup couldn't be updated (their page is fine)."
    st.session_state[f"pitch_toast_{record_id}"] = note
    st.rerun()  # the whole card: clip count, scores, email and subject


def _rerun_this_clip() -> None:
    """Rerun just this clip (the page stays put). If this run was a full-page run (rare in
    the browser; always in tests), Streamlit refuses that, so rerun the page instead."""
    try:
        st.rerun(scope="fragment")
    except StreamlitAPIException:
        st.rerun()


def _ignore() -> None:
    """on_change for widgets that only need Streamlit to remember their state."""


def _clip_replacer(record: dict[str, Any], start: float, end: float):
    """Swap a trimmed clip wherever the prospect can see it (their page, the Drive folder)."""
    page_name = str(record.get("page_name") or "")
    drive_link = str(record.get("drive_link") or "")
    if not page_name and not drive_link:
        return None

    def replace(path: Path, file_name: str) -> None:
        if page_name:
            pitch = load_pitch(str(record["record_id"])) if record.get("record_id") else None
            item = next((c for c in (pitch or {}).get("clips", []) if c.get("file") == file_name), None)
            poster_at = poster_after_trim(item, start, end) if item else None
            picked = {"poster_at": poster_at} if poster_at is not None else {}  # None: picked automatically
            clip_pages.replace_clip(page_name, file_name, path, start, end, **picked)
        if drive_link:
            replace_clip_in_drive(drive_link, file_name, path)

    return replace


def _render_pitch_badges(record: dict[str, Any]) -> None:
    """Clip count, the AI's clip scores, framing, channel size, video length."""
    clips = [clip for clip in record.get("clips") or [] if isinstance(clip, dict)]
    badges: list[tuple[str, str]] = []
    if clips:
        badges.append((f"{len(clips)} clip{'s' if len(clips) != 1 else ''}", "blue"))
        scores = [clip.get("judge") for clip in clips if isinstance(clip.get("judge"), (int, float))]
        if scores:
            badges.append((
                "AI scores " + " · ".join(str(int(score)) for score in scores),
                "green" if min(scores) >= 70 else "orange",
            ))
        layouts = sorted({LAYOUT_NAMES.get(str(clip.get("layout")), str(clip.get("layout"))) for clip in clips if clip.get("layout")})
        if layouts:
            badges.append((", ".join(layouts), "gray"))
    subscribers = record.get("subscriber_count")
    if isinstance(subscribers, int):
        badges.append((f"{subscribers:,} subs", "gray"))
    if record.get("forced"):
        badges.append(("⏩ forced through", "violet"))
    seconds = record.get("video_seconds")
    if isinstance(seconds, (int, float)) and seconds > 0:
        badges.append((f"{int(seconds) // 60} min video", "gray"))
    if badges:
        # Streamlit's inline badge syntax: one row, no layout columns.
        st.markdown(" ".join(f":{color}-badge[{label.replace(']', ')')}]" for label, color in badges))


def _about_url(record: dict[str, Any]) -> str:
    """The creator's YouTube About page (where the email usually is)."""
    contact = str(record.get("contact_email") or "")
    if contact.startswith(("http://", "https://")):
        return contact
    channel_url = str(record.get("channel_url") or "")
    if channel_url.startswith(("http://", "https://")):
        return channel_url.rstrip("/") + "/about"
    channel_id = str(record.get("channel_id") or "")
    if re.fullmatch(r"UC[A-Za-z0-9_-]{10,}", channel_id):
        return f"https://www.youtube.com/channel/{channel_id}/about"
    return ""


def _render_contact(record: dict[str, Any]) -> None:
    """The email if the worker found one, then the big About page button."""
    contact = str(record.get("contact_email") or "")
    if "@" in contact and not contact.startswith(("http://", "https://")):
        st.markdown(f"✉️ [{contact}](mailto:{contact})")
        if is_shared_inbox(contact):
            st.caption("⚠️ A shared inbox (staff read it, or no one). The host's own email or LinkedIn works better.")
    else:
        st.caption("No email found yet: it's usually on their About page.")
    about = _about_url(record)
    if about:
        st.link_button(
            "📇 Open their About page", about, key=f"about_{record['record_id']}", width="stretch",
            help="Their YouTube About page: the email is usually under 'View email address'.",
        )


# --- 🧭 Pipeline: what the worker does next ---------------------------------------------
def _render_pipeline(pending: list[dict[str, Any]]) -> None:
    forced = st.session_state.pop("lead_forced", None)
    if forced:
        st.success(f"⏩ {forced} is at the back of the queue. The worker gets to them after the creators already in it.")
    _render_lead_queue()
    _render_forceable_leads()
    lead_records = _latest_lead_records()
    finished_count = sum(_is_finished(record) for record in lead_records)
    st.caption(
        f"Active leads: {len(lead_records) - finished_count} | "
        f"Finished (hidden): {finished_count} | "
        f"Pending in outbox: {len(pending)}"
    )


# --- 🎬 Make clips: any video, by hand ---------------------------------------------------
CLIP_MODES = {
    "🎯 Highlights": "highlights",
    "🎞️ Series: the whole video in order": "series",
    "🧾 Finish a long video": "finish",
    "✂️ Quote grabber": "quote",
}
PATH_PLACEHOLDER = "/Users/you/Movies/My video.mp4"
PATH_HELP = (
    "For big files (uploads stop at 200 MB). In Finder, right-click the file, hold the Option "
    "key and choose “Copy … as Pathname”, then paste it here."
)
HIGHLIGHT_COUNTS = (3, 4, 5, 6, 7)  # 5-7 for your own long videos (one Short per section)
CAP_SECONDS = 59.0  # YouTube blocks claimed Shorts over 1 minute (a podcast quote can be claimed)
CAP_LABEL = "⏱ 59 s max"
CAP_HELP = (
    "Keeps every clip at 59 seconds or less, ending on a finished sentence. A Short over one "
    "minute with a copyright claim (for example a podcast quote) is blocked on YouTube."
)
INSTANT_TITLE_LABEL = "⚡ Title on screen from the first frame"
INSTANT_TITLE_HELP = "Off: the title card fades in over the first 0.2 s, as before."


def _render_clip_generator() -> None:
    st.caption(
        "Make clips from any YouTube link or video file, outside the worker. Nothing here is "
        "tracked as a pitch: the worker's pitches are in the 📬 Outbox."
    )
    pending_mode = st.session_state.pop("clip_mode_pending", None)
    if pending_mode in CLIP_MODES:  # "🎯 Make Shorts from it" after finishing a long video
        st.session_state["clip_mode"] = pending_mode
    mode = st.radio(
        "Mode", list(CLIP_MODES), key="clip_mode", horizontal=True,
        help="Highlights: the AI picks the best moments (3 to 7). Series: Part 1, Part 2, Part 3… "
        "covering the whole video in order, one batch of 3 at a time. Finish a long video: clean "
        "sound, a captions file, chapters and title ideas for your own long video. Quote grabber: "
        "one exact line from a podcast as a 16:9 clip for your slides.",
    )
    if CLIP_MODES[mode] == "series":
        _render_series_maker()
        return
    if CLIP_MODES[mode] == "finish":
        _render_long_video_finisher()
        return
    if CLIP_MODES[mode] == "quote":
        _render_quote_grabber()
        return
    st.session_state.setdefault("clip_count", HIGHLIGHT_COUNTS[0])
    st.session_state.setdefault("cap_59", True)
    st.session_state.setdefault("instant_title", True)
    count_column, cap_column = st.columns([3, 2], vertical_alignment="bottom")
    clip_count = int(count_column.select_slider(
        "How many clips", options=list(HIGHLIGHT_COUNTS), key="clip_count",
        help="3 for a creator's video. 5–7 for your own long video: about one Short per section.",
    ))
    cap = cap_column.checkbox(CAP_LABEL, key="cap_59", help=CAP_HELP)
    with st.form("clip_generator_form"):
        video_url = st.text_input(
            "YouTube link",
            placeholder="https://www.youtube.com/watch?v=...",
            autocomplete="off",
            key="video_url_input",
        )
        uploaded_video = st.file_uploader(
            "Or upload a video file",
            type=["mp4", "mov"],
            key=f"video_upload_{st.session_state.get('video_upload_epoch', 0)}",
        )
        local_path = st.text_input(
            "Or a video file on this Mac (its path)", key="video_path_input", placeholder=PATH_PLACEHOLDER,
            help=PATH_HELP,
        )
        channel_name = st.text_input(
            "Creator / channel name (optional)",
            placeholder="Found automatically for YouTube links",
            key="channel_name_input",
        )
        if st.session_state.get("crop_mode") not in LAYOUT_LABELS:
            st.session_state["crop_mode"] = next(iter(LAYOUT_LABELS))
        with st.expander("Framing (optional)"):
            layout_label = st.selectbox("Layout", list(LAYOUT_LABELS), key="crop_mode", help=WEBINAR_HELP)
            # Inside a form these can't appear only for the webinar layout, so they're always here.
            st.caption("**Webinar layout only:**")
            webcam_corner, webinar_options = _webinar_controls()
        pin_title = st.checkbox(PIN_TITLE_LABEL, key="pin_title", help=PIN_TITLE_HELP)
        instant_title = st.checkbox(INSTANT_TITLE_LABEL, key="instant_title", help=INSTANT_TITLE_HELP)
        generate_clicked = st.form_submit_button(f"Make {clip_count} vertical clips", type="primary")

    if generate_clicked:
        _make_clips(
            video_url, uploaded_video, channel_name, layout_label,
            webcam_corner=webcam_corner, webinar_options=webinar_options, pin_title=pin_title,
            clip_count=clip_count, max_seconds=CAP_SECONDS if cap else None, instant_title=instant_title,
            local_path=local_path,
        )
    _render_generated_clips()
    _render_manual_pages()


def _webinar_controls(prefix: str = "") -> tuple[str, dict[str, str]]:
    """Webinar layout settings: (webcam corner, {slides, divider, accent}). ``prefix``
    keeps the Series boxes apart from the 3-highlights ones."""
    defaults = {
        f"{prefix}webcam_corner": next(iter(WEBCAM_CORNERS)),
        f"{prefix}webinar_slides": next(iter(SLIDE_SHAPE_LABELS)),
        f"{prefix}webinar_divider": next(iter(DIVIDER_LABELS)),
    }
    for key, default in defaults.items():
        options = WEBCAM_CORNERS if key.endswith("corner") else SLIDE_SHAPE_LABELS if key.endswith("slides") else DIVIDER_LABELS
        if st.session_state.get(key) not in options:
            st.session_state[key] = default
    corner_column, slides_column = st.columns(2)
    corner = corner_column.selectbox(
        "Webcam corner", list(WEBCAM_CORNERS), key=f"{prefix}webcam_corner",
        help="Where the speaker's webcam is. Automatic finds the live webcam window next to the slides.",
    )
    slides = slides_column.selectbox(
        "Slides", list(SLIDE_SHAPE_LABELS), key=f"{prefix}webinar_slides",
        help="Auto shows the whole slide area. 16:9 or 4:3 crops it to exactly that shape, centered: "
        "slides are always scaled evenly, never stretched.",
    )
    divider_column, color_column = st.columns(2)
    divider = divider_column.selectbox(
        "Divider", list(DIVIDER_LABELS), key=f"{prefix}webinar_divider",
        help="Between the speaker and the slides.",
    )
    accent = color_column.color_picker(
        "Accent color (accent line)", value=webinar_defaults.DEFAULT_ACCENT, key=f"{prefix}webinar_accent",
    )
    return WEBCAM_CORNERS[corner], {
        "slides": SLIDE_SHAPE_LABELS[slides], "divider": DIVIDER_LABELS[divider], "accent": accent,
    }


def _make_clips(
    video_url: str,
    uploaded_video: Any,
    channel_name: str,
    layout_label: str,
    *,
    webcam_corner: str = "auto",
    webinar_options: dict[str, str] | None = None,
    pin_title: bool = False,
    clip_count: int = 3,
    max_seconds: float | None = None,
    instant_title: bool = False,
    local_path: str = "",
) -> None:
    try:
        _delete_previous_source_video()
    except OSError:
        logging.warning("Could not delete the previous source video", exc_info=True)
    for key in (
        "generated_clips",
        "clip_paths",
        "active_clip_run_id",
        "clip_render_contexts",
        "clip_source_path",
        "drive_folder_link",
        "outreach_pitch",
        "outreach_email_subject",
        "outreach_email_body",
        "outreach_video_title",
        "outreach_channel_name",
        "outreach_input_path",
        "outreach_channel_override",
        "outreach_title_override",
        "ai_clip_selection_error",
        "ai_outreach_email",
        "ai_outreach_subject",
        "agent_video_title",
        "agent_channel_name",
    ):
        st.session_state.pop(key, None)
    if sum((bool(video_url.strip()), uploaded_video is not None, bool(local_path.strip()))) != 1:
        st.error("Give one YouTube link, one uploaded file or one file path.")
        return

    uploaded_input: Path | None = None
    video_title_override = ""
    try:
        if uploaded_video is not None:
            settings = load_settings()
            suffix = Path(uploaded_video.name).suffix.lower()
            if suffix not in {".mp4", ".mov"}:
                raise ValueError("Uploaded files must be MP4 or MOV.")
            settings.temp_dir.mkdir(parents=True, exist_ok=True)
            video_title_override = Path(uploaded_video.name).stem
            uploaded_input = settings.temp_dir / f"upload_{uuid.uuid4().hex}{suffix}"
            uploaded_input.write_bytes(uploaded_video.getbuffer())
            input_path: str | Path = uploaded_input
        elif local_path.strip():
            input_path = _local_video(local_path)
            video_title_override = input_path.stem.removesuffix(" (finished)")
        else:
            input_value = video_url.strip()
            if not input_value.startswith(("http://", "https://")):
                raise ValueError("Enter a YouTube link starting with http:// or https://.")
            input_path = input_value

        st.session_state["outreach_channel_override"] = channel_name
        st.session_state["outreach_title_override"] = video_title_override
        generated = _generate_clips(
            input_path, clip_count, LAYOUT_LABELS[layout_label], webcam_corner=webcam_corner,
            webinar_options=webinar_options, pin_title=pin_title, max_seconds=max_seconds,
            title_fade_in=not instant_title,
        )
        st.session_state["generated_clips"] = generated
        st.session_state.pop("published_page", None)  # new clips: a new page
        st.session_state.pop("card_drafts", None)
        st.session_state.pop("card_clips", None)
        st.session_state["clip_paths"] = [str(item[0].expanduser().resolve()) for item in generated]
        if generated:
            st.session_state["outreach_input_path"] = str(input_path)
        if len(generated) == clip_count:
            st.success(f"All clips ready. Saved to {render_output_dir()}", icon="✅")
        elif generated:
            st.warning(f"Rendered {len(generated)} of {clip_count} clips. The ones that worked are below.")
        else:
            st.warning("No clips could be rendered. Check the errors above.")
    except Exception as exc:
        logging.exception("Video generation failed")
        st.session_state.pop("generated_clips", None)
        st.error(f"Making the clips failed: {exc}" + (f"\n\n{exc.hint}" if getattr(exc, "hint", "") else ""))
    finally:
        if uploaded_input is not None:
            uploaded_input.unlink(missing_ok=True)


def _local_video(text: str) -> Path:
    """A video file on this Mac, from a pasted path (quotes and a leading ~ are fine)."""
    path = Path(text.strip().strip("'\"")).expanduser()
    if not path.is_file():
        raise ValueError(f"There's no file at {path}. Copy its path again (Option + right-click → Copy as Pathname).")
    if path.suffix.lower() not in {".mp4", ".mov", ".m4v"}:
        raise ValueError("The file must be an MP4, MOV or M4V video.")
    return path.resolve()


# --- 🧾 Finish a long video (Make clips) ------------------------------------------------------
def _render_long_video_finisher() -> None:
    st.caption(
        "For your own long video (a Keynote or Canva export with your narration): the same video "
        "with clean, even sound (the picture isn't touched), a captions file for YouTube, and drafted "
        "chapters and title ideas. Then make the Shorts from it in one click."
    )
    upload = st.file_uploader("Upload the video", type=["mp4", "mov", "m4v"], key="finish_upload")
    path_text = st.text_input("Or its path on this Mac", key="finish_path", placeholder=PATH_PLACEHOLDER, help=PATH_HELP)
    if st.button("🧾 Finish it", type="primary", key="finish_go"):
        _finish_long_video(upload, path_text)
    result = st.session_state.get("finish_result")
    if not result:
        return
    st.success(f"Saved in {result['folder']}", icon="✅")
    for problem in result["problems"]:
        st.warning(problem)
    st.markdown(f"**Video:** `{result['video']}`")
    files = st.columns(2)
    files[0].download_button("⬇ Captions file (.srt)", Path(result["captions"]).read_bytes(),
                             file_name=Path(result["captions"]).name, key="finish_srt", width="stretch")
    files[1].download_button("⬇ Chapters and titles (.txt)", Path(result["notes"]).read_bytes(),
                             file_name=Path(result["notes"]).name, key="finish_notes", width="stretch")
    if result["titles"]:
        st.markdown("**Title ideas** (test them with Test & Compare in YouTube Studio):")
        for number, title in enumerate(result["titles"], start=1):
            st.markdown(f"{number}. {title}")
    if result["chapters"]:
        st.text_area("Chapters: paste into the description", result["chapters"], height=180, key="finish_chapters_text")
    if st.button("🎯 Make Shorts from it", key="finish_to_shorts", width="stretch",
                 help="Opens Highlights with this file, 5 clips and Auto layout (explainer for slide videos)."):
        st.session_state["clip_mode_pending"] = "🎯 Highlights"
        st.session_state["video_path_input"] = result["video"]
        st.session_state["video_url_input"] = ""
        st.session_state["clip_count"] = 5
        st.session_state["crop_mode"] = next(iter(LAYOUT_LABELS))
        st.rerun()


def _finish_long_video(upload: Any, path_text: str) -> None:
    if (upload is not None) == bool(path_text.strip()):
        st.error("Give one uploaded file or one file path.")
        return
    settings = load_settings()
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    uploaded: Path | None = None
    audio_path = settings.temp_dir / f"finish_{uuid.uuid4().hex}.wav"
    try:
        if upload is not None:
            source = settings.temp_dir / Path(upload.name).name
            source.write_bytes(upload.getbuffer())
            uploaded = source
        else:
            source = _local_video(path_text)
        with st.status("Finishing the video…", expanded=True) as status:
            duration = _video_duration(source)
            fingerprint = long_video.file_fingerprint(source)
            words = long_video.load_file_transcript(fingerprint)
            if words is None:
                st.write("Transcribing with Groq…")
                extract_audio_only(source, audio_path)
                words = transcribe_audio(audio_path, settings)
                long_video.save_file_transcript(fingerprint, words)
            else:
                st.write("Reusing this file's saved transcript (no Groq call).")
            st.write("Cleaning up the sound, writing captions, drafting chapters and titles…")
            folder = render_output_dir() / "Long videos" / _slug(source.stem.removesuffix(" (finished)"))
            finished = long_video.finish_long_video(source, folder, words, duration)
            # The finished file has the same words at the same times: no Groq call for the Shorts.
            long_video.save_file_transcript(long_video.file_fingerprint(finished.video), words)
            status.update(label="Finished", state="complete")
        st.session_state["finish_result"] = {
            "folder": str(folder), "video": str(finished.video), "captions": str(finished.captions),
            "notes": str(finished.notes), "titles": finished.titles,
            "chapters": long_video.chapters_text(finished.chapters), "problems": finished.problems,
        }
    except Exception as exc:
        logging.exception("Finishing the long video failed")
        st.error(f"Couldn't finish it: {exc}")
    finally:
        audio_path.unlink(missing_ok=True)
        if uploaded is not None:
            uploaded.unlink(missing_ok=True)


# --- ✂️ Quote grabber (Make clips) --------------------------------------------------------------
def _render_quote_grabber() -> None:
    st.caption(
        "One exact line from a podcast or interview, as a 16:9 clip to put on a slide. Search its "
        "captions, then only that part is downloaded and transcribed. Keep quotes short (under 15 s) "
        "and credit the show on the slide."
    )
    url = st.text_input("YouTube link", key="quote_url", placeholder="https://www.youtube.com/watch?v=...").strip()
    info = st.session_state.get("quote_info")
    if info and info.get("url") != url:
        for key in ("quote_info", "quote_section", "quote_saved"):
            st.session_state.pop(key, None)
        info = None
    if url and info is None and st.button("🔎 Read its captions", key="quote_find"):
        try:
            with st.spinner("Reading the captions (nothing is downloaded)…"):
                details, lines = youtube.fetch_captions(url)
        except Exception as exc:
            logging.exception("Could not read the captions")
            st.error(f"Couldn't read this video: {exc}" + (f"\n\n{exc.hint}" if getattr(exc, "hint", "") else ""))
            return
        info = {"url": url, **details, "lines": [list(line) for line in lines]}
        st.session_state["quote_info"] = info
    if not info:
        return
    st.markdown(f"**{info.get('title') or 'Video'}** · {info.get('channel') or ''}")
    lines = [tuple(line) for line in info["lines"]]
    picked: tuple[float, float] | None = None
    if lines:
        query = st.text_input("Search the captions", key="quote_query", placeholder="a few words of the line")
        found = quotes.search_captions(lines, query) if query.strip() else []
        if query.strip() and not found:
            st.info("No caption line has all of those words. Try fewer or different words, or type the time below.")
        if found:
            labels = [f"{long_video.timestamp(start)} · {text[:110]}" for start, _end, text in found]
            choice = st.radio("Lines found", labels, key="quote_pick")
            start, end, _text = found[labels.index(choice)]
            picked = (start, end)
    else:
        st.info("This video has no English captions. Type the time of the line instead (from YouTube's player).")
    time_text = st.text_input("Or the time in the video (mm:ss)", key="quote_time", placeholder="12:05")
    if time_text.strip():
        seconds = quotes.parse_time(time_text)
        if seconds is None:
            st.error("Write the time as mm:ss (or h:mm:ss), like 12:05.")
        else:
            picked = (seconds, seconds + 10.0)
    if picked and st.button("⬇ Get this part", key="quote_get", type="primary"):
        _grab_quote_section(info, picked)
    section = st.session_state.get("quote_section")
    if not section:
        return
    words = [Word(*row) for row in section["words"]]
    if not words:
        st.warning("No speech was found in that part. Pick another line or time.")
        return
    labels = [f"{long_video.timestamp(section['start'] + w.start)} {w.text}" for w in words]
    first, last = section["pick"]
    chosen = st.select_slider("The quote: drag to its first and last word", options=list(range(len(words))),
                              value=(first, last), format_func=lambda i: labels[i], key="quote_words")
    a, b = chosen
    length = words[b].end - words[a].start + quotes.PRE_ROLL + quotes.POST_ROLL
    st.markdown(f"> {' '.join(w.text for w in words[a:b + 1])}")
    st.caption(f"{length:.1f} s" + (" · long for a quote: under 15 s keeps it a quote" if length > 15 else ""))
    if st.button("✂️ Save quote", key="quote_save", type="primary"):
        name = f"{_slug(str(info.get('title') or 'quote'))[:40]}-{long_video.timestamp(section['start'] + words[a].start).replace(':', '')}.mp4"
        try:
            with st.spinner("Cutting the quote…"):
                saved = quotes.cut_quote(Path(section["path"]), words[a].start, words[b].end, render_output_dir() / "Quotes" / name)
        except Exception as exc:
            logging.exception("Could not cut the quote")
            st.error(f"Couldn't cut it: {exc}")
            return
        st.session_state["quote_saved"] = str(saved)
    saved = st.session_state.get("quote_saved")
    if saved and Path(saved).is_file():
        st.success(f"Saved to {saved}", icon="✅")
        st.video(saved)
        st.download_button("⬇ Download the quote", Path(saved).read_bytes(), file_name=Path(saved).name, key="quote_download")


def _grab_quote_section(info: dict[str, Any], picked: tuple[float, float]) -> None:
    settings = load_settings()
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    start, end = quotes.section_for(picked[0], picked[1], info.get("duration"))
    section = settings.temp_dir / f"quote_{uuid.uuid4().hex}.mp4"
    audio = section.with_suffix(".wav")
    try:
        with st.spinner(f"Downloading {end - start:.0f} s and transcribing them…"):
            youtube.download_section(info["url"], start, end, section)
            extract_audio_only(section, audio)
            words = transcribe_audio(audio, settings)
    except Exception as exc:
        logging.exception("Could not get the quote's part")
        section.unlink(missing_ok=True)
        st.error(f"Couldn't get that part: {exc}" + (f"\n\n{exc.hint}" if getattr(exc, "hint", "") else ""))
        return
    finally:
        audio.unlink(missing_ok=True)
    old = st.session_state.get("quote_section")
    if old:
        Path(old["path"]).unlink(missing_ok=True)
    first, last = quotes.words_near(words, picked[0] - start, picked[1] - start)
    st.session_state["quote_section"] = {
        "path": str(section), "start": start, "end": end,
        "words": [[w.text, w.start, w.end] for w in words], "pick": (first, last),
    }
    st.session_state.pop("quote_words", None)
    st.session_state.pop("quote_saved", None)


# --- 🎞️ Series mode (Make clips): Part 1, 2, 3… in order -------------------------------------
def _render_series_maker() -> None:
    st.caption(
        "Cuts the whole video into parts in order. Each part ends on a finished sentence and the "
        "next one starts right there, so nothing that's said is left out. Each batch makes 3 parts "
        "with **Part N: title** on the title card. They're saved on this Mac only: no Drive, no clip page."
    )
    url = st.text_input("YouTube link", key="series_url", placeholder="https://www.youtube.com/watch?v=...").strip()
    upload = st.file_uploader("Or upload a video file", type=["mp4", "mov"], key="series_upload")
    if url and upload is not None:
        st.error("Give either a YouTube link or one video file, not both.")
        return
    key = None
    if url:
        key = series.series_key(url=url)
    elif upload is not None:
        key = series.series_key(file_name=upload.name, file_head=bytes(upload.getbuffer()[:1_000_000]), file_size=upload.size)
    state = series.load_series(key) if key else None

    # A new video: fill in what was used for it last time (title, part length, next batch).
    if key and st.session_state.get("series_key") != key:
        st.session_state["series_key"] = key
        lengths = (int(state["shortest"]), int(state["longest"])) if state and state.get("shortest") else series.DEFAULT_LENGTHS
        st.session_state["series_title"] = str((state or {}).get("title") or "")
        st.session_state["series_lengths"] = lengths
        st.session_state["series_batch"] = series.next_batch(state, *lengths) if state and state.get("words") else 1
    if "series_batch_pending" in st.session_state:  # after a batch: suggest the next one
        st.session_state["series_batch"] = st.session_state.pop("series_batch_pending")
    if "series_title_pending" in st.session_state:  # the title found on the first batch
        st.session_state["series_title"] = st.session_state.pop("series_title_pending")
    note = st.session_state.pop("series_toast", None)
    if note:
        st.toast(note, icon="✅")
    st.session_state.setdefault("series_lengths", series.DEFAULT_LENGTHS)
    st.session_state.setdefault("series_batch", 1)

    st.text_input(
        "Title on every part", key="series_title", max_chars=series.MAX_TITLE_CHARS,
        placeholder="The video's title (found automatically)",
        help="Each part's title card reads 'Part N: <this title>'.",
    )
    shortest, longest = st.slider(
        "Part length (seconds)", min_value=series.MIN_PART_SECONDS, max_value=series.MAX_PART_SECONDS,
        step=5, key="series_lengths",
        help="Each part ends at the first sentence end after the shorter length, and never runs past "
        "the longer one. Only the very last part of the video can be shorter.",
    )
    batch = int(st.number_input(
        "Batch", min_value=1, step=1, key="series_batch",
        help="Batch 1 = Parts 1–3, batch 2 = Parts 4–6, and so on: run them one after another to cover the whole video.",
    ))
    first, last = (batch - 1) * series.BATCH_SIZE + 1, batch * series.BATCH_SIZE
    can_make = bool(key)
    if state and state.get("words"):
        parts = series.plan_parts(series.rows_to_words(state["words"]), float(state["duration"]), shortest, longest)
        count = series.batch_count(parts)
        if (state.get("shortest"), state.get("longest")) not in {(None, None), (shortest, longest)}:
            st.warning(
                f"Earlier parts of this video were {state['shortest']}–{state['longest']} s. A different length "
                "renumbers the parts, so the series won't line up with the ones you made."
            )
        if batch > count:
            st.warning(f"This video has {len(parts)} parts, so batches 1 to {count}.")
            can_make = False
        else:
            chosen = series.batch_parts(parts, batch)
            last = chosen[-1][0]
            made = sorted(int(n) for n in state.get("made") or [])
            made_text = f" Made so far: {_part_ranges(made)}." if made and (state.get("shortest"), state.get("longest")) == (shortest, longest) else ""
            st.caption(
                f"Batch {batch} of {count}: Parts {first}–{last}, {_clock(chosen[0][1])}–{_clock(chosen[-1][2])} "
                f"of {_clock(float(state['duration']))}.{made_text}"
            )
    elif key:
        st.caption(f"Batch {batch} = Parts {first}–{last}. The whole video is transcribed once, on the first batch.")
    if st.session_state.get("series_layout") not in LAYOUT_LABELS:
        st.session_state["series_layout"] = next(iter(LAYOUT_LABELS))
    corner, webinar_options = "auto", None
    with st.expander("Framing (optional)"):
        layout_label = st.selectbox("Layout", list(LAYOUT_LABELS), key="series_layout", help=WEBINAR_HELP)
        if LAYOUT_LABELS[layout_label] == "webinar":
            corner, webinar_options = _webinar_controls("series_")
    pin_title = st.checkbox(PIN_TITLE_LABEL, key="series_pin_title", help=PIN_TITLE_HELP)
    if st.button(f"Make parts {first}–{last}", type="primary", disabled=not can_make):
        _make_series_batch(
            key, url, upload, st.session_state.get("series_title", ""), shortest, longest, batch,
            LAYOUT_LABELS[layout_label], webcam_corner=corner, webinar_options=webinar_options, pin_title=pin_title,
        )
    _render_series_results()


def _part_ranges(numbers: list[int]) -> str:
    """[1, 2, 3, 4, 5, 6, 10, 11, 12] -> 'Parts 1–6, 10–12'."""
    ranges: list[list[int]] = []
    for number in numbers:
        if ranges and number == ranges[-1][1] + 1:
            ranges[-1][1] = number
        else:
            ranges.append([number, number])
    return "Parts " + ", ".join(f"{a}–{b}" if a != b else str(a) for a, b in ranges)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "series"


def _make_series_batch(
    key: str, url: str, upload: Any, title_input: str, shortest: int, longest: int, batch: int, layout_mode: str,
    *, webcam_corner: str = "auto", webinar_options: dict[str, str] | None = None, pin_title: bool = False,
) -> None:
    """Transcribe once (saved per video), plan the parts, render this batch's 3 locally."""
    settings = load_settings()
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    state = series.load_series(key) or {"key": key, "made": []}
    progress = st.progress(0.0, text="Starting…")
    audio_path = settings.temp_dir / f"audio_{uuid.uuid4().hex}.wav"
    all_made = False
    try:
        source_video: Path | None = None
        if upload is not None:
            suffix = Path(upload.name).suffix.lower()
            if suffix not in {".mp4", ".mov"}:
                raise ValueError("Uploaded files must be MP4 or MOV.")
            source_video = settings.raw_dir / f"series_{key}{suffix}"
            if not source_video.is_file():
                source_video.write_bytes(upload.getbuffer())
        if not state.get("words"):
            progress.progress(0.05, text="Transcribing the whole video (once per video)…")
            if url:
                download_audio_only(url, audio_path)
                duration = _audio_duration(audio_path)
            else:
                extract_audio_only(source_video, audio_path)
                duration = _video_duration(source_video)
            words = transcribe_audio(audio_path, settings)
            if not words:
                raise RuntimeError("No speech was found in this video.")
            video_title = Path(upload.name).stem if upload is not None else ""
            if url:
                try:
                    video_title = str(fetch_video_info(url).get("title") or "")
                except Exception:
                    logging.warning("Could not read the video title", exc_info=True)
            state.update(source=url or upload.name, title=series.clean_series_title(video_title),
                         duration=duration, words=series.words_to_rows(words))
            series.save_series(key, state)  # a failed render later doesn't lose the transcript
        words = series.rows_to_words(state["words"])
        title = series.clean_series_title(title_input) or str(state.get("title") or "") or "Series"
        parts = series.plan_parts(words, float(state["duration"]), shortest, longest)
        chosen = series.batch_parts(parts, batch)
        if (state.get("shortest"), state.get("longest")) != (shortest, longest):
            state["made"] = []  # new part lengths: new numbering
        progress.progress(0.35, text="Getting the video…")
        if url:
            source_video = Path(download_source_video(url))  # kept for a day, so the next batch reuses it
        if source_video is None:
            raise RuntimeError("No source video is available for rendering.")
        folder = render_output_dir() / "Series" / f"{_slug(title)}-{key[-6:]}"
        folder.mkdir(parents=True, exist_ok=True)

        def render_part(number: int, start: float, end: float) -> dict[str, Any]:
            clip = ClipResult(start_time=start, end_time=end, viral_title=series.part_title(number, title),
                              caption="", hashtags=[], virality_score=0)
            output = folder / f"part_{number:02d}.mp4"
            render_selected_clip(
                source_video, clip, words, settings, output,
                layout_mode=layout_mode, pin_title=pin_title, webinar_corner=webcam_corner,
                webinar_options=webinar_options, allow_explainer=layout_mode == "auto",
            )
            preview = settings.temp_dir / f"series_{key}_part_{number:02d}.preview.mp4"
            make_preview(output, preview)  # small copy for the player (the full file is ~10 MB a minute)
            return {"path": str(output), "preview": str(preview), "number": number, "start": start, "end": end}

        items: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(render_part, *part): part[0] for part in chosen}
            for done, future in enumerate(as_completed(futures), start=1):
                try:
                    items.append(future.result())
                except Exception as exc:
                    logging.exception("Part %d failed", futures[future])
                    st.warning(f"Part {futures[future]} failed to render: {exc}")
                progress.progress(0.35 + 0.65 * done / len(chosen), text=f"Rendered {done} of {len(chosen)} parts")
        items.sort(key=lambda item: item["number"])
        state.update(title=title, shortest=shortest, longest=longest,
                     made=sorted(set(int(n) for n in state.get("made") or []) | {item["number"] for item in items}))
        series.save_series(key, state)
        st.session_state["series_results"] = {"title": title, "folder": str(folder), "items": items, "total": len(parts)}
        st.session_state["series_batch_pending"] = min(batch + 1, series.batch_count(parts))
        st.session_state["series_title_pending"] = title
        progress.progress(1.0, text="Done")
        all_made = len(items) == len(chosen)
        if all_made:
            st.session_state["series_toast"] = f"Parts {chosen[0][0]}–{chosen[-1][0]} are ready (of {len(parts)})."
    except Exception as exc:
        logging.exception("Series batch failed")
        st.error(f"Making the parts failed: {exc}" + (f"\n\n{exc.hint}" if getattr(exc, "hint", "") else ""))
    finally:
        audio_path.unlink(missing_ok=True)
    if all_made:
        st.rerun()  # the form now shows the real part times and suggests the next batch


def _render_series_results() -> None:
    result = st.session_state.get("series_results")
    items = [item for item in (result or {}).get("items", []) if Path(item["path"]).is_file()]
    if not items:
        return
    slug = _slug(result["title"])
    st.subheader(f"{result['title']}: Parts {items[0]['number']}–{items[-1]['number']} of {result['total']}")
    names = [f"{slug}_part_{item['number']:02d}.mp4" for item in items]
    for column, item, name in zip(st.columns(series.BATCH_SIZE), items, names):
        with column:
            with st.container(key=f"preview_series_{item['number']}"):
                if Path(item["preview"]).is_file():
                    st.video(item["preview"], width=PREVIEW_PLAYER_WIDTH)
                else:
                    st.caption("Preview expired: the file is in the folder below.")
            st.markdown(f"**Part {item['number']}** · {_clock(item['start'])}–{_clock(item['end'])} · {item['end'] - item['start']:.0f} s")
            st.download_button(
                f"⬇ Download Part {item['number']}", data=functools.partial(Path(item["path"]).read_bytes),
                file_name=name, mime="video/mp4", key=f"series_download_{item['number']}", width="stretch",
            )
    st.caption(f"Also saved on this Mac: {result['folder']}")


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}" if total >= 3600 else f"{total // 60}:{total % 60:02d}"


def _render_generated_clips() -> None:
    generated_clips = st.session_state.get("generated_clips", [])
    if not generated_clips:
        return
    settings = load_settings()
    st.subheader("Your vertical clips")
    columns = st.columns(len(generated_clips))
    for index, (clip_path, clip) in enumerate(generated_clips):
        with columns[index]:
            st.markdown(f"**{clip.viral_title or f'Clip {index + 1}'}**")
            st.caption(f"{_clock(clip.start_time)}–{_clock(clip.end_time)} in the video")
            clip_path = Path(clip_path).expanduser().resolve()
            if clip_path.is_file() and clip_path.stat().st_size > 0:
                st.video(str(clip_path))
                # Read the file only when the button is clicked, not on every rerun.
                st.download_button(
                    "Download clip",
                    data=functools.partial(clip_path.read_bytes),
                    file_name=clip_path.name,
                    mime="video/mp4",
                    key=f"download_{clip_path.name}",
                    on_click="ignore",
                    use_container_width=True,
                )
            else:
                st.warning(f"Clip file not found: {clip_path.name}. Make the clips again.")
            if clip.caption:
                with st.expander("Why this moment"):
                    st.write(clip.caption)
                    if clip.hashtags:
                        st.markdown(" ".join(f"#{tag.lstrip('#')}" for tag in clip.hashtags))
            _render_trim_form(index, clip_path, clip, generated_clips, settings)

    _render_data_cards(generated_clips)
    _render_publish_page(generated_clips)

    if st.session_state.get("drive_folder_link"):
        st.info("These clips are in Google Drive. Discarding only removes the copies on this Mac.")
    approve_column, discard_column = st.columns(2)
    with approve_column:
        approve_upload = st.button(
            "✅ Upload to Google Drive",
            disabled=bool(st.session_state.get("drive_folder_link")),
            use_container_width=True,
        )
    with discard_column:
        discard_clips = st.button("🗑️ Discard these clips", use_container_width=True)

    if discard_clips:
        try:
            _discard_generated_clips()
        except (OSError, ValueError) as exc:
            logging.exception("Could not discard generated clips")
            st.error(f"Could not discard the clips: {exc}")
        else:
            st.success("The clips were discarded.")
            st.rerun()

    if approve_upload:
        _upload_generated_clips(generated_clips)

    drive_folder_link = st.session_state.get("drive_folder_link")
    if drive_folder_link:
        st.link_button("📁 Open the Google Drive folder", drive_folder_link)

    if st.session_state.get("ai_outreach_email") or st.session_state.get("outreach_pitch"):
        with st.expander("Email draft for this creator", expanded=bool(drive_folder_link)):
            if st.session_state.get("outreach_email_subject"):
                st.text_input("Subject", key="outreach_email_subject")
            st.text_area("Email", height=300, key="outreach_email_body")
            _render_copy_email_button(st.session_state.get("outreach_email_body", ""))
            if not drive_folder_link:
                st.caption("The Drive link is added after you upload the clips.")


# Round 27: what each graphic needs. "fields": the item columns shown (CardRow field -> header).
CARD_GUIDE: dict[str, tuple[str, dict[str, str]]] = {
    "kinetic_range": ("One number (or range) exactly as said, e.g. 96 hrs or ~$11–12T, and an optional short line "
                      "under it in the speaker's words.",
                      {"value": "Number as said", "label": "Line under it (optional)"}),
    "buckets": ("2–3 short boxes. Each appears when one of its words is said. Tick 'Dashed' for an expectation "
                "or projection, next to solid boxes for what happened.",
                {"label": "Box text", "dashed": "Dashed", "highlight": "Gold"}),
    "speaker_title": ("", {}),
    "timeline": ("2–4 points in time, in the order they come up. A point said earlier in the clip is drawn in "
                 "as a recap.", {"value": "When (as said)", "detail": "What (speaker's words)", "highlight": "Focus"}),
    "metric_bars": ("2–4 numbers in one unit. Each bar grows as its number is said.",
                    {"label": "Label", "value": "Number as said", "highlight": "Focus"}),
    "ranked_bars": ("2–5 numbers in one unit, largest first. The focus moves to each as it's said.",
                    {"label": "Label", "value": "Number as said", "highlight": "Focus"}),
    "diverging_bar": ("Two numbers that pull against each other (e.g. buyers vs sellers).",
                      {"label": "Label", "value": "Number as said", "highlight": "Focus"}),
    "share_split": ("3–5 shares of one whole. Percentages must add up to 100%.",
                    {"label": "Label", "value": "Share as said", "highlight": "Focus"}),
    "trend_line": ("2–6 values over time, oldest first. Keep the time labels short (2024, Q3 '25).",
                   {"label": "When", "value": "Number as said", "highlight": "Focus"}),
    "arr_bridge": ("First row the start, last row the end, and + or − steps between "
                   "(e.g. +$3.2M new, −$0.4M churn). It must add up.",
                   {"label": "Label", "value": "Number as said (+/−)", "highlight": "Focus"}),
    "sankey_flow": ("One starting row (e.g. Revenue), then rows that flow out of it. Each split must add up to "
                    "the row it flows from.", {"label": "Label", "value": "Number as said", "parent": "Flows from"}),
    "cac_payback": ("Row 1: what it costs to win a customer. Row 2: the months to earn it back (e.g. 12 months).",
                    {"label": "Label", "value": "Number as said"}),
    "candlestick_ema": ("Drawn from a price file you upload (CSV: date, open, high, low, close, volume), not from "
                        "speech. The chart shows its source.", {}),
}
CARD_ROWS = {"kinetic_range": 1, "buckets": 3, "timeline": 3, "metric_bars": 3, "ranked_bars": 3,
             "diverging_bar": 2, "share_split": 3, "trend_line": 4, "arr_bridge": 4, "sankey_flow": 4,
             "cac_payback": 2}
NAME_CARD_SECONDS = 3.3


def _card_clip_path(path: str | Path) -> Path | None:
    """The version of this clip with graphics, if you made one."""
    made = st.session_state.get("card_clips", {}).get(_clip_key(path))
    return Path(made) if made and Path(made).is_file() else None


def _render_brand_kit() -> None:
    brand = load_brand_kit()
    with st.expander(f"🎨 Accent color: {brand.accent}"):
        with st.form("brand_kit_form", border=False):
            accent = st.color_picker("The one highlight color (the client's brand color works well)", value=brand.accent)
            save = st.form_submit_button("Save")
        if save:
            if contrast(accent, CARD_CANVAS) < MIN_CONTRAST:
                st.error(f"{accent} is too dark to read on the dark background (contrast "
                         f"{contrast(accent, CARD_CANVAS):.1f}, needs {MIN_CONTRAST}). Pick a lighter color.")
                return
            save_brand_kit(BrandKit(wordmark=brand.wordmark, accent=accent, logo_path=brand.logo_path,
                                    show_name=brand.show_name))
            st.success("Saved. New graphics use it.")
            st.rerun()


def _card_summary(draft: dict[str, Any]) -> str:
    rows = draft.get("rows") or []
    if draft["template"] == "speaker_title":
        return rows[0]["label"] if rows else ""
    if draft["template"] == "candlestick_ema":
        return f"Source: {draft.get('source', '')}"
    return ", ".join((row.get("value") or row.get("label") or "")[:24] for row in rows)


def _new_card(template: str, key: str, clip: Any, spans: list[tuple[float, float, str]], hook: float) -> Card | None:
    """The inputs for one new graphic; returns the card when 'Check and add' is pressed."""
    if template == "speaker_title":
        name_col, role_col = st.columns(2)
        name = name_col.text_input("Name", max_chars=CARD_LIMITS["name"], key=f"name_{key}")
        role = role_col.text_input("Role, company", max_chars=CARD_LIMITS["role"], key=f"role_{key}")
        default = round(hook + 0.2, 1)
        start = st.number_input("Comes in at (seconds into the clip)", min_value=default,
                                max_value=max(default, round(clip.end_time - clip.start_time - 6.0, 1)),
                                value=default, step=0.5, key=f"name_at_{key}",
                                help=f"On screen for {NAME_CARD_SECONDS:g} s, then slides away. "
                                     "Best just after the title, when the guest starts speaking.")
        if not st.button("✔ Check and add", key=f"card_add_{key}"):
            return None
        at = clip.start_time + float(start)
        return Card("speaker_title", start=at, end=at + NAME_CARD_SECONDS,
                    rows=[CardRow(name.strip(), detail=role.strip())])
    label = lambda i: f"{_clock(spans[i][0] - clip.start_time)}  {spans[i][2][:70]}"  # noqa: E731
    first_col, last_col = st.columns(2)
    first = first_col.selectbox("Starts on", range(len(spans)), format_func=label, key=f"card_first_{key}")
    last = last_col.selectbox("…and ends after", range(len(spans)), format_func=label, key=f"card_last_{key}",
                              index=min(len(spans) - 1, first + 1))
    headline = ""
    if MODES[template] == "split":
        headline = st.text_input("Title (optional, a few words)", max_chars=CARD_LIMITS["headline"],
                                 key=f"card_title_{key}_{template}")
    rows: list[CardRow] = []
    source, data_path = "", ""
    if template == "candlestick_ema":
        prices = st.file_uploader("Price file (CSV)", type=["csv"], key=f"card_prices_{key}")
        source = st.text_input("Source (shown on the chart)", max_chars=40, key=f"card_source_{key}")
        if prices is not None:
            target = BRAND_KIT_PATH.parent / "price_files" / f"{hashlib.sha1(prices.getvalue()).hexdigest()[:12]}.csv"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(prices.getvalue())
            data_path = str(target)
    else:
        fields = CARD_GUIDE[template][1]
        blank = {field: (False if field in {"highlight", "dashed"} else "") for field in fields}
        config = {
            field: (st.column_config.CheckboxColumn(header, width="small") if field in {"highlight", "dashed"}
                    else st.column_config.TextColumn(header, max_chars={
                        "value": CARD_LIMITS["value"], "detail": CARD_LIMITS["detail"], "parent": CARD_LIMITS["label"],
                        "label": CARD_LIMITS["box" if template == "buckets" else
                                             "context" if template == "kinetic_range" else "label"],
                    }[field]))
            for field, header in fields.items()
        }
        edited = st.data_editor([dict(blank) for _ in range(CARD_ROWS[template])], column_config=config,
                                num_rows="fixed" if template in {"kinetic_range", "diverging_bar", "cac_payback"}
                                else "dynamic", hide_index=True, use_container_width=True,
                                key=f"card_rows_{key}_{template}")
        for item in edited:
            values = {field: (bool(item.get(field)) if field in {"highlight", "dashed"}
                              else str(item.get(field) or "").strip()) for field in fields}
            if values.get("label") or values.get("value") or values.get("detail"):
                rows.append(CardRow(**values))
    if not st.button("✔ Check and add", key=f"card_add_{key}"):
        return None
    return Card(template, start=spans[first][0], end=spans[max(first, last)][1] + 0.15, headline=headline.strip(),
                rows=rows, source=source.strip(), data_path=data_path)


def _render_data_cards(generated_clips: list[tuple[Any, Any]]) -> None:
    """📊 Graphics (optional): a number, boxes, a name card or one chart, each checked against
    the transcript and shown on the word it's said. Never part of picking the clips."""
    contexts = st.session_state.get("clip_render_contexts", {})
    choices = [k for k, (path, _) in enumerate(generated_clips) if _clip_key(path) in contexts]
    st.subheader("📊 Graphics")
    st.caption(
        "Optional, for the moments where a number or a name carries the point. Every number is checked against "
        "the transcript and appears on the word it's said. At most 1 chart, 2 numbers or boxes and 1 name card "
        "per clip."
    )
    if not choices:
        st.caption("Make clips first.")
        return
    settings = load_settings()
    hook = max(3.0, float(settings.hook_title_seconds))
    _render_brand_kit()
    index = st.selectbox(
        "Clip", choices, key="cards_clip",
        format_func=lambda k: f"Clip {k + 1}: {(generated_clips[k][1].viral_title or '')[:60]}",
    )
    path, clip = generated_clips[index]
    key = _clip_key(path)
    context = contexts[key]
    words = [w for w in context["words"] if clip.start_time <= float(w.start) < clip.end_time]
    spans = edl_sentences(words)
    if not spans:
        st.caption("No words found in this clip.")
        return
    drafts: list[dict[str, Any]] = st.session_state.setdefault("card_drafts", {}).setdefault(key, [])
    for number, draft in enumerate(drafts):
        left, right = st.columns([5, 1])
        left.markdown(f"**{number + 1}.** {TEMPLATE_LABELS[draft['template']]} · "
                      f"{_clock(draft['start'] - clip.start_time)}–{_clock(draft['end'] - clip.start_time)} · "
                      f"{_card_summary(draft)}")
        if right.button("Remove", key=f"card_remove_{key}_{number}"):
            drafts.pop(number)
            st.session_state.get("card_clips", {}).pop(key, None)
            st.rerun()
    with st.expander("➕ Add a graphic", expanded=not drafts):
        template = st.selectbox("Kind", TEMPLATES, format_func=TEMPLATE_LABELS.get, key=f"card_template_{key}")
        if CARD_GUIDE[template][0]:
            st.caption(CARD_GUIDE[template][0])
        card = _new_card(template, key, clip, spans, hook)
        if card is not None:
            edit = ClipEdit(start=clip.start_time, end=clip.end_time, cards=[Card(**d) for d in drafts] + [card])
            result = check_edit(edit, words, hook)[-1]
            for warning in result.warnings:
                st.warning(warning)
            if not result.ok:
                st.error("Not added. Fix this first:\n\n" + "\n".join(f"- {e}" for e in result.errors))
            else:
                drafts.append(asdict(edit.cards[-1]))
                drafts.sort(key=lambda d: d["start"])
                st.session_state.get("card_clips", {}).pop(key, None)
                for field_key in [k for k in st.session_state if str(k).startswith(("card_rows_", "card_title_"))]:
                    del st.session_state[field_key]
                st.rerun()
    if drafts and st.button("🎬 Render this clip with its graphics", type="primary", key=f"card_render_{key}"):
        edit = ClipEdit(start=clip.start_time, end=clip.end_time, cards=[Card(**draft) for draft in drafts],
                        brand=load_brand_kit())
        output = Path(path).with_name(Path(path).stem + "_graphics.mp4")
        work_dir = settings.temp_dir / "graphics" / Path(path).stem
        cached_crop = context.get("crop")

        def render_base(kept_words: list[Any]) -> Path:
            # Same framing as the clip; no bold captions while a graphic is up; the title
            # shows for the opening only, so it never sits under a graphic.
            return Path(rerender_clip(
                source_path=str(context["source_path"]), new_start=clip.start_time, new_end=clip.end_time,
                transcript_words=kept_words, output_path=work_dir / "base.mp4",
                cached_crop_x=cached_crop.x if isinstance(cached_crop, CropBox) else None,
                cached_crop=cached_crop if isinstance(cached_crop, CropBox) else None,
                layout_mode=str(context["layout_mode"]), source_duration=context.get("source_duration"),
                crop_plan=context.get("crop_plan"), hook_title=clip.viral_title, hook_seconds=settings.hook_title_seconds,
                highlight_active_word=settings.caption_highlight, pin_title=False, webinar=context.get("webinar"),
                title_fade_in=bool(context.get("title_fade_in", True)),
            ))

        with st.spinner("Drawing the graphics and adding them to the clip…"):
            try:
                work_dir.mkdir(parents=True, exist_ok=True)
                made, checks = render_with_cards(Path(context["source_path"]), edit, words, output, work_dir,
                                                 render_base, hook_seconds=hook)
            except ModuleNotFoundError:
                st.error("Graphics need Playwright. In Terminal: .venv/bin/pip install playwright && "
                         ".venv/bin/playwright install chromium")
                return
            except Exception as exc:
                logging.exception("Graphics failed")
                st.error(f"Couldn't add the graphics: {exc}")
                return
        problems = [f"Graphic {n + 1}: {e}" for n, c in enumerate(checks) for e in c.errors]
        if problems:
            st.error("Left out:\n\n" + "\n".join(f"- {p}" for p in problems))
        if made:
            st.session_state.setdefault("card_clips", {})[key] = str(made)
            st.session_state.pop("published_page", None)  # the page should show the new version
            st.rerun()
    made = _card_clip_path(path)
    if made:
        st.video(str(made))
        st.download_button("Download the version with graphics", data=functools.partial(made.read_bytes),
                           file_name=made.name, mime="video/mp4", key=f"card_download_{key}", on_click="ignore")
        st.caption("A published preview page uses this version for this clip.")


def _render_publish_page(generated_clips: list[tuple[Any, Any]]) -> None:
    """🌐 Publish the clips to a client preview page: the same R2 page as the worker's
    pitches, with the same open/play/download tracking."""
    st.subheader("🌐 Client preview page")
    if not clip_pages.configured():
        st.caption("Clip pages aren't set up (R2 settings in .env). Setup: claude_review/round16/README.md")
        return
    published = st.session_state.get("published_page")
    if published:
        st.success(f"Published: {published['page_url']}", icon="🌐")
        copy_column, open_column = st.columns(2)
        with copy_column:
            _render_copy_email_button(published["page_url"], label="📋 Copy Preview Link")
        with open_column:
            st.link_button("Open the page", published["page_url"], width="stretch")
        st.caption(
            "Opens, plays and downloads show under 'Published preview pages' below. "
            "Trimmed a clip after publishing? Publish again for a new page."
        )
        if st.button("Publish again (new address)", key="publish_again"):
            st.session_state.pop("published_page", None)
            st.rerun()
        return
    clips = [
        {"path": str(_card_clip_path(path) or Path(path).expanduser().resolve()), "start": clip.start_time,
         "end": clip.end_time}
        for path, clip in generated_clips
        if Path(path).expanduser().is_file()
    ][:9]
    channel_default = str(
        st.session_state.get("agent_channel_name") or st.session_state.get("outreach_channel_override") or ""
    )
    title_default = str(
        st.session_state.get("agent_video_title") or st.session_state.get("outreach_title_override") or ""
    )
    with st.form("publish_page_form", border=False):
        name_column, title_column = st.columns(2)
        channel = name_column.text_input("Client / channel name", value=channel_default)
        title = title_column.text_input("Episode title", value=title_default)
        slug = st.text_input(
            "Custom slug / prospect name (optional)",
            placeholder="e.g. dana-growth",
            help="Words for the address. A random 6-character end is always added, so no one can guess "
            "another page's address and no page is ever overwritten. Empty: the channel name is used.",
        )
        episode = st.toggle("Podcast or interview (the page says 'episode')", value=True)
        publish = st.form_submit_button(
            "🌐 Publish to Client Preview Page", type="primary", use_container_width=True, disabled=not clips,
        )
    if not publish:
        return
    if not channel.strip():
        st.error("Add the client or channel name first: the page greets them by it.")
        return
    with st.spinner(f"Uploading {len(clips)} clip(s) and making the page…"):
        try:
            name, url = clip_pages.publish_page(
                channel.strip(), title.strip(), clips, kind="episode" if episode else "video", slug=slug,
            )
            entry = clip_pages.record_manual_page(name, url, channel.strip(), title.strip(), len(clips))
        except Exception as exc:
            logging.exception("Could not publish the clip page")
            st.error(f"Couldn't publish the page: {exc}")
            return
    st.session_state["published_page"] = entry
    st.rerun()


def _render_manual_pages() -> None:
    """Pages published from Make clips, with what each visitor did."""
    pages = clip_pages.manual_pages() if clip_pages.configured() else []
    if not pages:
        return
    activity = _page_activity()
    with st.expander(f"🌐 Published preview pages ({len(pages)})"):
        if activity is None:
            st.caption("Couldn't check who opened them just now.")
            activity = {}
        for page in pages[:30]:
            st.markdown(
                f"**{page.get('channel_title') or 'Clips'}** · {str(page.get('created_at', ''))[:10]} · "
                f"[{page['page_url']}]({page['page_url']})"
            )
            st.caption(clip_pages.describe_activity(activity.get(str(page["page_name"]))))


def _clip_key(path: str | Path) -> str:
    """One spelling per clip file, for its saved framing. The clips list holds resolved
    paths and the render returns the path as written: on a Mac those differ whenever a
    parent folder is a symlink (/var -> /private/var), and the trim then found nothing."""
    return str(Path(path).expanduser().resolve())


def _render_trim_form(
    index: int,
    clip_path: Path,
    clip: ClipResult,
    generated_clips: list[tuple[Path, ClipResult]],
    settings: Any,
) -> None:
    with st.expander("✂️ Trim"):
        with st.form(key=f"trim_form_{clip_path.stem}"):
            start_shift = st.slider(
                "Move the start (seconds)", min_value=-15.0, max_value=15.0, value=0.0, step=0.5,
                key=f"start_shift_{index}_{clip_path.stem}",
            )
            end_shift = st.slider(
                "Move the end (seconds)", min_value=-15.0, max_value=15.0, value=0.0, step=0.5,
                key=f"end_shift_{index}_{clip_path.stem}",
            )
            rerender_clicked = st.form_submit_button("🔄 Apply Trim & Re-render", use_container_width=True)
    if not rerender_clicked:
        return
    contexts = st.session_state.get("clip_render_contexts", {})
    context = contexts.get(_clip_key(clip_path))
    cached_crop = context.get("crop") if context else None
    if context is None or not isinstance(cached_crop, CropBox):
        st.error("This clip's saved framing is gone. Make the clips again before trimming.")
        return
    adjusted_start = max(0.0, float(context["start_time"]) + start_shift)
    adjusted_end = float(context["end_time"]) + end_shift
    replacement_path = rerender_output_path(clip_path)
    try:
        updated_path = rerender_clip(
            source_path=str(context["source_path"]),
            new_start=adjusted_start,
            new_end=adjusted_end,
            transcript_words=context["words"],
            output_path=replacement_path,
            cached_crop_x=cached_crop.x,
            cached_crop=cached_crop,
            layout_mode=str(context["layout_mode"]),
            source_duration=float(context["source_duration"]),
            crop_plan=context.get("crop_plan"),
            hook_title=clip.viral_title,
            hook_seconds=settings.hook_title_seconds,
            highlight_active_word=settings.caption_highlight,
            pin_title=bool(context.get("pin_title")),
            webinar=context.get("webinar"),
            title_fade_in=bool(context.get("title_fade_in", True)),
        )
    except Exception as exc:
        logging.exception("Could not re-render clip %d", index + 1)
        st.error(f"Could not re-render this clip: {exc}")
        return
    updated_clip = clip.model_copy(update={"start_time": adjusted_start, "end_time": adjusted_end})
    updated_clips = list(generated_clips)
    updated_clips[index] = (updated_path, updated_clip)
    st.session_state["generated_clips"] = updated_clips
    st.session_state["clip_paths"] = [str(Path(item[0]).expanduser().resolve()) for item in updated_clips]
    contexts.pop(_clip_key(clip_path), None)
    contexts[_clip_key(updated_path)] = {**context, "start_time": adjusted_start, "end_time": adjusted_end}
    st.session_state["clip_render_contexts"] = contexts
    # Graphics keep their source times; they're checked again against the new start and end when rendered.
    st.session_state.get("card_clips", {}).pop(_clip_key(clip_path), None)
    card_drafts = st.session_state.get("card_drafts", {})
    if _clip_key(clip_path) in card_drafts:
        card_drafts[_clip_key(updated_path)] = card_drafts.pop(_clip_key(clip_path))
    clip_path.unlink(missing_ok=True)
    st.rerun()


def _upload_generated_clips(generated_clips: list[tuple[Path, ClipResult]]) -> None:
    try:
        video_title = st.session_state["agent_video_title"]
        resolved_channel_name = st.session_state["agent_channel_name"]
        drive_link = upload_clips_to_drive(
            resolved_channel_name,
            video_title,
            [str(item[0]) for item in generated_clips],
        )
    except Exception as exc:
        logging.exception("Google Drive clip upload failed")
        st.error(f"Could not upload clips to Google Drive: {exc}")
        return
    st.session_state["outreach_video_title"] = video_title
    st.session_state["outreach_channel_name"] = resolved_channel_name
    st.session_state["drive_folder_link"] = drive_link
    ai_email = st.session_state.get("ai_outreach_email", "")
    if ai_email:
        completed_email = fill_drive_link(ai_email, drive_link)
        st.session_state["ai_outreach_email"] = completed_email
        st.session_state["outreach_email_body"] = completed_email
        return
    try:
        pitch = _generate_outreach_pitch(video_title, drive_link, len(generated_clips), resolved_channel_name)
    except Exception as exc:
        logging.exception("Fallback outreach email generation failed")
        st.warning(f"The clips were uploaded, but the email draft could not be written: {exc}")
        return
    st.session_state["outreach_pitch"] = {"subject": pitch.subject, "body": pitch.body}
    st.session_state["outreach_email_subject"] = pitch.subject
    st.session_state["outreach_email_body"] = pitch.body


# --- 🩺 System: health, settings, storage, log -------------------------------------------
LOG_LINES = 40
PREVIEW_PLAYER_WIDTH = 240  # vertical clips: ~430 px tall, so the email stays in view


def _render_system() -> None:
    st.subheader("Worker")
    st.caption(
        "It keeps the Outbox filled: when it's full it waits, and it makes the next pitch as "
        "soon as you send or pass one. Stopping never loses a creator: one that was half done "
        "goes back in the queue."
    )
    if get_worker_status()["running"]:
        _render_stop_button()
    else:
        _render_start_button("start_worker_system")
    st.caption("Or in Terminal (stop it with **Ctrl+C**):")
    st.code(WORKER_COMMAND, language="bash")

    st.subheader("Settings in use")
    st.caption("Change these in `.env`, then restart the app and the worker.")
    settings = load_settings()
    loudness = loudness_target()
    st.table(
        [
            {"Setting": "CAPTION_STYLE", "Now": os.getenv("CAPTION_STYLE") or "bold"},
            {"Setting": "EMPHASIS_INTENSITY", "Now": os.getenv("EMPHASIS_INTENSITY") or f"{caption_style()['emphasis']} (the caption style's default)"},
            {"Setting": "SOURCE_MAX_HEIGHT", "Now": video_format_sort()[0].removeprefix("res:") + "p"},
            {"Setting": "YouTube sign-in (YTDLP_COOKIES_*)", "Now": sign_in_summary()},
            {"Setting": "LEAD_PROFILE", "Now": (
                "b2b: experts who sell a service (LEAD_PROFILE=wide for every niche)"
                if lead_profile() == "b2b" else "wide: every niche")},
            {"Setting": "Lead size (LEAD_MIN/MAX_SUBSCRIBERS)", "Now": "{:,}–{:,} subscribers".format(*subscriber_range())},
            {"Setting": "AUDIO_TARGET_LUFS", "Now": "off" if loudness is None else f"{loudness:g} LUFS"},
            {"Setting": "ALLOW_LOCAL_WHISPER", "Now": "on" if local_whisper_allowed() else "off (waits for Groq)"},
            {"Setting": "Clip quality bar", "Now": f"AI score {MIN_CLIP_SCORE}+ (MIN_CLIP_SCORE in autonomous_worker.py)"},
            {"Setting": "Title card", "Now": f"{settings.hook_title_seconds:g} s, {settings.hook_title_style} style"},
            {"Setting": "Clip pages (R2_*, CLIP_PAGES_URL)", "Now": (
                f"on: {clip_pages.page_settings().base_url}" if clip_pages.configured()
                else "off: emails link to Drive (setup: claude_review/round16/README.md)")},
            {"Setting": "DRIVE_BACKUP", "Now": "on" if drive_backup_on() or not clip_pages.configured() else "off"},
        ],
        hide_index=True,
    )

    st.subheader("Storage")
    output_dir = render_output_dir()
    storage_bytes = storage_usage_bytes(settings.raw_dir, settings.temp_dir, settings.output_dir, output_dir)
    st.metric("Downloads, temp files and clips on this Mac", f"{storage_bytes / (1024 * 1024):,.0f} MB")
    st.caption(
        f"Outbox previews and trim files: {pitch_files_bytes() / (1024 * 1024):,.0f} MB "
        "(kept apart; each pitch's are deleted when you send or pass it)."
    )
    if st.button("Clear downloads, temp files and made clips"):
        try:
            # Files the background worker is using right now are kept.
            kept = purge_storage(settings.raw_dir, settings.temp_dir, keep_recent_minutes=20)
            purge_storage(settings.output_dir, output_dir)
        except OSError as exc:
            logging.exception("Could not clear temporary and rendered storage")
            st.error(f"Could not clear everything: {exc}")
        except ValueError as exc:
            logging.exception("Refused unsafe storage cleanup")
            st.error(f"Cleanup was refused: {exc}")
        else:
            for key in (
                "generated_clips", "clip_paths", "clip_render_contexts", "clip_source_path",
                "active_clip_run_id", "ai_clip_selection_error", "ai_outreach_email",
                "ai_outreach_subject", "outreach_email_body",
            ):
                st.session_state.pop(key, None)
            st.success("Cleared." + (f" {kept} file(s) the worker is using were kept." if kept else ""))

    st.subheader("Worker log")
    with st.expander(f"Last {LOG_LINES} lines of storage/logs/worker.log"):
        try:
            lines = WORKER_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_LINES:]
        except OSError:
            lines = []
        st.code("\n".join(lines) or "No log yet.", language=None)


def _on_draft_edit(record_id: str, key: str, field: str) -> None:
    """Save an edited subject or email text right away."""
    value = st.session_state.get(key, "")
    try:
        update_pending_draft(record_id, **{field: value})
    except Exception:
        logging.exception("Could not save the draft edit")


def _on_greeting_change(record_id: str, name_key: str, body_key: str) -> None:
    """Put the new first name at the start of the email ('Marcela — I cut ...') and save both."""
    name = str(st.session_state.get(name_key, "")).strip()
    if not name:
        return
    body = set_greeting(str(st.session_state.get(body_key, "")), name)
    st.session_state[body_key] = body
    try:
        update_pending_draft(record_id, body=body, greeting_name=name)
    except Exception:
        logging.exception("Could not save the greeting")


def _render_pass_menu(channel_id: str, key: str) -> None:
    """Pass on a pitch with a one-click reason (saved so the worker can be tuned)."""
    with st.popover(
        "❌ Pass (not pitching)",
        use_container_width=True,
        help="You're not sending this one. The creator won't come back.",
    ):
        st.caption("Why? One click; it's saved so the lead finder and clip picking can be tuned.")
        for code, label in PASS_REASONS.items():
            if st.button(label, key=f"{key}_{code}", use_container_width=True):
                try:
                    skipped = skip_lead(channel_id, pass_reason=code)
                except Exception as exc:
                    logging.exception("Could not skip lead")
                    st.error(f"Could not skip this lead: {exc}")
                else:
                    if skipped:
                        st.session_state["lead_skipped"] = True
                        st.rerun()
                    else:
                        st.warning("This lead is no longer pending.")


_AUTO_SKIP_LABELS = {
    "auto_weak": "no clip strong enough",
    "auto_faceless": "nobody on camera",
    "auto_screen_share": "screen share with a corner webcam",
    "auto_age": "age-restricted video",
}


def _render_pass_summary() -> None:
    """One line: why pitches were passed in the last two weeks."""
    try:
        counts = pass_summary()
    except Exception:
        logging.exception("Could not count pass reasons")
        return
    yours = [f"{PASS_REASONS[code]} {counts[code]}" for code in PASS_REASONS if counts.get(code)]
    auto = [f"{label} {counts[code]}" for code, label in _AUTO_SKIP_LABELS.items() if counts.get(code)]
    if not yours and not auto:
        return
    line = "**Passed, last 14 days:** " + (" · ".join(yours) if yours else "none")
    if auto:
        line += "  \n**Skipped by the worker:** " + " · ".join(auto)
    st.caption(line)


def _render_recently_passed() -> None:
    """Passed pitches from the last two weeks, for ones that were actually sent."""
    _render_pass_summary()
    try:
        passed = get_recently_passed()
    except Exception as exc:
        st.error(f"Could not load passed pitches: {exc}")
        return
    if not passed:
        return
    with st.expander(f"Passed recently ({len(passed)}): sent one by mistake? Move it to Sent"):
        st.caption(
            "Pass removes a creator without recording a send, so no follow-up is scheduled. "
            "If you did email them, click **I sent this one**."
        )
        for record in passed:
            record_id = str(record.get("record_id"))
            left, right = st.columns([4, 1])
            with left:
                reason = PASS_REASONS.get(str(record.get("pass_reason") or ""), "")
                st.markdown(
                    f"**{record.get('channel_title') or 'Creator'}** · passed {str(record.get('skipped_at', ''))[:10]}"
                    + (f" · {reason}" if reason else "")
                )
            with right:
                if st.button("I sent this one", key=f"restore_sent_{record_id}", use_container_width=True):
                    restore_passed_as_sent(record_id)
                    st.rerun()


@st.cache_data(ttl=60, show_spinner=False)
def _page_activity() -> dict[str, dict[str, Any]] | None:
    """Opens/plays/downloads on every clip page (one R2 listing, checked once a minute)."""
    if not clip_pages.configured():
        return {}
    try:
        return clip_pages.page_activity()
    except Exception:
        logging.exception("Could not read clip page activity")
        return None


def _render_sent_pitches() -> None:
    """Sent pitches: the numbers, the replies, then each pitch's next follow-up."""
    try:
        waiting = get_awaiting_reply()
        replies = get_replies()
    except Exception as exc:
        st.error(f"Could not load sent pitches: {exc}")
        return
    records = waiting + replies
    activity = _page_activity() if any(record.get("page_name") for record in records) else {}
    if activity is None:
        st.caption("Couldn't check who opened their clip page just now.")
        activity = {}
    if records:
        _render_outreach_numbers(waiting, replies, activity)
    if replies:
        _render_replies(replies, activity)
    due_count = sum(1 for record in waiting if record.get("follow_up_due"))
    st.subheader(f"Sent: waiting for a reply ({len(waiting)})")
    if not waiting:
        st.caption(
            "Pitches you mark as sent show up here, with follow-ups on day 3, 7 and 12 "
            "(sent as replies in the same thread)."
        )
        return
    if due_count:
        st.caption(f"{due_count} follow-up(s) due.")
    played_only = st.toggle(
        "Only show who played a clip",
        key="sent_played_only",
        help="People who pressed play on their clip page have seen your work: follow up with them first.",
    )
    shown = [record for record in waiting if not played_only or _played(record, activity)]
    shown.sort(key=lambda record: not record.get("follow_up_due"))  # due first; newest first within each
    if played_only and not shown:
        st.caption("No one waiting for a reply has played a clip yet.")
    for record in shown:
        _render_waiting_pitch(record, activity)


def _played(record: dict[str, Any], activity: dict[str, dict[str, Any]]) -> bool:
    page = activity.get(str(record.get("page_name") or ""))
    return bool(page and page.get("played"))


def _opened(record: dict[str, Any], activity: dict[str, dict[str, Any]]) -> bool:
    page = activity.get(str(record.get("page_name") or ""))
    return bool(page and (page.get("opens") or page.get("played") or page.get("downloaded")))


def _render_outreach_numbers(
    waiting: list[dict[str, Any]], replies: list[dict[str, Any]], activity: dict[str, dict[str, Any]]
) -> None:
    """The test's numbers: how many were sent, followed up, opened, played and answered."""
    records = waiting + replies
    followed = sum(
        1 for record in records if record.get("follow_ups") or record.get("followed_up_at")
    )
    with_page = [record for record in records if record.get("page_name")]
    yes = sum(1 for record in replies if record.get("reply") == "yes")
    no = sum(1 for record in replies if record.get("reply") == "no")
    clients = sum(1 for record in replies if record.get("client_at"))
    sent_col, followed_col, opened_col, played_col, replies_col = st.columns(5)
    sent_col.metric("Sent", len(records), border=True, help="Pitches sent in the last 30 days, plus replies from the last 60.")
    followed_col.metric("Followed up", followed, border=True)
    opened_col.metric(
        "Opened their page", f"{sum(_opened(r, activity) for r in with_page)} / {len(with_page)}", border=True,
        help="Your own previews and email link scanners count as opens too. Plays are the signal to trust.",
    )
    played_col.metric("Played a clip", f"{sum(_played(r, activity) for r in with_page)} / {len(with_page)}", border=True)
    replies_col.metric(
        "Replies", len(replies), border=True,
        delta=f"{yes} yes · {no} no" + (f" · {clients} client{'s' if clients != 1 else ''}" if clients else ""),
        delta_color="off", delta_arrow="off",
    )
    st.caption(
        "Since round 25 the first email has no link, so a clip page only gets real visits after your "
        "day-7 follow-up or your reply to a yes."
    )


def _render_replies(replies: list[dict[str, Any]], activity: dict[str, dict[str, Any]]) -> None:
    """Who answered. A "yes" gets your reply (the link, then the offer) ready to copy."""
    open_yes = sum(1 for record in replies if record.get("reply") == "yes" and not record.get("client_at"))
    with st.expander(f"💬 Replies ({len(replies)})", expanded=bool(open_yes)):
        for record in replies:
            record_id = str(record["record_id"])
            title = str(record.get("channel_title") or "Creator")
            outcome = REPLY_OUTCOMES.get(str(record.get("reply")), "Other reply")
            with st.container(border=True):
                client = " · 🤝 trial or client" if record.get("client_at") else ""
                st.markdown(f"**{title}** · {outcome} · {str(record.get('replied_at', ''))[:10]}{client}")
                if record.get("page_name"):
                    st.caption(clip_pages.describe_activity(activity.get(str(record["page_name"]))))
                if record.get("reply") == "yes" and not record.get("client_at"):
                    st.caption("Send them this as a reply in the same thread. The price is set in core/outreach.py.")
                    st.text_input("Subject", value=record["yes_reply_subject"], key=f"yes_subject_{record_id}")
                    st.text_area("Your reply", value=record["yes_reply_body"], height=300, key=f"yes_body_{record_id}")
                    _render_copy_email_button(st.session_state.get(f"yes_body_{record_id}") or record["yes_reply_body"])
                buttons = st.columns(2)
                if record.get("reply") == "yes" and not record.get("client_at"):
                    if buttons[0].button("🤝 They started a trial or signed", key=f"client_{record_id}", use_container_width=True):
                        mark_follow_up(record_id, "client")
                        st.rerun()
                if buttons[1].button(
                    "↩ Undo (back to waiting)", key=f"undo_reply_{record_id}", use_container_width=True,
                    help="Marked the wrong button? This puts the pitch back in the waiting list.",
                ):
                    undo_reply(record_id)
                    st.rerun()


def _render_waiting_pitch(record: dict[str, Any], activity: dict[str, dict[str, Any]]) -> None:
    record_id = str(record["record_id"])
    title = str(record.get("channel_title") or "Creator")
    with st.container(border=True):
        number, total = record.get("follow_up_number"), record.get("follow_up_total")
        if record.get("follow_up_due"):
            label = f" · follow-up {number} of {total} due"
        elif record.get("follow_up_step"):
            label = f" · follow-up {number} of {total} on {record.get('follow_up_on')}"
        else:
            label = " · all follow-ups sent"
        st.markdown(f"**{title}** · sent {str(record.get('sent_at', ''))[:10]}{label}")
        if record.get("page_name"):
            st.caption(clip_pages.describe_activity(activity.get(str(record["page_name"]))))
        if record.get("follow_up_due"):
            st.caption(f"{record.get('follow_up_label')}. Send it as a reply in the same thread.")
            st.text_input("Follow-up subject", value=record["follow_up_subject"], key=f"follow_subject_{record_id}")
            st.text_area("Follow-up", value=record["follow_up_body"], height=180, key=f"follow_body_{record_id}")
            _render_copy_email_button(st.session_state.get(f"follow_body_{record_id}") or record["follow_up_body"])
        sent_col, yes_col, no_col, other_col = st.columns(4)
        with sent_col:
            if record.get("follow_up_due") and st.button(
                "Follow-up sent", key=f"followed_{record_id}", use_container_width=True
            ):
                mark_follow_up(record_id, "followed_up")
                st.rerun()
        for column, outcome, label in (
            (yes_col, "yes", "👍 Said yes"),
            (no_col, "no", "👎 Not interested"),
            (other_col, "other", "💬 Other reply"),
        ):
            if column.button(label, key=f"reply_{outcome}_{record_id}", use_container_width=True):
                mark_follow_up(record_id, outcome)
                st.rerun()


if __name__ == "__main__":
    main()
