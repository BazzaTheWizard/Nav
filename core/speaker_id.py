"""Who is who on screen, for the name card.

The guest is the person most often alone on screen across the video (the solo camera,
or the "active speaker" view of a call recording). Their face is matched with OpenCV's
SFace recognizer, so a name card only ever goes on the guest: on a solo shot of them,
or inside their own panel of a two-person split.

Also: where the guest's face sits in each shot (so the card avoids the chin and mouth),
and who is talking in a split (mouth movement), so a card in a panel comes in while
that person speaks.
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
import cv2
import numpy as np

from core.face_tracker import PROJECT_ROOT, YUNET_SCORE_THRESHOLD, _cascade_cache_path, _ssl_context, _yunet_model_path

logger = logging.getLogger(__name__)

SFACE_FILENAME = "face_recognition_sface_2021dec.onnx"
SFACE_SHA256 = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
SFACE_URL = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
    f"face_recognition_sface/{SFACE_FILENAME}"
)
SAME_PERSON = 0.363  # SFace cosine similarity at or above this: the same person (OpenCV's threshold)
ID_FRAME_WIDTH = 1280  # frames are matched at this width (faces stay sharp enough for SFace)
MIN_FACE_SHARE = 0.04  # faces narrower than this share of the frame aren't matched
VIDEO_SAMPLES = 36  # frames sampled across the whole video to find the guest
MOUTH_FPS = 12.5

_cache: dict[tuple[str, int, int], tuple[float, ...] | None] = {}


@dataclass(frozen=True)
class SeenFace:
    """A face in source pixels, with its SFace feature (unit length)."""

    cx: float
    cy: float
    w: float
    feature: tuple[float, ...]


def _model_path() -> Path | None:
    """The verified SFace model, downloaded once (like the YuNet face finder)."""
    candidates = [PROJECT_ROOT / "models" / SFACE_FILENAME, _cascade_cache_path().parent / SFACE_FILENAME]
    for candidate in candidates:
        if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == SFACE_SHA256:
            return candidate
    target = candidates[1]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(SFACE_URL, timeout=60, context=_ssl_context()) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != SFACE_SHA256:
            raise RuntimeError("Downloaded SFace model failed its checksum.")
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        return target
    except Exception:
        logger.warning("Face matching unavailable (SFace model); name cards fall back to screen time.", exc_info=True)
        return None


class _Matcher:
    def __init__(self) -> None:
        self.ready = False
        detector, recognizer = _yunet_model_path(), _model_path()
        if detector is None or recognizer is None or not hasattr(cv2, "FaceRecognizerSF"):
            return
        try:
            self.detector = cv2.FaceDetectorYN.create(str(detector), "", (320, 320), YUNET_SCORE_THRESHOLD, 0.3, 5000)
            self.recognizer = cv2.FaceRecognizerSF.create(str(recognizer), "")
            self.ready = True
        except cv2.error:
            logger.warning("Could not load the face matcher.", exc_info=True)

    def faces(self, image: np.ndarray, scale: float) -> list[SeenFace]:
        height, width = image.shape[:2]
        self.detector.setInputSize((width, height))
        _count, rows = self.detector.detect(image)
        found: list[SeenFace] = []
        for row in rows if rows is not None else []:
            x, y, w, h = (float(v) for v in row[:4])
            if w < MIN_FACE_SHARE * width:
                continue
            feature = self.recognizer.feature(self.recognizer.alignCrop(image, row)).flatten().astype(np.float64)
            feature /= max(np.linalg.norm(feature), 1e-9)
            found.append(SeenFace((x + w / 2) * scale, (y + h / 2) * scale, w * scale, tuple(feature.tolist())))
        return found


_matcher: _Matcher | None = None


def _get_matcher() -> _Matcher | None:
    global _matcher
    if _matcher is None:
        _matcher = _Matcher()
    return _matcher if _matcher.ready else None


def similarity(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    return float(np.dot(np.asarray(first), np.asarray(second)))


def faces_at(source: str | Path, times: list[float], frame_width: int) -> list[list[SeenFace]]:
    """The faces (source pixels) in the frames at these source times. Empty lists if the
    matcher isn't available."""
    matcher = _get_matcher()
    if matcher is None:
        return [[] for _ in times]
    scale = frame_width / ID_FRAME_WIDTH
    out: list[list[SeenFace]] = []
    for time in times:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, time):.3f}", "-i", str(source), "-frames:v", "1",
             "-vf", f"scale={ID_FRAME_WIDTH}:-2", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            capture_output=True,
        ).stdout
        height = len(raw) // (ID_FRAME_WIDTH * 3)
        if height < 2:
            out.append([])
            continue
        image = np.frombuffer(raw[: height * ID_FRAME_WIDTH * 3], np.uint8).reshape(height, ID_FRAME_WIDTH, 3)
        out.append(matcher.faces(image, scale))
    return out


def video_duration(source: str | Path) -> float:
    """The video's length in seconds (0 if it can't be read)."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(source)],
                             capture_output=True, text=True, timeout=30).stdout
        return float(out.strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def guest_face(source: str | Path, duration: float, frame_width: int) -> tuple[float, ...] | None:
    """The guest's face feature: whoever is alone on screen most often across the video
    (at least 4 of the sampled solo frames, and 35% of them). None if that's unclear."""
    path = Path(source)
    try:
        key = (str(path.resolve()), path.stat().st_size, int(path.stat().st_mtime))
    except OSError:
        return None
    if key in _cache:
        return _cache[key]
    times = [duration * (0.04 + 0.92 * k / (VIDEO_SAMPLES - 1)) for k in range(VIDEO_SAMPLES)]
    solos = [faces[0] for faces in faces_at(source, times, frame_width) if len(faces) == 1]
    groups: list[list[SeenFace]] = []
    for face in solos:
        for group in groups:
            if similarity(_mean(group), face.feature) >= SAME_PERSON:
                group.append(face)
                break
        else:
            groups.append([face])
    best = max(groups, key=len, default=[])
    result = _mean(best) if len(best) >= 4 and len(best) >= 0.35 * len(solos) else None
    _cache[key] = result
    return result


def _mean(faces: list[SeenFace]) -> tuple[float, ...]:
    total = np.sum([np.asarray(face.feature) for face in faces], axis=0)
    return tuple((total / max(np.linalg.norm(total), 1e-9)).tolist())


def best_match(faces: list[SeenFace], guest: tuple[float, ...]) -> SeenFace | None:
    """The face that is the guest, if any is."""
    scored = [(similarity(face.feature, guest), face) for face in faces]
    score, face = max(scored, key=lambda item: item[0], default=(0.0, None))
    return face if score >= SAME_PERSON else None


def mouth_activity(
    source: str | Path, start: float, end: float, faces: list[tuple[float, float, float]]
) -> np.ndarray | None:
    """How much each face's mouth moves, per frame at MOUTH_FPS, from ``start`` (source
    seconds): an array (frames, faces). For telling who talks in a split of webcams,
    where the faces hold still."""
    if not faces or end - start < 0.5:
        return None
    parts, labels = [], []
    for k, (cx, cy, w) in enumerate(faces):
        bw, bh = max(8, int(w * 0.6)) // 2 * 2, max(6, int(w * 0.36)) // 2 * 2
        parts.append(f"[s{k}]crop={bw}:{bh}:{max(0, int(cx - bw / 2))}:{max(0, int(cy + w * 0.30 - bh / 2))},"
                     f"scale=48:32,format=gray[m{k}]")
        labels.append(f"[m{k}]")
    graph = (f"[0:v]fps={MOUTH_FPS},split={len(faces)}" + "".join(f"[s{k}]" for k in range(len(faces))) + ";"
             + ";".join(parts) + ";" + "".join(labels) + (f"hstack=inputs={len(faces)}" if len(faces) > 1 else "null"))
    try:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
             "-filter_complex", graph, "-f", "rawvideo", "-"],
            capture_output=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return None
    frames = np.frombuffer(raw, np.uint8)
    width = 48 * len(faces)
    if frames.size < 2 * 32 * width:
        return None
    frames = frames[: frames.size // (32 * width) * 32 * width].reshape(-1, 32, width).astype(np.float32)
    motion = np.abs(np.diff(frames, axis=0))
    per_face = np.stack([motion[:, :, 48 * k:48 * (k + 1)].mean(axis=(1, 2)) for k in range(len(faces))], axis=1)
    window = max(1, int(MOUTH_FPS))
    kernel = np.ones(window) / window
    return np.stack([np.convolve(per_face[:, k], kernel, "same") for k in range(len(faces))], axis=1)
