"""Round 59: the footage library. Bought gameplay -> tagged shots in SQLite.

ingest() never copies or re-encodes footage: a shot is (file, start, end) plus tags, so a
100 GB footage drive stays 100 GB. What it does:

1. Finds the cuts: PySceneDetect's AdaptiveDetector when installed (it copes with fast
   camera moves), else FFmpeg's scene filter, else fixed windows.
2. Splits long takes into pieces of at most MAX_SHOT seconds (a Short changes picture every
   2-4 s), drops pieces under MIN_SHOT seconds.
3. Drops static pieces (pause menus, loading screens, standing still): mean frame
   difference below STATIC_MOTION. This is auto-editor's "motion" idea in a few lines of
   OpenCV, so it needs no extra install.
4. Tags every shot: words from its folder and file names (footage/beach_sunset/take1.mp4 ->
   beach, sunset), day/night from brightness, a motion level, and, when a tagger is given
   (core.narrate.tagger: Gemini), what the frames show.

pick() chooses footage for a script line: the most matching tags, then the least used,
never a shot used in the last few videos.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
MIN_SHOT = 1.2  # seconds: shorter pieces are dropped
MAX_SHOT = 6.0  # seconds: longer takes are split
STATIC_MOTION = 1.5  # mean absolute frame difference (0-255) below which a piece is "static"
NIGHT_LUMA = 70.0  # mean brightness (0-255) below which a shot is tagged night
RECENT_RENDERS = 3  # a shot used in the last N renders is not picked again
_NAME_WORDS = re.compile(r"[a-z]+")
_IGNORED_NAME_WORDS = {"mp4", "mov", "mkv", "clip", "take", "gta", "gameplay", "footage", "raw", "final", "copy",
                       "video", "part", "the", "and", "of", "vi", "v"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS shots (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    motion REAL NOT NULL DEFAULT 0,
    brightness REAL NOT NULL DEFAULT 0,
    used_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(path, start)
);
CREATE TABLE IF NOT EXISTS renders (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    made_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    shot_ids TEXT NOT NULL
);
"""


@dataclass
class Shot:
    id: int
    path: str
    start: float
    end: float
    tags: list[str] = field(default_factory=list)
    motion: float = 0.0
    brightness: float = 0.0
    used_count: int = 0

    @property
    def duration(self) -> float:
        return self.end - self.start


# --- probing and cuts ---------------------------------------------------------------------

def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "default=nw=1:nk=1", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def _cuts_scenedetect(path: Path) -> list[float] | None:
    try:
        from scenedetect import AdaptiveDetector, detect
    except ImportError:
        return None
    try:
        scenes = detect(str(path), AdaptiveDetector())
    except Exception:  # noqa: BLE001 - fall back to FFmpeg
        logger.warning("PySceneDetect failed on %s; using FFmpeg's scene filter.", path.name, exc_info=True)
        return None
    return [s[0].get_seconds() for s in scenes[1:]]


def _cuts_ffmpeg(path: Path, threshold: float = 0.35) -> list[float]:
    run = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-an", "-vf",
                          f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
                         capture_output=True, text=True)
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", run.stderr)]


def scene_cuts(path: Path) -> list[float]:
    cuts = _cuts_scenedetect(path)
    return sorted(set(cuts if cuts is not None else _cuts_ffmpeg(path)))


def pieces(duration: float, cuts: Iterable[float], max_shot: float = MAX_SHOT,
           min_shot: float = MIN_SHOT) -> list[tuple[float, float]]:
    """The (start, end) pieces between cuts, long takes split evenly, short ones dropped."""
    bounds = [0.0, *[c for c in cuts if 0.0 < c < duration], duration]
    out: list[tuple[float, float]] = []
    for a, b in zip(bounds, bounds[1:]):
        # trim 0.1 s off each side of a cut so a shot never opens on the frame before it
        a, b = (a + 0.1 if a > 0 else a), (b - 0.1 if b < duration else b)
        length = b - a
        if length < min_shot:
            continue
        parts = max(1, int(np.ceil(length / max_shot)))
        step = length / parts
        out.extend((a + i * step, a + (i + 1) * step) for i in range(parts))
    return out


def measure(path: Path, start: float, end: float, samples: int = 6) -> tuple[float, float, list[np.ndarray]]:
    """(motion, brightness, frames) for a piece: mean frame difference and mean luma from a
    few small frames, plus three of them for the tagger."""
    import cv2

    cap = cv2.VideoCapture(str(path))
    frames: list[np.ndarray] = []
    try:
        for t in np.linspace(start + 0.05, end - 0.05, samples):
            cap.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000.0)
            ok, frame = cap.read()
            if ok:
                frames.append(cv2.resize(frame, (320, 180)))
    finally:
        cap.release()
    if not frames:
        return 0.0, 0.0, []
    grey = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames]
    motion = float(np.mean([np.abs(a - b).mean() for a, b in zip(grey, grey[1:])])) if len(grey) > 1 else 0.0
    brightness = float(np.mean([g.mean() for g in grey]))
    keep = [frames[0], frames[len(frames) // 2], frames[-1]]
    return motion, brightness, keep


def name_tags(path: Path, root: Path | None = None) -> list[str]:
    """Words from the folder names (below root) and the file name."""
    parts = path.relative_to(root).parts if root and path.is_relative_to(root) else (path.parent.name, path.name)
    words: list[str] = []
    for part in parts:
        for word in _NAME_WORDS.findall(Path(part).stem.lower() if part == parts[-1] else part.lower()):
            if word not in _IGNORED_NAME_WORDS and len(word) > 2 and word not in words:
                words.append(word)
    return words


def auto_tags(motion: float, brightness: float) -> list[str]:
    tags = ["night" if brightness < NIGHT_LUMA else "day"]
    tags.append("fast" if motion > 18 else "calm" if motion < 6 else "moving")
    return tags


# --- the library ---------------------------------------------------------------------------

Tagger = Callable[[list[np.ndarray]], list[str]]


class Library:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ingest
    def ingest(self, folder: str | Path, tagger: Tagger | None = None,
               static_motion: float = STATIC_MOTION) -> dict[str, int]:
        """Add every video under folder. Files already in the library are skipped."""
        root = Path(folder)
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.suffix.lower() in VIDEO_EXTENSIONS)
        stats = {"files": 0, "shots": 0, "static": 0, "skipped_files": 0}
        for path in files:
            if self.db.execute("SELECT 1 FROM shots WHERE path = ? LIMIT 1", (str(path),)).fetchone():
                stats["skipped_files"] += 1
                continue
            stats["files"] += 1
            duration = probe_duration(path)
            base_tags = name_tags(path, root if root.is_dir() else None)
            for start, end in pieces(duration, scene_cuts(path)):
                motion, brightness, frames = measure(path, start, end)
                if motion < static_motion:
                    stats["static"] += 1
                    continue
                tags = [*base_tags, *auto_tags(motion, brightness)]
                if tagger is not None and frames:
                    try:
                        tags += [t for t in tagger(frames) if t not in tags]
                    except Exception:  # noqa: BLE001 - a shot without AI tags is still a shot
                        logger.warning("Tagger failed on %s @ %.1f s.", path.name, start, exc_info=True)
                self.db.execute("INSERT OR IGNORE INTO shots (path, start, end, tags, motion, brightness) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (str(path), round(start, 3), round(end, 3), json.dumps(tags), motion, brightness))
                stats["shots"] += 1
            self.db.commit()
        return stats

    # reading
    def shots(self) -> list[Shot]:
        rows = self.db.execute("SELECT id, path, start, end, tags, motion, brightness, used_count FROM shots").fetchall()
        return [Shot(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5], r[6], r[7]) for r in rows]

    def recent_shot_ids(self, renders: int = RECENT_RENDERS) -> set[int]:
        rows = self.db.execute("SELECT shot_ids FROM renders ORDER BY id DESC LIMIT ?", (renders,)).fetchall()
        return {i for (ids,) in rows for i in json.loads(ids)}

    def retag(self, shot_id: int, tags: list[str]) -> None:
        self.db.execute("UPDATE shots SET tags = ? WHERE id = ?", (json.dumps(tags), shot_id))
        self.db.commit()

    # choosing
    def pick(self, wanted: list[str], exclude: set[int] | None = None, pool: list[Shot] | None = None) -> Shot:
        """The best shot for these tags: most tag matches, then least used, then most motion.
        Shots in ``exclude`` (this video's and recent videos') are used only if nothing else is left."""
        pool = pool if pool is not None else self.shots()
        if not pool:
            raise LookupError("The footage library is empty. Run: python tools/narrate.py ingest <folder>")
        exclude = exclude or set()
        wanted_set = set(wanted)

        def score(shot: Shot) -> tuple[int, int, int, float]:
            return (0 if shot.id in exclude else 1, len(wanted_set & set(shot.tags)), -shot.used_count, shot.motion)

        return max(pool, key=score)

    def record_render(self, name: str, shot_ids: list[int]) -> None:
        self.db.executemany("UPDATE shots SET used_count = used_count + 1 WHERE id = ?", [(i,) for i in shot_ids])
        self.db.execute("INSERT INTO renders (name, shot_ids) VALUES (?, ?)", (name, json.dumps(sorted(set(shot_ids)))))
        self.db.commit()

    def reuse_share(self, shot_ids: list[int], renders: int = 10) -> float:
        """Share of these shots that appeared in the last ``renders`` videos (the variety guard)."""
        if not shot_ids:
            return 0.0
        seen = self.recent_shot_ids(renders)
        return sum(1 for i in shot_ids if i in seen) / len(shot_ids)
