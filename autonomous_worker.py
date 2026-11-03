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
    SignInRequired,
    UPDATE_HINT,
    allow_browser_cookies,
    download_source_video,
    downloaded_video_info,
    extract_audio_only,
    fetch_video_info,
    yt_dlp_version,
)
from core.drive_uploader import DriveAuthError, check_drive_access, remove_clip_in_drive, upload_clips_to_drive
from core.face_tracker import camera_check
from core.storage_manager import prune_stale_files
from core.hook_selector import ClipResult
from core.outreach import (
    FOLLOW_UP_DAYS,
    FOLLOW_UP_LABELS,
    LEGACY_SITE_URL,
    SITE_URL,
    build_follow_up_step,
    clean_first_name,
    build_outreach_email,
    build_yes_reply,
    follow_up_steps,
    fill_drive_link,
    greeting_name_of,
    is_old_template,
    modern_subject,
    set_clip_count,
    set_subject_count,
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
    lead_profile,
)
from core.hook_selector import replay_peaks
from core.pipeline import (
    MIN_CLIPS,
    WeakMoments,
    pick_clips_with_agent,
    render_selected_clip,
    video_info_and_peaks,
)
from core.service_pause import ServicePause
from core import clip_pages
from core.pitch_files import pitch_dir, prune_pitch_files, remove_clip_files, remove_pitch_files, save_pitch_files
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
# (narration over a still image, slides, B-roll): those creators aren't a fit, so they're
# skipped right after download, before any transcribing, AI, rendering or upload.
MIN_ON_CAMERA_SHARE = 0.25
# Animated channels (2D, anime, whiteboard, AI cartoon explainers) have no people on camera
# but characters with faces (core.character_faces). They pass when a face of any kind is in
# this share of the sampled frames: higher than for people, so the odd face-like chart or
# icon in a slide deck can't carry a faceless video through.
MIN_CHARACTER_SHARE = 0.40
# ⏩ Force to queue (Pipeline tab): the creator's clips are still all scored by the AI,
# but none is dropped for scoring under MIN_CLIP_SCORE.
FORCED_MIN_CLIP_SCORE = 0
FORCED_STATUS = "queued (forced)"
# What a lead carries (the queue entry), kept on its record for forcing it again later.
LEAD_KEYS = (
    "channel_id", "channel_title", "channel_url", "channel_about_url", "latest_video_url",
    "latest_video_title", "contact_email", "contact_info", "social_links", "subscriber_count",
    "duration_seconds", "shorts", "shorts_status", "shorts_summary", "priority",
)
# Slides, tutorials or gameplay with a small webcam in a corner (core.face_tracker
# camera_check): skipped as not a fit, with this code at the start of the reason.
SCREEN_SHARE_PIP = "screen_share_pip"
# Age-restricted videos need a signed-in 18+ YouTube account (core.downloader). Without
# one that works they're skipped as not a fit, so ⏩ Force to queue can retry them later.
AGE_RESTRICTED = "age_restricted"
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
                    facts=record.get("pitch") if isinstance(record.get("pitch"), dict) else None,
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


FOLLOW_UP_AFTER_DAYS = FOLLOW_UP_DAYS["bump"]  # the first follow-up (kept for older code)


def _link_in_first_email(record: dict[str, Any]) -> bool:
    """True for pitches sent before round 25: their first email had the clip link."""
    body = str(record.get("full_email_body") or "")
    link = clip_link(record)
    return (bool(link) and link in body) or "://" in body


def _follow_ups_sent(record: dict[str, Any]) -> list[str]:
    """When each follow-up went out (oldest first). Older records only have
    "followed_up_at" (one follow-up)."""
    sent = record.get("follow_ups")
    if isinstance(sent, list):
        return [str(item) for item in sent]
    return [str(record["followed_up_at"])] if record.get("followed_up_at") else []


def _content_format_of(record: dict[str, Any]) -> str:
    facts = record.get("pitch") if isinstance(record.get("pitch"), dict) else {}
    if facts.get("format"):
        return str(facts["format"])
    return "episode" if str(record.get("subject") or "").rstrip().endswith(" episode") else "video"


def _clip_count_of(record: dict[str, Any]) -> int:
    clips = [clip for clip in record.get("clips") or [] if isinstance(clip, dict)]
    return len(clips) or 3


def _first_name_of(record: dict[str, Any]) -> str:
    """The first name the pitch greeted ('' for 'Hi there' or a channel name)."""
    name = str(record.get("greeting_name") or "")
    return clean_first_name(name)


def next_follow_up(record: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Where a sent pitch is in its follow-ups: {"step": "bump"/"show"/"last"/
    "bump_link" or None when all are sent, "number": 1.., "total": steps,
    "due": bool, "due_on": ISO date}."""
    now = now or datetime.now(timezone.utc)
    steps = follow_up_steps(_link_in_first_email(record))
    done = len(_follow_ups_sent(record))
    if done >= len(steps):
        return {"step": None, "number": done, "total": len(steps), "due": False, "due_on": ""}
    step = steps[done]
    try:
        sent_at = datetime.fromisoformat(str(record.get("sent_at")))
    except ValueError:
        return {"step": None, "number": done, "total": len(steps), "due": False, "due_on": ""}
    due_at = sent_at + timedelta(days=FOLLOW_UP_DAYS[step])
    return {
        "step": step,
        "number": done + 1,
        "total": len(steps),
        "due": now >= due_at,
        "due_on": due_at.date().isoformat(),
    }


def _with_follow_up(record: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The record plus its next follow-up (text ready to send once it's due)."""
    body = str(record.get("full_email_body") or "")
    plan = next_follow_up(record, now)
    item = {
        **record,
        "follow_up_due": plan["due"],
        "follow_up_step": plan["step"],
        "follow_up_number": plan["number"],
        "follow_up_total": plan["total"],
        "follow_up_on": plan["due_on"],
        "follow_ups_sent": len(_follow_ups_sent(record)),
    }
    if plan["step"]:
        facts = record.get("pitch") if isinstance(record.get("pitch"), dict) else {}
        item["follow_up_label"] = FOLLOW_UP_LABELS[plan["step"]]
        item["follow_up_subject"] = f"Re: {record.get('subject') or subject_for(body)}"
        item["follow_up_body"] = build_follow_up_step(
            plan["step"],
            _first_name_of(record),
            clip_link(record),
            _clip_count_of(record),
            _content_format_of(record),
            video_title=str(record.get("video_title") or ""),
            video_topic=topic_from_email(body) or str(facts.get("topic") or ""),
        )
    return item


def get_follow_ups_due(now: datetime | None = None) -> list[dict[str, Any]]:
    """Sent pitches with a follow-up due now (see FOLLOW_UP_DAYS in core/outreach.py).

    Each comes with a ready "follow_up_body". Mark a creator as replied in the app
    (or mark the follow-up sent) and they drop off this list until the next one.
    """
    return [item for item in get_awaiting_reply(now) if item["follow_up_due"]]


AWAITING_REPLY_DAYS = 30  # sent emails older than this drop off the "waiting" list


def get_awaiting_reply(now: datetime | None = None) -> list[dict[str, Any]]:
    """Sent pitches with no reply yet (newest first), each with its next follow-up:
    "follow_up_due", "follow_up_step", "follow_up_number"/"follow_up_total",
    "follow_up_on" (date it's due) and, while there's one left, "follow_up_subject"
    and "follow_up_body".
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
        if now - sent_at > timedelta(days=AWAITING_REPLY_DAYS):
            continue
        waiting.append(_with_follow_up(record, now))
    waiting.sort(key=lambda item: str(item.get("sent_at")), reverse=True)
    return waiting


REPLY_OUTCOMES = {
    "yes": "Said yes",
    "no": "Not interested",
    "other": "Other reply",
}
REPLIES_SHOWN_DAYS = 60


def get_replies(now: datetime | None = None) -> list[dict[str, Any]]:
    """Pitches that got a reply in the last REPLIES_SHOWN_DAYS (newest reply first).
    "reply" is "yes", "no" or "other" (older replies, marked before round 25, are
    "other"). A "yes" comes with "yes_reply_body": your email with the link and offer."""
    now = now or datetime.now(timezone.utc)
    with _worker_lock():
        records = _pending_records()
    replies: list[dict[str, Any]] = []
    for record in records:
        if record.get("status") != "sent" or not record.get("replied_at"):
            continue
        try:
            replied_at = datetime.fromisoformat(str(record["replied_at"]))
        except ValueError:
            continue
        if now - replied_at > timedelta(days=REPLIES_SHOWN_DAYS):
            continue
        reply = str(record.get("reply") or "other")
        item = {**record, "reply": reply if reply in REPLY_OUTCOMES else "other"}
        if item["reply"] == "yes":
            body = str(record.get("full_email_body") or "")
            item["yes_reply_subject"] = f"Re: {record.get('subject') or subject_for(body)}"
            item["yes_reply_body"] = build_yes_reply(
                _first_name_of(record), clip_link(record), _clip_count_of(record), _content_format_of(record)
            )
        replies.append(item)
    replies.sort(key=lambda item: str(item.get("replied_at")), reverse=True)
    return replies


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
    nobody on camera, "auto_screen_share": a screen share with a corner webcam,
    "auto_age": an age-restricted video that needs a signed-in account)."""
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
            error = str(record.get("error", ""))
            key = (
                "auto_weak" if error.startswith("weak moments")
                else "auto_screen_share" if error.startswith(SCREEN_SHARE_PIP)
                else "auto_age" if error.startswith(AGE_RESTRICTED)
                else "auto_faceless"
            )
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
    """outcome: "followed_up" (the next follow-up was sent), "yes" / "no" / "other"
    (they replied; "replied" is the same as "other"), or "client" (a "yes" became a
    trial or a paying client)."""
    if outcome not in {"followed_up", "replied", "client", *REPLY_OUTCOMES}:
        raise KeyError(outcome)
    now = datetime.now(timezone.utc).isoformat()
    with _worker_lock():
        records = _pending_records()
        for record in records:
            if record.get("record_id") != record_id or record.get("status") != "sent":
                continue
            if outcome == "followed_up":
                record["follow_ups"] = _follow_ups_sent(record) + [now]
                record.setdefault("followed_up_at", now)
            elif outcome == "client":
                record["client_at"] = now
            else:
                record["replied_at"] = now
                record["reply"] = "other" if outcome == "replied" else outcome
            _write_json_atomic(PENDING_OUTREACH_PATH, records)
            return True
    return False


def undo_reply(record_id: str) -> bool:
    """Marked a reply by mistake: back to "waiting for a reply"."""
    with _worker_lock():
        records = _pending_records()
        for record in records:
            if record.get("record_id") == record_id and record.get("status") == "sent":
                for key in ("replied_at", "reply", "client_at"):
                    record.pop(key, None)
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


def remove_pitch_clip(record_id: str, index: int) -> dict[str, Any]:
    """Drop clip ``index`` (from 1) from a pitch in the Outbox: off their clip page, out
    of the Drive backup, this Mac's saved files and the record. Later clips move up one
    number, and the email and subject say the new count. The page and Drive change
    first, so if one fails, nothing on this Mac has changed yet and you can try again.
    Returns {"count": clips left, "email_updated": bool, "drive_failed": bool}."""
    with _worker_lock():
        record = next(
            (r for r in _pending_records() if r.get("record_id") == record_id and r.get("status", "pending") == "pending"),
            None,
        )
    if record is None:
        raise ValueError("This pitch isn't in the Outbox any more.")
    manifest = pitch_dir(record_id, _pitches_dir()) / "pitch.json"
    count = (
        len(json.loads(manifest.read_text(encoding="utf-8")).get("clips", []))
        if manifest.is_file()
        else len(record.get("clips") or [])
    )
    if count <= MIN_CLIPS:
        raise ValueError(f"A pitch keeps at least {MIN_CLIPS} clips. To send fewer, Pass this creator.")
    if not 1 <= index <= count:
        raise ValueError(f"There's no clip {index} in this pitch.")

    page_name = str(record.get("page_name") or "")
    if page_name and not record.get("page_removed_at"):
        clip_pages.remove_clip(page_name, index, count)
    drive_failed = False
    drive_link = str(record.get("drive_link") or "")
    if drive_link:
        try:
            remove_clip_in_drive(drive_link, index, count)
        except Exception:
            if not record.get("page_url"):
                raise  # Drive is the link in the email: it has to match
            drive_failed = True
            logger.warning("Couldn't remove clip %d from the Drive backup of %s.", index, record.get("channel_title"), exc_info=True)
    if manifest.is_file():
        remove_clip_files(record_id, index, _pitches_dir())

    with _worker_lock():
        pending = _pending_records()
        current = next((r for r in pending if r.get("record_id") == record_id), None)
        if current is None:
            raise ValueError("This pitch isn't in the Outbox any more.")
        clips = current.get("clips")
        removed: Any = None
        if isinstance(clips, list) and len(clips) >= index:
            # Kept for tuning: which clips you took out (their scores and framing).
            removed = clips.pop(index - 1)
            current.setdefault("removed_clips", []).append(removed)
        body = str(current.get("full_email_body") or "")
        email_updated, named_clip_removed = False, False
        facts = current.get("pitch") if isinstance(current.get("pitch"), dict) else None
        if facts and isinstance(removed, dict) and _same_moment(removed.get("start"), facts.get("at")):
            # The email names the moment you took out. If it's still the email the app
            # wrote, it now names the best clip left; if you edited it, the app tells you.
            unedited = body == _email_from_facts(current, facts, count)
            if unedited:
                _name_best_remaining(facts, clips if isinstance(clips, list) else [])
                body, email_updated = _email_from_facts(current, facts, count - 1), True
            else:
                named_clip_removed = True
        if not email_updated:
            body, email_updated = set_clip_count(body, count - 1)
        current["full_email_body"] = body
        current["subject"] = set_subject_count(str(current.get("subject") or ""), count - 1)
        _write_json_atomic(PENDING_OUTREACH_PATH, pending)
    logger.info("Removed clip %d from the pitch for %s; %d left.", index, record.get("channel_title"), count - 1)
    return {
        "count": count - 1,
        "email_updated": email_updated,
        "drive_failed": drive_failed,
        "named_clip_removed": named_clip_removed,
    }


def _same_moment(first: Any, second: Any) -> bool:
    try:
        return abs(float(first) - float(second)) <= 1.0
    except (TypeError, ValueError):
        return False


def _email_from_facts(record: dict[str, Any], facts: dict[str, Any], count: int) -> str:
    """The first email the app writes for this pitch, from its saved moment."""
    at = facts.get("at")
    return build_outreach_email(
        str(record.get("channel_title") or ""),
        video_title=str(record.get("video_title") or ""),
        video_topic=str(facts.get("topic") or ""),
        clip_count=count,
        opening_at=float(at) if isinstance(at, (int, float)) else None,
        key_point=str(facts.get("point") or ""),
        greeting_name=greeting_name_of(str(record.get("full_email_body") or "")),
        content_format=str(facts.get("format") or ""),
        business=bool(facts.get("business")),
    )


def _name_best_remaining(facts: dict[str, Any], clips: list[Any]) -> None:
    """Point the saved moment at the best clip left that has a point (by AI score);
    with none, the email falls back to 'I watched your recent ...'."""
    candidates = [clip for clip in clips if isinstance(clip, dict) and clip.get("point")]
    best = max(candidates, key=lambda clip: clip.get("judge") or 0, default=None)
    if best is None:
        facts.update(at=None, point="", seconds=None)
        return
    try:
        seconds = round(float(best["end"]) - float(best["start"]), 1)
    except (KeyError, TypeError, ValueError):
        seconds = None
    facts.update(at=best.get("start"), point=str(best["point"]), seconds=seconds)


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


FIRST_IN_LINE = 1000  # "Do next" (and a forced creator whose turn has come) go before every scored lead


def _lead_rank(lead: dict[str, Any]) -> int:
    """Rank from the lead alone. A lead that failed drops back so it doesn't block the
    queue. "Do next" comes first (the worker takes the highest rank, so moving one to
    the top of the list alone didn't make it next)."""
    try:
        priority = int(lead.get("priority", 0) or 0)
        attempts = int(lead.get("attempts", 0) or 0)
    except (TypeError, ValueError):
        priority, attempts = 0, 0
    return (FIRST_IN_LINE if lead.get("do_next") else 0) + priority - 3 * attempts


def _queue_ranks(queue: list[dict[str, Any]]) -> list[float]:
    """Each queued lead's rank, in queue order (the worker takes the highest; ties go
    in list order). A ⏩ forced creator joins the back of the line: while anyone it
    waits for ("waits_for": who was queued when it was forced) is still queued, it ranks
    just under the lowest of them, so it comes after all of them, and leads found later
    slot in by score as usual. Once they're done it goes first. Forced before round 23
    (no "waits_for"): first."""
    index_of = {str(lead.get("channel_id")): index for index, lead in enumerate(queue)}
    ranks: dict[int, float] = {}

    def rank(index: int, visiting: frozenset[int]) -> float:
        if index in ranks:
            return ranks[index]
        lead = queue[index]
        value: float = _lead_rank(lead)
        if lead.get("forced") and not lead.get("do_next"):
            waiting = {index_of[c] for c in map(str, lead.get("waits_for") or []) if c in index_of} - visiting - {index}
            if waiting:
                value = min(rank(other, visiting | {index}) for other in waiting) - 0.5
            else:
                value += FIRST_IN_LINE
        ranks[index] = value
        return value

    return [rank(index, frozenset()) for index in range(len(queue))]


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
            ranks = _queue_ranks(valid)
            chosen = valid.pop(max(range(len(valid)), key=ranks.__getitem__))  # max() keeps the first of equals
        _write_json_atomic(LEAD_QUEUE_PATH, valid)
        if chosen is not None:
            _reserve_lead(state, chosen)
        return chosen


def get_lead_queue() -> list[dict[str, Any]]:
    """Queued creators in the order the worker will take them (for the app)."""
    with _worker_lock():
        queue = _lead_queue()
    ranks = _queue_ranks(queue)
    return [queue[i] for i in sorted(range(len(queue)), key=ranks.__getitem__, reverse=True)]  # stable: ties keep list order


def prioritize_queued_lead(channel_id: str) -> bool:
    """Move a queued creator to the top, so the worker does them next."""
    with _worker_lock():
        queue = _lead_queue()
        index = next((i for i, lead in enumerate(queue) if str(lead.get("channel_id")) == channel_id), None)
        if index is None:
            return False
        queue.insert(0, {**queue.pop(index), "do_next": True})
        _write_json_atomic(LEAD_QUEUE_PATH, queue)
    return True


# --- ⏩ Force to queue (Pipeline tab) -----------------------------------------------------
FORCEABLE_GROUPS = {
    "unfinished": "Didn't finish",
    "worker": "Skipped by the worker",
    "you": "Passed or skipped by you",
}


# 🧹 Clear (Pipeline tab): hides a group's creators up to now. Nothing is deleted: they
# stay "done", so the harvester never finds them again, and ↩ Restore brings them back.
CLEARABLE_GROUPS = ("worker", "you")


def _cleared_path() -> Path:
    return STORAGE_DIR / "forceable_cleared.json"


def _cleared_cutoffs() -> dict[str, str]:
    raw = _read_json(_cleared_path(), {})
    return {str(k): str(v) for k, v in raw.items() if k in CLEARABLE_GROUPS} if isinstance(raw, dict) else {}


def _is_cleared(row: dict[str, Any], cutoffs: dict[str, str]) -> bool:
    cutoff = cutoffs.get(str(row.get("group")))
    if not cutoff:
        return False
    try:
        return datetime.fromisoformat(str(row.get("updated_at"))) <= datetime.fromisoformat(cutoff)
    except ValueError:
        return True  # no usable time: it was there before the clear


def clear_forceable_group(group: str) -> int:
    """🧹 Clear: hide every creator now in ``group`` ("worker": skipped by the worker,
    "you": passed or skipped by you). Doesn't touch the queue, the Outbox or sent
    pitches. Returns how many were hidden."""
    if group not in CLEARABLE_GROUPS:
        raise ValueError(f"Only these lists can be cleared: {', '.join(CLEARABLE_GROUPS)}.")
    visible = [row for row in get_forceable_leads() if row["group"] == group and not row["cleared"]]
    with _worker_lock():
        cutoffs = _cleared_cutoffs()
        cutoffs[group] = datetime.now(timezone.utc).isoformat()
        _write_json_atomic(_cleared_path(), cutoffs)
    logger.info("Cleared %d creator(s) from '%s'.", len(visible), FORCEABLE_GROUPS[group])
    return len(visible)


def restore_forceable_group(group: str) -> None:
    """↩ Restore: show a cleared list's creators again."""
    with _worker_lock():
        cutoffs = _cleared_cutoffs()
        cutoffs.pop(group, None)
        _write_json_atomic(_cleared_path(), cutoffs)


def get_forceable_leads() -> list[dict[str, Any]]:
    """Creators that can be forced back into the queue, newest first: ones that didn't
    finish (failed, stuck), ones the worker skipped ("not a fit"), and ones you passed
    or skipped. Not: sent, already in the Outbox or the queue, or being made right now.
    Each: {channel_id, channel_title, status, reason, group, updated_at, video_url,
    cleared (hidden by 🧹 Clear)}."""
    with _worker_lock():
        records = _processed_state()["records"]
        skipped = _skipped_records()
        queued = {str(lead.get("channel_id")) for lead in _lead_queue()}
        outbox = {str(r.get("channel_id")) for r in _pending_records() if r.get("status", "pending") == "pending"}
    status_now = get_worker_status()
    busy = status_now.get("detail") if status_now.get("running") and status_now.get("state") == "processing" else None
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        latest[str(record.get("channel_id") or "")] = record  # newest record per creator wins
    rows: dict[str, dict[str, Any]] = {}
    for channel_id, record in latest.items():
        status = str(record.get("status") or "")
        if not channel_id or status in {"sent", "completed", FORCED_STATUS} or channel_id in queued | outbox:
            continue
        if status == "processing" and busy == record.get("channel_title"):
            continue
        group = "worker" if status == "not a fit" else "you" if status == "skipped" else "unfinished"
        reason = record.get("error") or PASS_REASONS.get(str(record.get("pass_reason") or ""), "") or record.get("skip_reason") or ""
        rows[channel_id] = {
            "channel_id": channel_id, "channel_title": record.get("channel_title") or channel_id, "status": status,
            "reason": str(reason), "group": group, "updated_at": str(record.get("updated_at") or record.get("created_at") or ""),
            "video_url": f"https://www.youtube.com/watch?v={record['video_id']}" if record.get("video_id") else "",
        }
    for record in skipped:  # skipped from the queue: no work was done, so no record above
        channel_id = str(record.get("channel_id") or "")
        if not channel_id or channel_id in rows or channel_id in queued | outbox or record.get("status") != "skipped":
            continue
        if str(latest.get(channel_id, {}).get("status")) in {"sent", "completed", FORCED_STATUS}:
            continue
        rows[channel_id] = {
            "channel_id": channel_id, "channel_title": record.get("channel_title") or channel_id, "status": "skipped",
            "reason": PASS_REASONS.get(str(record.get("pass_reason") or ""), "") or "skipped from the queue",
            "group": "you", "updated_at": str(record.get("skipped_at") or ""),
            "video_url": str(record.get("latest_video_url") or ""),
        }
    cutoffs = _cleared_cutoffs()
    for row in rows.values():
        row["cleared"] = _is_cleared(row, cutoffs)
    return sorted(rows.values(), key=lambda row: row["updated_at"], reverse=True)


def _lead_from_saved(channel_id: str, record: dict[str, Any] | None, passed: dict[str, Any] | None) -> dict[str, Any]:
    """Rebuild a queue entry from what was saved: the record's lead (round 20 on), or
    the passed/skipped entry, or the record's own fields (older records)."""
    saved: dict[str, Any] = dict((record or {}).get("lead") or {})
    if not saved and passed:
        saved = {key: passed[key] for key in LEAD_KEYS if passed.get(key) not in (None, "")}
        saved.setdefault("latest_video_title", passed.get("video_title") or "")
        if not saved.get("latest_video_url") and passed.get("target_video_id"):
            saved["latest_video_url"] = f"https://www.youtube.com/watch?v={passed['target_video_id']}"
    if str(saved.get("contact_email") or "").startswith(("http://", "https://")):
        saved.pop("contact_email")  # the About page link, not an email: the worker adds it again
    video_id = (record or {}).get("video_id")
    lead = {key: saved[key] for key in LEAD_KEYS if saved.get(key) not in (None, "")}
    for key in ("shorts", "duration_seconds", "priority"):
        if key not in lead and (record or {}).get(key) not in (None, ""):
            lead[key] = record[key]
    lead.update(
        channel_id=channel_id,
        channel_title=str(saved.get("channel_title") or (record or {}).get("channel_title") or (passed or {}).get("channel_title") or channel_id),
        channel_url=str(saved.get("channel_url") or f"https://www.youtube.com/channel/{channel_id}"),
        latest_video_url=str(saved.get("latest_video_url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")),
        latest_video_title=str(saved.get("latest_video_title") or ""),
        attempts=0,
        forced=True,
        forced_at=datetime.now(timezone.utc).isoformat(),
    )
    return lead


def force_lead_to_queue(channel_id: str) -> dict[str, Any]:
    """⏩ Force to queue: put a creator back in line (one the worker skipped or gave up
    on, or one you passed or skipped), at the BACK: they're done after every creator
    already queued, without interrupting anything ("waits_for"). Their old status, error
    and pass/skip reason are cleared (kept under "forced_from" for tuning), and they're
    no longer marked as done. Forced creators skip the 'nobody on camera' and 'screen
    share' checks and the AI score bar. Returns the queued lead."""
    channel_id = channel_id.strip()
    if not channel_id:
        raise ValueError("channel_id must not be empty.")
    with _worker_lock():
        if any(str(r.get("channel_id")) == channel_id and r.get("status", "pending") == "pending" for r in _pending_records()):
            raise ValueError("They're already in your Outbox.")
        queue = _lead_queue()
        queued = next((lead for lead in queue if str(lead.get("channel_id")) == channel_id), None)
        if queued is not None:  # already queued: keep their place, just turn the checks off
            ranks = _queue_ranks(queue)
            mine = queue.index(queued)
            ahead = [str(lead.get("channel_id")) for i, lead in enumerate(queue)
                     if ranks[i] > ranks[mine] or (ranks[i] == ranks[mine] and i < mine)]
            updated = {**queued, "forced": True, "waits_for": ahead}
            queue[queue.index(queued)] = updated
            _write_json_atomic(LEAD_QUEUE_PATH, queue)
            return updated
        record = next((r for r in reversed(_processed_state()["records"]) if str(r.get("channel_id")) == channel_id), None)
        passed = next((r for r in reversed(_skipped_records()) if str(r.get("channel_id")) == channel_id), None)
    if record is not None and record.get("status") in {"sent", "completed"}:
        raise ValueError("They were already pitched.")
    if record is None and passed is None:
        raise ValueError("There are no saved details for this creator.")
    lead = _lead_from_saved(channel_id, record, passed)
    if not lead["latest_video_url"]:
        raise ValueError("No video is saved for this creator.")
    if not lead["latest_video_title"]:  # older records didn't keep it (one quick YouTube lookup)
        try:
            lead["latest_video_title"] = str(fetch_video_info(lead["latest_video_url"]).get("title") or "")
        except Exception as exc:
            logger.warning("Couldn't look up the video title for %s: %s", lead["channel_title"], exc)
        lead["latest_video_title"] = lead["latest_video_title"] or "Latest video"
    video_id = lead["latest_video_url"].rsplit("=", maxsplit=1)[-1]
    now = datetime.now(timezone.utc).isoformat()
    with _worker_lock():
        state = _processed_state()
        state["processed_channel_ids"] = [c for c in state["processed_channel_ids"] if c != channel_id]
        state["processed_video_ids"] = [v for v in state["processed_video_ids"] if v != video_id]
        latest = next((r for r in reversed(state["records"]) if str(r.get("channel_id")) == channel_id), None)
        if latest is not None:
            latest["forced_from"] = {key: latest.get(key) for key in ("status", "error", "skip_reason", "pass_reason") if latest.get(key)}
            for key in ("error", "skip_reason", "pass_reason"):
                latest.pop(key, None)
            latest["status"] = FORCED_STATUS
            latest["updated_at"] = now
        _write_json_atomic(PROCESSED_CREATORS_PATH, state)
        skipped = _skipped_records()
        remaining = [r for r in skipped if str(r.get("channel_id")) != channel_id]
        if len(remaining) != len(skipped):
            _write_json_atomic(SKIPPED_LEADS_PATH, remaining)
        queue = [entry for entry in _lead_queue() if str(entry.get("channel_id")) != channel_id]
        lead["waits_for"] = [str(entry.get("channel_id")) for entry in queue]
        queue.append(lead)  # the back of the line
        _write_json_atomic(LEAD_QUEUE_PATH, queue)
    logger.info("Forced %s into the queue (was: %s).", lead["channel_title"], (record or passed or {}).get("status"))
    return lead


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
            # Round 20: the lead itself, so "Force to queue" can put them back later.
            "lead": {key: lead[key] for key in LEAD_KEYS if lead.get(key) not in (None, "")},
            **({"forced": True} if lead.get("forced") else {}),
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
            title_fade_in=False,  # the hook is on screen from the very first frame
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
    forced = bool(lead.get("forced"))
    job_dir = settings.temp_dir / f"worker_{uuid.uuid4().hex}"
    job_dir.mkdir(parents=True, exist_ok=False)
    source_video: Path | None = None
    record_id = uuid.uuid4().hex
    queued = False
    page_name: str | None = None
    try:
        try:
            source_video = Path(download_source_video(video_url))
        except SignInRequired as exc:
            if exc.age_restricted:
                raise NotAFit(f"{AGE_RESTRICTED}: YouTube needs a signed-in 18+ account for this video") from exc
            raise
        # Right after the download, before any transcribing, AI, rendering or upload.
        # A creator you forced into the queue skips these checks: you decided.
        camera = camera_check(source_video) if not forced else None
        if camera is not None and camera.screen_share_pip:
            raise NotAFit(f"{SCREEN_SHARE_PIP}: a screen share with a small webcam in the {camera.pip_corner} corner")
        share = camera.share if camera is not None else None
        if share is not None and share < MIN_ON_CAMERA_SHARE:
            characters = camera.character_share
            if characters is None or characters < MIN_CHARACTER_SHARE:
                found = f"; cartoon or drawn faces in {characters:.0%}" if characters else ""
                raise NotAFit(f"no one on camera (a face in {share:.0%} of sampled frames{found})")
            logger.info("%s: animated characters on screen (%.0f%% of sampled frames); making clips.",
                        channel_title, characters * 100)
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
                # Forced: every clip is still scored (never unscored filler), but none
                # is dropped for a low score; remove weak ones in the Outbox.
                min_judge_score=FORCED_MIN_CLIP_SCORE if forced else MIN_CLIP_SCORE,
                report=report,
                business=bool(lead.get("business_offer")),
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
                    **({"point": point} if point else {}),
                }
                for clip, score, layout, point in zip(
                    clips,
                    report.get("judge_scores") or [None] * len(clips),
                    layouts or [None] * len(clips),
                    report.get("clip_points") or [""] * len(clips),
                )
            ],
            # Round 25: the moment the email names, for follow-ups and LinkedIn messages.
            **({"pitch": report["pitch_facts"]} if isinstance(report.get("pitch_facts"), dict) else {}),
            **({"business_offer": lead["business_offer"]} if lead.get("business_offer") else {}),
            **({"linkedin_url": lead["linkedin_url"]} if lead.get("linkedin_url") else {}),
            "subscriber_count": lead.get("subscriber_count"),
            "video_seconds": round(duration),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "channel_id": str(lead["channel_id"]),
            "target_video_id": video_id,
            **({"forced": True} if forced else {}),
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
    # Round 25: B2B experts by default (LEAD_PROFILE=wide in .env: every niche).
    queries = generate_search_queries(category="B2B" if lead_profile() == "b2b" else "All", count=5)
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
    # Browser cookies only if you opt in (core.downloader); a cookies.txt file is always used.
    load_settings()  # reads .env
    allow_browser_cookies(os.getenv("YTDLP_COOKIES_IN_WORKER", "").strip().lower() in {"1", "true", "yes", "on"})
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
