"""Transcripts of video files (Make clips), saved by the file's fingerprint, so making
clips from the same file again doesn't pay Groq a second time.

(Round 28: the "Finish a long video" tool that also lived here was removed.)
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from config import PROJECT_ROOT
from core.transcriber import Word

logger = logging.getLogger(__name__)

TRANSCRIPTS_DIR = PROJECT_ROOT / "storage" / "transcripts" / "files"
FINGERPRINT_BYTES = 4 * 1024 * 1024


# --- Transcripts of files ------------------------------------------------------------
def file_fingerprint(path: Path) -> str:
    """Same file, same fingerprint (its size and its first and last 4 MB), so a copy
    or an upload of the same export is recognized."""
    size = path.stat().st_size
    digest = hashlib.sha1(str(size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(FINGERPRINT_BYTES))
        if size > FINGERPRINT_BYTES:
            handle.seek(max(FINGERPRINT_BYTES, size - FINGERPRINT_BYTES))
            digest.update(handle.read(FINGERPRINT_BYTES))
    return digest.hexdigest()[:20]


def load_file_transcript(fingerprint: str) -> list[Word] | None:
    path = TRANSCRIPTS_DIR / f"{fingerprint}.json"
    try:
        return [Word(*row) for row in json.loads(path.read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return None


def save_file_transcript(fingerprint: str, words: list[Word]) -> None:
    try:
        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        rows = [[w.text, w.start, w.end, w.segment_start, w.segment_end] for w in words]
        (TRANSCRIPTS_DIR / f"{fingerprint}.json").write_text(json.dumps(rows), encoding="utf-8")
    except OSError:
        logger.warning("Couldn't save the transcript (%s).", fingerprint, exc_info=True)
