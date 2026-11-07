"""🎬 Make clips' batches and their Sent state (Round 32), like the Outbox's pitches.

Every batch made in Make clips (the highlights of one video, or one Series batch) is
kept in storage/made_clips.json, so it's still listed after the app restarts. A batch is
"to send" until you mark it sent (the whole batch, or clip by clip); then it's in the
📨 Sent view with when it was sent and, if you say, who to. Moving it back is one click.
Nothing here touches the clip files themselves: discarding or removing a clip in Make
clips takes it off the batch too.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _default_path() -> Path:
    """storage/made_clips.json; under tests (tests/run_all.py sets NAV_MADE_CLIPS, and every
    app test sets NAV_NO_CLIP_PAGES) a temporary file, so a test never lists fake batches
    in your real Make clips."""
    if os.getenv("NAV_MADE_CLIPS"):
        return Path(os.environ["NAV_MADE_CLIPS"])
    if os.getenv("NAV_NO_CLIP_PAGES") == "1":
        return Path(tempfile.mkdtemp(prefix="made-clips-")) / "made_clips.json"
    return Path(__file__).resolve().parents[1] / "storage" / "made_clips.json"


BATCHES_PATH = _default_path()
_LOCK = threading.Lock()
KEEP = 200  # the newest batches kept (older ones drop off the list)
TO_SEND, SENT, PARTLY = "to send", "sent", "partly sent"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _read() -> list[dict[str, Any]]:
    try:
        data = json.loads(BATCHES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [batch for batch in data if isinstance(batch, dict) and batch.get("batch_id")] if isinstance(data, list) else []


def _write(batches: list[dict[str, Any]]) -> None:
    BATCHES_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = BATCHES_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(batches[:KEEP], indent=2), encoding="utf-8")
    temporary.replace(BATCHES_PATH)


def status(batch: dict[str, Any]) -> str:
    """"sent" (every clip sent), "partly sent" or "to send"."""
    clips = batch.get("clips") or []
    sent = sum(bool(clip.get("sent_at")) for clip in clips)
    if clips and sent == len(clips):
        return SENT
    return PARTLY if sent else TO_SEND


def batches(view: str = "all") -> list[dict[str, Any]]:
    """The batches, newest first. ``view``: "all", "to send" (anything not fully sent,
    as the Outbox shows what's left to send) or "sent"."""
    found = sorted(_read(), key=lambda b: str(b.get("created_at")), reverse=True)
    if view == SENT:
        return [b for b in found if status(b) == SENT]
    if view == TO_SEND:
        return [b for b in found if status(b) != SENT]
    return found


def counts() -> dict[str, int]:
    found = _read()
    sent = sum(status(b) == SENT for b in found)
    return {TO_SEND: len(found) - sent, SENT: sent}


def get(batch_id: str) -> dict[str, Any] | None:
    return next((b for b in _read() if b.get("batch_id") == batch_id), None)


def record_batch(video_title: str, channel_title: str, source: str, clips: list[dict[str, Any]],
                 kind: str = "highlights", batch_id: str = "") -> dict[str, Any]:
    """Save a new batch: ``clips`` are {"path", "title", "start", "end"}. Returns it."""
    batch = {
        "batch_id": batch_id or uuid.uuid4().hex[:12],
        "kind": kind,
        "created_at": _now(),
        "video_title": video_title,
        "channel_title": channel_title,
        "source": source,
        "clips": [{"path": _key(c["path"]), "title": str(c.get("title") or ""), "start": c.get("start"),
                   "end": c.get("end"), "sent_at": None} for c in clips],
        "page_name": "",
        "page_url": "",
        "sent_to": "",
        "sent_at": None,
    }
    with _LOCK:
        found = [b for b in _read() if b.get("batch_id") != batch["batch_id"]]
        _write([batch, *found])
    return batch


def _update(batch_id: str, change) -> bool:
    with _LOCK:
        found = _read()
        batch = next((b for b in found if b.get("batch_id") == batch_id), None)
        if batch is None:
            return False
        change(batch)
        batch["sent_at"] = max((c["sent_at"] for c in batch.get("clips", []) if c.get("sent_at")), default=None) \
            if status(batch) == SENT else None
        _write(found)
        return True


def mark_sent(batch_id: str, paths: list[str] | None = None, sent_to: str | None = None) -> bool:
    """Mark the whole batch (or only the clips at ``paths``) as sent. False if it's gone."""
    keys = {_key(p) for p in paths} if paths is not None else None
    stamp = _now()

    def change(batch: dict[str, Any]) -> None:
        for clip in batch.get("clips", []):
            if (keys is None or clip.get("path") in keys) and not clip.get("sent_at"):
                clip["sent_at"] = stamp
        if sent_to is not None:
            batch["sent_to"] = sent_to.strip()

    return _update(batch_id, change)


def mark_unsent(batch_id: str, paths: list[str] | None = None) -> bool:
    """Move the batch (or some of its clips) back to "to send"."""
    keys = {_key(p) for p in paths} if paths is not None else None

    def change(batch: dict[str, Any]) -> None:
        for clip in batch.get("clips", []):
            if keys is None or clip.get("path") in keys:
                clip["sent_at"] = None

    return _update(batch_id, change)


def set_page(batch_id: str, page_name: str, page_url: str) -> bool:
    """The client preview page the batch was published to."""
    return _update(batch_id, lambda batch: batch.update(page_name=page_name, page_url=page_url))


def replace_clip(batch_id: str, old_path: str | Path, new_path: str | Path, start: float | None = None,
                 end: float | None = None) -> bool:
    """A clip was trimmed or re-rendered: the batch follows it (its sent state stays)."""
    old = _key(old_path)

    def change(batch: dict[str, Any]) -> None:
        for clip in batch.get("clips", []):
            if clip.get("path") == old:
                clip["path"] = _key(new_path)
                if start is not None:
                    clip["start"], clip["end"] = start, end

    return _update(batch_id, change)


def remove_clip(batch_id: str, path: str | Path) -> bool:
    """A clip was removed in Make clips: off the batch too (an empty batch is dropped)."""
    key = _key(path)
    with _LOCK:
        found = _read()
        for batch in found:
            if batch.get("batch_id") == batch_id:
                batch["clips"] = [c for c in batch.get("clips", []) if c.get("path") != key]
        _write([b for b in found if b.get("clips")])
    return True


def remove_batch(batch_id: str) -> bool:
    """The batch was discarded: it's gone from the list."""
    with _LOCK:
        found = _read()
        kept = [b for b in found if b.get("batch_id") != batch_id]
        _write(kept)
    return len(kept) != len(found)
