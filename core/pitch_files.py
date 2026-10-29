"""Per-pitch files on this Mac, so the Outbox can preview and trim clips.

storage/pitches/<record_id>/
    pitch.json                 what was rendered, and how to render it again
    clip_01.mp4                the clip as sent (full quality, same file as in Drive)
    clip_01.preview.mp4        720p copy the app plays (the app loads videos into memory)
    clip_01.source.mp4         the source from 30 s before to 30 s after the clip,
                               copied without re-encoding (starts on a keyframe)
    clip_01.trim.mp4           a trimmed version waiting for "Use this" (+ .trim.preview.mp4)

Everything is deleted when the pitch is sent or passed.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config import PROJECT_ROOT
from core.clipper import MAX_CLIP_DURATION, MIN_CLIP_DURATION
from core.transcriber import Word

logger = logging.getLogger(__name__)

PITCHES_DIR = PROJECT_ROOT / "storage" / "pitches"
TRIM_MARGIN_SEC = 30.0  # how far each end can move from the AI's cut
PREVIEW_WIDTH = 720
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def pitch_dir(record_id: str, root: Path | None = None) -> Path:
    """``root``: storage/pitches by default; the worker passes its own storage folder's."""
    if not _SAFE_ID.fullmatch(str(record_id)):
        raise ValueError(f"Not a pitch id: {record_id!r}")
    return (root or PITCHES_DIR) / str(record_id)


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} was not found. Install FFmpeg with `brew install ffmpeg`.")
    return path


def _run(command: list[str], timeout: float = 600) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{Path(command[0]).name} failed: {result.stderr.strip()[-400:]}")
    return result


def _duration(path: Path) -> float:
    out = _run([_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]).stdout
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def keyframe_at_or_before(source: Path, seconds: float) -> float:
    """Time of the last keyframe at or before ``seconds`` (0 if none is found).
    Cutting there without re-encoding gives an exact, known start."""
    if seconds <= 0:
        return 0.0
    ffprobe = _tool("ffprobe")
    start_out = _run([ffprobe, "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0", str(source)]).stdout
    try:
        file_start = float(start_out.strip())
    except ValueError:
        file_start = 0.0
    for look_back in (15.0, 60.0, 240.0):
        low = max(0.0, seconds - look_back)
        out = _run([
            ffprobe, "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
            "-show_entries", "frame=pts_time", "-of", "csv=p=0",
            "-read_intervals", f"{low + file_start:.3f}%{seconds + file_start + 0.05:.3f}", str(source),
        ]).stdout
        times = []
        for line in out.split():
            try:
                times.append(float(line.strip(",")) - file_start)
            except ValueError:
                continue
        before = [t for t in times if t <= seconds + 0.001]
        if before:
            return max(0.0, max(before))
    return 0.0


def cut_source_slice(source: Path, start: float, end: float, output: Path) -> float:
    """Copy source[keyframe <= start, end] without re-encoding. Returns the source time
    at the slice's 0:00 (source time = that + slice time, for picture and sound)."""
    keyframe = keyframe_at_or_before(source, start)
    _run([
        _tool("ffmpeg"), "-v", "error", "-y", "-ss", f"{keyframe:.3f}", "-i", str(source),
        "-t", f"{max(0.1, end - keyframe):.3f}", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(output),
    ])
    # The copied sound usually starts a few hundredths of a second before the first
    # frame (sound comes in fixed-size blocks), so the picture starts a little after 0:00.
    out = _run([
        _tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=start_time",
        "-of", "csv=p=0", str(output),
    ]).stdout
    try:
        video_start = float(out.strip().strip(","))
    except ValueError:
        video_start = 0.0
    return keyframe - video_start


def make_preview(clip: Path, output: Path) -> None:
    """Small copy for the app's player: same framing, captions and sound."""
    _run([
        _tool("ffmpeg"), "-v", "error", "-y", "-i", str(clip), "-vf", f"scale={PREVIEW_WIDTH}:-2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(output),
    ])


def _manifest_path(record_id: str) -> Path:
    return pitch_dir(record_id) / "pitch.json"


def _write_manifest(record_id: str, manifest: dict[str, Any], root: Path | None = None) -> None:
    path = pitch_dir(record_id, root) / "pitch.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_pitch(record_id: str) -> dict[str, Any] | None:
    """The saved pitch, or None (made before round 15, or already cleaned up)."""
    try:
        path = _manifest_path(record_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    directory = path.parent
    for item in manifest.get("clips", []):
        item["path"] = directory / item["file"]
        item["preview_path"] = directory / item["preview"]
        trim = directory / _trim_name(item["file"])
        item["trim_waiting"] = trim.is_file() and (directory / _trim_name(item["preview"])).is_file()
        item["trim_preview_path"] = directory / _trim_name(item["preview"])
    return manifest


def _trim_name(name: str) -> str:
    stem, _, suffix = name.partition(".")
    return f"{stem}.trim.{suffix}"


def save_pitch_files(
    record_id: str,
    source_video: Path,
    rendered: list[Any],
    words: list[Word],
    source_duration: float,
    root: Path | None = None,
) -> Path:
    """Keep what the Outbox needs to preview and trim this pitch's clips.
    ``rendered``: core.pipeline.RenderedClip items, in clip order."""
    directory = pitch_dir(record_id, root)
    directory.mkdir(parents=True, exist_ok=True)
    clips: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(rendered, start=1):
            name = f"clip_{index:02d}"
            start, end = float(item.clip.start_time), float(item.clip.end_time)
            shutil.copy2(item.path, directory / f"{name}.mp4")
            make_preview(directory / f"{name}.mp4", directory / f"{name}.preview.mp4")
            slice_end = min(source_duration, end + TRIM_MARGIN_SEC) if source_duration > 0 else end + TRIM_MARGIN_SEC
            slice_start = cut_source_slice(source_video, max(0.0, start - TRIM_MARGIN_SEC), slice_end, directory / f"{name}.source.mp4")
            slice_end = slice_start + _duration(directory / f"{name}.source.mp4")
            slice_start = max(slice_start, 0.0)
            window = [
                [word.text, round(word.start - slice_start, 3), round(word.end - slice_start, 3)]
                for word in words
                if word.end > slice_start and word.start < slice_end
            ]
            clips.append({
                "index": index,
                "file": f"{name}.mp4",
                "preview": f"{name}.preview.mp4",
                "source": f"{name}.source.mp4",
                "start": round(start, 3),
                "end": round(end, 3),
                "ai_start": round(start, 3),
                "ai_end": round(end, 3),
                "slice_start": round(slice_start, 3),
                "slice_end": round(slice_end, 3),
                "layout": item.layout_mode,
                "clip": item.clip.model_dump(),
                "words": window,
            })
        _write_manifest(record_id, {
            "record_id": record_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "clips": clips,
        }, root)
    except Exception:
        remove_pitch_files(record_id, root)
        raise
    return directory


def trim_limits(item: dict[str, Any]) -> tuple[float, float]:
    """Earliest start and latest end this clip can be trimmed to. (A clip that ends with
    the video can find the slice a few milliseconds short; its own end stays allowed.)"""
    return float(item["slice_start"]), max(float(item["slice_end"]) - 0.05, float(item["end"]))


def rerender_trim(record_id: str, index: int, new_start: float, new_end: float, settings: Any) -> Path:
    """Render the clip again with new start/end times (seconds in the source video),
    exactly like the worker does. Waits as clip_NN.trim.mp4 until accept_trim()."""
    from core.hook_selector import ClipResult
    from core.pipeline import render_selected_clip

    manifest = load_pitch(record_id)
    if manifest is None:
        raise RuntimeError("This pitch's files aren't on this Mac any more.")
    item = next((clip for clip in manifest["clips"] if clip["index"] == index), None)
    if item is None:
        raise ValueError(f"No clip {index} in this pitch.")
    earliest, latest = trim_limits(item)
    if new_start < earliest - 0.001 or new_end > latest + 0.001:
        raise ValueError(f"Trims can move each end up to {TRIM_MARGIN_SEC:.0f} s from the AI's cut.")
    length = new_end - new_start
    if not MIN_CLIP_DURATION <= length <= MAX_CLIP_DURATION:
        raise ValueError(f"A clip has to be {MIN_CLIP_DURATION:.0f}–{MAX_CLIP_DURATION:.0f} s long (this one would be {length:.1f} s).")

    offset = float(item["slice_start"])
    clip = ClipResult(**{**item["clip"], "start_time": new_start - offset, "end_time": new_end - offset})
    words = [Word(text=text, start=start, end=end) for text, start, end in item["words"]]
    directory = pitch_dir(record_id)
    work_dir = Path(settings.temp_dir) / f"trim_{uuid.uuid4().hex[:12]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        rendered = render_selected_clip(
            directory / item["source"], clip, words, settings, work_dir / item["file"],
            layout_mode=str(item["layout"]), work_dir=work_dir,
        )
        trimmed = directory / _trim_name(item["file"])
        shutil.move(str(rendered.path), trimmed)
        make_preview(trimmed, directory / _trim_name(item["preview"]))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    manifest_raw = json.loads(_manifest_path(record_id).read_text(encoding="utf-8"))
    for saved in manifest_raw["clips"]:
        if saved["index"] == index:
            saved["trim"] = {"start": round(new_start, 3), "end": round(new_end, 3)}
    _write_manifest(record_id, manifest_raw)
    logger.info("Trimmed clip %d of pitch %s to %.1f–%.1f s (not used yet).", index, record_id, new_start, new_end)
    return trimmed


def accept_trim(
    record_id: str,
    index: int,
    replace_remote: Callable[[Path, str], None] | None = None,
) -> dict[str, float]:
    """Use the trimmed version: ``replace_remote(path, file_name)`` swaps the file the
    prospect sees (Drive) first, then the local files follow. Returns the new times."""
    directory = pitch_dir(record_id)
    manifest = json.loads(_manifest_path(record_id).read_text(encoding="utf-8"))
    item = next((clip for clip in manifest["clips"] if clip["index"] == index), None)
    if item is None or "trim" not in item:
        raise ValueError("There's no trimmed version waiting for this clip.")
    trimmed = directory / _trim_name(item["file"])
    trimmed_preview = directory / _trim_name(item["preview"])
    if not trimmed.is_file() or not trimmed_preview.is_file():
        raise RuntimeError("The trimmed version is missing; render it again.")
    if replace_remote is not None:
        replace_remote(trimmed, item["file"])
    trimmed.replace(directory / item["file"])
    trimmed_preview.replace(directory / item["preview"])
    times = item.pop("trim")
    item["start"], item["end"] = times["start"], times["end"]
    _write_manifest(record_id, manifest)
    return times


def discard_trim(record_id: str, index: int) -> None:
    directory = pitch_dir(record_id)
    manifest = json.loads(_manifest_path(record_id).read_text(encoding="utf-8"))
    for item in manifest["clips"]:
        if item["index"] == index:
            item.pop("trim", None)
            (directory / _trim_name(item["file"])).unlink(missing_ok=True)
            (directory / _trim_name(item["preview"])).unlink(missing_ok=True)
    _write_manifest(record_id, manifest)


def remove_pitch_files(record_id: str, root: Path | None = None) -> None:
    """Best effort: called when a pitch is sent or passed."""
    try:
        shutil.rmtree(pitch_dir(record_id, root), ignore_errors=True)
    except ValueError:
        pass


def prune_pitch_files(keep: set[str], directory: Path) -> int:
    """Delete folders of pitches that are no longer in the Outbox. ``directory`` is
    passed in (storage/pitches) so a test pointed at a temp folder can't touch yours."""
    if not directory.is_dir():
        return 0
    removed = 0
    for folder in directory.iterdir():
        if folder.is_dir() and _SAFE_ID.fullmatch(folder.name) and folder.name not in keep:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed


def pitch_files_bytes() -> int:
    if not PITCHES_DIR.is_dir():
        return 0
    return sum(path.stat().st_size for path in PITCHES_DIR.rglob("*") if path.is_file())


__all__ = [
    "PITCHES_DIR", "TRIM_MARGIN_SEC", "accept_trim", "discard_trim", "load_pitch", "pitch_dir",
    "pitch_files_bytes", "prune_pitch_files", "remove_pitch_files", "rerender_trim", "save_pitch_files", "trim_limits",
]
