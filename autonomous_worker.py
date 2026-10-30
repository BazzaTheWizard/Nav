from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import re
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

from config import PROJECT_ROOT, load_settings
from core.downloader import (
    DownloadForbidden,
    UPDATE_HINT,
    download_source_video,
    downloaded_video_info,
    extract_audio_only,
    yt_dlp_version,
)
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
    modern_subject,
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
from core.hook_selector import replay_peaks
from core.pipeline import (
    WeakMoments,
    pick_clips_with_agent,
    render_selected_clip,
    video_info_and_peaks,
)
from core.service_pause import ServicePause
from core import clip_pages
from core.pitch_files import prune_pitch_files, remove_pitch_files, save_pitch_files
from core.transcriber import TranscriptionLimitReached, Word, transcribe_audio
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
CONSOLE_LOG_MAX_BYTES = 1_000_000
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
# Quality gate: clips the second AI pass scores below this (0-100) aren't sent. A
# lead with fewer than 2 clips left is skipped automatically, before your Outbox.
MIN_CLIP_SCORE = 60
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
    """The creator isn't a fit (nobody on camera, or no clip good enough to send).
    Skipped, never retried."""


def _groq_pause_text(error: TranscriptionLimitReached) -> str:
    resume = (datetime.now() + timedelta(seconds=error.retry_after + 60)).strftime("%-I:%M %p")
    return f"Groq transcription limit reached; continues at about {resume}"


# --- Worker status, for the app ------------------------------------------------------
# The worker writes what it's doing here; the app shows it, and checks the process
# id to tell "running" from "stopped" (or closed without saying so).
def _worker_status_path() -> Path:
    return STORAGE_DIR / "worker_status.json"


def clip_link(record: dict[str, Any]) -> str:
    """The link the prospect got: their clip page, or the Drive folder (older pitches,
    or when the page couldn't be made)."""
    return str(record.get("page_url") or record.get("drive_link") or "")


def drive_backup_on() -> bool:
    """DRIVE_BACKUP=off in .env stops the Drive copy once clip pages are set up."""
    return os.getenv("DRIVE_BACKUP", "on").strip().lower() not in {"off", "0", "false", "no"}


def _pitches_dir() -> Path:
    """Outbox preview/trim files (core.pitch_files); the app reads the same folder."""
    return STORAGE_DIR / "pitches"


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
        "stopping": running and _stop_requested(),
    }


# --- Start / stop from the app, and one worker at a time -----------------------------
# Two workers at once would double Groq use and overfill the Outbox, so a worker
# holds a lock for as long as it runs (the system releases it if it crashes).
WAITING_STATES = frozenset({"starting", "outbox_full", "discovering", "idle", "quota_pause", "drive_sign_in"})


def _instance_lock_path() -> Path:
    return STORAGE_DIR / ".worker_running.lock"


def _claim_single_instance() -> Any | None:
    """Open file holding the lock, or None if another worker already runs."""
    import fcntl

    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    handle = _instance_lock_path().open("a", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _stop_request_path() -> Path:
    return STORAGE_DIR / "stop_worker.request"


def _stop_requested() -> bool:
    return _stop_request_path().exists()


def _clear_stop_request() -> None:
    try:
        _stop_request_path().unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not clear the stop request.", exc_info=True)


def _is_worker_process(pid: int) -> bool:
    """Only ever signal the worker: the saved process id could belong to something
    else by now if the worker closed without saying so."""
    try:
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "autonomous_worker" in command


def start_worker() -> bool:
    """Start the worker in the background (keeps the Outbox filled). False if one runs."""
    if get_worker_status()["running"]:
        return False
    _clear_stop_request()
    root = Path(__file__).resolve().parent
    venv_python = root / ".venv" / "bin" / "python"
    command = [str(venv_python if venv_python.exists() else Path(sys.executable)), str(root / "autonomous_worker.py"), "--continuous"]
    if shutil.which("caffeinate"):
        command = ["caffeinate", "-i", *command]  # macOS: the Mac doesn't idle-sleep while it works
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    console_path = LOG_PATH.parent / "worker_console.log"
    try:  # keep it from growing forever: past 1 MB, the old one becomes worker_console.old.log
        if console_path.stat().st_size > CONSOLE_LOG_MAX_BYTES:
            console_path.replace(console_path.with_name("worker_console.old.log"))
    except OSError:
        pass
    with console_path.open("a", encoding="utf-8") as console:
        subprocess.Popen(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=console,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # keeps running if the app or Terminal closes
        )
    return True


def stop_worker() -> str:
    """Stop the worker without losing work. While it waits, it stops now (like Ctrl+C);
    while it's making clips, it finishes that creator first."""
    status = get_worker_status()
    if not status["running"]:
        return "not_running"
    raw = _read_json(_worker_status_path(), {})
    pid = raw.get("pid") if isinstance(raw, dict) else None
    if status["state"] in WAITING_STATES and isinstance(pid, int) and _is_worker_process(pid):
        os.kill(pid, signal.SIGINT)
        return "stopped"
    _stop_request_path().write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    return "after_current"


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


class StateFileError(RuntimeError):
    """A state file exists but can't be read. The worker (and the Outbox) stop instead
    of starting over empty, which would forget who was already pitched."""


def _read_json(path: Path, default: Any, *, required: bool = False) -> Any:
    """``required``: the files that remember who was pitched (processed creators, the
    Outbox, the lead queue). A damaged one is copied aside and StateFileError raised;
    other files fall back to ``default``."""
    if not path.exists():
        return default
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError("the file is empty")
        return json.loads(content)
    except (OSError, ValueError) as exc:  # JSONDecodeError is a ValueError
        if not required:
            logger.warning(
                "Could not read persistent state at %s: %s. Using default.",
                path,
                exc,
            )
            return default
        try:  # named after the file's own time, so the app's reruns don't copy it again
            backup = path.with_name(f"{path.name}.damaged-{datetime.fromtimestamp(path.stat().st_mtime):%Y%m%d-%H%M%S}")
            if not backup.exists():
                shutil.copy2(path, backup)
            saved = f"A copy is in storage/{backup.name}."
        except OSError:
            saved = "It couldn't be copied aside."
        raise StateFileError(
            f"storage/{path.name} can't be read ({exc}). {saved} Nothing was changed: fix or "
            "restore the file (Time Machine, or the copy), then start the worker again."
        ) from exc


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
    raw = _read_json(PROCESSED_CREATORS_PATH, None, required=True)
    if raw is not None and not isinstance(raw, dict):
        raise StateFileError(f"storage/{PROCESSED_CREATORS_PATH.name} isn't in the expected format; nothing was changed.")
    if raw is None:
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
    raw = _read_json(PENDING_OUTREACH_PATH, None, required=True)
    if raw is not None and not isinstance(raw, list):
        raise StateFileError(f"storage/{PENDING_OUTREACH_PATH.name} isn't in the expected format; nothing was changed.")
    if raw is None:
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
            # Unsent drafts written with an older template get the current email once
            # (same clip count, link and first name; a subject you wrote stays).
            body = str(record.get("full_email_body") or "")
            if record.get("status", "pending") == "pending" and is_old_template(body):
                clips = [clip for clip in record.get("clips") or [] if isinstance(clip, dict)]
                record["full_email_body"] = rewrite_old_draft(
                    body,
                    str(record.get("channel_title") or ""),
                    clip_link(record),
                    str(record.get("video_title") or ""),
                    shorts_status=str(record.get("shorts_status") or ""),
                    clip_count=len(clips) or 3,
                )
                old_subject = str(record.get("subject") or "")
                record["subject"] = modern_subject(old_subject) if old_subject else subject_for(record["full_email_body"])
                record["greeting_name"] = greeting_name_of(record["full_email_body"])
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
                    drive_link=clip_link(record),
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
                drive_link=clip_link(record),
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
        if not clip_link(record) or not record.get("record_id"):
            continue  # skipped from the queue before any clips were made
        try:
            when = datetime.fromisoformat(str(record.get("skipped_at")))
        except ValueError:
            continue
        if now - when <= timedelta(days=PASSED_LOOKBACK_DAYS):
            recent.append(record)
    recent.sort(key=lambda record: str(record.get("skipped_at")), reverse=True)
    return recent


def pass_summary(now: datetime | None = None) -> dict[str, int]:
    """Last PASSED_LOOKBACK_DAYS: how often each pass reason was used, plus leads
    the worker skipped by itself ("auto_weak": no clip good enough, "auto_faceless":
    nobody on camera)."""
    now = now or datetime.now(timezone.utc)
    cutoff = timedelta(days=PASSED_LOOKBACK_DAYS)
    counts: dict[str, int] = {}

    def recent(value: object) -> bool:
        try:
            return now - datetime.fromisoformat(str(value)) <= cutoff
        except ValueError:
            return False

    with _worker_lock():
        skipped = _skipped_records()
        state = _processed_state()
    for record in skipped:
        if record.get("pass_reason") and recent(record.get("skipped_at")):
            counts[record["pass_reason"]] = counts.get(record["pass_reason"], 0) + 1
    for record in state.get("records", []):
        if record.get("status") == "not a fit" and recent(record.get("updated_at")):
            key = "auto_weak" if str(record.get("error", "")).startswith("weak moments") else "auto_faceless"
            counts[key] = counts.get(key, 0) + 1
    return counts


def restore_passed_as_sent(record_id: str) -> bool:
    """You did send it: move a passed pitch to Sent (follow-ups then work for it)."""
    with _worker_lock():
        skipped = _skipped_records()
        index = next((i for i, r in enumerate(skipped) if r.get("record_id") == record_id), None)
        if index is None:
            return False
        record = skipped.pop(index)
        sent_at = str(record.get("skipped_at") or datetime.now(timezone.utc).isoformat())
        for key in ("skip_reason", "skipped_at", "pass_reason"):
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
                remove_pitch_files(record_id, _pitches_dir())  # previews are only for the Outbox
                return True
    return False


def record_clip_trim(record_id: str, index: int, start: float, end: float) -> bool:
    """Save a trim on the pitch record (clip ``index`` from 1), keeping the AI's
    original times, so how often and how far cuts get moved can be tuned later."""
    with _worker_lock():
        pending = _pending_records()
        for record in pending:
            clips = record.get("clips")
            if record.get("record_id") != record_id or not isinstance(clips, list):
                continue
            if not 1 <= index <= len(clips) or not isinstance(clips[index - 1], dict):
                return False
            clip = clips[index - 1]
            clip.setdefault("ai_start", clip.get("start"))
            clip.setdefault("ai_end", clip.get("end"))
            clip["start"], clip["end"] = round(start, 1), round(end, 1)
            clip["trimmed_at"] = datetime.now(timezone.utc).isoformat()
            _write_json_atomic(PENDING_OUTREACH_PATH, pending)
            return True
    return False


# Why you passed on a pitch (Outbox buttons). Saved on the record so the lead
# finder, clip picking and framing can be tuned from real numbers.
PASS_REASONS = {
    "bad_fit": "Not a good creator fit",
    "weak_moments": "Clips aren't strong enough",
    "bad_framing": "Framing or captions look off",
    "no_contact": "Can't find a way to contact them",
    "other": "Other",
}


def skip_lead(channel_id: str, reason: str = "user_skipped", pass_reason: str | None = None) -> bool:
    """Pass on a pitch. ``pass_reason``: one of PASS_REASONS (why)."""
    if not channel_id.strip():
        raise ValueError("channel_id must not be empty.")
    if pass_reason is not None and pass_reason not in PASS_REASONS:
        raise ValueError(f"Unknown pass reason {pass_reason!r}.")
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
        if pass_reason:
            skipped_record["pass_reason"] = pass_reason
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
                    if pass_reason:
                        record["pass_reason"] = pass_reason
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
    if skipped_record.get("record_id"):
        remove_pitch_files(str(skipped_record["record_id"]), _pitches_dir())
    return True


def _lead_queue() -> list[dict[str, Any]]:
    queue = _read_json(LEAD_QUEUE_PATH, [], required=True)
    if not isinstance(queue, list):
        raise StateFileError(f"storage/{LEAD_QUEUE_PATH.name} isn't in the expected format; nothing was changed.")
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


def _content_kind(report: dict[str, Any], subject: str, email: str) -> str:
    """'episode' (podcast, interview) or 'video', for the clip page's wording."""
    content_format = str(report.get("format") or "")
    if content_format:
        return "episode" if content_format == "episode" else "video"
    return "episode" if subject.rstrip().endswith(" episode") or " from the episode " in email else "video"


def _render_selected_clips(
    source_path: Path,
    clips: list[ClipResult],
    job_dir: Path,
    settings: Any,
    words: list[Word],
    layouts_out: list[str] | None = None,
    rendered_out: list[Any] | None = None,
) -> list[Path]:
    """Render each clip (2 or 3 after the quality gate). ``layouts_out`` receives
    each clip's layout, for the Outbox record; ``rendered_out`` the full results
    (for the Outbox preview and trim files)."""
    rendered = [
        render_selected_clip(
            source_path,
            clip,
            words,
            settings,
            job_dir / f"clip_{index:02d}.mp4",
            layout_mode="auto",
            work_dir=job_dir,
        )
        for index, clip in enumerate(clips, start=1)
    ]
    if not clips or len(rendered) != len(clips):
        raise RuntimeError(f"Expected {len(clips)} rendered clips; got {len(rendered)}.")
    if layouts_out is not None:
        layouts_out.extend(item.layout_mode for item in rendered)
    if rendered_out is not None:
        rendered_out.extend(rendered)
    return [item.path for item in rendered]


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


def _requeue_interrupted(lead: dict[str, Any]) -> None:
    """Stopped mid-creator (Ctrl+C or the app): put them back, so they aren't lost."""
    try:
        _handle_lead_failure(lead, RuntimeError("stopped before the clips were finished"), count_attempt=False)
        logger.info("%s is back in the queue (stopped before finishing).", lead.get("channel_title"))
    except Exception:
        logger.exception("Could not put %s back in the queue", lead.get("channel_title"))


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
    pages_on = clip_pages.configured()
    # Round 17: with clip pages set up, the page is the link and Drive is only a backup
    # copy, so a Drive problem no longer stops pitches. Without pages, Drive is the link.
    drive_wanted = drive_backup_on() or not pages_on
    if not pages_on:
        check_drive_access()  # before any download: a dead Drive login must not burn the lead
    channel_title = str(lead["channel_title"])
    video_title = str(lead["latest_video_title"])
    video_url = str(lead["latest_video_url"])
    video_id = video_url.rsplit("=", maxsplit=1)[-1]
    shorts_status = str((lead.get("shorts") or {}).get("status") or "")
    job_dir = settings.temp_dir / f"worker_{uuid.uuid4().hex}"
    job_dir.mkdir(parents=True, exist_ok=False)
    source_video: Path | None = None
    record_id = uuid.uuid4().hex
    queued = False
    page_name: str | None = None
    try:
        source_video = Path(download_source_video(video_url))
        share = on_camera_share(source_video)
        if share is not None and share < MIN_ON_CAMERA_SHARE:
            raise NotAFit(f"no one on camera (a face in {share:.0%} of sampled frames)")
        words = load_cached_transcript(video_id)
        if words is None:
            audio_path = extract_audio_only(source_video, job_dir / "source.wav")
            words = transcribe_audio(audio_path, settings)
            save_cached_transcript(video_id, words)  # a retry won't pay Groq again
        else:
            logger.info("Reusing the saved transcript of %s (no Groq call).", video_title)
        duration = _video_duration(source_video)
        if duration <= 0:
            raise RuntimeError(f"Source video has invalid duration: {source_video}")

        downloaded = downloaded_video_info(video_url)
        if downloaded is not None:  # the download already had the heatmap: no second YouTube call
            peaks = replay_peaks(downloaded.get("heatmap"))  # type: ignore[arg-type]
            if peaks:
                logger.info("Using %d 'Most replayed' peak(s) for clip picking.", len(peaks))
        else:
            _info, peaks = video_info_and_peaks(video_url)
        report: dict[str, Any] = {}
        try:
            clips, outreach_email, subject = pick_clips_with_agent(
                words,
                duration,
                CLIPS_PER_LEAD,
                channel_title,
                video_title,
                peaks,
                shorts_status=shorts_status,
                min_judge_score=MIN_CLIP_SCORE,
                report=report,
            )
        except WeakMoments as exc:
            raise NotAFit(f"weak moments: {exc}") from exc
        layouts: list[str] = []
        rendered_items: list[Any] = []
        rendered = _render_selected_clips(
            source_video,
            clips,
            job_dir,
            settings,
            words,
            layouts,
            rendered_items,
        )
        if rendered_items:
            try:
                save_pitch_files(record_id, source_video, rendered_items, words, duration, _pitches_dir())
            except Exception:
                # The pitch is still good; it just can't be previewed or trimmed in the app.
                logger.warning("Could not save the Outbox preview files for %s.", channel_title, exc_info=True)
        page_url = ""
        if pages_on:
            try:
                page_name, page_url = clip_pages.publish_page(
                    channel_title,
                    video_title,
                    [{"path": path, "start": clip.start_time, "end": clip.end_time} for path, clip in zip(rendered, clips)],
                    kind=_content_kind(report, subject, outreach_email),
                )
            except Exception:
                logger.warning(
                    "Couldn't make the clip page for %s%s.", channel_title,
                    "; the email uses the Drive link" if drive_wanted else "", exc_info=True,
                )
        drive_link = ""
        if drive_wanted:
            try:
                drive_link = upload_clips_to_drive(
                    channel_title,
                    video_title,
                    [str(path) for path in rendered],
                )
            except Exception:
                if not page_url:
                    raise  # Drive is the only link
                logger.warning("Drive backup failed for %s; the email uses the clip page.", channel_title, exc_info=True)
        link = page_url or drive_link
        if not link:
            raise RuntimeError("The clip page couldn't be made and DRIVE_BACKUP is off, so there's no link to send.")
        email_body = fill_drive_link(outreach_email, link)
        channel_url = str(
            lead.get("channel_url")
            or f"https://www.youtube.com/channel/{lead['channel_id']}"
        )
        contact = lead.get("contact_email") or f"{channel_url}/about"
        pending_record = {
            "record_id": record_id,
            "channel_title": channel_title,
            "contact_email": contact,
            "drive_link": drive_link,
            "page_name": page_name,
            "page_url": page_url or None,
            "subject": subject,
            "full_email_body": email_body,
            "video_title": video_title,
            "greeting_name": greeting_name_of(email_body),
            "shorts_status": shorts_status,
            "site_url": SITE_URL,
            # For tuning later: what was sent, and how it scored and looked.
            "clips": [
                {
                    "start": round(clip.start_time, 1),
                    "end": round(clip.end_time, 1),
                    "judge": score,
                    "layout": layout,
                }
                for clip, score, layout in zip(
                    clips,
                    report.get("judge_scores") or [None] * len(clips),
                    layouts or [None] * len(clips),
                )
            ],
            "subscriber_count": lead.get("subscriber_count"),
            "video_seconds": round(duration),
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
                    queued = True
                    break
            logger.info(
                "Pending outreach buffer reached its %d-lead cap; waiting for a "
                "lead to be processed.",
                MAX_PENDING_LEADS,
            )
            time.sleep(PENDING_SLOT_POLL_INTERVAL_SEC)
        return pending_record
    except NotAFit:
        drop_cached_transcript(video_id)  # never retried
        raise
    finally:
        if queued:
            drop_cached_transcript(video_id)
        if not queued:
            remove_pitch_files(record_id, _pitches_dir())
            if page_name:
                try:
                    clip_pages.delete_page(page_name)
                except Exception:
                    logger.warning("Couldn't remove the unused clip page %s.", page_name, exc_info=True)
        if source_video is not None:
            source_video.unlink(missing_ok=True)
            for path in settings.raw_dir.glob(f"{source_video.stem}*"):
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
        for path in job_dir.iterdir():
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
        job_dir.rmdir()


# --- Saved transcripts: a retried creator (YouTube error, Gemini pause, crash) doesn't
# pay Groq again. Removed once the pitch is made or the creator is skipped, and after
# TRANSCRIPT_KEEP_DAYS otherwise.
TRANSCRIPT_KEEP_DAYS = 7
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{6,20}")


def _transcripts_dir() -> Path:
    return STORAGE_DIR / "transcripts"


def _transcript_path(video_id: str) -> Path | None:
    return _transcripts_dir() / f"{video_id}.json" if _VIDEO_ID.fullmatch(video_id or "") else None


def load_cached_transcript(video_id: str) -> list[Word] | None:
    path = _transcript_path(video_id)
    if path is None or not path.is_file():
        return None
    try:
        return [Word(*row) for row in json.loads(path.read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        logger.warning("Ignoring an unreadable saved transcript: %s", path)
        return None


def save_cached_transcript(video_id: str, words: list[Word]) -> None:
    path = _transcript_path(video_id)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        rows = [[w.text, w.start, w.end, w.segment_start, w.segment_end] for w in words]
        temporary.write_text(json.dumps(rows), encoding="utf-8")
        temporary.replace(path)
    except (OSError, AttributeError):
        logger.warning("Couldn't save the transcript of %s.", video_id, exc_info=True)


def drop_cached_transcript(video_id: str) -> None:
    path = _transcript_path(video_id)
    if path is not None:
        path.unlink(missing_ok=True)


# Old title-card images and subtitle files too: an interrupted render can leave them.
TEMP_PATTERNS = ("*.mp4", "*.mov", "*.part", "*.wav", "*.m4a", "*.png", "*.ass")


def _prune_leftovers(settings: Any) -> None:
    """Delete downloads/temp files older than a day (the app does this on each run too),
    and saved transcripts older than TRANSCRIPT_KEEP_DAYS.
    Best effort: a cleanup problem never stops the worker."""
    try:
        removed = (
            prune_stale_files(settings.raw_dir)
            + prune_stale_files(settings.temp_dir, patterns=TEMP_PATTERNS)
            + prune_stale_files(_transcripts_dir(), max_age_hours=TRANSCRIPT_KEEP_DAYS * 24, patterns=("*.json",))
        )
    except (AttributeError, OSError):
        logger.debug("Skipped leftover cleanup.", exc_info=True)
        return
    if removed:
        logger.info("Removed %d leftover download/temp file(s) older than a day.", removed)
    try:
        with _worker_lock():
            keep = {str(r.get("record_id")) for r in _pending_records() if r.get("status", "pending") == "pending"}
        pruned = prune_pitch_files(keep, _pitches_dir())
    except (OSError, RuntimeError):
        logger.debug("Skipped preview cleanup.", exc_info=True)
        return
    if pruned:
        logger.info("Removed preview files of %d pitch(es) that left the Outbox.", pruned)
    try:
        removed_pages = prune_clip_pages()
    except Exception:
        logger.warning("Skipped clip page cleanup.", exc_info=True)
        return
    if removed_pages:
        logger.info("Removed %d old clip page(s).", removed_pages)


PAGE_KEEP_DAYS = 60  # a sent pitch's page, if they never replied


def _older_than(value: Any, days: float, now: datetime) -> bool:
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return now - when > timedelta(days=days)


def prune_clip_pages(now: datetime | None = None) -> int:
    """Delete pages nobody needs any more: sent PAGE_KEEP_DAYS ago without a reply, or
    passed more than PASSED_LOOKBACK_DAYS ago (until then 'Passed recently -> it was
    sent' can still bring a pitch back). Only pages named on a record are touched."""
    if not clip_pages.configured():
        return 0
    now = now or datetime.now(timezone.utc)
    with _worker_lock():
        pending, skipped = _pending_records(), _skipped_records()
    due: dict[str, str] = {}  # record_id -> page name
    for record in pending:
        if (record.get("status") == "sent" and record.get("page_name") and not record.get("page_removed_at")
                and not record.get("replied_at") and _older_than(record.get("sent_at"), PAGE_KEEP_DAYS, now)):
            due[str(record.get("record_id"))] = str(record["page_name"])
    for record in skipped:
        if (record.get("page_name") and not record.get("page_removed_at")
                and _older_than(record.get("skipped_at"), PASSED_LOOKBACK_DAYS, now)):
            due[str(record.get("record_id"))] = str(record["page_name"])
    removed: set[str] = set()
    for record_id, name in due.items():
        try:
            clip_pages.delete_page(name)
            removed.add(record_id)
        except Exception:
            logger.warning("Couldn't remove the old clip page %s.", name, exc_info=True)
    if removed:
        stamp = now.isoformat()
        with _worker_lock():
            for path, records in ((PENDING_OUTREACH_PATH, _pending_records()), (SKIPPED_LEADS_PATH, _skipped_records())):
                changed = False
                for record in records:
                    if str(record.get("record_id")) in removed and not record.get("page_removed_at"):
                        record["page_removed_at"] = stamp
                        changed = True
                if changed:
                    _write_json_atomic(path, records)
    return len(removed)


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


# --- Pauses for outside services (Round 17) --------------------------------------------
# YouTube blocking downloads, Gemini's limit used up, the clip judge not answering: the
# creator goes back in the queue without using an attempt, and the worker waits. If the
# same service pauses again before a pitch gets through, the wait doubles (up to 4 h),
# so a limit that lasts all night doesn't cost a download every half hour.
MAX_SERVICE_PAUSE_SEC = 4 * 3600
FORBIDDEN_STREAK_PAUSE = 2  # HTTP 403s in a row (different creators) before pausing
_pause_streak: dict[str, int] = {}


def _pause_for_service(lead: dict[str, Any], error: ServicePause) -> float:
    """Requeue the creator (no attempt used), show the pause in the app; returns how
    many seconds to wait."""
    _handle_lead_failure(lead, error, count_attempt=False)
    streak = _pause_streak.get(error.service, 0)
    _pause_streak[error.service] = streak + 1
    wait = min(error.retry_after * 2**streak, MAX_SERVICE_PAUSE_SEC)
    resume = (datetime.now() + timedelta(seconds=wait)).strftime("%-I:%M %p")
    text = f"{error}; trying again at about {resume}" + (f". {error.hint}" if error.hint else "")
    _set_status("quota_pause", text)
    logger.warning("%s. %s is back in the queue (no attempt used).", text.rstrip("."), lead.get("channel_title"))
    return wait


def _forbidden_pause() -> ServicePause:
    return ServicePause(
        f"YouTube refused {FORBIDDEN_STREAK_PAUSE} downloads in a row (HTTP 403)",
        30 * 60, service="youtube", hint=UPDATE_HINT,
    )


def run_worker(batch: int = 3) -> int:
    if batch < 1:
        raise ValueError("batch must be at least 1.")
    settings = load_settings()
    processed_count = 0
    init_storage_files()
    _prune_leftovers(settings)
    failures = 0
    discovered = False

    _clear_stop_request()
    _wait_for_pending_slot()
    while processed_count < batch:
        if _stop_requested():
            _clear_stop_request()
            logger.info("Stopped from the app.")
            break
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
        except KeyboardInterrupt:
            _requeue_interrupted(lead)
            raise
        except StateFileError:
            raise
        except DriveAuthError as exc:
            _handle_lead_failure(lead, exc, count_attempt=False)
            logger.error("%s Stopping; the lead is back in the queue.", exc)
            return 1
        except ServicePause as exc:
            _pause_for_service(lead, exc)
            logger.warning("Stopping this run; start it again later.")
            return 1
        except TranscriptionLimitReached as exc:
            _handle_lead_failure(lead, exc, count_attempt=False)
            _set_status("quota_pause", _groq_pause_text(exc))
            logger.warning("%s Stopping; the lead is back in the queue.", exc)
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
        _pause_streak.clear()
        processed_count += 1
        logger.info("Prepared outreach for %s; clips: %s", pending_record["channel_title"], clip_link(pending_record))

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
    _clear_stop_request()  # an old request must not stop a fresh start
    _set_status("starting")
    discovery_backoff = DISCOVERY_BACKOFF_START_SEC
    logged_full_at: int | None = None
    forbidden_streak = 0
    while True:
        if _stop_requested():
            _clear_stop_request()
            logger.info("Stopped from the app.")
            return
        with _worker_lock():
            pending_count = sum(
                record.get("status", "pending") == "pending"
                for record in _pending_records()
            )

        if pending_count >= buffer_size:
            if logged_full_at != pending_count:  # say it once, not every 5 seconds
                _set_status("outbox_full", f"{pending_count}/{buffer_size} pitches waiting")
                logger.info(
                    "Outbox is full (%d/%d pitches waiting). Leave this running: it makes "
                    "the next pitch as soon as you send or pass one.",
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
        except KeyboardInterrupt:
            _requeue_interrupted(fresh_lead)
            raise
        except StateFileError:
            raise
        except DriveAuthError as exc:
            _handle_lead_failure(fresh_lead, exc, count_attempt=False)
            _set_status("drive_sign_in", "Google Drive needs signing in again")
            logger.error("%s Checking again in %d minutes.", exc, DRIVE_SIGN_IN_WAIT_SEC // 60)
            time.sleep(DRIVE_SIGN_IN_WAIT_SEC)
            continue
        except TranscriptionLimitReached as exc:
            # No switching to the less accurate local model: wait for Groq to reset.
            _handle_lead_failure(fresh_lead, exc, count_attempt=False)
            _set_status("quota_pause", _groq_pause_text(exc))
            logger.warning("%s Waiting; the lead is back in the queue.", exc)
            time.sleep(exc.retry_after + 60)
            continue
        except ServicePause as exc:
            time.sleep(_pause_for_service(fresh_lead, exc))
            continue
        except NotAFit as exc:
            _skip_not_a_fit(fresh_lead, exc)
            forbidden_streak = 0
            continue
        except Exception as exc:
            if isinstance(exc, DownloadForbidden):
                forbidden_streak += 1
                if forbidden_streak >= FORBIDDEN_STREAK_PAUSE:
                    forbidden_streak = 0
                    time.sleep(_pause_for_service(fresh_lead, _forbidden_pause()))
                    continue
            else:
                forbidden_streak = 0
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
        _pause_streak.clear()
        forbidden_streak = 0
        logger.info("Queued outreach for %s; clips: %s", pending_record["channel_title"], clip_link(pending_record))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover creators and prepare clip-based outreach drafts."
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=None,
        help="One-off run: make pitches for this many creators, then stop.",
    )
    parser.add_argument(
        "--continuous",
        "--continuous-queue",
        dest="continuous_queue",
        action="store_true",
        help="Keep the Outbox filled as pitches are sent (the default without --batch).",
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
    if arguments.batch is not None and arguments.batch < 1:
        parser.error("--batch must be at least 1.")
    continuous = arguments.continuous_queue or arguments.batch is None
    _setup_logging()
    logger.info("Worker starting (yt-dlp %s).", yt_dlp_version())  # most YouTube 403 waves are fixed by an update
    instance = _claim_single_instance()
    if instance is None:
        logger.info("Another worker is already running (see the app), so this one isn't starting.")
        return 0
    try:
        init_storage_files()
        if continuous:
            try:
                run_continuous_queue(
                    buffer_size=arguments.buffer_size,
                    poll_interval_sec=arguments.poll_interval_sec,
                )
            except KeyboardInterrupt:
                logger.info("Continuous queue stopped.")
            except ValueError as exc:
                parser.error(str(exc))
            return 0
        try:
            return run_worker(arguments.batch)
        except KeyboardInterrupt:
            logger.info("Worker stopped.")
            return 0
    except StateFileError as exc:
        logger.error("Worker stopped: %s", exc)
        return 1
    finally:
        _set_status("stopped")
        instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
