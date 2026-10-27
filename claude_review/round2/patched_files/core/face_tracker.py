from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import ssl
import statistics
import subprocess
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


logger = logging.getLogger(__name__)
__all__ = [
    "CropAnalysis",
    "CropBox",
    "CropPlan",
    "Shot",
    "analyze_crop_plan",
    "analyze_vertical_crop",
    "find_vertical_crop",
]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
YUNET_FILENAME = "face_detection_yunet_2023mar.onnx"
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
YUNET_URL = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
    f"face_detection_yunet/{YUNET_FILENAME}"
)
YUNET_SCORE_THRESHOLD = 0.7
CASCADE_FILENAME = "haarcascade_frontalface_default.xml"
CASCADE_URL = (
    "https://raw.githubusercontent.com/opencv/opencv/master/data/haarcascades/"
    f"{CASCADE_FILENAME}"
)


@dataclass(frozen=True)
class CropBox:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class CropAnalysis:
    crop: CropBox
    use_wide_layout: bool


@dataclass(frozen=True)
class _FaceDetection:
    center_x: float
    area: float
    frame_number: int


FACE_CONFIDENCE_THRESHOLD = 0.5
FACE_CLUSTER_DISTANCE_RATIO = 0.18


def _cascade_cache_path() -> Path:
    if os.name == "nt":
        cache_root = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif platform.system() == "Darwin":
        cache_root = Path.home() / "Library" / "Caches"
    else:
        cache_root = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_root / "short-form-video-generator" / CASCADE_FILENAME


def _cascade_classifier(path: Path) -> cv2.CascadeClassifier | None:
    try:
        detector = cv2.CascadeClassifier(str(path))
        return detector if not detector.empty() else None
    except cv2.error:
        return None


def _load_face_detector() -> cv2.CascadeClassifier | None:
    try:
        bundled_path = Path(cv2.data.haarcascades) / CASCADE_FILENAME
        detector = _cascade_classifier(bundled_path)
        if detector is not None:
            return detector

        cache_path = _cascade_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cached_detector = (
            _cascade_classifier(cache_path) if cache_path.is_file() else None
        )
        if cached_detector is not None:
            return cached_detector
        if cache_path.exists():
            cache_path.unlink()

        temporary_path: Path | None = None
        try:
            try:
                import certifi

                context = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                context = ssl.create_default_context()
            with urllib.request.urlopen(
                CASCADE_URL,
                timeout=20,
                context=context,
            ) as response:
                with tempfile.NamedTemporaryFile(
                    dir=cache_path.parent,
                    prefix=f"{CASCADE_FILENAME}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    shutil.copyfileobj(response, temporary_file)
            downloaded_detector = _cascade_classifier(temporary_path)
            if downloaded_detector is None:
                raise RuntimeError("Downloaded Haar Cascade XML is invalid.")
            temporary_path.replace(cache_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        detector = _cascade_classifier(cache_path)
        if detector is None:
            cache_path.unlink(missing_ok=True)
            raise RuntimeError(f"Could not load the Haar Cascade XML from {cache_path}.")
        return detector
    except Exception:
        logger.warning(
            "OpenCV face detector is unavailable; using a centered crop instead.",
            exc_info=True,
        )
        return None



# ---------------------------------------------------------------------------
# Face detection: YuNet (accurate, handles side profiles) with Haar fallback
# ---------------------------------------------------------------------------

def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _yunet_model_path() -> Path | None:
    """Return a verified YuNet model, downloading it once if needed."""
    candidates = [
        PROJECT_ROOT / "models" / YUNET_FILENAME,
        _cascade_cache_path().parent / YUNET_FILENAME,
    ]
    for candidate in candidates:
        if candidate.is_file() and _sha256(candidate) == YUNET_SHA256:
            return candidate
    target = candidates[1]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(YUNET_URL, timeout=20, context=_ssl_context()) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != YUNET_SHA256:
            raise RuntimeError("Downloaded YuNet model failed its checksum.")
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        return target
    except Exception:
        logger.warning(
            "YuNet face model unavailable; falling back to the Haar cascade.",
            exc_info=True,
        )
        return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _FaceDetector:
    """Returns faces as (center_x, center_y, width, height, score) in frame pixels."""

    def __init__(self) -> None:
        self._yunet = None
        self._yunet_size: tuple[int, int] | None = None
        self._haar = None
        model = _yunet_model_path()
        if model is not None and hasattr(cv2, "FaceDetectorYN"):
            try:
                self._yunet = cv2.FaceDetectorYN.create(
                    str(model), "", (320, 320), YUNET_SCORE_THRESHOLD, 0.3, 5000
                )
            except cv2.error:
                logger.warning("Could not load YuNet; using Haar cascade.", exc_info=True)
        if self._yunet is None:
            self._haar = _load_face_detector()

    @property
    def available(self) -> bool:
        return self._yunet is not None or self._haar is not None

    def detect(self, frame: np.ndarray) -> list[tuple[float, float, float, float, float]]:
        height, width = frame.shape[:2]
        if self._yunet is not None:
            if self._yunet_size != (width, height):
                self._yunet.setInputSize((width, height))
                self._yunet_size = (width, height)
            _count, faces = self._yunet.detect(frame)
            if faces is None:
                return []
            return [
                (float(x + w / 2), float(y + h / 2), float(w), float(h), float(score))
                for x, y, w, h, *_landmarks, score in faces
            ]
        if self._haar is None:
            return []
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        detections, _, scores = self._haar.detectMultiScale3(
            gray,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(24, 24),
            outputRejectLevels=True,
        )
        return [
            (float(x + w / 2), float(y + h / 2), float(w), float(h), float(score))
            for (x, y, w, h), score in zip(detections, scores)
            if float(score) >= FACE_CONFIDENCE_THRESHOLD
        ]


_thread_state = threading.local()


def _face_detector() -> _FaceDetector:
    """One detector per thread: the app renders two clips at once, and an
    OpenCV DNN model shouldn't run on two threads at the same time."""
    detector = getattr(_thread_state, "detector", None)
    if detector is None:
        detector = _FaceDetector()
        _thread_state.detector = detector
    return detector


# ---------------------------------------------------------------------------
# Per-shot crop plan
# ---------------------------------------------------------------------------

CUT_ANALYSIS_FPS = 30
CUT_FRAME_SIZE = (96, 54)
FACE_ANALYSIS_FPS = 2
FACE_FRAME_WIDTH = 640
CUT_MIN_DIFFERENCE = 18.0
CUT_DIFFERENCE_RATIO = 4.0
CUT_HISTOGRAM_CORRELATION = 0.5
MIN_SHOT_SECONDS = 0.6
FACE_SHOT_PRESENCE = 0.4
GROUP_SHOT_PRESENCE = 0.5
SAME_X_TOLERANCE_PX = 8


@dataclass(frozen=True)
class Shot:
    """One camera shot, in absolute source seconds, with its crop x position."""

    start: float
    end: float
    x: int
    has_face: bool
    group: bool


@dataclass(frozen=True)
class CropPlan:
    frame_width: int
    frame_height: int
    crop_width: int
    crop_height: int
    y: int
    shots: tuple[Shot, ...]

    @property
    def duration(self) -> float:
        return sum(shot.end - shot.start for shot in self.shots) or 1e-9

    @property
    def face_coverage(self) -> float:
        return sum(s.end - s.start for s in self.shots if s.has_face) / self.duration

    @property
    def group_coverage(self) -> float:
        return sum(s.end - s.start for s in self.shots if s.group) / self.duration

    @property
    def cut_count(self) -> int:
        return max(0, len(self.shots) - 1)

    @property
    def crop(self) -> CropBox:
        """Single crop for callers that can't follow shots: the longest face shot."""
        face_shots = [shot for shot in self.shots if shot.has_face] or list(self.shots)
        best = max(face_shots, key=lambda shot: shot.end - shot.start)
        return CropBox(x=best.x, y=self.y, width=self.crop_width, height=self.crop_height)

    def x_expression(self, clip_start: float, clip_end: float) -> str:
        """FFmpeg crop x expression that switches position at each camera cut.

        ``t`` is clip-relative because the renderer seeks with ``-ss`` before
        ``-i``. Crop moves happen exactly on cuts, so viewers never see a pan.
        """
        segments: list[tuple[float, int]] = []
        for shot in self.shots:
            if shot.end <= clip_start or shot.start >= clip_end:
                continue
            boundary = min(shot.end, clip_end) - clip_start
            if segments and abs(segments[-1][1] - shot.x) <= SAME_X_TOLERANCE_PX:
                segments[-1] = (boundary, segments[-1][1])
            else:
                segments.append((boundary, shot.x))
        if not segments:
            return str(self.crop.x)
        expression = str(segments[-1][1])
        for boundary, x in reversed(segments[:-1]):
            expression = f"if(lt(t,{boundary:.3f}),{x},{expression})"
        return expression


def _probe_display_size(video_path: Path) -> tuple[int, int]:
    """Frame size as FFmpeg will decode it (rotation metadata applied)."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:stream_side_data=rotation:stream_tags=rotate",
            "-of",
            "json",
            str(video_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = (json.loads(result.stdout).get("streams") or [{}])[0]
    width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    rotation = 0
    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            rotation = int(float(side_data["rotation"]))
    if not rotation and (stream.get("tags") or {}).get("rotate"):
        rotation = int(float(stream["tags"]["rotate"]))
    if abs(rotation) % 180 == 90:
        width, height = height, width
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Video has invalid dimensions: {video_path}")
    return width, height


def _decode_analysis_frames(
    video_path: Path,
    start_time: float,
    duration: float,
    face_size: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray]:
    """Decode the clip once into tiny gray frames (cuts) and small color frames (faces)."""
    cut_w, cut_h = CUT_FRAME_SIZE
    face_w, face_h = face_size
    with tempfile.TemporaryDirectory(prefix="crop-analysis-") as temp_dir:
        face_file = Path(temp_dir) / "faces.raw"
        command = [
            shutil.which("ffmpeg") or "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{start_time:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(video_path),
            "-an",
            "-filter_complex",
            (
                f"[0:v]split=2[a][b];"
                f"[a]fps={CUT_ANALYSIS_FPS},scale={cut_w}:{cut_h},format=gray[cuts];"
                f"[b]fps={FACE_ANALYSIS_FPS},scale={face_w}:{face_h},format=bgr24[faces]"
            ),
            "-map",
            "[cuts]",
            "-f",
            "rawvideo",
            "pipe:1",
            "-map",
            "[faces]",
            "-f",
            "rawvideo",
            str(face_file),
        ]
        result = subprocess.run(command, check=True, capture_output=True)
        cut_frames = np.frombuffer(result.stdout, dtype=np.uint8)
        cut_frames = cut_frames[: cut_frames.size - cut_frames.size % (cut_w * cut_h)]
        cut_frames = cut_frames.reshape(-1, cut_h, cut_w)
        face_bytes = np.fromfile(face_file, dtype=np.uint8)
        frame_size = face_w * face_h * 3
        face_frames = face_bytes[: face_bytes.size - face_bytes.size % frame_size]
        face_frames = face_frames.reshape(-1, face_h, face_w, 3)
    return cut_frames, face_frames


def _cut_times(cut_frames: np.ndarray) -> list[float]:
    """Times (clip-relative) where the camera cuts.

    Uses pixel difference between consecutive frames, which catches cuts between
    two angles of the same set (same colors), plus a histogram check.
    """
    times: list[float] = []
    previous: np.ndarray | None = None
    previous_hist: np.ndarray | None = None
    recent: list[float] = []
    for index, frame in enumerate(cut_frames):
        hist = cv2.calcHist([frame], [0], None, [32], [0, 256])
        cv2.normalize(hist, hist)
        if previous is not None:
            difference = float(np.mean(cv2.absdiff(frame, previous)))
            baseline = statistics.median(recent) if recent else 0.0
            correlation = cv2.compareHist(previous_hist, hist, cv2.HISTCMP_CORREL)
            # A big jump in pixels, or a colour change backed by a moderate jump.
            # (Histogram correlation alone is noisy on flat or dark frames.)
            is_cut = (
                difference >= CUT_MIN_DIFFERENCE
                and difference >= CUT_DIFFERENCE_RATIO * max(baseline, 1.0)
            ) or (
                correlation < CUT_HISTOGRAM_CORRELATION
                and difference >= CUT_MIN_DIFFERENCE / 2
            )
            if is_cut:
                times.append(index / CUT_ANALYSIS_FPS)
                recent.clear()
            else:
                recent.append(difference)
                del recent[:-15]
        previous = frame
        previous_hist = hist
    return times


def _cluster(values: list[tuple[float, float]], distance: float) -> list[list[tuple[float, float]]]:
    clusters: list[list[tuple[float, float]]] = []
    for value in sorted(values):
        for cluster in clusters:
            center = sum(item[0] for item in cluster) / len(cluster)
            if abs(value[0] - center) <= distance:
                cluster.append(value)
                break
        else:
            clusters.append([value])
    return clusters


def analyze_crop_plan(
    video_path: str | Path,
    start_time: float,
    end_time: float,
) -> CropPlan:
    """Split the clip into camera shots and choose a 9:16 crop for each one."""
    source = Path(video_path)
    if end_time <= start_time:
        raise ValueError("end_time must be after start_time.")
    frame_width, frame_height = _probe_display_size(source)
    crop_width = min(frame_width, round(frame_height * 9 / 16))
    crop_height = min(frame_height, round(crop_width * 16 / 9))
    crop_width = min(frame_width, round(crop_height * 9 / 16)) // 2 * 2
    crop_height = crop_height // 2 * 2
    y = max(0, (frame_height - crop_height) // 2)
    center_x = ((frame_width - crop_width) // 2) // 2 * 2

    duration = end_time - start_time
    face_height = max(2, round(FACE_FRAME_WIDTH * frame_height / frame_width / 2) * 2)
    cut_frames, face_frames = _decode_analysis_frames(
        source, start_time, duration, (FACE_FRAME_WIDTH, face_height)
    )

    # Shot boundaries (clip-relative), dropping flash cuts shorter than MIN_SHOT_SECONDS.
    boundaries = [0.0]
    for cut in _cut_times(cut_frames):
        if cut - boundaries[-1] >= MIN_SHOT_SECONDS and duration - cut >= MIN_SHOT_SECONDS:
            boundaries.append(cut)
    boundaries.append(duration)

    detector = _face_detector()
    scale = frame_width / FACE_FRAME_WIDTH
    samples: list[tuple[float, list[tuple[float, float]]]] = []
    for index, frame in enumerate(face_frames):
        faces = detector.detect(frame) if detector.available else []
        samples.append(
            (index / FACE_ANALYSIS_FPS, [(cx * scale, w * h * scale * scale) for cx, _cy, w, h, _s in faces])
        )

    cluster_distance = frame_width * FACE_CLUSTER_DISTANCE_RATIO
    raw_shots: list[tuple[float, float, int | None, bool]] = []
    for shot_start, shot_end in zip(boundaries, boundaries[1:]):
        shot_samples = [faces for t, faces in samples if shot_start <= t < shot_end]
        if not shot_samples:  # very short shot between two face samples
            nearest = min(samples, key=lambda item: abs(item[0] - (shot_start + shot_end) / 2), default=None)
            shot_samples = [nearest[1]] if nearest else []
        detections = [face for faces in shot_samples for face in faces]
        x: int | None = None
        has_face = False
        group = False
        if detections:
            clusters = _cluster(detections, cluster_distance)
            dominant = max(clusters, key=lambda c: (len(c), sum(a for _x, a in c) / len(c)))
            has_face = len(dominant) >= FACE_SHOT_PRESENCE * len(shot_samples)
            face_center = statistics.median(cx for cx, _a in dominant)
            x = int(max(0, min(frame_width - crop_width, face_center - crop_width / 2))) // 2 * 2
            spread_samples = sum(
                1
                for faces in shot_samples
                if len(faces) >= 2
                and max(cx for cx, _a in faces) - min(cx for cx, _a in faces) > crop_width * 0.9
            )
            group = spread_samples >= GROUP_SHOT_PRESENCE * len(shot_samples)
        raw_shots.append((shot_start, shot_end, x if has_face else None, group))

    # Shots without a face reuse the nearest face shot's position (or the center).
    shots: list[Shot] = []
    known = [(s, x) for s, _e, x, _g in raw_shots if x is not None]
    for shot_start, shot_end, x, group in raw_shots:
        has_face = x is not None
        if x is None:
            x = min(known, key=lambda item: abs(item[0] - shot_start))[1] if known else center_x
        shots.append(
            Shot(
                start=start_time + shot_start,
                end=start_time + shot_end,
                x=x,
                has_face=has_face,
                group=group,
            )
        )
    return CropPlan(
        frame_width=frame_width,
        frame_height=frame_height,
        crop_width=crop_width,
        crop_height=crop_height,
        y=y,
        shots=tuple(shots),
    )


def analyze_vertical_crop(
    video_path: str | Path,
    start_time: float,
    end_time: float,
    sample_interval_seconds: float = 1.0,
) -> CropAnalysis:
    """Backward-compatible wrapper: one crop plus a "has camera cuts" hint."""
    plan = analyze_crop_plan(video_path, start_time, end_time)
    return CropAnalysis(crop=plan.crop, use_wide_layout=plan.cut_count > 0)


def find_vertical_crop(
    video_path: str | Path,
    start_time: float,
    end_time: float,
    sample_interval_seconds: float = 1.0,
) -> CropBox:
    """Return a single 9:16 crop for callers that do not follow shots."""
    return analyze_crop_plan(video_path, start_time, end_time).crop
