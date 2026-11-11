"""Round 48: the multi-cam director for two-person shots (stage chats, studio podcasts).

Before, a two-person shot stayed on one framing for the whole clip: the stacked 2-up, or a
letterboxed wide shot where both faces are tiny (GTMnow's AGM fireside chat: 45 s of one
man talking, both people a few percent of the frame). A TV director cuts between cameras:

- the two-shot (here the stacked 2-up split view) establishes the scene for the first
  ESTABLISH seconds, and returns for back-and-forth, short replies and reactions;
- when one person holds the floor for MIN_TURN seconds or more, cut to a close-up of them:
  a real 9:16 crop around their face (CLOSE_FACE_SHARE of the frame's width), never more than
  MAX_UPSCALE source pixels blown up (a soft close-up is worse than none);
- a close-up longer than MAX_CLOSEUP seconds gets a BREATHER on the two-shot at a sentence
  break (the listener's reaction), then back;
- every cut lands between words, and no shot is shorter than MIN_SHOT seconds.

Who is talking: each face's mouth, aligned on YuNet's mouth-corner landmarks so a head turn
or a nod doesn't count, frame to frame at ACTIVITY_FPS, compared phrase by phrase (phrases
from the word timings). Measured on the AGM chat: the speaker's mouth 1.3-2.7x the
listener's in every phrase. A phrase without a clear winner (CONFIDENT_RATIO) or a face we
lost is "unsure", and an unsure turn never gets a close-up: when in doubt the director
stays on the two-shot, so it never puts the wrong person full-screen.

A close-up that would show mostly flat background (a podcast template's backdrop), cut the
face off, or need too much upscaling isn't used.

SPEAKER_CLOSEUPS=0 in .env turns it off. Only in the B2B modes (VISUAL_OVERLAYS isn't "full").
"""
from __future__ import annotations

import logging
import math
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

logger = logging.getLogger(__name__)

CLOSE_FACE_SHARE = 0.22  # the face is this share of the close-up's width (head and shoulders)
MAX_UPSCALE = 2.0  # a close-up window is at least 1080 / this source pixels wide
MIN_TURN = 3.5  # seconds of one person talking before a close-up is worth it
ESTABLISH = 3.0  # the two-shot opens the clip for this long
MAX_CLOSEUP = 15.0  # longer: a breather on the two-shot in the middle
BREATHER = 2.5
MIN_SHOT = 2.0
TEASER_TURN = 1.2  # the cold open is one line: a close-up when it's clearly one person's
CONFIDENT_RATIO = 1.25  # the speaker's mouth moves at least this much more than the other's
PHRASE_GAP = 0.45  # a silence this long between words ends a phrase
ACTIVITY_FPS = 15
ANALYSIS_WIDTH = 1920
FLAT_MAX = 0.6  # a close-up with more than this share of flat pixels is mostly backdrop


def enabled(settings: Any = None) -> bool:
    if os.getenv("SPEAKER_CLOSEUPS", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    from core import executive

    return executive.b2b_clean_mode(settings)


@dataclass(frozen=True)
class CloseUp:
    """One speaker's close-up window in source pixels (even sizes, 9:16)."""

    speaker: int
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Turn:
    """A stretch of talk (clip seconds): ``speaker`` 0 / 1, or None when unsure."""

    speaker: int | None
    start: float
    end: float
    confidence: float = 1.0


@dataclass
class Direction:
    """The close-ups to cut to (clip seconds) and why."""

    layers: list[tuple[CloseUp, list[tuple[float, float]]]] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def windows(self) -> list[tuple[float, float]]:
        return sorted(span for _box, spans in self.layers for span in spans)

    def describe(self) -> str:
        if not self.layers:
            return "; ".join(self.notes) or "no close-ups"
        cuts = sorted((a, b, box.speaker) for box, spans in self.layers for a, b in spans)
        shots = ", ".join(f"{'left' if s == 0 else 'right'} close-up {a:.1f}-{b:.1f} s" for a, b, s in cuts)
        return shots + (f" ({'; '.join(self.notes)})" if self.notes else "")


# ------------------------------------------------------------------ geometry
def closeup_box(speaker: tuple[float, float, float], frame_width: int, frame_height: int,
                index: int = 0) -> CloseUp | None:
    """A 9:16 close-up around a face (cx, cy, face width in source pixels), or None when it
    would be too soft (more than MAX_UPSCALE) or can't fit the frame."""
    cx, cy, face = speaker
    width = max(1080 / MAX_UPSCALE, face / CLOSE_FACE_SHARE)
    width = min(width, frame_height * 9 / 16, frame_width)
    if width * MAX_UPSCALE < 1080 - 1:
        return None
    width = int(width) // 2 * 2
    height = int(round(width * 16 / 9)) // 2 * 2
    if height > frame_height:
        return None
    x = int(max(0, min(frame_width - width, cx - width / 2))) // 2 * 2
    y = int(max(0, min(frame_height - height, cy - 0.36 * height))) // 2 * 2
    if not (x <= cx - face / 2 and cx + face / 2 <= x + width and y <= cy - face * 0.6):
        return None  # the face would be cut off at an edge
    return CloseUp(index, x, y, width, height)


def flat_share(gray: np.ndarray) -> float:
    """Share of pixels whose 5x5 neighbourhood is flat colour (a backdrop, a blank wall)."""
    values = gray.astype(np.float32)
    mean = cv2.blur(values, (5, 5))
    spread = np.sqrt(np.maximum(0.0, cv2.blur(values * values, (5, 5)) - mean * mean))
    return float((spread < 1.5).mean())


def box_has_picture(frame: np.ndarray, box: CloseUp) -> bool:
    """The close-up shows a picture (a person against a set), not mostly blank backdrop."""
    region = frame[box.y:box.y + box.height, box.x:box.x + box.width]
    if region.size == 0:
        return False
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if region.ndim == 3 else region
    small = cv2.resize(gray, (180, 320), interpolation=cv2.INTER_AREA)
    return float(small.std()) >= 12.0 and flat_share(small) <= FLAT_MAX


# ------------------------------------------------------------------ who is talking
def _read_frames(video: Path, start: float, end: float, width: int, height: int, fps: int,
                 crop: tuple[int, int, int, int] | None = None):
    region = f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}," if crop else ""
    process = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(video),
         "-vf", f"fps={fps},{region}scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = width * height * 3
    try:
        while True:
            buffer = process.stdout.read(size) if process.stdout else b""
            if len(buffer) < size:
                break
            yield np.frombuffer(buffer, np.uint8).reshape(height, width, 3)
    finally:
        if process.stdout:
            process.stdout.close()
        process.wait()


def mouth_energy(video: Path, start: float, end: float, speakers: list[tuple[float, float, float]],
                 frame_size: tuple[int, int], detector: Any = None,
                 frames: Any = None) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    """(times in clip seconds, mouth movement per speaker per frame (NaN: face not found),
    one full-size sample frame). Each mouth is warped onto a fixed 40x24 patch from its two
    corners, so a head turn or nod doesn't read as talking."""
    from core.face_tracker import _face_detector

    detector = detector or _face_detector()
    frame_width, frame_height = frame_size
    # Only the band with the faces is read (a quarter to a half of the frame): faster, same detail.
    top = int(max(0, min(cy - 1.6 * w for _cx, cy, w in speakers))) // 2 * 2
    bottom = int(min(frame_height, max(cy + 2.2 * w for _cx, cy, w in speakers)))
    band = max(2, (bottom - top) // 2 * 2)
    width = min(ANALYSIS_WIDTH, frame_width) // 2 * 2
    scale = width / frame_width
    height = max(2, int(round(band * scale)) // 2 * 2)
    targets = [(cx * scale, (cy - top) * scale, w * scale) for cx, cy, w in speakers]
    anchor = np.float32([[10, 10], [30, 10]])
    previous: list[np.ndarray | None] = [None] * len(speakers)
    energy: list[list[float]] = [[] for _ in speakers]
    sample = None
    stream = frames if frames is not None else _read_frames(video, start, end, width, height, ACTIVITY_FPS,
                                                             crop=(frame_width, band, 0, top))
    for frame in stream:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        rows, cols = gray.shape
        for k, (sx, sy, sw) in enumerate(targets):
            # Detect in a small square around this speaker only (a tenth of the pixels).
            half = int(1.8 * sw)
            x0, y0 = max(0, int(sx) - half), max(0, int(sy) - half)
            x1, y1 = min(cols, int(sx) + half), min(rows, int(sy) + half)
            window = np.ascontiguousarray(frame[y0:y1, x0:x1])
            faces = _landmarked_faces(detector, window) if window.size else []
            near = [f for f in faces if abs(f[0] + x0 - sx) < sw and abs(f[1] + y0 - sy) < sw]
            matrix = None
            if near:
                corners = np.float32([[near[0][2][0] + x0, near[0][2][1] + y0], [near[0][3][0] + x0, near[0][3][1] + y0]])
                matrix, _ = cv2.estimateAffinePartial2D(corners, anchor)
            if matrix is None:
                energy[k].append(math.nan)
                previous[k] = None
                continue
            patch = cv2.warpAffine(gray, matrix, (40, 24), flags=cv2.INTER_LINEAR).astype(np.float32)
            patch = (patch - patch.mean()) / (patch.std() + 1e-3)
            energy[k].append(float(np.abs(patch - previous[k]).mean()) if previous[k] is not None else math.nan)
            previous[k] = patch
    count = min((len(e) for e in energy), default=0)
    times = np.arange(count) / ACTIVITY_FPS
    if frames is None:
        sample = _frame_at(video, start + min(1.0, (end - start) / 2))
    return times, np.array([e[:count] for e in energy], dtype=np.float64), sample


def _frame_at(video: Path, at: float) -> np.ndarray | None:
    """One full-size frame (for the close-up's picture check)."""
    capture = cv2.VideoCapture(str(video))
    try:
        capture.set(cv2.CAP_PROP_POS_MSEC, at * 1000.0)
        ok, frame = capture.read()
        return frame if ok else None
    finally:
        capture.release()


def _landmarked_faces(detector: Any, frame: np.ndarray) -> list[tuple[float, float, tuple, tuple]]:
    """(center x, center y, right mouth corner, left mouth corner) per face (YuNet)."""
    yunet = getattr(detector, "_yunet", None)
    if yunet is None:
        return []
    height, width = frame.shape[:2]
    if getattr(detector, "_yunet_size", None) != (width, height):
        yunet.setInputSize((width, height))
        detector._yunet_size = (width, height)
    _count, faces = yunet.detect(frame)
    if faces is None:
        return []
    return [(float(f[0] + f[2] / 2), float(f[1] + f[3] / 2), (float(f[10]), float(f[11])), (float(f[12]), float(f[13])))
            for f in faces]


def phrases(words: list[Any]) -> list[tuple[float, float]]:
    """Stretches of talk (clip seconds): words with less than PHRASE_GAP between them."""
    out: list[list[float]] = []
    for word in words:
        a, b = float(word.start), float(word.end)
        if out and a - out[-1][1] <= PHRASE_GAP:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def turns_from(words: list[Any], times: np.ndarray, energy: np.ndarray) -> list[Turn]:
    """Who says each phrase (the mouth that moves CONFIDENT_RATIO more), merged into turns.
    A phrase with no clear winner joins the turn around it only when it's short (under 2 s);
    otherwise it's an unsure turn of its own."""
    labelled: list[Turn] = []
    for a, b in phrases(words):
        inside = (times >= a) & (times <= b)
        if inside.sum() < 3:
            labelled.append(Turn(None, a, b, 0.0))
            continue
        means = []
        for row in energy:
            values = row[inside]
            values = values[~np.isnan(values)]
            means.append(float(values.mean()) if len(values) >= max(3, 0.4 * inside.sum()) else math.nan)
        if any(math.isnan(m) for m in means) or len(means) < 2:
            labelled.append(Turn(None, a, b, 0.0))
            continue
        order = sorted(range(len(means)), key=lambda k: -means[k])
        ratio = means[order[0]] / max(1e-6, means[order[1]])
        labelled.append(Turn(order[0] if ratio >= CONFIDENT_RATIO else None, a, b, ratio))
    for i, turn in enumerate(labelled):  # short unsure phrases between the same speaker's phrases
        if turn.speaker is None and turn.end - turn.start < 2.0:
            before = labelled[i - 1].speaker if i > 0 else None
            after = next((t.speaker for t in labelled[i + 1:] if t.speaker is not None), None)
            if before is not None and before == after:
                labelled[i] = Turn(before, turn.start, turn.end, CONFIDENT_RATIO)
    merged: list[Turn] = []
    for turn in labelled:
        if merged and merged[-1].speaker == turn.speaker and turn.start - merged[-1].end < 1.5:
            last = merged[-1]
            span = (last.end - last.start) + (turn.end - turn.start)
            confidence = ((last.end - last.start) * last.confidence + (turn.end - turn.start) * turn.confidence) / max(1e-6, span)
            merged[-1] = Turn(turn.speaker, last.start, turn.end, confidence)
        else:
            merged.append(turn)
    return merged


# ------------------------------------------------------------------ the cuts
def _gaps(words: list[Any]) -> list[tuple[float, float, bool]]:
    """(gap start, gap end, a sentence ends there) between consecutive words."""
    out = []
    for first, second in zip(words, words[1:]):
        end = re.search(r"[.?!][\"'”’)]*$", str(first.text)) is not None
        out.append((float(first.end), float(second.start), end or float(second.start) - float(first.end) >= 0.5))
    return out


def snap(t: float, words: list[Any], reach: float = 0.8, sentence: bool = False) -> float:
    """The middle of the word gap nearest ``t`` (within ``reach``; a sentence break if asked)."""
    best = None
    for a, b, ends in _gaps(words):
        if sentence and not ends:
            continue
        middle = (a + b) / 2
        if abs(middle - t) <= reach and (best is None or abs(middle - t) < abs(best - t)):
            best = middle
    return round(best if best is not None else t, 3)


def plan_cuts(turns: list[Turn], span: tuple[float, float], words: list[Any],
              establish: float = ESTABLISH, min_turn: float = MIN_TURN) -> list[tuple[float, float, int]]:
    """Close-up windows (start, end, speaker) inside one two-person span (clip seconds)."""
    first, last = span
    earliest = first + establish
    joined: list[Turn] = []
    for turn in turns:  # the same person again after a short pause: one turn (split evenly below)
        if (joined and turn.speaker is not None and joined[-1].speaker == turn.speaker
                and turn.start - joined[-1].end < MIN_SHOT):
            last_turn = joined[-1]
            joined[-1] = Turn(turn.speaker, last_turn.start, turn.end, min(last_turn.confidence, turn.confidence))
        else:
            joined.append(turn)
    windows: list[list[float]] = []
    for turn in joined:
        if turn.speaker is None or turn.confidence < CONFIDENT_RATIO or turn.end - turn.start < min_turn:
            continue
        a = max(turn.start - 0.12, earliest)
        b = min(turn.end + 0.25, last)
        if a > turn.start:  # cut in mid-turn (after the establishing shot): between two words
            a = snap(a, words, reach=1.0)
        if b - a < min(MIN_SHOT, min_turn) or (min_turn >= MIN_TURN and b - a < MIN_TURN - 0.5):
            continue
        pieces = int(math.ceil((b - a + BREATHER) / (MAX_CLOSEUP + BREATHER))) if b - a > MAX_CLOSEUP else 1
        if pieces == 1:
            windows.append([a, b, turn.speaker])
            continue
        size = (b - a) / pieces
        cursor = a
        for k in range(1, pieces):  # a breather on the two-shot at a sentence break near each split
            at = snap(a + k * size - BREATHER / 2, words, reach=3.0, sentence=True)
            back = snap(at + BREATHER, words, reach=0.8)
            if at - cursor < MIN_SHOT * 2 or back >= b - MIN_SHOT:
                continue
            windows.append([cursor, at, turn.speaker])
            cursor = back
        windows.append([cursor, b, turn.speaker])
    windows.sort()
    tidy: list[list[float]] = []
    for window in windows:
        if tidy and window[0] - tidy[-1][1] < MIN_SHOT:
            previous = tidy[-1]
            if previous[2] == window[2]:
                previous[1] = max(previous[1], window[1])  # a too-short two-shot between: stay on them
                continue
            middle = snap((previous[1] + window[0]) / 2, words, reach=0.6)  # straight from one close-up to the other
            previous[1], window[0] = middle, middle
        tidy.append(window)
    tidy = [w for w in tidy if w[1] - w[0] >= MIN_SHOT]
    if tidy and last - tidy[-1][1] < MIN_SHOT:
        tidy[-1][1] = last  # no flash of the two-shot at the end
    return [(round(a, 3), round(b, 3), int(s)) for a, b, s in tidy]


def subtract(spans: list[tuple[float, float]] | tuple, windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """``spans`` without ``windows`` (the two-shot's time once the close-ups are cut in)."""
    out: list[tuple[float, float]] = []
    for a, b in spans:
        pieces = [(a, b)]
        for c, d in windows:
            pieces = [p for x, y in pieces for p in ((x, min(y, c)), (max(x, d), y)) if p[1] - p[0] > 0.04]
        out += pieces
    return out


def direct(source_video: Path, start: float, end: float, plan: Any, split: Any, words: list[Any],
           establish: bool = True, energy_fn: Any = None, min_turn: float | None = None) -> Direction | None:
    """The close-ups for a clip's two-person shots (``words`` in clip seconds), or None when
    there's nothing to cut to. ``split``: the plan's split view for the clip (the two-shot)."""
    if plan is None or split is None or not getattr(split, "spans", None):
        return None
    direction = Direction()
    built: dict[int, CloseUp] = {}
    spans_by: dict[int, list[tuple[float, float]]] = {}
    for shot in plan.shots:
        if not (shot.group and shot.speakers) or shot.end <= start or shot.start >= end:
            continue
        a, b = max(shot.start, start), min(shot.end, end)
        relative = (a - start, b - start)
        if not any(x < relative[1] and relative[0] < y for x, y in split.spans):
            continue  # this two-person shot isn't the split view (shown another way)
        speakers = [tuple(map(float, s)) for s in shot.speakers][:2]
        boxes = [closeup_box(s, plan.frame_width, plan.frame_height, k) for k, s in enumerate(speakers)]
        if not any(boxes):
            direction.notes.append(f"{relative[0]:.1f}-{relative[1]:.1f} s: faces too small for a sharp close-up")
            continue
        times, energy, sample = (energy_fn or mouth_energy)(
            source_video, a, b, speakers, (plan.frame_width, plan.frame_height))
        if sample is not None:
            for k, box in enumerate(boxes):
                if box is not None and not box_has_picture(sample, box):
                    direction.notes.append(f"{'left' if k == 0 else 'right'} close-up would be mostly backdrop")
                    boxes[k] = None
        shot_words = [w for w in words if relative[0] <= float(w.start) < relative[1]]
        local = [type("W", (), {"text": w.text, "start": float(w.start) - relative[0],
                                "end": float(w.end) - relative[0]})() for w in shot_words]
        turns = turns_from(local, times, energy)
        direction.turns += [Turn(t.speaker, t.start + relative[0], t.end + relative[0], t.confidence) for t in turns]
        usable = [t if t.speaker is not None and boxes[t.speaker] is not None else Turn(None, t.start, t.end, 0.0)
                  for t in turns]
        cuts = plan_cuts(usable, (0.0, relative[1] - relative[0]), local,
                         establish=ESTABLISH if establish and relative[0] <= 0.05 else (MIN_SHOT if establish else 0.0),
                         min_turn=min_turn if min_turn is not None else (MIN_TURN if establish else TEASER_TURN))
        for c0, c1, k in cuts:
            built.setdefault(k, boxes[k])
            spans_by.setdefault(k, []).append((round(c0 + relative[0], 3), round(c1 + relative[0], 3)))
        if not cuts:
            direction.notes.append(f"{relative[0]:.1f}-{relative[1]:.1f} s: no one holds the floor for "
                                   f"{MIN_TURN:g} s with a clear lead (stays on the two-shot)")
    direction.layers = [(built[k], spans) for k, spans in sorted(spans_by.items())]
    return direction if direction.layers else (direction if direction.notes else None)
