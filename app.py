from __future__ import annotations

import base64
import functools
import logging
import re
import shutil
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st
from pydantic import BaseModel

from autonomous_worker import (
    PASS_REASONS,
    get_awaiting_reply,
    get_cached_leads,
    get_recently_passed,
    get_worker_status,
    mark_follow_up,
    prioritize_queued_lead,
    restore_passed_as_sent,
    update_pending_draft,
    get_lead_queue,
    get_pending_outreach,
    is_youtube_quota_exhausted,
    mark_outreach_sent,
    pass_summary,
    skip_lead,
    skip_queued_lead,
)
from config import load_settings
from core.downloader import (
    download_audio_only,
    download_source_video,
    extract_audio_only,
)
from core.clipper import rerender_clip, rerender_output_path
from core.face_tracker import CropBox
from core.drive_uploader import upload_clips_to_drive
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
    pick_clips_with_agent,
    render_selected_clip,
    video_info_and_peaks,
)
from core.storage_manager import (
    prune_output_folder,
    prune_stale_files,
    purge_storage,
    render_output_dir,
    storage_usage_bytes,
)
from core.transcriber import (
    transcribe_audio,
    youtube_video_id,
)
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
    "outbox_full": "paused: the Outbox is full ({detail}). Send or pass a pitch in 📬 Outbox / Approval",
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
    if status["running"]:
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
            "**The worker isn't running**, so the queue won't move and no new pitches are made. "
            "Start it in Terminal:",
            icon="🔴",
        )
        st.code(WORKER_COMMAND, language="bash")


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
        shorts_text = lead.get("shorts_summary") or "Shorts: not checked (queued before this update)"
        with st.container(border=True):
            info, next_col, action = st.columns([6, 1, 1])
            with info:
                st.markdown(f"**{index + 1}. [{title}]({channel_url})** · {subscriber_text} · {email_text}")
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


LAYOUT_LABELS = {
    "Auto (face crop when faces are clear)": "auto",
    "Blurred Background (Best for Sports)": "blurred_background",
    "Automotive / Wide Video (Blurred Background)": "wide_blurred_background",
    "Stacked Split-Screen": "stacked",
    "Face Crop (follows camera cuts)": "smart_crop",
}


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
        <button id="copy-email" style="padding: 0.45rem 0.8rem; cursor: pointer;">
          Copy email
        </button>
        <span id="copy-status" aria-live="polite"></span>
        <script>
          const bytes = Uint8Array.from(atob("{encoded_email}"), c => c.charCodeAt(0));
          const email = new TextDecoder().decode(bytes);
          const button = document.getElementById("copy-email");
          const status = document.getElementById("copy-status");
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
              status.textContent = " Copied";
            }} catch (error) {{
              status.textContent = " Copy failed; select and copy the email.";
            }}
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
                )
            except Exception:
                output_path.unlink(missing_ok=True)
                raise
            render_contexts[str(result.path)] = {
                "source_path": str(source_video),
                "words": words,
                "layout_mode": result.layout_mode,
                "crop": result.crop,
                "crop_plan": result.crop_plan,
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


def main() -> None:
    st.set_page_config(page_title="AI Short-Form Video Generator", page_icon="🎬", layout="wide")
    if st.session_state.pop("_reset_clip_inputs_pending", False):
        st.session_state["video_url_input"] = ""
        st.session_state["channel_name_input"] = ""
        st.session_state["video_upload_epoch"] = (
            int(st.session_state.get("video_upload_epoch", 0)) + 1
        )
    st.title("AI Short-Form Video Generator")

    quota_exhausted = is_youtube_quota_exhausted()
    try:
        initial_pending = get_pending_outreach()
    except Exception:
        initial_pending = []

    if not initial_pending and quota_exhausted:
        st.warning(
            "Autonomous harvester paused due to YouTube quota; manual URL processing remains available",
            icon="⚠️",
        )

    with st.sidebar:
        settings = load_settings()
        output_dir = render_output_dir()
        storage_bytes = storage_usage_bytes(
            settings.raw_dir,
            settings.temp_dir,
            settings.output_dir,
            output_dir,
        )
        storage_status = st.empty()
        storage_status.caption(f"Storage Used: {storage_bytes / (1024 * 1024):.1f} MB")
        if st.button("Clear Temp & Render Cache", use_container_width=True):
            try:
                # Files the background worker is using right now are kept.
                kept = purge_storage(settings.raw_dir, settings.temp_dir, keep_recent_minutes=20)
                purge_storage(settings.output_dir, output_dir)
            except OSError as exc:
                logging.exception("Could not clear temporary and rendered storage")
                st.error(f"Could not clear all temporary files or rendered clips: {exc}")
            except ValueError as exc:
                logging.exception("Refused unsafe storage cleanup")
                st.error(f"Storage cleanup was refused: {exc}")
            else:
                st.session_state.pop("generated_clips", None)
                st.session_state.pop("clip_paths", None)
                st.session_state.pop("clip_render_contexts", None)
                st.session_state.pop("clip_source_path", None)
                st.session_state.pop("active_clip_run_id", None)
                st.session_state.pop("ai_clip_selection_error", None)
                st.session_state.pop("ai_outreach_email", None)
                st.session_state.pop("ai_outreach_subject", None)
                st.session_state.pop("outreach_email_body", None)
                storage_status.caption("Storage cleared")
                st.success(
                    "Temporary files and rendered clips were cleared."
                    + (f" {kept} item(s) the background worker is using were kept." if kept else "")
                )

    target_url = st.session_state.get("target_url")
    if target_url:
        st.session_state["app_tabs"] = "🎬 Clip Generator"
        st.session_state["auto_process_video"] = True
        st.session_state["auto_process_url"] = target_url
        target_channel_name = st.session_state.pop("target_channel_name", "")
        if target_channel_name:
            st.session_state["channel_name_input"] = target_channel_name
        st.session_state["_auto_process_title_override"] = st.session_state.pop(
            "target_video_title",
            "",
        )
        st.session_state.pop("target_url")

    clip_tab, lead_tab, scout_tab, outbox_tab = st.tabs(
        [
            "🎬 Clip Generator",
            "🎯 Creator Lead Finder",
            "🧭 Autonomous Lead Scout",
            "📬 Outbox / Approval",
        ],
        key="app_tabs",
        on_change="rerun",
    )

    with clip_tab:
        st.write("Turn a YouTube video into engaging, subtitled 9:16 clips.")
        with st.form("clip_generator_form"):
            video_url = st.text_input(
                "YouTube URL",
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
                placeholder="Auto-detected for YouTube links",
                key="channel_name_input",
            )
            clip_count = 3
            st.caption("The AI agent selects and renders exactly three clips.")
            if st.session_state.get("crop_mode") not in LAYOUT_LABELS:
                st.session_state["crop_mode"] = next(iter(LAYOUT_LABELS))
            layout_label = st.selectbox(
                "Layout mode",
                list(LAYOUT_LABELS),
                index=0,
                key="crop_mode",
            )
            generate_clicked = st.form_submit_button(
                "Generate Vertical Clips",
                type="primary",
            )
        auto_process = st.session_state.pop("auto_process_video", False)

        if generate_clicked or auto_process:
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
            if auto_process:
                input_value = st.session_state.pop("auto_process_url", "").strip()
            elif bool(video_url.strip()) == (uploaded_video is not None):
                st.error("Provide either a YouTube URL or upload one video file, not both.")
                input_value = ""
            elif uploaded_video is not None:
                input_value = ""
            else:
                input_value = video_url.strip()

            uploaded_input: Path | None = None
            video_title_override = (
                st.session_state.pop("_auto_process_title_override", "")
                if auto_process
                else ""
            )
            try:
                if not auto_process and uploaded_video is not None:
                    settings = load_settings()
                    suffix = Path(uploaded_video.name).suffix.lower()
                    if suffix not in {".mp4", ".mov"}:
                        raise ValueError("Uploaded files must be MP4 or MOV.")
                    settings.temp_dir.mkdir(parents=True, exist_ok=True)
                    video_title_override = Path(uploaded_video.name).stem
                    uploaded_input = (
                        settings.temp_dir / f"upload_{uuid.uuid4().hex}{suffix}"
                    )
                    uploaded_input.write_bytes(uploaded_video.getbuffer())
                    input_path: str | Path = uploaded_input
                else:
                    if not input_value.startswith(("http://", "https://")):
                        raise ValueError(
                            "Enter a YouTube URL starting with http:// or https://."
                        )
                    input_path = input_value

                st.session_state["outreach_channel_override"] = channel_name
                st.session_state["outreach_title_override"] = video_title_override
                generated = _generate_clips(
                    input_path,
                    clip_count,
                    LAYOUT_LABELS[layout_label],
                )
                st.session_state["generated_clips"] = generated
                st.session_state["clip_paths"] = [
                    str(item[0].expanduser().resolve()) for item in generated
                ]
                if generated:
                    st.session_state["outreach_input_path"] = str(input_path)
                    st.session_state["outreach_channel_override"] = channel_name
                    st.session_state["outreach_title_override"] = video_title_override
                if len(generated) == clip_count:
                    st.success(
                        "All clips ready! Saved to ~/Desktop/ShortFormClips/",
                        icon="✅",
                    )
                elif generated:
                    st.warning(
                        f"Rendered {len(generated)} of {clip_count} clips. "
                        "Successful clips are available below."
                    )
                else:
                    st.warning("No clips could be rendered. Check the errors above.")
            except Exception as exc:
                logging.exception("Video generation failed")
                st.session_state.pop("generated_clips", None)
                st.error(f"Video generation failed: {exc}")
            finally:
                if uploaded_input is not None:
                    uploaded_input.unlink(missing_ok=True)

        generated_clips = st.session_state.get("generated_clips", [])
        if generated_clips:
            st.subheader("Your vertical clips")
            columns = st.columns(2)
            for index, (clip_path, clip) in enumerate(generated_clips):
                with columns[index % len(columns)]:
                    st.markdown(f"**{clip.viral_title or f'Clip {index + 1}'}**")
                    st.caption(
                        f"Selected timestamps: {clip.start_time:.1f}s–"
                        f"{clip.end_time:.1f}s"
                    )
                    clip_path = Path(clip_path).expanduser().resolve()
                    clip_path.parent.mkdir(parents=True, exist_ok=True)
                    if clip_path.is_file() and clip_path.stat().st_size > 0:
                        st.video(str(clip_path))
                    else:
                        st.warning(
                            f"Clip file not found: {clip_path.name}. "
                            "Re-render may be required."
                        )
                    with st.expander("✂️ Adjust Clip Boundaries"):
                        with st.form(key=f"trim_form_{clip_path.stem}"):
                            start_shift = st.slider(
                                "Adjust Start (seconds)",
                                min_value=-15.0,
                                max_value=15.0,
                                value=0.0,
                                step=0.5,
                                key=f"start_shift_{index}_{clip_path.stem}",
                            )
                            end_shift = st.slider(
                                "Adjust End (seconds)",
                                min_value=-15.0,
                                max_value=15.0,
                                value=0.0,
                                step=0.5,
                                key=f"end_shift_{index}_{clip_path.stem}",
                            )
                            rerender_clicked = st.form_submit_button(
                                "🔄 Apply Trim & Re-render",
                                use_container_width=True,
                            )
                        if rerender_clicked:
                            contexts = st.session_state.get(
                                "clip_render_contexts", {}
                            )
                            context = contexts.get(str(clip_path))
                            if context is None:
                                st.error(
                                    "Cached source/transcript data for this clip is "
                                    "unavailable. Generate clips again before adjusting."
                                )
                            else:
                                original_start = float(context["start_time"])
                                original_end = float(context["end_time"])
                                cached_crop = context.get("crop")
                                if not isinstance(cached_crop, CropBox):
                                    st.error(
                                        "Cached crop framing for this clip is unavailable. "
                                        "Generate clips again before adjusting."
                                    )
                                    continue
                                adjusted_start = max(0.0, original_start + start_shift)
                                adjusted_end = original_end + end_shift
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
                                    )
                                except Exception as exc:
                                    logging.exception(
                                        "Could not re-render clip %d", index + 1
                                    )
                                    st.error(f"Could not re-render this clip: {exc}")
                                else:
                                    updated_clip = clip.model_copy(
                                        update={
                                            "start_time": adjusted_start,
                                            "end_time": adjusted_end,
                                        }
                                    )
                                    updated_clips = list(generated_clips)
                                    updated_clips[index] = (updated_path, updated_clip)
                                    st.session_state["generated_clips"] = updated_clips
                                    updated_clip_paths = list(
                                        st.session_state.get("clip_paths", [])
                                    )
                                    if len(updated_clip_paths) != len(updated_clips):
                                        updated_clip_paths = [
                                            str(item[0].resolve())
                                            for item in updated_clips
                                        ]
                                    updated_clip_paths[index] = str(
                                        updated_path.expanduser().resolve()
                                    )
                                    st.session_state["clip_paths"] = updated_clip_paths
                                    contexts.pop(str(clip_path), None)
                                    contexts[str(updated_path)] = {
                                        **context,
                                        "start_time": adjusted_start,
                                        "end_time": adjusted_end,
                                    }
                                    st.session_state["clip_render_contexts"] = contexts
                                    clip_path.unlink(missing_ok=True)
                                    st.rerun()
                    st.markdown(
                        "**Why this moment works**"
                        if not st.session_state.get("ai_clip_selection_error")
                        else "**Post caption**"
                    )
                    st.code(clip.caption, language=None)
                    if clip.hashtags:
                        st.markdown(" ".join(f"#{tag.lstrip('#')}" for tag in clip.hashtags))
                    if clip_path.is_file() and clip_path.stat().st_size > 0:
                        # Read the file only when the button is clicked, not on
                        # every rerun of the page.
                        st.download_button(
                            "Download clip",
                            data=functools.partial(clip_path.read_bytes),
                            file_name=clip_path.name,
                            mime="video/mp4",
                            key=f"download_{clip_path.name}",
                            on_click="ignore",
                        )

            if st.session_state.get("drive_folder_link"):
                st.info(
                    "The clips are uploaded to Google Drive. Discarding here removes "
                    "only the local clips; it does not delete the Drive folder."
                )
            else:
                st.write("Review the clips above, then choose what to do next.")
            st.caption(
                "This page doesn't track pitches. Discard / Reset only clears these clips. "
                "Creators the worker found are sent and tracked in 📬 Outbox / Approval (✅ Mark as Sent)."
            )

            approve_column, discard_column = st.columns(2)
            with approve_column:
                approve_upload = st.button(
                    "✅ Approve & Upload to Google Drive",
                    disabled=bool(st.session_state.get("drive_folder_link")),
                    use_container_width=True,
                )
            with discard_column:
                discard_clips = st.button(
                    "🗑️ Discard / Reset",
                    use_container_width=True,
                )

            if discard_clips:
                try:
                    _discard_generated_clips()
                except (OSError, ValueError) as exc:
                    logging.exception("Could not discard generated clips")
                    st.error(f"Could not discard the clips: {exc}")
                else:
                    st.success("The current clips were discarded.")
                    st.rerun()

            if approve_upload:
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
                else:
                    st.session_state["outreach_video_title"] = video_title
                    st.session_state["outreach_channel_name"] = resolved_channel_name
                    st.session_state["drive_folder_link"] = drive_link
                    ai_email = st.session_state.get("ai_outreach_email", "")
                    if ai_email:
                        completed_email = fill_drive_link(ai_email, drive_link)
                        st.session_state["ai_outreach_email"] = completed_email
                        st.session_state["outreach_email_body"] = completed_email
                    else:
                        try:
                            pitch = _generate_outreach_pitch(
                                video_title,
                                drive_link,
                                len(generated_clips),
                                resolved_channel_name,
                            )
                        except Exception as exc:
                            logging.exception("Fallback outreach email generation failed")
                            st.warning(
                                "Your clips were uploaded, but the fallback outreach "
                                f"email could not be generated: {exc}"
                            )
                        else:
                            st.session_state["outreach_pitch"] = {
                                "subject": pitch.subject,
                                "body": pitch.body,
                            }
                            st.session_state["outreach_email_subject"] = pitch.subject
                            st.session_state["outreach_email_body"] = pitch.body

            drive_folder_link = st.session_state.get("drive_folder_link")
            if drive_folder_link:
                st.link_button(
                    "📁 Open Client Google Drive Folder",
                    drive_folder_link,
                )

            pitch_data = st.session_state.get("outreach_pitch")
            ai_email = st.session_state.get("ai_outreach_email")
            if ai_email or pitch_data:
                with st.expander(
                    "📬 AI Creator Outreach Email Draft",
                    expanded=True,
                ):
                    if st.session_state.get("outreach_email_subject"):
                        st.text_input(
                            "Subject",
                            key="outreach_email_subject",
                        )
                    st.text_area(
                        "Email body",
                        height=320,
                        key="outreach_email_body",
                    )
                    _render_copy_email_button(
                        st.session_state.get("outreach_email_body", "")
                    )
                    if not st.session_state.get("drive_folder_link"):
                        st.caption(
                            "The Drive link will be inserted after you approve and "
                            "upload the clips."
                        )

    with lead_tab:
        st.write("Review creators discovered for short-form video repurposing.")
        quota_exhausted = is_youtube_quota_exhausted()
        try:
            background_pending = get_pending_outreach()
        except Exception as exc:
            logging.exception("Could not load background worker leads")
            st.error(f"Could not load background worker leads: {exc}")
            background_pending = []

        cached_leads = _active_cached_leads()
        all_leads = background_pending or cached_leads

        if not background_pending and quota_exhausted:
            st.warning(
                "Autonomous harvester paused due to YouTube quota; manual URL processing remains available"
            )

        if all_leads:
            st.subheader("Discovered creator leads")
            for index, lead in enumerate(background_pending):
                with st.container(border=True):
                    channel_title = lead.get("channel_title") or "Unknown channel"
                    channel_id = lead.get("channel_id", index)
                    channel_url = lead.get("channel_url") or f"https://www.youtube.com/channel/{channel_id}"
                    st.markdown(f"### [{channel_title}]({channel_url})")
                    contact_info = lead.get("contact_email") or lead.get("contact_info")
                    if contact_info:
                        if str(contact_info).startswith(("http://", "https://")):
                            st.markdown(f"Contact: [Channel About]({contact_info})")
                        elif "@" in str(contact_info):
                            st.markdown(f"Contact: [{contact_info}](mailto:{contact_info})")
                        else:
                            st.write(f"Contact: **{contact_info}**")
                    target_vid = lead.get("target_video_id")
                    if target_vid:
                        video_url = f"https://www.youtube.com/watch?v={target_vid}"
                        st.markdown(f"Target video: [{video_url}]({video_url})")
                    else:
                        video_url = lead.get("latest_video_url") or ""
                        video_title = lead.get("latest_video_title") or "Recent video"
                        if video_url:
                            st.markdown(f"Target video: [{video_title}]({video_url})")
                    drive_link = lead.get("drive_link")
                    if drive_link:
                        st.link_button("Open Google Drive clips", drive_link)
                    st.caption(
                        "Pitch is ready: review and copy it in 📬 Outbox / Approval, send it from Gmail, "
                        "then click ✅ Mark as Sent there."
                    )
                    col_proc, col_skip = st.columns([1, 1])
                    with col_proc:
                        if video_url and st.button(
                            "Process & Generate 3 Clips",
                            key=f"process_pending_lead_{channel_id}_{index}",
                            use_container_width=True,
                        ):
                            st.session_state["target_url"] = video_url
                            st.session_state["target_channel_name"] = channel_title
                            st.session_state["target_video_title"] = lead.get("latest_video_title", "")
                            st.rerun()
                    with col_skip:
                        _render_pass_menu(str(channel_id), f"skip_lead_tab_{channel_id}_{index}")

            for index, r in enumerate(cached_leads):
                if any(p.get("channel_id") == r.get("channel_id") for p in background_pending):
                    continue
                with st.container(border=True):
                    channel_title = r.get("channel_title") or r.get("channel_id", "Creator")
                    channel_id = r.get("channel_id", index)
                    channel_url = f"https://www.youtube.com/channel/{channel_id}"
                    st.markdown(f"**[{channel_title}]({channel_url})**")
                    vid_id = r.get("video_id")
                    if vid_id:
                        vid_url = f"https://www.youtube.com/watch?v={vid_id}"
                        st.markdown(f"Latest video: [{vid_url}]({vid_url})")
                        if st.button(
                            "Process & Generate 3 Clips",
                            key=f"process_cached_lead_{channel_id}_{index}",
                        ):
                            st.session_state["target_url"] = vid_url
                            st.session_state["target_channel_name"] = channel_title
                            st.rerun()
                    st.caption(f"Status: {r.get('status', 'recorded')}")
        else:
            if not quota_exhausted:
                st.info(
                    "No leads remaining in queue. Background worker will scavenge new creators shortly."
                )

        st.info(
            "💡 **Autonomous Discovery**: Channel discovery is managed exclusively by the autonomous background worker. "
            "Run `python autonomous_worker.py --continuous-queue` in a terminal to scout creators without blocking the UI."
        )

    with scout_tab:
        st.write(
            "Autonomous scout status and background harvesting controls."
        )
        quota_exhausted = is_youtube_quota_exhausted()
        try:
            pending_outreach = get_pending_outreach()
        except Exception:
            pending_outreach = []

        if not pending_outreach and quota_exhausted:
            st.warning(
                "Autonomous harvester paused due to YouTube quota; manual URL processing remains available"
            )
        elif quota_exhausted:
            st.warning(
                "YouTube API quota limit reached. Harvester is on cooldown. "
                "Existing pending leads remain available for review and manual URL processing is fully operational."
            )

        _render_worker_status()
        _render_lead_queue()

        with st.container(border=True):
            st.subheader("Autonomous Worker Architecture")
            st.markdown(
                "To eliminate runtime blocking and preserve YouTube API quota, channel discovery runs "
                "decoupled as a standalone background worker process:\n\n"
                "```bash\n"
                "python autonomous_worker.py --continuous-queue\n"
                "```\n\n"
                "The worker queries YouTube, screens channels with repurposing deficits, renders clips, "
                "uploads to Google Drive, and queues pitches directly into **📬 Outbox / Approval**."
            )
            lead_records = _latest_lead_records()
            finished_count = sum(_is_finished(record) for record in lead_records)
            st.caption(
                f"Active leads: {len(lead_records) - finished_count} | "
                f"Finished (hidden): {finished_count} | "
                f"Pending in outbox: {len(pending_outreach)}"
            )

    with outbox_tab:
        if st.session_state.pop("outreach_marked_sent", False):
            st.success("Marked as sent. It's under 'Sent: waiting for a reply' below, and the worker will fill the free spot.")
        if st.session_state.pop("lead_skipped", False):
            st.info("Passed. If you actually sent it, use 'Passed recently' at the bottom to move it to Sent.")
        st.markdown(
            "**For each pitch:** set the first name → review the email → **Copy email** → paste into "
            "Gmail and send → **✅ Mark as Sent**. Not pitching someone? **❌ Pass**. "
            "Either way the worker fills the free spot with the next creator."
        )
        _render_worker_status()
        try:
            pending_outreach = get_pending_outreach()
        except Exception as exc:
            logging.exception("Could not load pending outreach")
            st.error(f"Could not load pending outreach: {exc}")
            pending_outreach = []

        quota_exhausted = is_youtube_quota_exhausted()
        if not pending_outreach:
            if quota_exhausted:
                st.warning(
                    "Autonomous harvester paused due to YouTube quota; manual URL processing remains available"
                )
            else:
                st.info(
                    "No leads remaining in queue. Background worker will scavenge new creators shortly."
                )
        for record in pending_outreach:
            record_id = str(record["record_id"])
            channel_id = str(record.get("channel_id") or record_id)
            channel_title = str(record.get("channel_title") or "Unknown creator")
            with st.container(border=True):
                st.subheader(channel_title)
                st.write(f"Contact: {record.get('contact_email') or 'Check About page'}")
                drive_link = str(record.get("drive_link") or "")
                if drive_link:
                    st.link_button("Open Google Drive clips", drive_link)
                subject_key = f"pending_subject_{record_id}"
                body_key = f"pending_pitch_{record_id}"
                name_key = f"pending_name_{record_id}"
                email_body = str(record.get("full_email_body") or "")
                # Widgets start from the saved draft; edits are saved as you make them.
                st.session_state.setdefault(subject_key, str(record.get("subject") or SUBJECT))
                st.session_state.setdefault(body_key, email_body)
                st.session_state.setdefault(
                    name_key, str(record.get("greeting_name") or greeting_name_of(email_body))
                )
                st.text_input(
                    "First name for the greeting",
                    key=name_key,
                    on_change=_on_greeting_change,
                    args=(record_id, name_key, body_key),
                    help="Who you're writing to, e.g. Marcela. Updates the 'Hi ___,' line.",
                )
                st.text_input(
                    "Subject",
                    key=subject_key,
                    on_change=_on_draft_edit,
                    args=(record_id, subject_key, "subject"),
                )
                st.text_area(
                    "Barry Navan outreach pitch",
                    height=320,
                    key=body_key,
                    on_change=_on_draft_edit,
                    args=(record_id, body_key, "body"),
                )
                _render_copy_email_button(st.session_state[body_key])
                col_send, col_skip = st.columns([1, 1])
                with col_send:
                    if st.button(
                        "✅ Mark as Sent",
                        type="primary",
                        key=f"mark_sent_{record_id}",
                        use_container_width=True,
                        help="Click after you've sent it from Gmail. It moves to 'Sent: waiting for a reply'.",
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
                                st.warning(
                                    "This pitch is no longer pending. Refresh the outbox."
                                )
                with col_skip:
                    _render_pass_menu(channel_id, f"skip_lead_{record_id}")

        _render_sent_pitches()
        _render_recently_passed()


def _on_draft_edit(record_id: str, key: str, field: str) -> None:
    """Save an edited subject or email text right away."""
    value = st.session_state.get(key, "")
    try:
        update_pending_draft(record_id, **{field: value})
    except Exception:
        logging.exception("Could not save the draft edit")


def _on_greeting_change(record_id: str, name_key: str, body_key: str) -> None:
    """Put the new first name into the 'Hi ___,' line and save both."""
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
    for record in waiting:
        record_id = str(record["record_id"])
        title = str(record.get("channel_title") or "Creator")
        with st.container(border=True):
            label = " · follow-up due" if record.get("follow_up_due") else ""
            st.markdown(f"**{title}** · sent {str(record.get('sent_at', ''))[:10]}{label}")
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
