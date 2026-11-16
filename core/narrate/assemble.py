"""Round 59: timed script + footage library -> one 16:9 video with your voiceover as its sound.

Each script line is on screen for exactly as long as you take to say it (core.narrate.align).
That time is filled with shots from the library that match the line's tags, cut every
2-3.5 s (faster on the hook) so the picture keeps moving. Game sound from the footage plays
under your voice at GAME_AUDIO_DB (or not at all with game_audio_db=None).

[ZOOM] lines get a 1.12x punch-in.

Round 60, frame: "wide" keeps the 16:9 picture (the renderer shows it as a strip over blur);
"tall" (4:5) and "vertical" (9:16) crop each shot around its action (core.narrate.reframe), so
the gameplay fills far more of the phone screen. [TEXT] and [SFX] markers are returned (with their times)
for core.narrate.finish, which draws them on the finished vertical clip.

The result goes through core.pipeline.render_selected_clip like any talking-head video.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core.narrate import reframe
from core.narrate.align import LineSpan
from core.narrate.library import Library, Shot
from core.narrate.script import Script

logger = logging.getLogger(__name__)

WIDTH, HEIGHT, FPS = 1920, 1080, 30
CUT_EVERY = 3.2  # seconds: the longest a single shot stays on screen
HOOK_CUT_EVERY = 1.8  # the hook line cuts faster
MIN_PIECE = 0.6  # never a flash shorter than this (it merges into the previous piece)
GAME_AUDIO_DB = -20.0  # game sound under the voiceover
ZOOM = 1.12


@dataclass
class Piece:
    shot_id: int
    path: str
    src_start: float
    duration: float
    at: float  # where it starts in the assembled video
    line: int
    zoom: bool = False
    centre: tuple[float, float] = (0.5, 0.5)  # Round 60: the crop's centre at the piece's start and end


@dataclass
class Cue:
    kind: str  # TEXT, SFX, ZOOM, FREEZE
    value: str
    at: float  # seconds in the assembled video
    line: int


@dataclass
class Assembly:
    video: Path
    duration: float
    pieces: list[Piece] = field(default_factory=list)
    cues: list[Cue] = field(default_factory=list)

    @property
    def shot_ids(self) -> list[int]:
        return [p.shot_id for p in self.pieces]

    def save(self, path: Path) -> Path:
        path.write_text(json.dumps({"video": str(self.video), "duration": self.duration,
                                    "pieces": [asdict(p) for p in self.pieces],
                                    "cues": [asdict(c) for c in self.cues]}, indent=2))
        return path


def plan(script: Script, spans: list[LineSpan], library: Library, avoid_recent: bool = True) -> tuple[list[Piece], list[Cue]]:
    """Which shot plays when. Never the same shot twice in one video while others are left."""
    pool = library.shots()
    footage = sum(s.duration for s in pool)
    needed = spans[-1].end if spans else 0.0
    if pool and footage < needed:
        logger.warning("The library holds %.0f s of footage for a %.0f s voiceover: shots will repeat. "
                       "Add footage (python tools/narrate.py ingest <folder>).", footage, needed)
    used: set[int] = set(library.recent_shot_ids()) if avoid_recent else set()
    pieces: list[Piece] = []
    cues: list[Cue] = []
    for span in spans:
        line = script.lines[span.index]
        for marker in line.markers:
            cues.append(Cue(marker.kind, marker.value, round(span.start, 3), span.index))
        zoom = any(m.kind == "ZOOM" for m in line.markers)
        every = HOOK_CUT_EVERY if (line.hook or span.index == 0) else CUT_EVERY
        t, end = span.start, span.end
        while end - t > 1e-3:
            want = min(every, end - t)
            if end - t - want < MIN_PIECE:  # don't leave a flash at the end of the line
                want = end - t
            shot = library.pick(line.tags, exclude=used, pool=pool)
            used.add(shot.id)
            take = min(want, shot.duration)
            # start in the middle of a long shot sometimes, so two lines never open on the same frame
            offset = max(0.0, (shot.duration - take) / 2.0) if shot.duration > take + 1.0 else 0.0
            pieces.append(Piece(shot.id, shot.path, round(shot.start + offset, 3), round(take, 3), round(t, 3),
                                span.index, zoom))
            t += take
    return pieces, cues


def _has_audio(path: str) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", path], capture_output=True, text=True)
    return bool(out.stdout.strip())


def _render_piece(piece: Piece, out: Path, frame: str = "wide") -> None:
    if frame == "wide":
        scale = f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT}"
        if piece.zoom:
            zw, zh = int(WIDTH * ZOOM) // 2 * 2, int(HEIGHT * ZOOM) // 2 * 2
            scale = f"scale={zw}:{zh}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT}"
    else:
        scale = reframe.crop_filter(piece.path, reframe.Track(*piece.centre), frame, piece.duration,
                                    ZOOM if piece.zoom else 1.0)
    vf = f"{scale},fps={FPS},setsar=1,format=yuv420p"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{piece.src_start:.3f}",
           "-t", f"{piece.duration:.3f}", "-i", piece.path]
    if _has_audio(piece.path):
        audio_map = "0:a:0"
    else:
        cmd += ["-f", "lavfi", "-t", f"{piece.duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        audio_map = "1:a:0"
    cmd += ["-vf", vf, "-map", "0:v:0", "-map", audio_map, "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-t", f"{piece.duration:.3f}", str(out)]
    subprocess.run(cmd, check=True)


def assemble(script: Script, spans: list[LineSpan], library: Library, voiceover: str | Path, out_path: str | Path,
             work_dir: str | Path, game_audio_db: float | None = GAME_AUDIO_DB, frame: str = "wide") -> Assembly:
    if frame not in reframe.FRAMES:
        raise ValueError(f"frame must be one of {', '.join(reframe.FRAMES)}")
    out_path, work_dir = Path(out_path), Path(work_dir)
    pieces_dir = work_dir / f"{out_path.stem}_pieces"
    shutil.rmtree(pieces_dir, ignore_errors=True)
    pieces_dir.mkdir(parents=True)
    pieces, cues = plan(script, spans, library)
    files: list[Path] = []
    if frame != "wide":
        out_w, out_h = reframe.FRAMES[frame]
        for piece in pieces:
            tr = reframe.track(piece.path, piece.src_start, piece.duration, out_w / out_h)
            piece.centre = (round(tr.start, 4), round(tr.end, 4))
    for n, piece in enumerate(pieces):
        f = pieces_dir / f"{n:03d}.mp4"
        _render_piece(piece, f, frame)
        files.append(f)
    listing = pieces_dir / "list.txt"
    listing.write_text("".join(f"file '{f.resolve()}'\n" for f in files))
    joined = pieces_dir / "joined.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i",
                    str(listing), "-c", "copy", str(joined)], check=True)
    duration = round(sum(p.duration for p in pieces), 3)
    if game_audio_db is None:
        audio = f"[1:a]aresample=48000,apad,atrim=0:{duration:.3f}[a]"
    else:
        audio = (f"[0:a]volume={game_audio_db}dB[g];[1:a]aresample=48000,apad[v];"
                 f"[g][v]amix=inputs=2:duration=first:normalize=0,atrim=0:{duration:.3f}[a]")
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(joined), "-i", str(voiceover),
                    "-filter_complex", audio, "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
                    "-b:a", "192k", "-ar", "48000", "-t", f"{duration:.3f}", str(out_path)], check=True)
    result = Assembly(out_path, duration, pieces, cues)
    result.save(out_path.with_suffix(".assembly.json"))
    shutil.rmtree(pieces_dir, ignore_errors=True)
    return result
