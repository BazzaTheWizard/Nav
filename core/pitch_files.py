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

import numpy as np

from config import PROJECT_ROOT
from core.clipper import MAX_CLIP_DURATION, MIN_CLIP_DURATION
from core.transcriber import Word

logger = logging.getLogger(__name__)

PITCHES_DIR = PROJECT_ROOT / "storage" / "pitches"
TRIM_MARGIN_SEC = 30.0  # how far each end can move from the AI's cut
PREVIEW_WIDTH = 720
# Cut a slice from just after its keyframe. FFmpeg goes back to the keyframe at or before
# the time it's given; a time written to the millisecond can land a hair *before* the
# keyframe (298.96530 written as 298.965), and the slice then started one keyframe
# (2-5 s) earlier than recorded: trimmed clips' captions were off by that much.
SEEK_NUDGE = 0.004  # under half a frame even at 120 fps
# Every slice's start is checked by finding the clip's sound in it (10 ms steps).
ALIGN_STEP = 0.01
MIN_ALIGN_MATCH = 0.8  # correlation of the two loudness curves; real matches are ~1.0
# A stream-copied slice's sound can sit ~20-30 ms off its picture (AAC comes in 21 ms
# blocks). Captions follow the sound, so the slice start measured from the sound wins.
SLICE_FIX_LOG = 0.04  # seconds: a bigger difference is logged as a wrong slice
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
        _tool("ffmpeg"), "-v", "error", "-y", "-ss", f"{keyframe + SEEK_NUDGE if keyframe > 0 else 0.0:.4f}", "-i", str(source),
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


def _loudness_curve(path: Path) -> np.ndarray:
    """Log loudness every ALIGN_STEP seconds (mono, 8 kHz)."""
    raw = subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
        capture_output=True, timeout=300,
    ).stdout
    samples = np.frombuffer(raw, np.int16).astype(np.float64)
    block = int(8000 * ALIGN_STEP)
    samples = samples[: samples.size // block * block].reshape(-1, block)
    return np.log10((samples ** 2).mean(axis=1) + 1.0) if samples.size else np.zeros(0)


def find_clip_in_slice(slice_path: Path, clip_path: Path) -> float | None:
    """Where (seconds into the slice) the clip's sound starts, or None when it can't be
    matched (no sound, or too different). The clip was rendered from the full source,
    so this is the ground truth for the slice's start: slice start = clip start - this.
    (Loudness normalization only scales the sound, so the curves still line up.)"""
    try:
        long, short = _loudness_curve(slice_path), _loudness_curve(clip_path)
    except (OSError, subprocess.SubprocessError):
        return None
    n = short.size
    if n < 200 or long.size < n or short.std() < 0.05:
        return None
    short = short - short.mean()
    numerator = np.correlate(long, short, mode="valid")
    sums = np.concatenate([[0.0], np.cumsum(long)])
    squares = np.concatenate([[0.0], np.cumsum(long ** 2)])
    window_sum = sums[n:] - sums[:-n]
    window_var = np.maximum(squares[n:] - squares[:-n] - window_sum ** 2 / n, 1e-12)
    match = numerator / np.sqrt(window_var * float((short ** 2).sum()))
    best = int(np.argmax(match))
    if match[best] < MIN_ALIGN_MATCH:
        return None
    return best * ALIGN_STEP


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
            found = find_clip_in_slice(directory / f"{name}.source.mp4", directory / f"{name}.mp4")
            if found is not None:
                if abs((start - found) - slice_start) > SLICE_FIX_LOG:
                    logger.warning("Clip %d's source slice starts at %.3f s, not %.3f s; using what its sound says.",
                                   index, start - found, slice_start)
                slice_start = start - found
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
                "slice_checked": found is not None,
                **_render_options(item),
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


def _render_options(item: Any) -> dict[str, Any]:
    """The first render's own options (pinned title, webinar layout), so a trim keeps them."""
    options: dict[str, Any] = {}
    if getattr(item, "pin_title", False):
        options["pin_title"] = True
    if getattr(item, "title_fade_in", True) is False:
        options["title_fade_in"] = False
    if getattr(item, "webinar", None) is not None:
        from core.webinar import layout_to_dict

        options["webinar"] = layout_to_dict(item.webinar)
    return options


def check_slice(record_id: str, index: int) -> float:
    """Make sure clip ``index``'s slice start is right (pitches made before round 22
    could be a keyframe off). Fixes the saved start, the trim limits and the words'
    times if it wasn't. Returns the correction in seconds (0 when it was right or
    couldn't be checked)."""
    directory = pitch_dir(record_id)
    manifest = json.loads(_manifest_path(record_id).read_text(encoding="utf-8"))
    item = next((clip for clip in manifest["clips"] if clip["index"] == index), None)
    if item is None or item.get("slice_checked"):
        return 0.0
    found = find_clip_in_slice(directory / item["source"], directory / item["file"])
    correction = 0.0
    if found is not None:
        true_start = float(item["start"]) - found
        correction = round(true_start - float(item["slice_start"]), 3)
        if abs(correction) >= 0.02:
            # Words were saved relative to the wrong start: move them by the difference.
            item["words"] = [[text, round(a - correction, 3), round(b - correction, 3)] for text, a, b in item["words"]]
            item["slice_start"] = round(true_start, 3)
            item["slice_end"] = round(float(item["slice_end"]) + correction, 3)
            if abs(correction) > SLICE_FIX_LOG:
                logger.warning("Pitch %s clip %d: its source slice was %.2f s off; fixed.", record_id, index, correction)
        else:
            correction = 0.0
        item["slice_checked"] = True
        _write_manifest(record_id, manifest)
    return correction


SNAP_PAD = 0.05  # when snapping to whole words: this much room before/after the word
CUT_WORDS = 5  # words shown at each end of a cut


def cut_report(item: dict[str, Any], new_start: float, new_end: float) -> dict[str, Any]:
    """What a cut sounds like at its edges (times in the source video):
    {"opens": first words, "closes": last words, "start_word" / "end_word": the word a
    cut lands inside (None if it falls between words), "snap_start" / "snap_end": the
    cut moved out to take that whole word (inside the trim limits)}."""
    offset = float(item["slice_start"])
    words = [(str(text), float(a) + offset, float(b) + offset) for text, a, b in item.get("words", [])]
    inside = [w for w in words if w[2] > new_start and w[1] < new_end]
    earliest, latest = trim_limits(item)
    start_word = next((w for w in words if w[1] < new_start - 0.02 and new_start + 0.02 < w[2]), None)
    end_word = next((w for w in words if w[1] < new_end - 0.02 and new_end + 0.02 < w[2]), None)
    return {
        "opens": " ".join(w[0] for w in inside[:CUT_WORDS]),
        "closes": " ".join(w[0] for w in inside[-CUT_WORDS:]),
        "start_word": start_word[0] if start_word else None,
        "end_word": end_word[0] if end_word else None,
        "snap_start": max(earliest, start_word[1] - SNAP_PAD) if start_word else new_start,
        "snap_end": min(latest, end_word[2] + SNAP_PAD) if end_word else new_end,
    }


def poster_after_trim(item: dict[str, Any], new_start: float, new_end: float) -> float | None:
    """A picked poster frame after a trim: the same moment of the video, now further in
    or out of the clip. None (picked automatically again) if none was picked or that
    moment is no longer in the clip."""
    if item.get("poster_at") is None:
        return None
    at = round(float(item["poster_at"]) + float(item["start"]) - new_start, 2)
    return at if 0.0 <= at <= new_end - new_start - 0.1 else None


def set_poster_time(record_id: str, index: int, at: float | None) -> None:
    """Remember the poster frame picked for a clip (None: automatic again)."""
    manifest = json.loads(_manifest_path(record_id).read_text(encoding="utf-8"))
    for item in manifest["clips"]:
        if item["index"] == index:
            if at is None:
                item.pop("poster_at", None)
            else:
                item["poster_at"] = round(float(at), 2)
    _write_manifest(record_id, manifest)


def trim_limits(item: dict[str, Any]) -> tuple[float, float]:
    """Earliest start and latest end this clip can be trimmed to. (A clip that ends with
    the video can find the slice a few milliseconds short; its own end stays allowed.)"""
    return float(item["slice_start"]), max(float(item["slice_end"]) - 0.05, float(item["end"]))


def video_info_for(record: dict[str, Any] | None) -> Any:
    """Round 52: what the worker's render knew about the video (title, show, this month's theme),
    so a trim writes the clip's LinkedIn post and Shorts & Reels caption again for the new cut."""
    if not isinstance(record, dict):
        return None
    from core.graphics_director import VideoInfo

    facts = record.get("pitch") if isinstance(record.get("pitch"), dict) else {}
    return VideoInfo(title=str(record.get("video_title") or ""), channel=str(record.get("channel_title") or ""),
                     theme=str(facts.get("theme") or ""))


def _outbox_record(record_id: str) -> dict[str, Any] | None:
    """The pitch's record in the Outbox (storage/pending_outreach.json), or None."""
    try:
        import autonomous_worker as W

        return next((r for r in W._pending_records() if r.get("record_id") == record_id), None)
    except Exception:  # noqa: BLE001 - the trim still renders, just without new posts
        return None


def _side_files(clip: Path) -> list[Path]:
    """A rendered clip's companions: captions (.srt), LinkedIn post, Shorts & Reels caption."""
    from core.linkedin_post import post_path
    from core.social_post import post_path as social_path

    return [Path(clip).with_suffix(".srt"), post_path(Path(clip)), social_path(Path(clip))]


def rerender_trim(record_id: str, index: int, new_start: float, new_end: float, settings: Any,
                  info: Any = None) -> Path:
    """Render the clip again with new start/end times (seconds in the source video),
    exactly like the worker does. Waits as clip_NN.trim.mp4 until accept_trim().
    ``info`` (video_info_for(record)): the clip's posts are written again for the new cut; by
    default it's read from the pitch's Outbox record."""
    if info is None:
        info = video_info_for(_outbox_record(record_id))
    from core.hook_selector import ClipResult
    from core.pipeline import render_selected_clip

    if load_pitch(record_id) is None:
        raise RuntimeError("This pitch's files aren't on this Mac any more.")
    check_slice(record_id, index)
    manifest = load_pitch(record_id)
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
        kept: dict[str, Any] = {}  # the first render's own options (only when it had them)
        if item.get("pin_title"):
            kept["pin_title"] = True
        if item.get("title_fade_in") is False:
            kept["title_fade_in"] = False
        if item.get("webinar"):
            from core.webinar import layout_from_dict

            kept["webinar_layout"] = layout_from_dict(item["webinar"])
        rendered = render_selected_clip(
            directory / item["source"], clip, words, settings, work_dir / item["file"],
            layout_mode=str(item["layout"]), work_dir=work_dir, graphics=info, **kept,
        )
        trimmed = directory / _trim_name(item["file"])
        shutil.move(str(rendered.path), trimmed)
        # Round 52: the captions file and the posts go with the trimmed clip. They used to stay in
        # the work folder and be deleted with it, so "Use this cut" put the clip on their page
        # without its captions file (and with the old cut's posts).
        for made, kept_as in zip(_side_files(rendered.path), _side_files(trimmed)):
            if made.is_file():
                shutil.move(str(made), kept_as)
            else:
                kept_as.unlink(missing_ok=True)
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
    poster_at = poster_after_trim(item, times["start"], times["end"])
    if poster_at is None:
        item.pop("poster_at", None)
    else:
        item["poster_at"] = poster_at
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


def remove_clip_files(record_id: str, index: int, root: Path | None = None) -> int:
    """Delete clip ``index`` (from 1) from the pitch's saved files (the clip, preview,
    source slice and any waiting trim); later clips move up one number, files and
    manifest both, a waiting trim moving with its clip. Returns the new count."""
    directory = pitch_dir(record_id, root)
    manifest = json.loads((directory / "pitch.json").read_text(encoding="utf-8"))
    clips = manifest.get("clips", [])
    removed = next((clip for clip in clips if clip["index"] == index), None)
    if removed is None:
        raise ValueError(f"No clip {index} in this pitch.")
    for key in ("file", "preview", "source"):
        (directory / removed[key]).unlink(missing_ok=True)
        (directory / _trim_name(removed[key])).unlink(missing_ok=True)
    remaining = sorted((clip for clip in clips if clip is not removed), key=lambda clip: clip["index"])
    for number, clip in enumerate(remaining, start=1):
        if clip["index"] == number:
            continue
        names = {"file": f"clip_{number:02d}.mp4", "preview": f"clip_{number:02d}.preview.mp4",
                 "source": f"clip_{number:02d}.source.mp4"}
        for key, new_name in names.items():
            for old, new in ((clip[key], new_name), (_trim_name(clip[key]), _trim_name(new_name))):
                if (directory / old).is_file():
                    (directory / old).replace(directory / new)
            clip[key] = new_name
        clip["index"] = number
    manifest["clips"] = remaining
    _write_manifest(record_id, manifest, root)
    return len(remaining)


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


# ------------------------------------------------------------------ Round 48: re-render a pitch
def rerender_pitch(record_id: str, settings: Any, record: dict[str, Any] | None = None,
                   replace_remote: Callable[[dict[str, Any], dict[str, Any]], Callable[[Path, str], None] | None] | None = None,
                   ) -> list[int]:
    """Render every clip of a pitch again, at the same times, with the current editing (a fix
    shipped after the pitch was made: Round 48's grey screens), and swap each one where the
    prospect sees it (their clip page, the Drive folder) exactly like the Outbox's Trim does.
    Returns the clip numbers replaced."""
    manifest = load_pitch(record_id)
    if manifest is None:
        raise RuntimeError("This pitch's files aren't on this Mac any more.")
    done: list[int] = []
    for item in manifest["clips"]:
        start, end = float(item["start"]), float(item["end"])
        rerender_trim(record_id, int(item["index"]), start, end, settings)
        replacer = replace_remote(record or {}, {"start": start, "end": end, "file": item["file"]}) if replace_remote else None
        accept_trim(record_id, int(item["index"]), replacer)
        logger.info("Re-rendered clip %d of pitch %s.", item["index"], record_id)
        done.append(int(item["index"]))
    return done


def _publish_replacer(record: dict[str, Any], clip: dict[str, Any]) -> Callable[[Path, str], None] | None:
    """The app's _clip_replacer: the clip page (and the Drive folder) get the new file."""
    page_name = str(record.get("page_name") or "")
    drive_link = str(record.get("drive_link") or "")
    if not page_name and not drive_link:
        return None

    def replace(path: Path, file_name: str) -> None:
        if page_name:
            from core import clip_pages

            clip_pages.replace_clip(page_name, file_name, path, clip["start"], clip["end"])
        if drive_link:
            from core.drive_uploader import replace_clip_in_drive

            replace_clip_in_drive(drive_link, file_name, path)

    return replace


def main(argv: list[str] | None = None) -> int:
    """python -m core.pitch_files rerender <channel name or record id> [--local]"""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m core.pitch_files")
    sub = parser.add_subparsers(dest="command", required=True)
    again = sub.add_parser("rerender", help="render a pending pitch's clips again and update its clip page")
    again.add_argument("pitch", help="the channel name (or the record id) in the Outbox")
    again.add_argument("--local", action="store_true", help="only the files on this Mac, not the clip page")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from autonomous_worker import get_pending_outreach
    from config import load_settings

    wanted = args.pitch.strip().lower()
    records = [r for r in get_pending_outreach()
               if wanted in {str(r.get("record_id", "")).lower(), str(r.get("channel_title", "")).strip().lower()}]
    if not records:
        parser.error(f"No pending pitch named {args.pitch!r} in the Outbox.")
    record = records[0]
    done = rerender_pitch(str(record["record_id"]), load_settings(), record,
                          None if args.local else _publish_replacer)
    where = "on this Mac" if args.local else (f"and on {record.get('page_url') or 'its clip page'}")
    print(f"Re-rendered clip(s) {', '.join(map(str, done))} of {record.get('channel_title')} {where}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
