from __future__ import annotations

import os
import shutil
import time
from pathlib import Path


def render_output_dir() -> Path:
    return Path.home() / "Desktop" / "ShortFormClips"


def purge_storage(*directories: str | Path, keep_recent_minutes: float = 0) -> int:
    """Remove generated files and nested directories without removing storage roots.

    The background worker's job folders (worker_*) are always kept, and so is any
    file changed in the last ``keep_recent_minutes`` (a download or render that is
    still running). Returns how many items were kept.
    """
    cutoff = time.time() - keep_recent_minutes * 60
    kept = 0
    for directory_value in directories:
        directory = Path(directory_value).expanduser()
        if directory.is_symlink():
            raise ValueError(f"Refusing to purge a symlinked storage directory: {directory}")
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        for child in directory.iterdir():
            if child.name.startswith("worker_") and child.is_dir() and not child.is_symlink():
                kept += 1
                continue
            if keep_recent_minutes and not child.is_symlink():
                try:
                    if child.stat().st_mtime >= cutoff:
                        kept += 1
                        continue
                except FileNotFoundError:
                    continue
            if child.is_symlink() or child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
    return kept


def prune_output_folder(
    max_clips: int = 15,
    max_age_hours: float = 48,
    output_dir: str | Path | None = None,
) -> int:
    """Delete generated clips older than the age limit or beyond the newest-file limit."""
    if max_clips < 0:
        raise ValueError("max_clips must be non-negative.")
    if max_age_hours < 0:
        raise ValueError("max_age_hours must be non-negative.")

    directory = Path(output_dir) if output_dir is not None else render_output_dir()
    directory = directory.expanduser()
    if directory.is_symlink():
        raise ValueError(f"Refusing to prune a symlinked output directory: {directory}")
    directory.mkdir(parents=True, exist_ok=True)

    now = time.time()
    max_age_seconds = max_age_hours * 3600
    clips = sorted(
        (
            path
            for path in directory.glob("clip_*.mp4")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    retained = 0
    for clip in clips:
        age_seconds = now - clip.stat().st_mtime
        if age_seconds > max_age_seconds or retained >= max_clips:
            clip.unlink()
            removed += 1
        else:
            retained += 1
    return removed


def storage_usage_bytes(*directories: str | Path) -> int:
    """Return total bytes stored beneath the supplied directories."""
    total = 0
    for directory_value in directories:
        directory = Path(directory_value).expanduser()
        if directory.is_symlink() or not directory.exists():
            continue
        for root, subdirectories, filenames in os.walk(directory, followlinks=False):
            root_path = Path(root)
            subdirectories[:] = [
                name for name in subdirectories if not (root_path / name).is_symlink()
            ]
            for filename in filenames:
                file_path = root_path / filename
                if file_path.is_symlink():
                    continue
                total += file_path.stat().st_size
    return total


def prune_stale_files(
    directory: str | Path,
    max_age_hours: float = 24,
    patterns: tuple[str, ...] = ("*.mp4", "*.mov", "*.part", "*.wav", "*.m4a"),
    keep: tuple[str | Path, ...] = (),
) -> int:
    """Delete leftover downloads/temp files older than the age limit.

    Only files directly inside ``directory`` that match ``patterns`` are removed;
    folders (for example an in-progress worker job) are left alone.
    """
    if max_age_hours < 0:
        raise ValueError("max_age_hours must be non-negative.")
    folder = Path(directory).expanduser()
    if folder.is_symlink() or not folder.is_dir():
        return 0
    keep_paths = {Path(path).expanduser().resolve() for path in keep}
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for pattern in patterns:
        for path in folder.glob(pattern):
            if path.is_symlink() or not path.is_file():
                continue
            if path.resolve() in keep_paths:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
    return removed
