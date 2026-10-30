from __future__ import annotations

import base64
import functools
import logging
import os
import re
import shutil
import time
import uuid
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
    FORCEABLE_GROUPS,
    PASS_REASONS,
    force_lead_to_queue,
    get_awaiting_reply,
    get_forceable_leads,
    get_cached_leads,
    get_recently_passed,
    get_worker_status,
    mark_follow_up,
    prioritize_queued_lead,
    record_clip_trim,
    remove_pitch_clip,
    drive_backup_on,
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
from core import webinar as webinar_defaults
from core.clipper import MIN_CLIP_DURATION, rerender_clip, rerender_output_path
from core.face_tracker import CropBox
from core.drive_uploader import replace_clip_in_drive, upload_clips_to_drive
from core.outreach import (
    SUBJECT,
    build_outreach_email,
    build_subject,
    fill_drive_link,
    greeting_name_of,
    set_greeting,
    subject_for,
)
from core.hook_selector import ClipResult, select_clips
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
    st.subheader(f"Skipped or unfinished ({len(rows)})")
    st.caption(
        "**⏩ Force to queue** puts a creator at the top of the queue and clears why they were "
        "skipped. The worker then makes their clips without the automatic checks (on camera, "
        "screen share, AI score bar); every clip still gets an AI score, so you can remove weak "
        "ones in the Outbox."
    )
    for group, label in FORCEABLE_GROUPS.items():
        members = [row for row in rows if row["group"] == group]
        if not members:
            continue
        with st.expander(f"{label} ({len(members)})"):
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


LAYOUT_LABELS = {
    "Auto (recommended): face-tracked, split view for two people": "auto",
    "Face-tracked crop": "smart_crop",
    "Full frame over a blurred background": "blurred_background",
    "Wide video, letterboxed": "wide_blurred_background",
    "Wide shot on top, face below": "stacked",
    "Webinar / screen + speaker: webcam on top, slides below": "webinar",
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
    "For slides, webinars and tutorials with a small webcam in a corner: the webcam goes on top "
    "(about a quarter of the screen), the whole slide under it, then the title and captions."
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


def _render_copy_email_button(email_body: str) -> None:
    encoded_email = base64.b64encode(email_body.encode("utf-8")).decode("ascii")
    st.iframe(
        f"""
        <style>html, body {{ margin: 0; padding: 0; }}</style>
        <button id="copy-email" aria-live="polite" style="width: 100%; height: 40px; cursor: pointer;
          border: 1px solid rgba(49, 51, 63, 0.2); border-radius: 8px; background: #fff;
          color: #31333f; font: 400 14px 'Source Sans Pro', 'Source Sans 3', sans-serif;">
          📋 Copy email
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
            setTimeout(() => {{ button.textContent = "📋 Copy email"; }}, 2500);
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
) -> list[tuple[Path, ClipResult]]:
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
            extract_audio_only(source_video, audio_path)
            duration = _video_duration(source_video)
        update_progress(0.20, "")

        st.write("**Step 2 of 4: Transcribing speech with Groq**")
        words = transcribe_audio(
            audio_path,
            settings,
            on_progress=stage_progress(0.20, 0.45),
        )
        update_progress(0.45, "")

        st.write("**Step 3 of 4: AI selecting three clip hooks**")
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
                "start_time": clip.start_time,
                "end_time": clip.end_time,
                "source_duration": duration,
            }
            return result.path, clip

        if len(clips) == 3:
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
                help="Who you're writing to, e.g. Marcela. The email opens with it: 'Marcela — I cut ...'. "
                "Until you type one it says 'Hi there'.",
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
    if not result["email_updated"]:
        note = f"Clip {index} removed. Your email doesn't use the usual 'I cut N highlight moments' line: check the number in it."
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
        st.success(f"⏩ {forced} is at the top of the queue. The worker takes them next.")
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
    "🎯 3 highlights": "highlights",
    "🎞️ Series: the whole video in order": "series",
}


def _render_clip_generator() -> None:
    st.caption(
        "Make clips from any YouTube link or video file, outside the worker. Nothing here is "
        "tracked as a pitch: the worker's pitches are in the 📬 Outbox."
    )
    mode = st.radio(
        "Mode", list(CLIP_MODES), key="clip_mode", horizontal=True,
        help="3 highlights: the AI picks the 3 best moments. Series: Part 1, Part 2, Part 3… "
        "covering the whole video in order, one batch of 3 at a time.",
    )
    if CLIP_MODES[mode] == "series":
        _render_series_maker()
        return
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
        generate_clicked = st.form_submit_button("Make 3 vertical clips", type="primary")

    if generate_clicked:
        _make_clips(
            video_url, uploaded_video, channel_name, layout_label,
            webcam_corner=webcam_corner, webinar_options=webinar_options, pin_title=pin_title,
        )
    _render_generated_clips()


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
) -> None:
    clip_count = 3
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
    if bool(video_url.strip()) == (uploaded_video is not None):
        st.error("Give either a YouTube link or one video file, not both.")
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
        else:
            input_value = video_url.strip()
            if not input_value.startswith(("http://", "https://")):
                raise ValueError("Enter a YouTube link starting with http:// or https://.")
            input_path = input_value

        st.session_state["outreach_channel_override"] = channel_name
        st.session_state["outreach_title_override"] = video_title_override
        generated = _generate_clips(
            input_path, clip_count, LAYOUT_LABELS[layout_label], webcam_corner=webcam_corner,
            webinar_options=webinar_options, pin_title=pin_title,
        )
        st.session_state["generated_clips"] = generated
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
                webinar_options=webinar_options,
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
    """Sent pitches waiting for a reply, with a short follow-up once due."""
    try:
        waiting = get_awaiting_reply()
    except Exception as exc:
        st.error(f"Could not load sent pitches: {exc}")
        return
    due_count = sum(1 for record in waiting if record.get("follow_up_due"))
    st.subheader(f"Sent: waiting for a reply ({len(waiting)})")
    if not waiting:
        st.caption(
            "Pitches you mark as sent show up here, with a short follow-up after 4 days "
            "without an answer."
        )
        return
    if due_count:
        st.caption(f"{due_count} follow-up(s) due.")
    activity = _page_activity() if any(record.get("page_name") for record in waiting) else {}
    if activity is None:
        st.caption("Couldn't check who opened their clip page just now.")
        activity = {}
    for record in waiting:
        record_id = str(record["record_id"])
        title = str(record.get("channel_title") or "Creator")
        with st.container(border=True):
            label = " · follow-up due" if record.get("follow_up_due") else ""
            st.markdown(f"**{title}** · sent {str(record.get('sent_at', ''))[:10]}{label}")
            if record.get("page_name"):
                st.caption(clip_pages.describe_activity(activity.get(str(record["page_name"]))))
            if record.get("follow_up_due"):
                st.text_input("Follow-up subject", value=record["follow_up_subject"], key=f"follow_subject_{record_id}")
                st.text_area("Follow-up", value=record["follow_up_body"], height=180, key=f"follow_body_{record_id}")
                _render_copy_email_button(record["follow_up_body"])
            sent_col, replied_col = st.columns(2)
            with sent_col:
                if record.get("follow_up_due") and st.button(
                    "Follow-up sent", key=f"followed_{record_id}", use_container_width=True
                ):
                    mark_follow_up(record_id, "followed_up")
                    st.rerun()
            with replied_col:
                if st.button("They replied", key=f"replied_{record_id}", use_container_width=True):
                    mark_follow_up(record_id, "replied")
                    st.rerun()


if __name__ == "__main__":
    main()
