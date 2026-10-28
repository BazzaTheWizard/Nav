from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import re
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

from config import PROJECT_ROOT, load_settings
from core.downloader import download_source_video, extract_audio_only
from core.drive_uploader import DriveAuthError, check_drive_access, upload_clips_to_drive
from core.face_tracker import on_camera_share
from core.storage_manager import prune_stale_files
from core.hook_selector import ClipResult
from core.outreach import (
    LEGACY_SITE_URL,
    SITE_URL,
    build_follow_up,
    fill_drive_link,
    greeting_name_of,
    is_old_template,
    refresh_site_links,
    rewrite_old_draft,
    site_host,
    subject_for,
    topic_from_email,
)
from core.lead_harvester import (
    YouTubeQuotaExceededError,
    discover_channels,
    generate_search_queries,
)
from core.pipeline import (
    pick_clips_with_agent,
    render_selected_clip,
    video_info_and_peaks,
)
from core.transcriber import Word, transcribe_audio
from main import _video_duration


logger = logging.getLogger("autonomous_worker")
STORAGE_DIR = PROJECT_ROOT / "storage"
PROCESSED_CREATORS_PATH = STORAGE_DIR / "processed_creators.json"
PENDING_OUTREACH_PATH = STORAGE_DIR / "pending_outreach.json"
LOCK_PATH = STORAGE_DIR / ".autonomous_worker.lock"
QUOTA_STATUS_PATH = STORAGE_DIR / "quota_status.json"
SKIPPED_LEADS_PATH = STORAGE_DIR / "skipped_leads.json"
SEEN_CHANNELS_PATH = STORAGE_DIR / "seen_channels.json"
LEAD_QUEUE_PATH = STORAGE_DIR / "lead_queue.json"
MAX_PENDING_LEADS = 5
PENDING_SLOT_POLL_INTERVAL_SEC = 5
# Each discovery run costs ~500 YouTube API units (5 searches x 100) out of a
# 10,000/day quota, so back off when it finds nothing instead of retrying every 5 s.
DISCOVERY_BACKOFF_START_SEC = 5 * 60
DISCOVERY_BACKOFF_MAX_SEC = 60 * 60
QUOTA_COOLDOWN_SEC = 15 * 60
# A lead that fails (network blip, API hiccup) goes back in the queue this many
# times before it's marked failed for good.
MAX_LEAD_ATTEMPTS = 3
CLIPS_PER_LEAD = 3
DRIVE_SIGN_IN_WAIT_SEC = 10 * 60
# Below this share of sampled frames with a face, nobody is really on camera
# (narration, slides, animation): those creators aren't a fit, so they're
# skipped right after download, before any transcribing, AI, rendering or upload.
MIN_ON_CAMERA_SHARE = 0.25
LOG_PATH = STORAGE_DIR / "logs" / "worker.log"

# yt-dlp/YouTube errors that will fail the same way every time: fail the lead now
# instead of re-downloading it three times.
_PERMANENT_ERROR = re.compile(
    r"private video|video is private|members[- ]only|join this channel|"
    r"confirm your age|age[- ]restricted|live event will begin|premieres in|"
    r"video unavailable|has been removed|copyright|not available in your country",
    re.IGNORECASE,
)


def is_permanent_error(error: Exception) -> bool:
    return bool(_PERMANENT_ERROR.search(str(error)))


class NotAFit(Exception):
    """The creator isn't a fit (for example, nobody on camera). Skipped, never retried."""


# --- Worker status, for the app ------------------------------------------------------
# The worker writes what it's doing here; the app shows it, and checks the process
# id to tell "running" from "stopped" (or closed without saying so).
def _worker_status_path() -> Path:
    return STORAGE_DIR / "worker_status.json"


def _set_status(state: str, detail: str = "") -> None:
    """Best effort: a status write must never stop the worker."""
    try:
        _write_json_atomic(
            _worker_status_path(),
            {
                "pid": os.getpid(),
                "state": state,
                "detail": detail,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except (OSError, TypeError, ValueError):
        logger.debug("Could not write worker status.", exc_info=True)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def get_worker_status() -> dict[str, Any]:
    """{"running": bool, "state": str, "detail": str, "updated_at": str}."""
    raw = _read_json(_worker_status_path(), None)
    if not isinstance(raw, dict):
        return {"running": False, "state": "never_started", "detail": "", "updated_at": ""}
    pid = raw.get("pid")
    running = (
        raw.get("state") != "stopped"
        and isinstance(pid, int)
        and _process_alive(pid)
    )
    return {
        "running": running,
        "state": str(raw.get("state") or ""),
        "detail": str(raw.get("detail") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
    }


def is_youtube_quota_exhausted() -> bool:
    if QUOTA_STATUS_PATH.exists():
        try:
            data = json.loads(QUOTA_STATUS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return bool(data.get("quota_exhausted", False))
        except Exception:
            pass
    return False


def set_youtube_quota_exhausted(exhausted: bool, message: str = "") -> None:
    try:
        data = {
            "quota_exhausted": bool(exhausted),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "message": str(message),
        }
        _write_json_atomic(QUOTA_STATUS_PATH, data)
    except Exception as exc:
        logger.warning("Could not write quota status: %s", exc)


def get_cached_leads() -> list[dict[str, Any]]:
    try:
        state = _processed_state()
        records = state.get("records", [])
        return [r for r in records if isinstance(r, dict)]
    except Exception:
        return []


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return default
        return json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Could not read persistent state at %s: %s. Using default.",
            path,
            exc,
        )
        return default


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise RuntimeError(f"Could not save persistent state at {path}: {exc}") from exc


@contextmanager
def _worker_lock() -> Generator[None, None, None]:
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError("The autonomous worker requires POSIX file locking.") from exc

    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _processed_state() -> dict[str, Any]:
    default_state: dict[str, Any] = {
        "processed_channel_ids": [],
        "processed_video_ids": [],
        "records": [],
    }
    raw = _read_json(PROCESSED_CREATORS_PATH, None)
    if raw is None or not isinstance(raw, dict):
        logger.info("Initializing %s with default schema.", PROCESSED_CREATORS_PATH)
        _write_json_atomic(PROCESSED_CREATORS_PATH, default_state)
        return dict(default_state)

    changed = False
    # Backward compatibility: handle 'creators' key if present
    if "creators" in raw and "records" not in raw:
        if isinstance(raw["creators"], list):
            raw["records"] = raw.pop("creators")
            changed = True

    for key in ("processed_channel_ids", "processed_video_ids", "records"):
        if not isinstance(raw.get(key), list):
            raw[key] = []
            changed = True

    valid_records: list[dict[str, Any]] = []
    for item in raw["records"]:
        if isinstance(item, dict):
            valid_records.append(item)
        else:
            changed = True
    raw["records"] = valid_records

    valid_channels = [
        str(x) for x in raw["processed_channel_ids"] if isinstance(x, (str, int))
    ]
    if len(valid_channels) != len(raw["processed_channel_ids"]):
        raw["processed_channel_ids"] = valid_channels
        changed = True

    valid_videos = [
        str(x) for x in raw["processed_video_ids"] if isinstance(x, (str, int))
    ]
    if len(valid_videos) != len(raw["processed_video_ids"]):
        raw["processed_video_ids"] = valid_videos
        changed = True

    if changed:
        logger.info(
            "Auto-repaired %s to match required schema.",
            PROCESSED_CREATORS_PATH,
        )
        _write_json_atomic(PROCESSED_CREATORS_PATH, raw)

    return raw


def _pending_records() -> list[dict[str, Any]]:
    raw = _read_json(PENDING_OUTREACH_PATH, None)
    if raw is None or not isinstance(raw, list):
        logger.info("Initializing %s with empty list.", PENDING_OUTREACH_PATH)
        _write_json_atomic(PENDING_OUTREACH_PATH, [])
        return []

    valid: list[dict[str, Any]] = []
    changed = False
    for item in raw:
        if isinstance(item, dict):
            valid.append(item)
        else:
            changed = True

    if changed:
        logger.info(
            "Auto-repaired %s to ensure all entries are objects.",
            PENDING_OUTREACH_PATH,
        )
        _write_json_atomic(PENDING_OUTREACH_PATH, valid)

    return valid


def _skipped_records() -> list[dict[str, Any]]:
    raw = _read_json(SKIPPED_LEADS_PATH, None)
    if raw is None or not isinstance(raw, list):
        logger.info("Initializing %s with empty list.", SKIPPED_LEADS_PATH)
        _write_json_atomic(SKIPPED_LEADS_PATH, [])
        return []

    valid: list[dict[str, Any]] = []
    changed = False
    for item in raw:
        if isinstance(item, dict):
            valid.append(item)
        else:
            changed = True

    if changed:
        logger.info(
            "Auto-repaired %s to ensure all entries are objects.",
            SKIPPED_LEADS_PATH,
        )
        _write_json_atomic(SKIPPED_LEADS_PATH, valid)

    return valid


def _seen_channels() -> list[str]:
    raw = _read_json(SEEN_CHANNELS_PATH, None)
    if raw is None or not isinstance(raw, list):
        logger.info("Initializing %s with empty list.", SEEN_CHANNELS_PATH)
        _write_json_atomic(SEEN_CHANNELS_PATH, [])
        return []

    valid = [str(x) for x in raw if isinstance(x, (str, int))]
    if len(valid) != len(raw):
        logger.info(
            "Auto-repaired %s to ensure all entries are strings.",
            SEEN_CHANNELS_PATH,
        )
        _write_json_atomic(SEEN_CHANNELS_PATH, valid)

    return valid


def init_storage_files() -> None:
    """Ensure all storage JSON files exist with valid default empty schemas."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with _worker_lock():
        _processed_state()
        _pending_records()
        _skipped_records()
        _seen_channels()
        if (
            not QUOTA_STATUS_PATH.exists()
            or not QUOTA_STATUS_PATH.read_text(encoding="utf-8").strip()
            or not isinstance(_read_json(QUOTA_STATUS_PATH, None), dict)
        ):
            _write_json_atomic(
                QUOTA_STATUS_PATH,
                {"quota_exhausted": False, "updated_at": "", "message": ""},
            )


def _wait_for_pending_slot(
    poll_interval_sec: float = PENDING_SLOT_POLL_INTERVAL_SEC,
) -> None:
    while True:
        with _worker_lock():
            pending_count = sum(
                record.get("status", "pending") == "pending"
                for record in _pending_records()
            )
        if pending_count < MAX_PENDING_LEADS:
            return
        _set_status("outbox_full", f"{pending_count}/{MAX_PENDING_LEADS} pitches waiting")
        logger.info(
            "Pending outreach buffer is full (%d/%d); waiting for a lead to be "
            "processed.",
            pending_count,
            MAX_PENDING_LEADS,
        )
        time.sleep(poll_interval_sec)


def get_pending_outreach() -> list[dict[str, Any]]:
    with _worker_lock():
        pending = _pending_records()
        changed = False
        for record in pending:
            if not isinstance(record.get("record_id"), str):
                record["record_id"] = (
                    f"{record.get('channel_id', 'unknown')}:"
                    f"{record.get('target_video_id', uuid.uuid4().hex)}"
                )
                changed = True
            # Unsent drafts written with the old template get the new email once.
            body = str(record.get("full_email_body") or "")
            if record.get("status", "pending") == "pending" and is_old_template(body):
                record["full_email_body"] = rewrite_old_draft(
                    body,
                    str(record.get("channel_title") or ""),
                    str(record.get("drive_link") or ""),
                    str(record.get("video_title") or ""),
                    shorts_status=str(record.get("shorts_status") or ""),
                )
                record["subject"] = subject_for(record["full_email_body"])
                changed = True
            # Unsent drafts follow the current website address (core/outreach.py SITE_URL).
            old_site = str(record.get("site_url") or LEGACY_SITE_URL)
            if record.get("status", "pending") == "pending" and site_host(old_site) != site_host(SITE_URL):
                record["full_email_body"] = refresh_site_links(str(record.get("full_email_body") or ""), old_site)
                record["site_url"] = SITE_URL
                changed = True
        if changed:
            _write_json_atomic(PENDING_OUTREACH_PATH, pending)
        return [
            record
            for record in pending
            if record.get("status", "pending") == "pending"
        ]


FOLLOW_UP_AFTER_DAYS = 4


def get_follow_ups_due(now: datetime | None = None) -> list[dict[str, Any]]:
    """Sent pitches with no follow-up yet, sent at least FOLLOW_UP_AFTER_DAYS ago.

    Each comes with a ready "follow_up_body". Mark a creator as replied in the app
    (or by sending the follow-up) and they drop off this list.
    """
    now = now or datetime.now(timezone.utc)
    with _worker_lock():
        records = _pending_records()
    due: list[dict[str, Any]] = []
    for record in records:
        if record.get("status") != "sent" or record.get("followed_up_at") or record.get("replied_at"):
            continue
        try:
            sent_at = datetime.fromisoformat(str(record.get("sent_at")))
        except ValueError:
            continue
        if now - sent_at < timedelta(days=FOLLOW_UP_AFTER_DAYS):
            continue
        body = str(record.get("full_email_body") or "")
        due.append(
            {
                **record,
                "follow_up_subject": f"Re: {record.get('subject') or subject_for(body)}",
                "follow_up_body": build_follow_up(
                    str(record.get("greeting_name") or record.get("channel_title") or ""),
                    video_title=str(record.get("video_title") or ""),
                    video_topic=topic_from_email(body),
                    drive_link=str(record.get("drive_link") or ""),
                ),
            }
        )
    return due


AWAITING_REPLY_DAYS = 30  # sent emails older than this drop off the "waiting" list


def get_awaiting_reply(now: datetime | None = None) -> list[dict[str, Any]]:
    """Sent pitches with no reply yet (newest first). "follow_up_due" is True once
    FOLLOW_UP_AFTER_DAYS passed without a follow-up, with "follow_up_subject" and
    "follow_up_body" ready to send.
    """
    now = now or datetime.now(timezone.utc)
    with _worker_lock():
        records = _pending_records()
    waiting: list[dict[str, Any]] = []
    for record in records:
        if record.get("status") != "sent" or record.get("replied_at"):
            continue
        try:
            sent_at = datetime.fromisoformat(str(record.get("sent_at")))
        except ValueError:
            continue
        age = now - sent_at
        if age > timedelta(days=AWAITING_REPLY_DAYS):
            continue
        body = str(record.get("full_email_body") or "")
        item = {
            **record,
            "follow_up_due": not record.get("followed_up_at") and age >= timedelta(days=FOLLOW_UP_AFTER_DAYS),
        }
        if item["follow_up_due"]:
            item["follow_up_subject"] = f"Re: {record.get('subject') or subject_for(body)}"
            item["follow_up_body"] = build_follow_up(
                str(record.get("greeting_name") or record.get("channel_title") or ""),
                video_title=str(record.get("video_title") or ""),
                video_topic=topic_from_email(body),
                drive_link=str(record.get("drive_link") or ""),
            )
        waiting.append(item)
    waiting.sort(key=lambda item: str(item.get("sent_at")), reverse=True)
    return waiting


def update_pending_draft(
    record_id: str,
    *,
    subject: str | None = None,
    body: str | None = None,
    greeting_name: str | None = None,
) -> bool:
    """Save your edits to an unsent pitch (subject, email text, greeting name)."""
    with _worker_lock():
        records = _pending_records()
        for record in records:
            if record.get("record_id") == record_id and record.get("status", "pending") == "pending":
                if subject is not None:
                    record["subject"] = subject
                if body is not None:
                    record["full_email_body"] = body
                if greeting_name is not None:
                    record["greeting_name"] = greeting_name
                _write_json_atomic(PENDING_OUTREACH_PATH, records)
                return True
    return False


PASSED_LOOKBACK_DAYS = 14


def get_recently_passed(now: datetime | None = None) -> list[dict[str, Any]]:
    """Pitches you passed on in the last PASSED_LOOKBACK_DAYS (newest first), so one
    you actually sent can be moved to Sent."""
    now = now or datetime.now(timezone.utc)
    with _worker_lock():
        skipped = _skipped_records()
    recent: list[dict[str, Any]] = []
    for record in skipped:
        if not record.get("drive_link") or not record.get("record_id"):
            continue  # skipped from the queue before any clips were made
        try:
            when = datetime.fromisoformat(str(record.get("skipped_at")))
        except ValueError:
            continue
        if now - when <= timedelta(days=PASSED_LOOKBACK_DAYS):
            recent.append(record)
    recent.sort(key=lambda record: str(record.get("skipped_at")), reverse=True)
    return recent


def restore_passed_as_sent(record_id: str) -> bool:
    """You did send it: move a passed pitch to Sent (follow-ups then work for it)."""
    with _worker_lock():
        skipped = _skipped_records()
        index = next((i for i, r in enumerate(skipped) if r.get("record_id") == record_id), None)
        if index is None:
            return False
        record = skipped.pop(index)
        sent_at = str(record.get("skipped_at") or datetime.now(timezone.utc).isoformat())
        for key in ("skip_reason", "skipped_at"):
            record.pop(key, None)
        record["status"] = "sent"
        record["sent_at"] = sent_at
        records = _pending_records()
        records.append(record)
        _write_json_atomic(PENDING_OUTREACH_PATH, records)
        _write_json_atomic(SKIPPED_LEADS_PATH, skipped)
        channel_id = record.get("channel_id")
        if isinstance(channel_id, str):
            _update_lead_status(_processed_state(), channel_id, "sent")
    return True


def mark_follow_up(record_id: str, outcome: str) -> bool:
    """outcome: "followed_up" (second email sent) or "replied" (no follow-up needed)."""
    field = {"followed_up": "followed_up_at", "replied": "replied_at"}[outcome]
    with _worker_lock():
        records = _pending_records()
        for record in records:
            if record.get("record_id") == record_id and record.get("status") == "sent":
                record[field] = datetime.now(timezone.utc).isoformat()
                _write_json_atomic(PENDING_OUTREACH_PATH, records)
                return True
    return False


def mark_outreach_sent(record_id: str, *, subject: str | None = None, body: str | None = None) -> bool:
    """Record a pitch as sent. ``subject``/``body``: the text you actually sent, if edited."""
    if not record_id.strip():
        raise ValueError("record_id must not be empty.")
    with _worker_lock():
        pending = _pending_records()
        for record in pending:
            existing_record_id = record.get("record_id") or (
                f"{record.get('channel_id', 'unknown')}:"
                f"{record.get('target_video_id', '')}"
            )
            if (
                existing_record_id == record_id
                and record.get("status", "pending") == "pending"
            ):
                record["record_id"] = record_id
                record["status"] = "sent"
                record["sent_at"] = datetime.now(timezone.utc).isoformat()
                if subject:
                    record["subject"] = subject
                if body:
                    record["full_email_body"] = body
                _write_json_atomic(PENDING_OUTREACH_PATH, pending)
                state = _processed_state()
                channel_id = record.get("channel_id")
                if isinstance(channel_id, str):
                    _update_lead_status(state, channel_id, "sent")
                return True
    return False


def skip_lead(channel_id: str, reason: str = "user_skipped") -> bool:
    if not channel_id.strip():
        raise ValueError("channel_id must not be empty.")
    with _worker_lock():
        pending = _pending_records()
        target_index = None
        for i, record in enumerate(pending):
            if (
                record.get("channel_id") == channel_id
                or record.get("record_id") == channel_id
            ):
                target_index = i
                break
        if target_index is None:
            return False

        skipped_record = pending.pop(target_index)
        _write_json_atomic(PENDING_OUTREACH_PATH, pending)

        actual_channel_id = str(skipped_record.get("channel_id") or channel_id)

        # 1. Move the target lead from storage/pending_outreach.json to storage/skipped_leads.json
        skipped_leads = _skipped_records()
        skipped_record["status"] = "skipped"
        skipped_record["skip_reason"] = reason
        skipped_record["skipped_at"] = datetime.now(timezone.utc).isoformat()
        skipped_leads.append(skipped_record)
        _write_json_atomic(SKIPPED_LEADS_PATH, skipped_leads)

        # 2. Add the channel ID to storage/seen_channels.json so the harvester never re-scrapes or re-adds this creator
        seen = _seen_channels()
        seen_set = set(seen)
        seen_set.add(actual_channel_id)
        _write_json_atomic(SEEN_CHANNELS_PATH, sorted(seen_set))

        # 3. Update processed_creators.json state
        try:
            state = _processed_state()
            if actual_channel_id not in state["processed_channel_ids"]:
                state["processed_channel_ids"].append(actual_channel_id)
            found = False
            for record in reversed(state["records"]):
                if record.get("channel_id") == actual_channel_id:
                    record["status"] = "skipped"
                    record["skip_reason"] = reason
                    record["updated_at"] = datetime.now(timezone.utc).isoformat()
                    found = True
                    break
            if not found:
                state["records"].append(
                    {
                        "channel_id": actual_channel_id,
                        "channel_title": str(
                            skipped_record.get("channel_title") or actual_channel_id
                        ),
                        "status": "skipped",
                        "skip_reason": reason,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            _write_json_atomic(PROCESSED_CREATORS_PATH, state)
        except Exception as exc:
            logger.warning("Could not update processed state for skipped lead: %s", exc)

        logger.info(
            "Skipped lead for channel %s (%s). Active buffer now: %d",
            skipped_record.get("channel_title", actual_channel_id),
            actual_channel_id,
            sum(r.get("status", "pending") == "pending" for r in pending),
        )
        return True


def _lead_queue() -> list[dict[str, Any]]:
    queue = _read_json(LEAD_QUEUE_PATH, [])
    if not isinstance(queue, list):
        return []
    return [lead for lead in queue if isinstance(lead, dict)]


def _enqueue_leads(leads: list[dict[str, Any]]) -> int:
    """Save every qualified lead from a discovery run, not just the first one.

    discover_channels marks all returned channels as seen, so leads that are not
    saved here are never found again.
    """
    with _worker_lock():
        queue = _lead_queue()
        queued_ids = {str(lead.get("channel_id")) for lead in queue}
        added = 0
        for lead in leads:
            channel_id = str(lead.get("channel_id", ""))
            if channel_id and channel_id not in queued_ids:
                queue.append(lead)
                queued_ids.add(channel_id)
                added += 1
        _write_json_atomic(LEAD_QUEUE_PATH, queue)
    return added


def _lead_priority(lead: dict[str, Any]) -> int:
    """Queue rank. A lead that failed drops back so it doesn't block the queue."""
    try:
        priority = int(lead.get("priority", 0) or 0)
        attempts = int(lead.get("attempts", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return priority - 3 * attempts


def _pop_fresh_lead() -> dict[str, Any] | None:
    """Take the best unprocessed lead off the queue and reserve it atomically.

    Best = highest "priority" (Shorts gap, email found, video length); ties keep
    the order they were found in. Leads queued before priorities existed count as 0.
    """
    with _worker_lock():
        queue = _lead_queue()
        state = _processed_state()
        processed_channels = set(state["processed_channel_ids"])
        processed_videos = set(state["processed_video_ids"])
        valid: list[dict[str, Any]] = []
        for lead in queue:
            channel_id = str(lead.get("channel_id", ""))
            video_url = str(lead.get("latest_video_url", ""))
            video_id = video_url.rsplit("=", maxsplit=1)[-1]
            if (
                channel_id
                and video_id
                and channel_id not in processed_channels
                and video_id not in processed_videos
            ):
                valid.append(lead)
        chosen: dict[str, Any] | None = None
        if valid:
            chosen = max(valid, key=_lead_priority)  # max() keeps the first of equals
            valid.remove(chosen)
        _write_json_atomic(LEAD_QUEUE_PATH, valid)
        if chosen is not None:
            _reserve_lead(state, chosen)
        return chosen


def get_lead_queue() -> list[dict[str, Any]]:
    """Queued creators in the order the worker will take them (for the app)."""
    with _worker_lock():
        queue = _lead_queue()
    return sorted(queue, key=_lead_priority, reverse=True)  # sorted() is stable


def prioritize_queued_lead(channel_id: str) -> bool:
    """Move a queued creator to the top, so the worker does them next."""
    with _worker_lock():
        queue = _lead_queue()
        index = next((i for i, lead in enumerate(queue) if str(lead.get("channel_id")) == channel_id), None)
        if index is None:
            return False
        queue.insert(0, queue.pop(index))
        _write_json_atomic(LEAD_QUEUE_PATH, queue)
    return True


def skip_queued_lead(channel_id: str, reason: str = "user_skipped_before_processing") -> bool:
    """Remove a creator from the queue before any work is done on them."""
    if not channel_id.strip():
        raise ValueError("channel_id must not be empty.")
    with _worker_lock():
        queue = _lead_queue()
        remaining = [lead for lead in queue if str(lead.get("channel_id")) != channel_id]
        if len(remaining) == len(queue):
            return False
        skipped = next(lead for lead in queue if str(lead.get("channel_id")) == channel_id)
        _write_json_atomic(LEAD_QUEUE_PATH, remaining)
        skipped_leads = _skipped_records()
        skipped_leads.append(
            {
                **skipped,
                "status": "skipped",
                "skip_reason": reason,
                "skipped_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _write_json_atomic(SKIPPED_LEADS_PATH, skipped_leads)
        seen = set(_seen_channels())
        seen.add(channel_id)
        _write_json_atomic(SEEN_CHANNELS_PATH, sorted(seen))
    logger.info("Skipped queued lead %s (%s).", skipped.get("channel_title", channel_id), channel_id)
    return True


def _reserve_lead(
    state: dict[str, Any],
    lead: dict[str, Any],
) -> None:
    channel_id = str(lead["channel_id"])
    video_id = str(lead["latest_video_url"]).rsplit("=", maxsplit=1)[-1]
    state["processed_channel_ids"].append(channel_id)
    state["processed_video_ids"].append(video_id)
    state["records"].append(
        {
            "channel_id": channel_id,
            "video_id": video_id,
            "channel_title": str(lead["channel_title"]),
            "status": "processing",
            "created_at": datetime.now(timezone.utc).isoformat(),
            # Kept so the Shorts cut-offs can be tuned later against what you skip.
            "priority": lead.get("priority"),
            "shorts": lead.get("shorts"),
            "has_email": bool(lead.get("contact_email")),
            "duration_seconds": lead.get("duration_seconds"),
        }
    )
    _write_json_atomic(PROCESSED_CREATORS_PATH, state)


def _update_lead_status(
    state: dict[str, Any],
    channel_id: str,
    status: str,
    error: str | None = None,
) -> None:
    for record in reversed(state["records"]):
        if record.get("channel_id") == channel_id:
            record["status"] = status
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            if error is not None:
                record["error"] = error
            break
    _write_json_atomic(PROCESSED_CREATORS_PATH, state)


def _render_selected_clips(
    source_path: Path,
    clips: list[ClipResult],
    job_dir: Path,
    settings: Any,
    words: list[Word],
) -> list[Path]:
    rendered_paths = [
        render_selected_clip(
            source_path,
            clip,
            words,
            settings,
            job_dir / f"clip_{index:02d}.mp4",
            layout_mode="auto",
            work_dir=job_dir,
        ).path
        for index, clip in enumerate(clips, start=1)
    ]
    if len(rendered_paths) != CLIPS_PER_LEAD:
        raise RuntimeError(
            f"Expected {CLIPS_PER_LEAD} rendered clips; got {len(rendered_paths)}."
        )
    return rendered_paths


def _handle_lead_failure(
    lead: dict[str, Any],
    error: Exception,
    *,
    count_attempt: bool = True,
) -> bool:
    """Requeue a failed lead until MAX_LEAD_ATTEMPTS; return True if requeued.

    Permanent errors (private/members-only/removed video, ...) fail right away.
    ``count_attempt=False`` puts the lead back untouched, e.g. while Drive needs
    a sign-in, so waiting never burns a creator.
    """
    if is_permanent_error(error):
        with _worker_lock():
            state = _processed_state()
            _update_lead_status(state, str(lead.get("channel_id", "")), "failed", str(error))
        logger.info("Not retrying %s: the video can't be used (%s).", lead.get("channel_title"), error)
        return False
    attempts = int(lead.get("attempts", 0)) + (1 if count_attempt else 0)
    channel_id = str(lead.get("channel_id", ""))
    video_id = str(lead.get("latest_video_url", "")).rsplit("=", maxsplit=1)[-1]
    with _worker_lock():
        state = _processed_state()
        if attempts < MAX_LEAD_ATTEMPTS:
            state["processed_channel_ids"] = [
                value for value in state["processed_channel_ids"] if value != channel_id
            ]
            state["processed_video_ids"] = [
                value for value in state["processed_video_ids"] if value != video_id
            ]
            _update_lead_status(
                state,
                channel_id,
                f"retry {attempts}/{MAX_LEAD_ATTEMPTS - 1} queued" if count_attempt else "waiting",
                str(error),
            )
            queue = _lead_queue()
            queue.append({**lead, "attempts": attempts})
            _write_json_atomic(LEAD_QUEUE_PATH, queue)
            return True
        _update_lead_status(state, channel_id, "failed", str(error))
        return False


def _skip_not_a_fit(lead: dict[str, Any], reason: NotAFit) -> None:
    """Record why, and never queue this creator again."""
    channel_id = str(lead.get("channel_id", ""))
    with _worker_lock():
        state = _processed_state()
        _update_lead_status(state, channel_id, "not a fit", str(reason))
        seen = set(_seen_channels())
        if channel_id and channel_id not in seen:
            seen.add(channel_id)
            _write_json_atomic(SEEN_CHANNELS_PATH, sorted(seen))
    logger.info("Skipped %s: %s.", lead.get("channel_title", channel_id), reason)


def _process_lead(
    lead: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    _wait_for_pending_slot()
    check_drive_access()  # before any download: a dead Drive login must not burn the lead
    channel_title = str(lead["channel_title"])
    video_title = str(lead["latest_video_title"])
    video_url = str(lead["latest_video_url"])
    video_id = video_url.rsplit("=", maxsplit=1)[-1]
    shorts_status = str((lead.get("shorts") or {}).get("status") or "")
    job_dir = settings.temp_dir / f"worker_{uuid.uuid4().hex}"
    job_dir.mkdir(parents=True, exist_ok=False)
    source_video: Path | None = None
    try:
        source_video = Path(download_source_video(video_url))
        share = on_camera_share(source_video)
        if share is not None and share < MIN_ON_CAMERA_SHARE:
            raise NotAFit(f"no one on camera (a face in {share:.0%} of sampled frames)")
        audio_path = extract_audio_only(source_video, job_dir / "source.wav")
        words = transcribe_audio(audio_path, settings)
        duration = _video_duration(source_video)
        if duration <= 0:
            raise RuntimeError(f"Source video has invalid duration: {source_video}")

        _info, peaks = video_info_and_peaks(video_url)
        clips, outreach_email, subject = pick_clips_with_agent(
            words,
            duration,
            CLIPS_PER_LEAD,
            channel_title,
            video_title,
            peaks,
            shorts_status=shorts_status,
        )
        rendered = _render_selected_clips(
            source_video,
            clips,
            job_dir,
            settings,
            words,
        )
        drive_link = upload_clips_to_drive(
            channel_title,
            video_title,
            [str(path) for path in rendered],
        )
        email_body = fill_drive_link(outreach_email, drive_link)
        channel_url = str(
            lead.get("channel_url")
            or f"https://www.youtube.com/channel/{lead['channel_id']}"
        )
        contact = lead.get("contact_email") or f"{channel_url}/about"
        pending_record = {
            "record_id": uuid.uuid4().hex,
            "channel_title": channel_title,
            "contact_email": contact,
            "drive_link": drive_link,
            "subject": subject,
            "full_email_body": email_body,
            "video_title": video_title,
            "greeting_name": greeting_name_of(email_body),
            "shorts_status": shorts_status,
            "site_url": SITE_URL,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "channel_id": str(lead["channel_id"]),
            "target_video_id": video_id,
            "status": "pending",
        }
        while True:
            with _worker_lock():
                pending = _pending_records()
                pending_count = sum(
                    record.get("status", "pending") == "pending"
                    for record in pending
                )
                if pending_count < MAX_PENDING_LEADS:
                    pending.append(pending_record)
                    _write_json_atomic(PENDING_OUTREACH_PATH, pending)
                    break
            logger.info(
                "Pending outreach buffer reached its %d-lead cap; waiting for a "
                "lead to be processed.",
                MAX_PENDING_LEADS,
            )
            time.sleep(PENDING_SLOT_POLL_INTERVAL_SEC)
        return pending_record
    finally:
        if source_video is not None:
            source_video.unlink(missing_ok=True)
            for path in settings.raw_dir.glob(f"{source_video.stem}*"):
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
        for path in job_dir.iterdir():
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
        job_dir.rmdir()


def _prune_leftovers(settings: Any) -> None:
    """Delete downloads/temp files older than a day (the app does this on each run too).
    Best effort: a cleanup problem never stops the worker."""
    try:
        removed = prune_stale_files(settings.raw_dir) + prune_stale_files(settings.temp_dir)
    except (AttributeError, OSError):
        logger.debug("Skipped leftover cleanup.", exc_info=True)
        return
    if removed:
        logger.info("Removed %d leftover download/temp file(s) older than a day.", removed)


def _setup_logging() -> None:
    """Terminal output plus storage/logs/worker.log (2 MB x 3 files), so overnight
    problems leave a record."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [console, file_handler]


def _discover_leads(max_results: int) -> list[dict[str, Any]]:
    queries = generate_search_queries(category="All", count=5)
    leads = discover_channels(queries, max_results=max_results)
    set_youtube_quota_exhausted(False)
    return leads


def run_worker(batch: int = 3) -> int:
    if batch < 1:
        raise ValueError("batch must be at least 1.")
    settings = load_settings()
    processed_count = 0
    init_storage_files()
    _prune_leftovers(settings)
    failures = 0
    discovered = False

    _wait_for_pending_slot()
    while processed_count < batch:
        lead = _pop_fresh_lead()
        if lead is None:
            if discovered:
                break
            try:
                leads = _discover_leads(min(50, max(batch * 3, batch)))
            except YouTubeQuotaExceededError as exc:
                set_youtube_quota_exhausted(True, str(exc))
                logger.error(
                    "YouTube quota limit reached; stopping this worker run: %s",
                    exc,
                )
                return 1
            discovered = True
            logger.info("Queued %d new lead(s).", _enqueue_leads(leads))
            continue

        channel_id = str(lead.get("channel_id", ""))
        _set_status("processing", str(lead.get("channel_title") or channel_id))
        try:
            pending_record = _process_lead(lead, settings)
        except DriveAuthError as exc:
            _handle_lead_failure(lead, exc, count_attempt=False)
            logger.error("%s Stopping; the lead is back in the queue.", exc)
            return 1
        except NotAFit as exc:
            _skip_not_a_fit(lead, exc)
            continue
        except Exception as exc:
            failures += 1
            logger.exception(
                "Processing failed for %s (%s)",
                lead.get("channel_title", channel_id),
                channel_id,
            )
            if _handle_lead_failure(lead, exc):
                logger.info("Lead requeued for another attempt.")
            continue
        with _worker_lock():
            state = _processed_state()
            _update_lead_status(state, channel_id, "completed")
        processed_count += 1
        logger.info(
            "Prepared outreach for %s; Drive folder: %s",
            pending_record["channel_title"],
            pending_record["drive_link"],
        )

    logger.info(
        "Worker finished: %d completed, %d failed, batch limit %d.",
        processed_count,
        failures,
        batch,
    )
    return 0 if failures == 0 else 1


def run_continuous_queue(
    buffer_size: int = MAX_PENDING_LEADS,
    poll_interval_sec: float = 5,
) -> None:
    if buffer_size < 1:
        raise ValueError("buffer_size must be at least 1.")
    if poll_interval_sec <= 0:
        raise ValueError("poll_interval_sec must be greater than 0.")
    buffer_size = min(buffer_size, MAX_PENDING_LEADS)

    settings = load_settings()
    init_storage_files()
    _prune_leftovers(settings)
    _set_status("starting")
    discovery_backoff = DISCOVERY_BACKOFF_START_SEC
    logged_full_at: int | None = None
    while True:
        with _worker_lock():
            pending_count = sum(
                record.get("status", "pending") == "pending"
                for record in _pending_records()
            )

        if pending_count >= buffer_size:
            if logged_full_at != pending_count:  # say it once, not every 5 seconds
                _set_status("outbox_full", f"{pending_count}/{buffer_size} pitches waiting")
                logger.info(
                    "Outbox is full (%d/%d pitches waiting). Send or skip one in the "
                    "app and the worker continues.",
                    pending_count,
                    buffer_size,
                )
                logged_full_at = pending_count
            time.sleep(poll_interval_sec)
            continue
        logged_full_at = None

        fresh_lead = _pop_fresh_lead()
        if fresh_lead is None:
            print("Lead queue empty. Scouting new creators...", flush=True)
            _set_status("discovering", "looking for new creators")
            try:
                leads = _discover_leads(max_results=10)
            except YouTubeQuotaExceededError as exc:
                set_youtube_quota_exhausted(True, str(exc))
                _set_status("quota_pause", f"YouTube quota reached; trying again in {QUOTA_COOLDOWN_SEC // 60} min")
                logger.info(
                    "YouTube quota limit reached; pausing discovery for %d minutes.",
                    QUOTA_COOLDOWN_SEC // 60,
                )
                time.sleep(QUOTA_COOLDOWN_SEC)
                continue
            except Exception as exc:
                logger.warning(
                    "Could not discover leads; retrying in 60 seconds: %s",
                    exc,
                )
                time.sleep(60)
                continue
            added = _enqueue_leads(leads)
            if added == 0:
                _set_status("idle", f"no new creators found; looking again in {discovery_backoff // 60} min")
                logger.info(
                    "No new qualified creators found; next discovery in %d minutes.",
                    discovery_backoff // 60,
                )
                time.sleep(discovery_backoff)
                discovery_backoff = min(discovery_backoff * 2, DISCOVERY_BACKOFF_MAX_SEC)
            else:
                logger.info("Queued %d new lead(s).", added)
                discovery_backoff = DISCOVERY_BACKOFF_START_SEC
            continue

        channel_id = str(fresh_lead["channel_id"])
        print("Buffer below threshold. Processing next creator...", flush=True)
        _set_status("processing", str(fresh_lead.get("channel_title") or channel_id))
        try:
            pending_record = _process_lead(fresh_lead, settings)
        except DriveAuthError as exc:
            _handle_lead_failure(fresh_lead, exc, count_attempt=False)
            _set_status("drive_sign_in", "Google Drive needs signing in again")
            logger.error("%s Checking again in %d minutes.", exc, DRIVE_SIGN_IN_WAIT_SEC // 60)
            time.sleep(DRIVE_SIGN_IN_WAIT_SEC)
            continue
        except NotAFit as exc:
            _skip_not_a_fit(fresh_lead, exc)
            continue
        except Exception as exc:
            logger.warning(
                "Continuous queue processing failed for %s (%s); retrying the next "
                "lead in 10 seconds: %s",
                fresh_lead.get("channel_title", channel_id),
                channel_id,
                exc,
            )
            if _handle_lead_failure(fresh_lead, exc):
                logger.info("Lead requeued for another attempt.")
            time.sleep(10)
            continue
        with _worker_lock():
            state = _processed_state()
            _update_lead_status(state, channel_id, "completed")
        logger.info(
            "Queued outreach for %s; Drive folder: %s",
            pending_record["channel_title"],
            pending_record["drive_link"],
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover creators and prepare clip-based outreach drafts."
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=3,
        help="Maximum number of creators to process (default: 3).",
    )
    parser.add_argument(
        "--continuous",
        "--continuous-queue",
        dest="continuous_queue",
        action="store_true",
        help="Keep the pending-outreach buffer filled as pitches are marked sent.",
    )
    parser.add_argument(
        "--buffer-size",
        type=int,
        default=MAX_PENDING_LEADS,
        help=(
            "Pending pitch threshold for continuous mode "
            f"(default: {MAX_PENDING_LEADS}, maximum: {MAX_PENDING_LEADS})."
        ),
    )
    parser.add_argument(
        "--poll-interval-sec",
        type=float,
        default=5,
        help="Seconds between outbox checks (default: 5).",
    )
    arguments = parser.parse_args()
    if arguments.batch < 1:
        parser.error("--batch must be at least 1.")
    _setup_logging()
    init_storage_files()
    try:
        if arguments.continuous_queue:
            try:
                run_continuous_queue(
                    buffer_size=arguments.buffer_size,
                    poll_interval_sec=arguments.poll_interval_sec,
                )
            except KeyboardInterrupt:
                logger.info("Continuous queue stopped.")
                return 0
            except ValueError as exc:
                parser.error(str(exc))
        try:
            return run_worker(arguments.batch)
        except KeyboardInterrupt:
            logger.info("Worker stopped.")
            return 0
    finally:
        _set_status("stopped")


if __name__ == "__main__":
    raise SystemExit(main())
