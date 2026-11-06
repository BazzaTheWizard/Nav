from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import platform
import re
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
    "SplitView",
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
        return self._haar_faces(gray)

    def faces_with_eyes(
        self, frame: np.ndarray
    ) -> list[tuple[float, float, float, float, float, tuple[float, float], tuple[float, float]]]:
        """Faces with eye centers: (cx, cy, w, h, score, right eye, left eye). YuNet only
        (the Haar fallback doesn't find eyes): [] without it."""
        if self._yunet is None:
            return []
        height, width = frame.shape[:2]
        if self._yunet_size != (width, height):
            self._yunet.setInputSize((width, height))
            self._yunet_size = (width, height)
        _count, faces = self._yunet.detect(frame)
        if faces is None:
            return []
        return [
            (float(x + w / 2), float(y + h / 2), float(w), float(h), float(face[-1]),
             (float(face[4]), float(face[5])), (float(face[6]), float(face[7])))
            for face in faces
            for x, y, w, h in [face[:4]]
        ]

    def _haar_faces(self, gray: np.ndarray) -> list[tuple[float, float, float, float, float]]:
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
# Is anyone on camera? (cheap check before a video is clipped)
# ---------------------------------------------------------------------------
ON_CAMERA_SAMPLES = 16  # evenly spaced frames between 5% and 95% of the video
MIN_FACE_WIDTH_RATIO = 0.025  # faces narrower than this share of the frame don't count

# Screen share with a webcam overlay (slides, tutorial, gameplay + a small face cam in
# a corner): those creators are skipped ("screen_share_pip"). Measured on real
# podcasts: a close-up face box is 3-4% of the frame; a wide two-person shot's faces
# are 0.3-0.5% each but at mid-height on both sides. A webcam face is under ~1.5%,
# near a corner, and stays in the same spot. (A size limit alone, like "under 8-10%
# of the frame", would also catch every normal talking-head video.)
PIP_MAX_FACE_AREA = 0.015  # face box area / frame area
PIP_CORNER_X = 0.25  # face center within this share of the width from the left or right edge
PIP_CORNER_Y = 0.30  # ...and within this share of the height from the top or bottom edge
PIP_MIN_SHARE = 0.7  # of the sampled frames that show a face
PIP_MIN_FRAMES = 4
PIP_SAME_SPOT = 0.06  # an overlay doesn't move: within this share of the width (and height)


@dataclass(frozen=True)
class CameraCheck:
    """``share``: share of sampled frames with a face (None: couldn't tell).
    ``screen_share_pip``: the only face is a small webcam overlay in ``pip_corner``.
    ``character_share``: share of sampled frames with a face of any kind, people or
    cartoon/anime/drawn characters (core.character_faces); None when not measured."""

    share: float | None
    screen_share_pip: bool = False
    pip_corner: str = ""
    character_share: float | None = None


def pip_corner(cx: float, cy: float, area: float) -> str | None:
    """'top-right' etc. for a face small enough and close enough to a corner to be a
    webcam overlay; None otherwise. All values are shares of the frame (0-1)."""
    if area > PIP_MAX_FACE_AREA:
        return None
    side = "left" if cx <= PIP_CORNER_X else "right" if cx >= 1 - PIP_CORNER_X else None
    level = "top" if cy <= PIP_CORNER_Y else "bottom" if cy >= 1 - PIP_CORNER_Y else None
    return f"{level}-{side}" if side and level else None


def classify_camera(frames: list[list[tuple[float, float, float]]], checked: int) -> CameraCheck:
    """``frames``: each sampled frame's faces as (center x, center y, area), shares of
    the frame. A screen share with a webcam overlay: in at least PIP_MIN_SHARE of the
    frames that show a face, the faces are all in one corner, small, and in the same
    spot every time."""
    if checked < 1:
        return CameraCheck(None)
    with_face = [faces for faces in frames if faces]
    share = len(with_face) / checked
    spots: list[tuple[str, float, float]] = []
    for faces in with_face:
        corners = {pip_corner(cx, cy, area) for cx, cy, area in faces}
        if len(corners) == 1 and None not in corners:
            spots.append((corners.pop(), statistics.median(f[0] for f in faces), statistics.median(f[1] for f in faces)))
    needed = max(PIP_MIN_FRAMES, PIP_MIN_SHARE * len(with_face))
    if len(spots) < needed:
        return CameraCheck(share)
    corner = statistics.mode(spot[0] for spot in spots)
    same = [spot for spot in spots if spot[0] == corner]
    x = statistics.median(spot[1] for spot in same)
    y = statistics.median(spot[2] for spot in same)
    steady = [spot for spot in same if abs(spot[1] - x) <= PIP_SAME_SPOT and abs(spot[2] - y) <= PIP_SAME_SPOT * 16 / 9]
    if len(steady) < needed:
        return CameraCheck(share)
    return CameraCheck(share, True, corner)


def camera_check(video_path: Path, samples: int = ON_CAMERA_SAMPLES) -> CameraCheck:
    """One look at ``samples`` frames: is anyone on camera, and is it a screen share
    with a corner webcam? CameraCheck(None) when it can't tell (no detector, unreadable
    video), so a broken check never skips a creator."""
    detector = _face_detector()
    if not detector.available or samples < 1:
        return CameraCheck(None)
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return CameraCheck(None)
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        if fps <= 0 or frame_count <= 0:
            return CameraCheck(None)
        duration = frame_count / fps
        from core.character_faces import has_character_face

        frames: list[list[tuple[float, float, float]]] = []
        characters = 0  # frames with a face of any kind
        for index in range(samples):
            seconds = duration * (0.05 + 0.9 * (index + 0.5) / samples)
            capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000.0)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            height, width = frame.shape[:2]
            if width > FACE_FRAME_WIDTH:
                frame = cv2.resize(frame, (FACE_FRAME_WIDTH, max(1, round(height * FACE_FRAME_WIDTH / width))))
            height, width = frame.shape[:2]
            frames.append([
                (cx / width, cy / height, w * h / (width * height))
                for cx, cy, w, h, _score in detector.detect(frame)
                if w >= MIN_FACE_WIDTH_RATIO * width
            ])
            try:
                characters += bool(frames[-1]) or has_character_face(frame)
            except cv2.error:
                logger.warning("Character face check failed on one frame.", exc_info=True)
        if len(frames) < max(1, samples // 2):
            return CameraCheck(None)
        return dataclasses.replace(classify_camera(frames, len(frames)), character_share=characters / len(frames))
    finally:
        capture.release()


def on_camera_share(video_path: Path, samples: int = ON_CAMERA_SAMPLES) -> float | None:
    """Share of sampled frames that show a face, from 0.0 to 1.0 (None: can't tell)."""
    return camera_check(video_path, samples).share


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
MIN_SHOT_SECONDS = 0.6  # shorter: an edge sliver (held over) or a quick shot (framed only if it has a face)
FLASH_SECONDS = 0.2  # cuts closer together than this are a flash, not a shot
SAMPLE_MARGIN = 0.12  # face samples this close to a cut may show the neighbouring shot
# Inside a shot the crop is locked (no jitter, no chasing head movements). It re-frames
# only when the face sits outside the dead zone for REFRAME_HOLD seconds, with an eased pan.
DEAD_ZONE = 0.28  # share of the crop width the face may sit off-centre
REFRAME_HOLD = 2.0
REFRAME_SECONDS = 0.8
FACE_SHOT_PRESENCE = 0.4
GROUP_SHOT_PRESENCE = 0.6  # two people in the same frames for most of the shot
# Embedded content (Round 30): a chart, slide, document or screen share in the recording
# itself, maybe with small webcam tiles beside it. A 9:16 face crop (or the two-person
# split) would cut it in half, so the whole frame is shown instead, like a slide.
CONTENT_FACE_MAX = 0.11  # the biggest face is at most this share of the frame width: webcam tiles, not the subject
CONTENT_FLAT_MIN = 0.45  # at least this share of the picture outside the faces is flat colour (a slide, a chart)
CONTENT_EDGE_MIN = 0.02  # ...with text or lines on it (a blank or black screen isn't content)
CONTENT_PRESENCE = 0.6  # in this share of a shot's samples
CONTENT_CROP_FIT = 1.02  # content up to this share of the crop's width is framed on (shown big), not shown as the whole frame
SAME_X_TOLERANCE_PX = 8
# Two-speaker split view: two people side by side (a two-person shot or a remote
# call's side-by-side webcams) become two stacked 1080x960 panels, one face each.
PAIR_PRESENCE = 0.6  # both faces in the same frame, in at least this share of the shot's samples
PAIR_SIZE_RATIO = 0.45  # the smaller face is at least this share of the larger one's width
# Gradual transitions (a cross-dissolve, or a call-recording app animating from a solo
# layout to side-by-side tiles) have no single-frame jump for _cut_times to find.
DISSOLVE_SPAN = 0.3  # seconds compared on each side of a frame
DISSOLVE_MIN_DIFFERENCE = 20.0  # mean pixel change over 2 x DISSOLVE_SPAN (talking: under ~11)
PANEL_FACE_WIDTHS = 5.5  # panel width = this many face widths (head and shoulders)
MAX_PANEL_UPSCALE = 2.0  # a panel window is at least 1080 / this source pixels wide
TOP_PANEL_FACE_Y = 0.58  # face height in the top panel: room for the title above it
BOTTOM_PANEL_FACE_Y = 0.45  # bottom panel: room for the captions on the seam above it
PANEL_OUTPUT = (1080, 960)


PULL_IN = 0.05  # the hook's pull-in: 1.00x to 1.05x
PULL_IN_SECONDS = 3.2  # eased out over this long, then held until the first camera cut
PULL_IN_CENTER_Y = 0.42  # zoom toward the face (it sits a little above the middle of a crop)


def pull_in_scale(t: float, until: float) -> float:
    """The hook pull-in's zoom at clip second ``t`` (1.0 from the first cut, ``until``)."""
    if t >= until:
        return 1.0
    progress = min(1.0, max(0.0, t / PULL_IN_SECONDS))
    return 1.0 + PULL_IN * (1 - (1 - progress) ** 3)


@dataclass(frozen=True)
class Shot:
    """One camera shot, in absolute source seconds, with its crop x position.

    ``speakers``: for a two-person shot, the left and right faces as
    (center_x, center_y, face_width) in source pixels; None otherwise.
    ``moves``: re-frames inside a long shot when the person really moves (leans or walks
    out of the dead zone and stays there): (absolute start second, new x). Each is an
    eased REFRAME_SECONDS pan; head movements inside the dead zone never move the crop.
    """

    start: float
    end: float
    x: int
    has_face: bool
    group: bool
    speakers: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None
    moves: tuple[tuple[float, int], ...] = ()
    content: bool = False  # the recording's own chart, page or screen share, framed on it (Round 30)


@dataclass(frozen=True)
class SplitView:
    """Where the two stacked panels come from during a clip's two-person shots.

    ``spans`` are clip-relative; the x/y values are FFmpeg expressions in ``t``
    (clip seconds) for the top-left corner of each panel's source window.
    """

    spans: tuple[tuple[float, float], ...]
    width: int
    height: int
    top_x: str
    top_y: str
    bottom_x: str
    bottom_y: str
    # Same windows as plain numbers per span, for the title-card face check.
    windows: tuple[tuple[float, float, int, int, int, int], ...]


@dataclass(frozen=True)
class CropPlan:
    frame_width: int
    frame_height: int
    crop_width: int
    crop_height: int
    y: int
    shots: tuple[Shot, ...]
    cuts: tuple[float, ...] = ()  # every camera cut found, absolute source seconds

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
    def unsplit_group_coverage(self) -> float:
        """Wide shots of 2+ people that can't become a two-speaker split view."""
        return sum(s.end - s.start for s in self.shots if s.group and not self._splittable(s)) / self.duration

    def _room(self, shot: Shot) -> float:
        """Widest panel window this shot allows: each panel stays on its own
        speaker's side of the midpoint between them (so it never shows the other
        person or the other webcam), and is never taller than the frame."""
        if shot.speakers is None:
            return 0.0
        mid = (shot.speakers[0][0] + shot.speakers[1][0]) / 2
        return min(mid, self.frame_width - mid, self.frame_height * PANEL_OUTPUT[0] / PANEL_OUTPUT[1])

    def _splittable(self, shot: Shot) -> bool:
        """Two speakers with room for a sharp panel each (upscaled at most MAX_PANEL_UPSCALE)."""
        return shot.speakers is not None and self._room(shot) >= PANEL_OUTPUT[0] / MAX_PANEL_UPSCALE

    def panel_size(self, clip_start: float | None = None, clip_end: float | None = None) -> tuple[int, int] | None:
        """One source window size (9:8) for every split panel in the clip, or None.

        Sized to about PANEL_FACE_WIDTHS face widths, never wider than every
        split shot's room allows (see _room), and never so small that a panel is
        upscaled more than MAX_PANEL_UPSCALE.
        """
        shots = [
            shot
            for shot in self.shots
            if self._splittable(shot)
            and (clip_start is None or shot.end > clip_start)
            and (clip_end is None or shot.start < clip_end)
        ]
        if not shots:
            return None
        largest = min(self._room(shot) for shot in shots)
        wanted = statistics.median(face[2] for shot in shots for face in shot.speakers) * PANEL_FACE_WIDTHS
        width = max(PANEL_OUTPUT[0] / MAX_PANEL_UPSCALE, min(wanted, largest))
        width = int(width) // 2 * 2
        height = int(round(width * PANEL_OUTPUT[1] / PANEL_OUTPUT[0])) // 2 * 2
        return width, height

    def split_view(self, clip_start: float, clip_end: float) -> SplitView | None:
        """The two-speaker split view for this clip, or None when it has no
        two-person shots that can be split."""
        size = self.panel_size(clip_start, clip_end)
        if size is None:
            return None
        width, height = size
        windows: list[tuple[float, float, int, int, int, int]] = []
        first, last = self._visible(clip_start, clip_end)
        for shot in self.shots:
            if not self._splittable(shot) or shot.end <= first + 1e-6 or shot.start >= last - 1e-6:
                continue
            start = (clip_start if shot.start <= first + 1e-6 else shot.start) - clip_start
            end = (clip_end if shot.end >= last - 1e-6 else shot.end) - clip_start
            if end - start < MIN_SHOT_SECONDS:
                continue
            (lx, ly, _lw), (rx, ry, _rw) = shot.speakers
            mid = (lx + rx) / 2
            top = self._panel_origin(lx, ly, width, height, TOP_PANEL_FACE_Y, 0, mid)
            bottom = self._panel_origin(rx, ry, width, height, BOTTOM_PANEL_FACE_Y, mid, self.frame_width)
            if windows and start - windows[-1][1] < 0.05 and windows[-1][2:] == (*top, *bottom):
                windows[-1] = (windows[-1][0], end, *top, *bottom)
            else:
                windows.append((start, end, *top, *bottom))
        if not windows:
            return None

        def expression(index: int) -> str:
            # Piecewise by span; outside the spans the value doesn't matter.
            value = str(windows[-1][index])
            for window in reversed(windows[:-1]):
                value = f"if(lt(t,{window[1]:.3f}),{window[index]},{value})"
            return value

        return SplitView(
            spans=tuple((start, end) for start, end, *_rest in windows),
            width=width,
            height=height,
            top_x=expression(2),
            top_y=expression(3),
            bottom_x=expression(4),
            bottom_y=expression(5),
            windows=tuple(windows),
        )

    def _panel_origin(
        self, cx: float, cy: float, width: int, height: int, face_y: float, left: float, right: float
    ) -> tuple[int, int]:
        """Top-left of a panel window centred on a face, kept between ``left`` and
        ``right`` (that speaker's side) and inside the frame."""
        low, high = int(left + 1) // 2 * 2, int(right - width) // 2 * 2
        x = int(min(max(cx - width / 2, low), max(low, high))) // 2 * 2
        y = int(max(0, min(self.frame_height - height, cy - face_y * height))) // 2 * 2
        return x, y

    def holds(self, clip_start: float, clip_end: float) -> tuple[float, float]:
        """(head, tail): seconds at the clip's start and end that show a sliver of another
        camera (a cut under MIN_SHOT_SECONDS from the edge). The renderer holds the main
        shot's first frame over the head and its last frame over the tail, so a clip never
        opens or closes on a flash of the wrong shot."""
        head = max((c - clip_start for c in self.cuts if 0 < c - clip_start < MIN_SHOT_SECONDS), default=0.0)
        tail = max((clip_end - c for c in self.cuts if 0 < clip_end - c < MIN_SHOT_SECONDS), default=0.0)
        if head + tail >= clip_end - clip_start - MIN_SHOT_SECONDS:
            return 0.0, 0.0
        return round(head, 3), round(tail, 3)

    def _visible(self, clip_start: float, clip_end: float) -> tuple[float, float]:
        """The part of the clip whose own frames are shown (the holds cover the rest)."""
        head, tail = self.holds(clip_start, clip_end)
        return clip_start + head, clip_end - tail

    @property
    def cut_count(self) -> int:
        return max(0, len(self.shots) - 1)

    @property
    def crop(self) -> CropBox:
        """Single crop for callers that can't follow shots: the longest face shot."""
        face_shots = [shot for shot in self.shots if shot.has_face] or list(self.shots)
        best = max(face_shots, key=lambda shot: shot.end - shot.start)
        return CropBox(x=best.x, y=self.y, width=self.crop_width, height=self.crop_height)

    def full_frame_ranges(self, clip_start: float, clip_end: float) -> list[tuple[float, float]]:
        """Clip-relative (start, end) spans to show uncropped: shots with no face
        (slides, screen shares, b-roll) and wide shots of 2+ people. A 9:16 crop
        would cut those off; the renderer shows the whole frame there instead."""
        spans: list[tuple[float, float]] = []
        split = self.split_view(clip_start, clip_end) is not None
        first, last = self._visible(clip_start, clip_end)
        for shot in self.shots:
            if shot.has_face and not shot.group:
                continue
            if split and self._splittable(shot):
                continue  # shown as the two-speaker split view instead
            if shot.end <= first + 1e-6 or shot.start >= last - 1e-6:
                continue
            start = (clip_start if shot.start <= first + 1e-6 else shot.start) - clip_start
            end = (clip_end if shot.end >= last - 1e-6 else shot.end) - clip_start
            if end - start < MIN_SHOT_SECONDS:
                continue
            if spans and start - spans[-1][1] < 0.05:
                spans[-1] = (spans[-1][0], end)
            else:
                spans.append((start, end))
        return spans

    def x_expression(self, clip_start: float, clip_end: float) -> str:
        """FFmpeg crop x expression that switches position at each camera cut.

        ``t`` is clip-relative because the renderer seeks with ``-ss`` before
        ``-i``. Crop moves happen exactly on cuts, so viewers never see a jump inside
        a shot; the only in-shot motion is a rare eased re-frame (Shot.moves).
        """
        segments, ramps = self._x_track(clip_start, clip_end)
        if not segments:
            return str(self.crop.x)
        expression = str(segments[-1][1])
        for boundary, x in reversed(segments[:-1]):
            expression = f"if(lt(t,{boundary:.3f}),{x},{expression})"
        for begin, end, start_x, end_x in reversed(ramps):
            # smoothstep ease from start_x to end_x between begin and end
            progress = f"(t-{begin:.3f})/{end - begin:.3f}"
            eased = f"{start_x}+({end_x - start_x})*{progress}*{progress}*(3-2*{progress})"
            expression = f"if(between(t,{begin:.3f},{end:.3f}),{eased},{expression})"
        return expression

    def x_at(self, t: float, clip_start: float, clip_end: float) -> float:
        """The crop's x at clip second ``t``: the same value x_expression gives FFmpeg."""
        segments, ramps = self._x_track(clip_start, clip_end)
        for begin, end, start_x, end_x in ramps:
            if begin <= t <= end:
                progress = (t - begin) / (end - begin)
                return start_x + (end_x - start_x) * progress * progress * (3 - 2 * progress)
        for boundary, x in segments:
            if t < boundary:
                return float(x)
        return float(segments[-1][1]) if segments else float(self.crop.x)

    def base_rect_at(self, t: float, clip_start: float, clip_end: float) -> tuple[float, float, float, float] | None:
        """The source rectangle (x, y, w, h) the smart-crop clip shows at clip second ``t``,
        or None when that moment shows the whole frame or the two-speaker split instead."""
        if any(a <= t < b for a, b in self.full_frame_ranges(clip_start, clip_end)):
            return None
        split = self.split_view(clip_start, clip_end)
        if split is not None and any(a <= t < b for a, b in split.spans):
            return None
        return self.x_at(t, clip_start, clip_end), float(self.y), float(self.crop_width), float(self.crop_height)

    def pull_in_until(self, clip_start: float, clip_end: float, hook_seconds: float = 3.2) -> float:
        """How long the hook's pull-in zoom may run (clip seconds): until the first camera
        cut, when the clip opens on a plain crop of one person for the whole hook; 0
        otherwise (a split view or the whole frame at the start never zooms)."""
        duration = clip_end - clip_start
        cuts = [cut - clip_start for cut in self.cuts if 0.5 < cut - clip_start < duration]
        until = min(cuts, default=duration)
        checks = [t for t in (0.05, hook_seconds / 2, min(hook_seconds, until) - 0.05) if 0 <= t < until]
        if until < 1.0 or any(self.base_rect_at(t, clip_start, clip_end) is None for t in checks):
            return 0.0
        if any(shot.content and shot.start - 1e-6 <= clip_start + t < shot.end for shot in self.shots for t in checks):
            return 0.0  # a shared page or chart never zooms
        return until

    def shown_rect_at(self, t: float, clip_start: float, clip_end: float,
                      pull_until: float = 0.0) -> tuple[float, float, float, float] | None:
        """base_rect_at, with the hook pull-in's zoom (see pull_in_scale) applied."""
        rect = self.base_rect_at(t, clip_start, clip_end)
        if rect is None or pull_until <= 0:
            return rect
        x, y, w, h = rect
        zoom = pull_in_scale(t, pull_until)
        cx, cy = x + w / 2, y + h * PULL_IN_CENTER_Y
        return cx - (w / 2) / zoom, cy - (h * PULL_IN_CENTER_Y) / zoom, w / zoom, h / zoom

    def _x_track(self, clip_start: float, clip_end: float) -> tuple[list[tuple[float, int]], list[tuple[float, float, int, int]]]:
        """(segments: (until clip second, x)), (ramps: (start, end, from x, to x))."""
        segments: list[tuple[float, int]] = []
        ramps: list[tuple[float, float, int, int]] = []  # (start, end, from x, to x), clip-relative
        first, last = self._visible(clip_start, clip_end)
        for shot in self.shots:
            if shot.end <= first + 1e-6 or shot.start >= last - 1e-6:
                continue
            boundary = (clip_end if shot.end >= last - 1e-6 else shot.end) - clip_start
            x = shot.x
            for at, target in shot.moves:
                begin = at - clip_start
                if begin <= 0 or begin + REFRAME_SECONDS >= boundary:
                    if begin <= 0:
                        x = target
                    continue
                if segments and abs(segments[-1][1] - x) <= SAME_X_TOLERANCE_PX:
                    segments[-1] = (begin, segments[-1][1])
                else:
                    segments.append((begin, x))
                ramps.append((begin, begin + REFRAME_SECONDS, x, target))
                x = target
            if segments and abs(segments[-1][1] - x) <= SAME_X_TOLERANCE_PX:
                segments[-1] = (boundary, segments[-1][1])
            else:
                segments.append((boundary, x))
        return segments, ramps


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
        timeout=60,
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
) -> tuple[np.ndarray, np.ndarray, list[float], list[float]]:
    """Decode the clip once into tiny gray frames (cuts) and small color frames (faces),
    with each one's time (clip-relative).

    The cut frames are every frame of the source (no frame-rate conversion), with each
    frame's exact time (clip-relative, the same ``t`` the renderer sees), so a crop
    switch lands between the last frame of one shot and the first of the next.
    (Resampling a 29.97 fps source to 30 fps put switches up to half a frame late:
    the first frame of the new shot kept the old crop.)"""
    cut_w, cut_h = CUT_FRAME_SIZE
    face_w, face_h = face_size
    with tempfile.TemporaryDirectory(prefix="crop-analysis-") as temp_dir:
        face_file = Path(temp_dir) / "faces.raw"
        command = [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-v",
            "info",
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
                f"[a]showinfo,scale={cut_w}:{cut_h},format=gray[cuts];"
                # Face samples: real source frames about 1/FACE_ANALYSIS_FPS apart, each
                # with its exact time (an fps filter's frames only approximate their time,
                # which put a quick shot's sample in the shot next to it).
                f"[b]select='isnan(prev_selected_t)+gte(t-prev_selected_t,{1 / FACE_ANALYSIS_FPS - 0.001:.3f})',"
                f"showinfo,scale={face_w}:{face_h},format=bgr24[faces]"
            ),
            "-map",
            "[cuts]",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "pipe:1",
            "-map",
            "[faces]",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            str(face_file),
        ]
        # A 60 s clip decodes in seconds; a stuck FFmpeg must not stall the worker.
        result = subprocess.run(command, check=True, capture_output=True, timeout=300)
        cut_frames = np.frombuffer(result.stdout, dtype=np.uint8)
        cut_frames = cut_frames[: cut_frames.size - cut_frames.size % (cut_w * cut_h)]
        cut_frames = cut_frames.reshape(-1, cut_h, cut_w)
        face_bytes = np.fromfile(face_file, dtype=np.uint8)
        frame_size = face_w * face_h * 3
        face_frames = face_bytes[: face_bytes.size - face_bytes.size % frame_size]
        face_frames = face_frames.reshape(-1, face_h, face_w, 3)
    log = result.stderr.decode("utf-8", "replace")
    times, face_times = _frame_times(log, 0), _frame_times(log, 1)
    if len(times) != len(cut_frames):  # no timing info: the source's own steady frame rate
        rate = _log_frame_rate(log) or CUT_ANALYSIS_FPS
        times = [index / rate for index in range(len(cut_frames))]
    if len(face_times) != len(face_frames):  # the same pick the select filter made, from the frame times
        face_times = _selected_times(times)
    if len(face_times) != len(face_frames):
        face_times = [index / FACE_ANALYSIS_FPS for index in range(len(face_frames))]
    return cut_frames, face_frames, times, face_times


_SHOWINFO_FRAME = re.compile(r"n:\s*(\d+)\s+pts:\s*\S+\s+pts_time:\s*(-?[0-9.]+)")
_SHOWINFO_OWNER = re.compile(r"\[Parsed_showinfo_(\d+) @ [^\]]*\]\s*$")
_SHOWINFO_RATE = re.compile(r"\[Parsed_showinfo_\d+ @ [^\]]*\] config in time_base: \S+ frame_rate: (\d+)/(\d+)")


def _frame_times(log: str, which: int = 0) -> list[float]:
    """Each frame's time from FFmpeg's showinfo lines, in frame order. ``which``: 0 for the
    first showinfo in the graph (every frame, for cuts), 1 for the second (face samples).

    The decoder logs from its own threads, and a message of its without a line end (an
    h264 "Late SEI", say) can take the start of a showinfo line, prefix and all. Those
    frames are put back where they fit (the filter missing that frame number, between its
    neighbours' times). Before, one such line threw all the times away and the clip was
    timed as 30 fps: on a 25 fps video every cut landed 20% early (random from run to run)."""
    by_filter: dict[int, dict[int, float]] = {}
    loose: list[tuple[int, float]] = []
    for line in log.splitlines():
        for k, match in enumerate(_SHOWINFO_FRAME.finditer(line)):
            frame, time = int(match.group(1)), float(match.group(2))
            owner = _SHOWINFO_OWNER.search(line[: match.start()]) if k == 0 else None
            if owner:
                by_filter.setdefault(int(owner.group(1)), {})[frame] = time
            else:
                loose.append((frame, time))
    filters = sorted(by_filter)
    for frame, time in loose:
        for key in filters:
            got = by_filter[key]
            if frame not in got and got.get(frame - 1, -1e9) < time < got.get(frame + 1, 1e9):
                got[frame] = time
                break
    if len(filters) <= which:
        return []
    found = by_filter[filters[which]]
    return [found[index] for index in range(len(found))] if found and max(found) == len(found) - 1 else []


def _log_frame_rate(log: str) -> float | None:
    """The source's frame rate as the showinfo filters were told it (None if not found)."""
    match = _SHOWINFO_RATE.search(log)
    if not match or int(match.group(2)) == 0 or int(match.group(1)) == 0:
        return None
    return int(match.group(1)) / int(match.group(2))


def _selected_times(times: list[float]) -> list[float]:
    """The face samples' times: the frames the analysis select filter keeps (the first,
    then each one at least 1/FACE_ANALYSIS_FPS after the last one kept)."""
    gap = round(1 / FACE_ANALYSIS_FPS - 0.001, 3)
    picked: list[float] = []
    for time in times:
        if not picked or time - picked[-1] >= gap - 1e-9:
            picked.append(time)
    return picked


def _frame_at(video_path: Path, time: float, size: tuple[int, int]) -> np.ndarray | None:
    """One color frame at ``time`` (source seconds), for a shot too short for the
    2 fps face samples."""
    width, height = size
    try:
        raw = subprocess.run(
            [shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-ss", f"{time:.3f}", "-i", str(video_path),
             "-frames:v", "1", "-vf", f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
            check=True, capture_output=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if len(raw) < width * height * 3:
        return None
    return np.frombuffer(raw[: width * height * 3], dtype=np.uint8).reshape(height, width, 3)


def _cut_times(cut_frames: np.ndarray, times: list[float] | None = None) -> list[float]:
    """Times (clip-relative) where the camera cuts.

    Uses pixel difference between consecutive frames, which catches cuts between
    two angles of the same set (same colors), plus a histogram check. With the
    frames' ``times``, each cut is halfway between the last frame of the old shot
    and the first frame of the new one; without, frame index / CUT_ANALYSIS_FPS.
    """
    found: list[float] = []
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
                found.append((times[index - 1] + times[index]) / 2 if times else index / CUT_ANALYSIS_FPS)
                recent.clear()
            else:
                recent.append(difference)
                del recent[:-15]
        previous = frame
        previous_hist = hist
    return found


def _reframes(
    track: list[tuple[float, float]], x: int, crop_width: int, frame_width: int, start_time: float
) -> tuple[tuple[float, int], ...]:
    """Dead-zone re-framing for one shot. ``track``: (clip second, face centre x) samples.
    The crop stays put while the face is within DEAD_ZONE of its centre; when the face
    sits outside it for REFRAME_HOLD seconds, one eased pan re-centres it (hysteresis:
    a quick lean or a gesture never moves the camera)."""
    moves: list[tuple[float, int]] = []
    center = x + crop_width / 2
    outside: list[tuple[float, float]] = []
    for t, face_x in track:
        if abs(face_x - center) <= DEAD_ZONE * crop_width:
            outside.clear()
            continue
        outside.append((t, face_x))
        if outside[-1][0] - outside[0][0] >= REFRAME_HOLD:
            target_center = statistics.median(value for _t, value in outside)
            new_x = int(max(0, min(frame_width - crop_width, target_center - crop_width / 2))) // 2 * 2
            if abs(new_x - (center - crop_width / 2)) > SAME_X_TOLERANCE_PX:
                moves.append((start_time + outside[0][0], new_x))
                center = new_x + crop_width / 2
            outside.clear()
    return tuple(moves)


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


def _speaker_pair(
    clusters: list[list[tuple[float, ...]]],
    shot_samples: list[list[tuple[float, ...]]],
    cluster_distance: float,
    min_gap: float,
) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    """Exactly two people on screen together through the shot: (left, right) as
    (center_x, center_y, face_width), or None.

    The two-face gate: both faces in the same frame in at least PAIR_PRESENCE of the
    shot's samples (one person seen in two places at different times is never a pair),
    at least ``min_gap`` apart, and of comparable size (PAIR_SIZE_RATIO). Three or more
    steady faces -> None (the whole frame is shown instead)."""
    count = max(1, len(shot_samples))
    steady = [cluster for cluster in clusters if len(cluster) >= PAIR_PRESENCE * count]
    if len(steady) != 2:
        return None
    faces = sorted(
        (
            statistics.median(face[0] for face in cluster),
            statistics.median(face[2] for face in cluster),
            statistics.median(face[3] for face in cluster),
        )
        for cluster in steady
    )
    (left_x, _ly, left_w), (right_x, _ry, right_w) = faces
    if right_x - left_x < min_gap or min(left_w, right_w) < PAIR_SIZE_RATIO * max(left_w, right_w):
        return None
    together = sum(
        1
        for faces_now in shot_samples
        if any(abs(face[0] - left_x) <= cluster_distance for face in faces_now)
        and any(abs(face[0] - right_x) <= cluster_distance for face in faces_now)
    )
    if together < PAIR_PRESENCE * count:
        return None
    return faces[0], faces[1]


def _dissolve_times(
    cut_frames: np.ndarray,
    times: list[float],
    samples: list[tuple[float, list[tuple[float, ...]]]],
    cluster_distance: float,
    cuts: list[float],
) -> list[float]:
    """Gradual transitions _cut_times can't see (clip-relative): where the faces on
    screen change (one face -> two, or a different place) and stay changed, and the
    picture changes by DISSOLVE_MIN_DIFFERENCE across DISSOLVE_SPAN on each side. Each
    is placed at the dissolve's middle (its biggest change). A face missed in one sample
    or a person looking away is never a transition: both need a big picture change."""
    if len(samples) < 4 or len(cut_frames) < 3 or len(times) != len(cut_frames):
        return []
    fps = (len(times) - 1) / max(times[-1] - times[0], 1e-6)
    span = max(2, round(DISSOLVE_SPAN * fps))

    def same(first: list[tuple[float, ...]], second: list[tuple[float, ...]]) -> bool:
        if len(first) != len(second):
            return False
        return all(abs(a[0] - b[0]) <= cluster_distance for a, b in zip(sorted(first), sorted(second)))

    runs: list[list[int]] = []  # [first sample, last sample] with the same faces
    for index, (_time, faces) in enumerate(samples):
        if runs and same(samples[runs[-1][1]][1], faces):
            runs[-1][1] = index
        else:
            runs.append([index, index])
    steady: list[list[int]] = []  # one-sample runs are detector noise
    for run in runs:
        if run[1] == run[0]:
            continue
        if steady and same(samples[steady[-1][1]][1], samples[run[0]][1]):
            steady[-1][1] = run[1]
        else:
            steady.append(list(run))

    found: list[float] = []
    for before, after in zip(steady, steady[1:]):
        t0, t1 = samples[before[1]][0] - 0.25, samples[after[0]][0] + 0.25
        if any(t0 - 0.05 <= cut <= t1 + 0.05 for cut in cuts):
            continue  # a hard cut already explains it
        best, best_time = 0.0, None
        for index in range(span, len(cut_frames) - span):
            if t0 <= times[index] <= t1:
                change = float(np.mean(cv2.absdiff(cut_frames[index - span], cut_frames[index + span])))
                if change > best:
                    best, best_time = change, times[index]
        if best_time is not None and best >= DISSOLVE_MIN_DIFFERENCE:
            found.append(best_time)
    return found


def _content_like(frame: np.ndarray, faces: list[tuple[float, float, float, float]], scale: float,
                  frame_width: int) -> bool:
    """The sample frame shows content of its own (a chart, slide, document or screen
    share), not a person: no face bigger than a webcam tile, and the picture outside the
    faces is mostly flat colour with text or lines on it. Measured on a screen-share
    podcast: tiles at 9-10% of the width over a page 50-74% flat; talking heads 12-20%,
    with 26-44% flat."""
    if any(face[3] > CONTENT_FACE_MAX * frame_width for face in faces):
        return False
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    rows, cols = gray.shape
    keep = np.ones(gray.shape, bool)
    for cx, _area, cy, width in faces:  # the tiles' faces (and hair and shoulders) don't count
        x, y, half = cx / scale, cy / scale, width / scale
        keep[max(0, int(y - 1.1 * half)):min(rows, int(y + 1.4 * half)), max(0, int(x - half)):min(cols, int(x + half))] = False
    if keep.mean() < 0.3:
        return False
    values = gray.astype(np.float32)
    mean = cv2.blur(values, (5, 5))
    spread = np.sqrt(np.maximum(0.0, cv2.blur(values * values, (5, 5)) - mean * mean))
    flat = float((spread < 1.5)[keep].mean())
    edges = float((cv2.Canny(gray, 80, 200) > 0)[keep].mean())
    return flat >= CONTENT_FLAT_MIN and edges >= CONTENT_EDGE_MIN


def _content_box(frame: np.ndarray, faces: list[tuple[float, float, float, float]],
                 scale: float) -> tuple[float, float] | None:
    """The shared content's centre and width (source pixels) on a call layout: the
    biggest block that stands out from the call's plain background and holds no face
    (the webcam tiles do). None when there's no plain background around it (a chart
    filling the frame) or nothing stands out."""
    rows, cols = frame.shape[:2]
    border = np.concatenate([frame[:4].reshape(-1, 3), frame[-4:].reshape(-1, 3),
                             frame[:, :4].reshape(-1, 3), frame[:, -4:].reshape(-1, 3)]).astype(int)
    background = np.median(border, axis=0)
    if float(np.mean(np.abs(border - background).max(axis=1) < 14)) < 0.8:
        return None  # no plain call background around the content
    standing = (np.abs(frame.astype(int) - background).max(axis=2) > 18).astype(np.uint8)
    standing = cv2.morphologyEx(standing, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    count, _labels, stats, _centres = cv2.connectedComponentsWithStats(standing, 8)
    best: tuple[int, float, float] | None = None
    for label in range(1, count):
        x, y, w, h, area = stats[label]
        if area < 0.02 * rows * cols:
            continue
        if any(x <= cx / scale <= x + w and y <= cy / scale <= y + h for cx, _a, cy, _w in faces):
            continue  # a webcam tile
        if best is None or area > best[0]:
            best = (int(area), (x + w / 2) * scale, w * scale)
    return (best[1], best[2]) if best else None


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
    cut_frames, face_frames, frame_times, face_times = _decode_analysis_frames(
        source, start_time, duration, (FACE_FRAME_WIDTH, face_height)
    )

    # Shot boundaries (clip-relative). A cut within MIN_SHOT_SECONDS of the clip's start
    # or end is a sliver of another camera: the renderer holds the main shot's first (or
    # last) frame over it instead (CropPlan.holds), so it isn't a boundary here.
    # Real quick cuts inside the clip stay (each gets its own framing below); only
    # flashes shorter than FLASH_SECONDS are ignored.
    detector = _face_detector()
    scale = frame_width / FACE_FRAME_WIDTH
    frame_area = float(FACE_FRAME_WIDTH * face_height)

    def faces_in(frame: np.ndarray) -> list[tuple[float, float, float, float]]:
        """(center x, area, center y, width) in source pixels. A lone small face in a
        corner is a webcam overlay on a screen share: it never steers the crop (that shot
        shows the whole frame instead, like a slide). Two or more faces are people: a
        wide shot can frame two speakers high up near the edges."""
        found = detector.detect(frame) if detector.available else []
        return [
            (cx * scale, w * h * scale * scale, cy * scale, w * scale)
            for cx, cy, w, h, _s in found
            if len(found) > 1 or pip_corner(cx / FACE_FRAME_WIDTH, cy / face_height, w * h / frame_area) is None
        ]

    samples = [(time, faces_in(frame)) for time, frame in zip(face_times, face_frames)]
    content = [_content_like(frame, faces, scale, frame_width) for frame, (_t, faces) in zip(face_frames, samples)]
    boxes = [_content_box(frame, faces, scale) if flag else None
             for frame, (_t, faces), flag in zip(face_frames, samples, content)]
    cluster_distance = frame_width * FACE_CLUSTER_DISTANCE_RATIO

    cuts = _cut_times(cut_frames, frame_times)
    cuts = sorted(cuts + _dissolve_times(cut_frames, frame_times, samples, cluster_distance, cuts))
    head = max((cut for cut in cuts if cut < MIN_SHOT_SECONDS), default=0.0)
    tail = max((duration - cut for cut in cuts if duration - cut < MIN_SHOT_SECONDS), default=0.0)
    boundaries = [0.0]
    for cut in cuts:
        if head < cut < duration - tail and cut - boundaries[-1] >= FLASH_SECONDS and duration - tail - cut >= FLASH_SECONDS:
            boundaries.append(cut)
    boundaries.append(duration)

    raw_shots: list[tuple[float, float, int | None, bool, tuple | None, tuple]] = []
    for index, (shot_start, shot_end) in enumerate(zip(boundaries, boundaries[1:])):
        # Only samples well inside the shot (2 fps samples near a cut can show the
        # neighbouring shot); the edge slivers aren't this shot's camera.
        inner_start = max(shot_start, head if index == 0 else 0.0) + SAMPLE_MARGIN
        inner_end = min(shot_end, duration - tail if shot_end >= duration else shot_end) - SAMPLE_MARGIN
        shot_samples = [faces for t, faces in samples if inner_start <= t <= inner_end]
        shot_content = [flag for (t, _faces), flag in zip(samples, content) if inner_start <= t <= inner_end]
        embedded = bool(shot_content) and sum(shot_content) >= CONTENT_PRESENCE * len(shot_content)
        # Content narrow enough for the 9:16 crop (a post, a phone screen, a tall chart) is
        # framed on, so it's shown big; wider content is shown as the whole frame.
        content_x: int | None = None
        if embedded:
            spans = [box for (t, _faces), box in zip(samples, boxes) if inner_start <= t <= inner_end and box]
            if spans and len(spans) >= 0.5 * len(shot_content) and max(w for _c, w in spans) <= crop_width * CONTENT_CROP_FIT:
                middle = statistics.median(c for c, _w in spans)
                content_x = int(max(0, min(frame_width - crop_width, middle - crop_width / 2))) // 2 * 2
        if not shot_samples:  # a short shot: look at its middle frame
            middle = _frame_at(source, start_time + (inner_start + inner_end) / 2, (FACE_FRAME_WIDTH, face_height))
            shot_samples = [faces_in(middle)] if middle is not None else []
        detections = [] if embedded else [face for faces in shot_samples for face in faces]
        x: int | None = None
        has_face = False
        group = False
        speakers = None
        if detections:
            clusters = _cluster(detections, cluster_distance)
            dominant = max(clusters, key=lambda c: (len(c), sum(face[1] for face in c) / len(c)))
            has_face = len(dominant) >= FACE_SHOT_PRESENCE * len(shot_samples)
            face_center = statistics.median(face[0] for face in dominant)
            x = int(max(0, min(frame_width - crop_width, face_center - crop_width / 2))) // 2 * 2
            spread_samples = sum(
                1
                for faces in shot_samples
                if len(faces) >= 2
                and max(face[0] for face in faces) - min(face[0] for face in faces) > crop_width * 0.9
            )
            group = spread_samples >= GROUP_SHOT_PRESENCE * len(shot_samples)
            if group:
                speakers = _speaker_pair(clusters, shot_samples, cluster_distance, crop_width * 0.9)
        if raw_shots and shot_end - shot_start < MIN_SHOT_SECONDS and not has_face:
            # A quick shot with no face to frame: part of the shot before it.
            previous = raw_shots[-1]
            raw_shots[-1] = (previous[0], shot_end, *previous[2:])
            continue
        moves: tuple[tuple[float, int], ...] = ()
        if has_face and not group and x is not None:
            track = [
                (t, min(faces, key=lambda face: abs(face[0] - face_center))[0])
                for t, faces in samples
                if inner_start <= t <= inner_end and faces
            ]
            moves = _reframes(track, x, crop_width, frame_width, start_time)
        if content_x is not None:
            x, has_face, group, speakers, moves = content_x, True, False, None, ()
        raw_shots.append((shot_start, shot_end, x if has_face else None, group, speakers, moves, content_x is not None))

    # Shots without a face reuse the nearest face shot's position (or the center).
    shots: list[Shot] = []
    known = [(s, x) for s, _e, x, _g, _p, _m, _c in raw_shots if x is not None]
    for shot_start, shot_end, x, group, speakers, moves, framed in raw_shots:
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
                speakers=speakers,
                moves=moves if has_face else (),
                content=framed,
            )
        )
    return CropPlan(
        frame_width=frame_width,
        frame_height=frame_height,
        crop_width=crop_width,
        crop_height=crop_height,
        y=y,
        shots=tuple(shots),
        cuts=tuple(start_time + cut for cut in cuts),
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
