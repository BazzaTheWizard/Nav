"""Round 57: the clean (textless) copy of a clip, for post-production teams (NYT Opinion Video ask).

Naming follows post-production usage: the "dirty" copy is today's finished clip (captions,
title card, data cards and lower third burned in); the "clean" copy is the same picture and
sound with none of them. Same cuts, framing, camera moves, mid-clip trims and climax teaser,
frame for frame, so the clip's .srt (and an editor's own titles) line up on both copies.

How: render_selected_clip decides everything once, renders the dirty copy, then renders again
inside ``textless_pass()``. While the pass is on, core.video_renderer.render_clip leaves out the
caption/title layers and encodes a high-bitrate master, the graphics step is skipped, and the
teaser stitch keeps its sound effect out of the mix (it is delivered as its own stem).

Delivered in ``<clip>_clean/`` next to the clip (a folder, so the app's ``clip_*.mp4`` lists
never see it as another clip):
    <clip>_textless.mp4   high-bitrate H.264, keyframe every 0.5 s (or .mov ProRes 422 HQ, TEXTLESS_PRORES=1)
    <clip>.srt            the captions, on the timeline of both copies
    <clip>_dialogue.wav   the clean copy's sound, 48 kHz 24-bit (a podcast source has one mixed track,
                          so this is dialogue plus whatever was in the room; it is not separated)
    <clip>_sfx.wav        the sound effects the dirty copy adds (the teaser's hit), when there are any

On/off: TEXTLESS_MASTER=1, or settings.textless_master. Off by default: it renders every clip twice.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

FOLDER_SUFFIX = "_clean"
MASTER_BITRATE, MASTER_MAXRATE, MASTER_BUFSIZE = "40M", "50M", "80M"
MASTER_KEYFRAME = "15"  # every 0.5 s at 30 fps: editors scrub and cut frame by frame
_ON = {"1", "true", "yes", "on"}


@dataclass
class _Pass:
    work: Path
    stems: dict[str, Path] = field(default_factory=dict)


_ACTIVE: contextvars.ContextVar[_Pass | None] = contextvars.ContextVar("nav_textless_pass", default=None)


def enabled(settings: Any = None) -> bool:
    """Make a clean copy of every clip (TEXTLESS_MASTER=1, or settings.textless_master)."""
    value = getattr(settings, "textless_master", None) if settings is not None else None
    if value is not None:
        return bool(value)
    return os.getenv("TEXTLESS_MASTER", "0").strip().lower() in _ON


def prores() -> bool:
    return os.getenv("TEXTLESS_PRORES", "0").strip().lower() in _ON


def active() -> bool:
    """True while the clean copy is being rendered (core.video_renderer checks this)."""
    return _ACTIVE.get() is not None


@contextlib.contextmanager
def textless_pass() -> Iterator[_Pass]:
    work = Path(tempfile.mkdtemp(prefix="nav_clean_"))
    token = _ACTIVE.set(_Pass(work))
    try:
        yield _ACTIVE.get()  # type: ignore[misc]
    finally:
        _ACTIVE.reset(token)


def keep_stem(name: str, track: Path) -> None:
    """During the pass: keep ``track`` (a sound kept out of the clean mix) as the ``name`` stem."""
    current = _ACTIVE.get()
    if current is None or not Path(track).is_file():
        return
    kept = current.work / f"{name}.wav"
    shutil.copy2(track, kept)
    current.stems[name] = kept


def encoder_args(encoders: str) -> tuple[str, ...]:
    """The master's video encoding: about 4x the dirty copy's bitrate, a short keyframe interval."""
    rate = ("-g", MASTER_KEYFRAME, "-maxrate", MASTER_MAXRATE, "-bufsize", MASTER_BUFSIZE, "-profile:v", "high")
    if "h264_videotoolbox" in encoders:
        return ("-c:v", "h264_videotoolbox", "-b:v", MASTER_BITRATE, *rate)
    return ("-c:v", "libx264", "-preset", "fast", "-crf", "12", *rate)


def folder_for(dirty: Path) -> Path:
    return Path(dirty).parent / f"{Path(dirty).stem}{FOLDER_SUFFIX}"


def frame_count(video: Path) -> int:
    """Video frames in ``video`` (counted from the packets, not estimated from the duration)."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return -1
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(video)],
                         capture_output=True, text=True, timeout=120)
    try:
        return int(out.stdout.strip().split(",")[0])
    except ValueError:
        return -1


@dataclass(frozen=True)
class CleanCopy:
    folder: Path
    video: Path
    srt: Path | None
    dialogue: Path | None
    stems: dict[str, Path]
    frames: int
    dirty_frames: int

    @property
    def matches(self) -> bool:
        """Same number of frames as the dirty copy (so the .srt fits both)."""
        return self.frames == self.dirty_frames and self.frames > 0

    def files(self) -> dict[str, str]:
        out = {"clean": self.video.name, **{name: path.name for name, path in self.stems.items()}}
        if self.srt is not None:
            out["srt"] = self.srt.name
        if self.dialogue is not None:
            out["dialogue"] = self.dialogue.name
        return {key: f"{self.folder.name}/{value}" for key, value in out.items()}


def finish(rendered: Path, dirty: Path, srt: Path | None, current: _Pass) -> CleanCopy:
    """Move the clean render into ``<dirty>_clean/`` with its .srt, dialogue and stems."""
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    folder = folder_for(dirty)
    folder.mkdir(parents=True, exist_ok=True)
    stem = Path(dirty).stem
    video = folder / f"{stem}_textless.mp4"
    shutil.move(str(rendered), video)
    if prores():
        mov = folder / f"{stem}_textless.mov"
        subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(video), "-c:v", "prores_ks", "-profile:v", "3",
                        "-pix_fmt", "yuv422p10le", "-c:a", "pcm_s24le", "-ar", "48000", str(mov)],
                       check=True, capture_output=True, timeout=1800)
        video.unlink(missing_ok=True)
        video = mov
    dialogue: Path | None = folder / f"{stem}_dialogue.wav"
    try:
        subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(video), "-vn", "-c:a", "pcm_s24le", "-ar", "48000",
                        str(dialogue)], check=True, capture_output=True, timeout=600)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        logger.warning("No dialogue track for %s (the clip may have no sound).", video.name)
        dialogue = None
    stems: dict[str, Path] = {}
    for name, track in current.stems.items():
        stems[name] = folder / f"{stem}_{name}.wav"
        shutil.copy2(track, stems[name])
    shutil.rmtree(current.work, ignore_errors=True)
    copied = None
    if srt is not None and Path(srt).is_file():
        copied = folder / Path(srt).name
        shutil.copy2(srt, copied)
    made = CleanCopy(folder, video, copied, dialogue, stems, frame_count(video), frame_count(dirty))
    if not made.matches:
        logger.error("Clean copy of %s has %d frames, the dirty copy %d: the .srt won't line up on both.",
                     stem, made.frames, made.dirty_frames)
    return made
