from __future__ import annotations

import logging
import os
import platform
import shutil
import ssl
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import cv2


logger = logging.getLogger(__name__)
__all__ = ["CropAnalysis", "CropBox", "analyze_vertical_crop", "find_vertical_crop"]
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


def analyze_vertical_crop(
    video_path: str | Path,
    start_time: float,
    end_time: float,
    sample_interval_seconds: float = 1.0,
) -> CropAnalysis:
    """Analyze sampled faces and scene changes, returning a crop and safe-layout hint."""
    if sample_interval_seconds <= 0:
        raise ValueError("sample_interval_seconds must be positive.")

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open video for face tracking: {video_path}")

    try:
        frame_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if frame_width <= 0 or frame_height <= 0 or fps <= 0 or frame_count <= 0:
            raise RuntimeError(f"Video has invalid dimensions or timing metadata: {video_path}")

        face_detections: list[_FaceDetection] = []
        use_wide_layout = False
        previous_histogram = None
        detector = _load_face_detector()
        crop_width = min(frame_width, round(frame_height * 9 / 16))
        crop_height = min(frame_height, round(crop_width * 16 / 9))
        crop_width = round(crop_height * 9 / 16)
        if detector is None:
            x = ((frame_width - crop_width) // 2) & ~1
            y = (frame_height - crop_height) // 2
            return CropAnalysis(
                crop=CropBox(x=x, y=y, width=crop_width, height=crop_height),
                use_wide_layout=False,
            )

        first_frame = min(frame_count - 1, max(0, round(start_time * fps)))
        last_frame = min(frame_count - 1, max(first_frame, round(end_time * fps)))
        sample_interval_frames = max(1, round(fps * sample_interval_seconds))
        frame_numbers = list(range(first_frame, last_frame + 1, sample_interval_frames))
        if not frame_numbers:
            frame_numbers = [first_frame]

        for frame_number in frame_numbers:
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
            ok, frame = capture.read()
            if not ok:
                continue

            hsv_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            histogram = cv2.calcHist(
                [hsv_frame],
                [0, 1, 2],
                None,
                [16, 16, 8],
                [0, 180, 0, 256, 0, 256],
            )
            cv2.normalize(histogram, histogram)
            if (
                previous_histogram is not None
                and cv2.compareHist(
                    previous_histogram,
                    histogram,
                    cv2.HISTCMP_CORREL,
                )
                < 0.45
            ):
                use_wide_layout = True
            previous_histogram = histogram

            gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            detections, _, confidence_scores = (
                detector.detectMultiScale3(
                    gray_frame,
                    scaleFactor=1.1,
                    minNeighbors=5,
                    minSize=(30, 30),
                    outputRejectLevels=True,
                )
            )
            for detection, confidence in zip(detections, confidence_scores):
                if float(confidence) < FACE_CONFIDENCE_THRESHOLD:
                    continue
                x, _, face_width, face_height = detection
                face_detections.append(
                    _FaceDetection(
                        center_x=float(x) + float(face_width) / 2,
                        area=float(face_width) * float(face_height),
                        frame_number=frame_number,
                    )
                )

        if not face_detections:
            logger.info(
                "No confident faces were detected in %s; using a centered crop.",
                video_path,
            )
            use_wide_layout = False
            center_x = frame_width / 2
        else:
            clusters: list[list[_FaceDetection]] = []
            cluster_distance = frame_width * FACE_CLUSTER_DISTANCE_RATIO
            for detection in sorted(face_detections, key=lambda item: item.center_x):
                matching_cluster = next(
                    (
                        cluster
                        for cluster in clusters
                        if abs(
                            detection.center_x
                            - sum(item.center_x for item in cluster) / len(cluster)
                        )
                        <= cluster_distance
                    ),
                    None,
                )
                if matching_cluster is None:
                    clusters.append([detection])
                else:
                    matching_cluster.append(detection)
            dominant_cluster = max(
                clusters,
                key=lambda cluster: (
                    len({detection.frame_number for detection in cluster}),
                    sum(detection.area for detection in cluster) / len(cluster),
                ),
            )
            center_x = sum(
                detection.center_x for detection in dominant_cluster
            ) / len(dominant_cluster)

        target_x = center_x - crop_width / 2
        crop_x = int(max(0, min(frame_width - crop_width, target_x)))
        crop_x -= crop_x % 2
        y = min(max((frame_height - crop_height) // 2, 0), frame_height - crop_height)
        return CropAnalysis(
            crop=CropBox(x=crop_x, y=y, width=crop_width, height=crop_height),
            use_wide_layout=use_wide_layout,
        )
    finally:
        capture.release()


def find_vertical_crop(
    video_path: str | Path,
    start_time: float,
    end_time: float,
    sample_interval_seconds: float = 1.0,
) -> CropBox:
    """Return the tracked 9:16 crop for callers that do not need layout analysis."""
    return analyze_vertical_crop(
        video_path,
        start_time,
        end_time,
        sample_interval_seconds,
    ).crop
