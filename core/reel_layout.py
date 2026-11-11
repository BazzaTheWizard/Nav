"""Round 43: full-screen reels for slide talks (no horizontal strip, ever).

The problem (a prospect's own words: "I don't like how it's still horizontal. I think
they'd look better as full screen videos as a reel"): a creator who talks over slides,
with their webcam in a circle in the corner and a few full-camera shots in between. A
9:16 crop of a slide cuts its text off, so those shots went out as the whole 16:9 frame,
a 1080 x 608 strip over a blurred copy of itself, and when slides were most of the clip
the whole clip fell back to that blurred-background layout, the camera shots included.

Now every shot fills the 9:16 frame:
- camera shots: the face-tracked full-screen crop (smart_crop), as before;
- slide + webcam shots ("slide_cam"): the slide card, enlarged to the frame's width, at
  the top; the webcam, enlarged and re-cut as a clean circle with a white ring, below the
  captions; on the slide's own background colour. Composed, not letterboxed;
- text cards with no webcam ("text"): the text block enlarged and centred over its own
  background colour.
Wide shots of two or more people keep the whole frame (that's a different fix: cropping
to whoever is talking).

auto now picks this (smart_crop + reel) instead of the blurred-background layout when
the clip has real camera shots and its other shots are slides. REEL_LAYOUT=0 turns it off.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

W, H = 1080, 1920
SLIDE_BOX = (1040, 860)  # the slide fits inside this (output px)
SLIDE_ZONE = (190, 1090)  # the slide is centred in here: under the apps' top bar, above the captions
CAM_SIZE = 460  # the webcam circle's diameter
CAM_RING = 10  # its white ring
CAM_TOP = 1290  # under the captions (they sit at about 1130-1260)
TEXT_BOX = (1040, 760)
TEXT_CENTER_Y = 640  # the middle of SLIDE_ZONE
BRIGHT = 236  # a slide card's surface (grey level)
INK_DELTA = 40  # text: this far from the background colour
SAMPLES_PER_SPAN = 3


def enabled() -> bool:
    return os.getenv("REEL_LAYOUT", "1").strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int


@dataclass
class ReelPlan:
    """How each non-camera span of a clip is composed (clip seconds)."""

    frame: tuple[int, int]
    background: str  # "#RRGGBB": the slides' own background colour
    slide: Box | None = None  # the slide card (minus the webcam), for slide_cam spans
    camera: Box | None = None  # the webcam window
    text: Box | None = None  # the text block, for text spans (the widest; see texts)
    texts: list[tuple[tuple[float, float], Box]] = field(default_factory=list)  # each text card's own block
    slide_cam: list[tuple[float, float]] = field(default_factory=list)
    text_spans: list[tuple[float, float]] = field(default_factory=list)
    kept: list[tuple[float, float]] = field(default_factory=list)  # wide group shots: the whole frame, as before
    card_colour: str = "#FFFFFF"  # the slide card's surface: the webcam is painted out of the slide with it

    @property
    def composed(self) -> bool:
        return bool(self.slide_cam or self.text_spans)


def _even(v: float) -> int:
    return int(round(v / 2)) * 2


def _frames(source: Path, times: list[float], size: tuple[int, int]) -> list[np.ndarray]:
    out = []
    for t in times:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(source), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=60).stdout
        if len(raw) == size[0] * size[1] * 3:
            out.append(np.frombuffer(raw, np.uint8).reshape(size[1], size[0], 3))
    return out


def _background(frames: list[np.ndarray]) -> np.ndarray:
    border = np.concatenate([np.concatenate([f[:12].reshape(-1, 3), f[-12:].reshape(-1, 3), f[:, :12].reshape(-1, 3),
                                             f[:, -12:].reshape(-1, 3)]) for f in frames])
    return np.median(border, axis=0)


def _card(frame: np.ndarray) -> Box | None:
    """The slide card: the biggest bright, nearly white rectangle (None for a full-bleed page)."""
    gray = frame.mean(axis=2)
    bright = gray >= BRIGHT
    rows, cols = np.where(bright.mean(axis=1) > 0.5)[0], np.where(bright.mean(axis=0) > 0.4)[0]
    if len(rows) < 50 or len(cols) < 50:
        return None
    y0, y1, x0, x1 = rows.min(), rows.max(), cols.min(), cols.max()
    h, w = frame.shape[:2]
    if (x1 - x0) > 0.97 * w and (y1 - y0) > 0.95 * h:
        return None  # the whole frame is the page: a text card
    return Box(int(x0), int(y0), int(x1 - x0), int(y1 - y0))


def _ink(frame: np.ndarray, background: np.ndarray, ignore: Box | None = None) -> Box | None:
    """The bounding box of everything that isn't background (text, icons), with a margin."""
    diff = np.abs(frame.astype(int) - background.astype(int)).max(axis=2) > INK_DELTA
    if ignore is not None:
        diff[ignore.y:ignore.y + ignore.h, ignore.x:ignore.x + ignore.w] = False
    # a row or column counts with 3+ text pixels: lone pixels (grid crossings, noise) don't stretch the box
    rows, cols = np.where(diff.sum(axis=1) >= 3)[0], np.where(diff.sum(axis=0) >= 3)[0]
    if len(rows) < 4 or len(cols) < 4:
        return None
    h, w = frame.shape[:2]
    m = int(0.03 * w)
    x0, x1 = max(0, cols.min() - m), min(w, cols.max() + m)
    y0, y1 = max(0, rows.min() - m), min(h, rows.max() + m)
    return Box(int(x0), int(y0), int(x1 - x0), int(y1 - y0))


def _faces(frame: np.ndarray) -> list[tuple[float, float, float, float]]:
    from core.face_tracker import _face_detector

    detector = _face_detector()
    if detector is None or not getattr(detector, "available", True):
        return []
    import cv2

    return [tuple(f[:4]) for f in detector.detect(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))]


def analyze(source: Path, clip_start: float, spans: list[tuple[float, float]], frame: tuple[int, int],
            camera: Any | None = None) -> ReelPlan | None:
    """Classify each full-frame span (clip seconds) as slide_cam, text or kept (people),
    and measure the slide card, the webcam window and the background. ``camera``: the
    webcam window (core.webinar's CropBox) if already found. None when no span can be
    composed."""
    width, height = frame
    found_cam = Box(camera.x, camera.y, camera.width, camera.height) if camera is not None else None
    plan = ReelPlan(frame=frame, background="#FFFFFF")
    cards: list[Box] = []
    inks: list[Box] = []
    sampled: list[np.ndarray] = []
    circles: list[Box] = []
    finals: list[np.ndarray] = []  # each slide span's last sample: all its bullets are in
    for a, b in spans:
        times = [clip_start + a + (b - a) * (k + 1) / (SAMPLES_PER_SPAN + 1) for k in range(SAMPLES_PER_SPAN)]
        frames = _frames(source, times, frame)
        if not frames:
            plan.kept.append((a, b))
            continue
        sampled += frames
        people = [len([f for f in _faces(fr) if f[2] > 0.06 * width]) for fr in frames]
        if max(people, default=0) >= 2:
            plan.kept.append((a, b))  # a wide shot of people: not a slide
            continue
        card = _card(frames[len(frames) // 2])
        circle = _circle(frames[-1], found_cam)
        if circle is not None:
            circles.append(circle)
        if card is not None:  # the span's very end: every bullet of the slide is in
            end_frame = _frames(source, [clip_start + max(a, b - 0.4)], frame)
            finals.append(end_frame[0] if end_frame else frames[-1])
        if card is not None and (found_cam is not None or circle is not None) and _overlaps(card, circle or found_cam):
            plan.slide_cam.append((a, b))
            cards.append(card)
        elif card is None:
            plan.text_spans.append((a, b))
        else:
            (plan.slide_cam if (found_cam or circle) is not None else plan.text_spans).append((a, b))
            cards.append(card)
    if not sampled:
        return None
    background = _background(sampled)
    plan.background = "#{:02X}{:02X}{:02X}".format(*(int(c) for c in background))
    if circles:
        found_cam = Box(*(int(np.median([getattr(c, k) for c in circles])) for k in ("x", "y", "w", "h")))
    if cards:
        med = Box(*(int(np.median([getattr(c, k) for c in cards])) for k in ("x", "y", "w", "h")))
        # Crop the card to where its text actually goes (every bullet in, by the span's end), so
        # the words are as big as they can be; never under 45 % of the card's height.
        card_bg = np.median(np.concatenate([f[med.y:med.y + med.h, med.x:med.x + med.w].reshape(-1, 3) for f in finals[:4]]),
                            axis=0) if finals else np.array([255, 255, 255])
        right, bottom = med.x + med.w // 3, med.y + int(0.45 * med.h)
        for f in finals:
            region = f[med.y:med.y + med.h, med.x:med.x + med.w]
            pad = 0 if found_cam is None else max(16, found_cam.w // 6)  # its ring and drop shadow too
            local = None if found_cam is None else Box(max(0, found_cam.x - med.x - pad), max(0, found_cam.y - med.y - pad),
                                                       found_cam.w + 2 * pad, found_cam.h + 2 * pad)
            ink = _ink(region, card_bg, local)
            if ink is not None:
                right, bottom = max(right, med.x + ink.x + ink.w), max(bottom, med.y + ink.y + ink.h)
        right, bottom = min(med.x + med.w, right + 24), min(med.y + med.h, bottom + 24)
        plan.slide = Box(_even(med.x), _even(med.y), _even(right - med.x), _even(bottom - med.y))
        plan.card_colour = "#{:02X}{:02X}{:02X}".format(*(int(c) for c in card_bg))
    plan.camera = found_cam
    for a, b in plan.text_spans:
        # its middle and its end (a second line often types in late), each card on its own
        frames = _frames(source, [clip_start + (a + b) / 2, clip_start + max(a, b - 0.4)], frame)
        # a sample caught in a dissolve to the next shot differs from the background nearly everywhere: skip it
        boxes = [x for x in (_ink(fr, background, found_cam) for fr in frames)
                 if x is not None and x.w * x.h < 0.45 * width * height]
        if len(boxes) == 2 and (boxes[1].w > 1.2 * boxes[0].w + 40 or boxes[1].x < boxes[0].x - 0.08 * width):
            boxes = boxes[:1]  # the end sample caught the next shot coming in: the middle one is the card
        if boxes:
            box = Box(min(x.x for x in boxes), min(x.y for x in boxes), max(x.x + x.w for x in boxes) - min(x.x for x in boxes),
                      max(x.y + x.h for x in boxes) - min(x.y for x in boxes))
            box = Box(_even(box.x), _even(box.y), _even(box.w), _even(box.h))
            inks.append(box)
            plan.texts.append(((a, b), box))
    if inks:
        plan.text = Box(_even(min(i.x for i in inks)), _even(min(i.y for i in inks)),
                        _even(max(i.x + i.w for i in inks) - min(i.x for i in inks)),
                        _even(max(i.y + i.h for i in inks) - min(i.y for i in inks)))
    elif plan.text_spans:
        plan.kept += plan.text_spans
        plan.text_spans = []
    if plan.slide is None and plan.slide_cam:
        plan.kept += plan.slide_cam
        plan.slide_cam = []
    if plan.slide_cam and plan.camera is None:
        plan.text_spans += plan.slide_cam
        plan.slide_cam = []
    return plan if plan.composed else None


def _circle(frame: np.ndarray, near: Box | None = None) -> Box | None:
    """The webcam circle itself (a round picture-in-picture with a ring): its bounding box,
    found with a Hough transform and kept only if a face is inside it. More exact than the
    motion window around the face (which can take in a corner of the slide)."""
    import cv2

    h, w = frame.shape[:2]
    gray = cv2.medianBlur(cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY), 5)
    found = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, dp=1.2, minDist=h * 0.2, param1=120, param2=45,
                             minRadius=int(h * 0.08), maxRadius=int(h * 0.3))
    if found is None:
        return None
    faces = _faces(frame)
    best = None
    for cx, cy, r in found[0]:
        if not any((fx + fw / 2 - cx) ** 2 + (fy + fh / 2 - cy) ** 2 < (0.8 * r) ** 2 for fx, fy, fw, fh in faces):
            continue
        dist = 0.0 if near is None else abs(cx - (near.x + near.w / 2)) + abs(cy - (near.y + near.h / 2))
        if best is None or dist < best[0]:
            best = (dist, cx, cy, r)
    if best is None:
        return None
    _d, cx, cy, r = best
    r = r - max(2.0, 0.01 * h)  # inside its ring
    return Box(_even(max(0, cx - r)), _even(max(0, cy - r)), _even(min(w, 2 * r)), _even(min(h, 2 * r)))


def _overlaps(a: Box, b: Box) -> bool:
    return a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h


def _fit(box: Box, limit: tuple[int, int]) -> tuple[int, int]:
    scale = min(limit[0] / box.w, limit[1] / box.h)
    return _even(box.w * scale), _even(box.h * scale)


def _paint_out(box: Box, colour: str) -> str:
    """The webcam circle (and its ring) covered with the card's colour, so the enlarged slide
    doesn't carry a second, cut-off copy of the speaker."""
    m = max(8, box.w // 12)
    return (f"drawbox=x={box.x - m}:y={box.y - m}:w={box.w + 2 * m}:h={box.h + 2 * m}:"
            f"color=0x{colour.lstrip('#')}:t=fill,")


def layers(plan: ReelPlan, sharpen: str = "") -> tuple[list[str], str, list[tuple[str, list[tuple[float, float]]]]]:
    """FFmpeg graph pieces for render_clip's smart_crop layers: (the source labels it needs
    split off [0:v], the graph, [(layer label, clip-second spans)])."""
    colour = "0x" + plan.background.lstrip("#")
    sources: list[str] = []
    parts: list[str] = []
    out: list[tuple[str, list[tuple[float, float]]]] = []
    if plan.slide_cam and plan.slide and plan.camera:
        s, c = plan.slide, plan.camera
        sw, sh = _fit(s, SLIDE_BOX)
        side = _even(max(c.w, c.h))
        cx, cy = c.x + c.w / 2, c.y + c.h / 2
        fx, fy = plan.frame
        cam_x = _even(max(0, min(fx - side, cx - side / 2)))
        cam_y = _even(max(0, min(fy - side, cy - side / 2)))
        d = CAM_SIZE + 2 * CAM_RING
        r_in, r_out = CAM_SIZE / 2, d / 2
        sources += ["[rc_bg]", "[rc_slide]", "[rc_cam]"]
        parts.append(
            f"[rc_bg]scale={W}:{H},drawbox=x=0:y=0:w=iw:h=ih:color={colour}:t=fill[rc_canvas];"
            f"[rc_slide]{_paint_out(c, plan.card_colour)}crop={s.w}:{s.h}:{s.x}:{s.y},scale={sw}:{sh}:flags=lanczos{sharpen}[rc_s];"
            f"[rc_cam]crop={side}:{side}:{cam_x}:{cam_y},scale={CAM_SIZE}:{CAM_SIZE}:flags=lanczos{sharpen},"
            f"pad={d}:{d}:{CAM_RING}:{CAM_RING}:color=white,format=yuva444p,"
            f"geq=lum='if(gt(hypot(X-{r_out}\\,Y-{r_out})\\,{r_in})\\,235\\,lum(X\\,Y))':"
            f"cb='if(gt(hypot(X-{r_out}\\,Y-{r_out})\\,{r_in})\\,128\\,cb(X\\,Y))':"
            f"cr='if(gt(hypot(X-{r_out}\\,Y-{r_out})\\,{r_in})\\,128\\,cr(X\\,Y))':"
            f"a='if(lte(hypot(X-{r_out}\\,Y-{r_out})\\,{r_out})\\,255\\,0)'[rc_c];"
            f"[rc_canvas][rc_s]overlay=x=(W-w)/2:y={_even((SLIDE_ZONE[0] + SLIDE_ZONE[1] - sh) / 2)}[rc_1];"
            f"[rc_1][rc_c]overlay=x=(W-w)/2:y={CAM_TOP},format=yuv420p[reel_cam];"
        )
        out.append(("[reel_cam]", list(plan.slide_cam)))
    cards = plan.texts or ([(span, plan.text) for span in plan.text_spans] if plan.text else [])
    for k, (span, t) in enumerate(cards):  # each text card enlarged on its own (a short line gets big)
        tw, th = _fit(t, TEXT_BOX)
        sources += [f"[rt_bg{k}]", f"[rt_text{k}]"]
        parts.append(
            f"[rt_bg{k}]scale={W}:{H},drawbox=x=0:y=0:w=iw:h=ih:color={colour}:t=fill[rt_canvas{k}];"
            f"[rt_text{k}]crop={t.w}:{t.h}:{t.x}:{t.y},scale={tw}:{th}:flags=lanczos{sharpen}[rt_t{k}];"
            f"[rt_canvas{k}][rt_t{k}]overlay=x=(W-w)/2:y={_even(TEXT_CENTER_Y - th / 2)},format=yuv420p[reel_text{k}];"
        )
        out.append((f"[reel_text{k}]", [span]))
    return sources, "".join(parts), out
