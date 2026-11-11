"""Round 27: putting the animated graphics on the clip.

- overlay graphics (a number or range, wireframe boxes): the speaker stays full-screen;
  the graphic's frames go over the video, and the video behind each box is blurred
  (the frosted-glass look) while the box is up.
- split charts (Round 39: 54/46): the speaker keeps the top 54% of the frame (y 0-1040,
  never under half), face-centred: the viewport follows the full-screen crop's centre
  (``crop_at``), so the face stays big with natural headroom. A wide shot with no single
  face to follow (two people, the blurred-background layouts) shows the whole picture
  across the viewport over its own blurred copy. The compact card sits right under it,
  bottom-anchored (core.cards). Getting there is one camera move, never a cross-fade (two
  sizes of the same face at once): the picture zooms out into the viewport while the dark
  panel slides up from the bottom edge; leaving, the reverse. Those frames are drawn here
  with sub-pixel resampling (no FFmpeg rounding wobble); the rest of the split view is FFmpeg.
- While a graphic is up, the bold word-by-word captions give way to calm sentence-case
  ones: under a split chart, one bold 50 px line on the seam (the bottom of the speaker's
  viewport, just above the card); over the speaker, one 44 px line near the bottom.
- Leaving: every graphic slides down and fades over its last 0.4 s: no snap back.

render_with_cards() checks every card (core.card_checks), renders the ones that pass
(core.cards), asks for a base clip without captions during the graphics, then composes.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from core.cards import CANVAS, EXIT, FPS, NAME_CARD_SIZE, SPLIT_TOP, STAT_PILL_SIZE, STAT_PILL_TOP, Frames, parse_place
from core.edl import MODES, ClipEdit
from core.subtitle_engine import CAPTION_MARGIN_V
from core.video_renderer import _video_encoder_args

FRAME_W, FRAME_H = 1080, 1920
FONTS = Path(__file__).resolve().parents[1] / "assets" / "fonts"
MOVE = 0.4  # seconds (10 frames at 25 fps, 12 at 30): the move into and out of the split view
CAPTION_BOTTOM = 1440  # calm captions (over-the-speaker graphics) sit with their bottom here
CAPTION_CHARS = 30
CALM_SIZE = 44  # px: the calm captions over the speaker (Round 39: was 40)
SPLIT_CAPTION_SIZE = 50  # px, bold: the captions under a split chart (Round 39: on the seam, never tiny)
SEAM_CAPTION_BOTTOM = SPLIT_TOP - 24  # their bottom: just above the card, over the speaker's chest
SPLIT_CAPTION_CHARS = 28  # one line at 50 px
BLUR = "boxblur=15:2"  # behind glass pills: two 31-px box passes, a Gaussian of about 12.6 px (CSS blur(12px), Round 31)


def _hex(color: str) -> str:
    return "0x" + color.lstrip("#")


def windows_of(edit: ClipEdit) -> list[tuple[float, float]]:
    """Clip-relative (start, end) of each card."""
    return [(card.start - edit.start, card.end - edit.start) for card in edit.cards]


def _during(word: Any, card: Any) -> bool:
    """The word is said (even partly) while the card is up."""
    return float(word.start) < card.end and float(word.end) > card.start


# Where the bold captions sit (output px): full screen, and on a two-person split's seam.
BOLD_CAPTIONS = (FRAME_H - CAPTION_MARGIN_V - 110, FRAME_H - CAPTION_MARGIN_V + 20)
SEAM_CAPTIONS = (FRAME_H // 2 - 70, FRAME_H // 2 + 70)
CAPTION_CLEARANCE = 24


def _box_rows(card: Any) -> tuple[float, float, float] | None:
    """(top, bottom, scale) of a name card or micro-stat (output rows); None for others."""
    if card.template == "speaker_title":
        _x, top, scale, _align = parse_place(card.place)
        return top, top + NAME_CARD_SIZE[1] * scale, scale
    if card.template == "stat_pill":
        top = parse_place(card.place)[1] if card.place else STAT_PILL_TOP
        return top, top + STAT_PILL_SIZE[1], 1.0
    from core.proof_cards import PILL_H, PILL_TEMPLATES, pill_top

    if card.template in PILL_TEMPLATES:  # Round 41: under the captions, in the 1320-1580 band
        return float(pill_top(card.template)), float(pill_top(card.template) + PILL_H[card.template]), 1.0
    return None


def keeps_bold_captions(card: Any, edit: ClipEdit, seams: list[tuple[float, float]] | None = None) -> bool:
    """A name card or micro-stat placed clear of the bold captions (above the head on a
    close-up, in a corner, or in the guest's own panel) leaves them as they are: the
    captions don't switch to the small ones and back for it. A data graphic, or a name
    card in the standard lower spot, still gets the small captions."""
    box = _box_rows(card)
    if box is None or (card.template == "speaker_title" and not card.place):
        return False
    top, bottom, _scale = box
    a, b = card.start - edit.start, card.end - edit.start
    spans = seams or []
    bands = ([SEAM_CAPTIONS] if any(s < b and a < e for s, e in spans) else []) \
        + ([] if any(s <= a and b <= e for s, e in spans) else [BOLD_CAPTIONS])
    return all(bottom + CAPTION_CLEARANCE <= low or top >= high + CAPTION_CLEARANCE for low, high in bands)


CALM_CAPTIONS = (CAPTION_BOTTOM - 74, CAPTION_BOTTOM + 12)  # one small line, box included
SPLIT_CAPTIONS = (SEAM_CAPTION_BOTTOM - 80, SEAM_CAPTION_BOTTOM + 10)  # one bold line on the seam, box included
NAME_CARD_TOP_LIMIT, NAME_CARD_BOTTOM_LIMIT = 130, 1700  # the app's own bars cover the rest


def _caption_bands(card: Any, edit: ClipEdit, seams: list[tuple[float, float]] | None) -> list[tuple[float, float]]:
    """Where captions are drawn (output rows) while ``card`` is up."""
    a, b = card.start - edit.start, card.end - edit.start
    spans = seams or []
    in_split = any(s < b and a < e for s, e in spans)
    on_full = not any(s <= a and b <= e for s, e in spans)
    if keeps_bold_captions(card, edit, seams):
        return ([SEAM_CAPTIONS] if in_split else []) + ([BOLD_CAPTIONS] if on_full else [])
    return ([SEAM_CAPTIONS] if in_split else []) + ([CALM_CAPTIONS] if on_full else [])


def clear_of_captions(card: Any, edit: ClipEdit, seams: list[tuple[float, float]] | None = None) -> str:
    """A name card's (or micro-stat's) place, checked against the captions actually drawn
    while it's up, and moved clear of them if it would touch them: just above (or below)
    them, a little smaller if it has to be (Round 30). The placer already avoids them;
    this is the last check before drawing, so a card and a caption never overlap."""
    box = _box_rows(card)
    if box is None or card.template in ("brand_proof", "wiki_proof", "stat_proof"):
        return card.place  # Round 41: a proof pill's band (1320-1580) is always under the captions
    x, top, scale, align = parse_place(card.place) if card.place or card.template == "speaker_title" \
        else (56.0, float(STAT_PILL_TOP), 1.0, "left")
    tall_at_one = (box[1] - box[0]) / box[2]
    resizable = card.template == "speaker_title"

    def touches(y: float, size: float) -> bool:
        trial = replace(card, place=f"{x:.0f},{y:.0f},{size:.2f},{align}")
        bottom = y + tall_at_one * size
        return any(not (bottom + CAPTION_CLEARANCE <= low or y >= high + CAPTION_CLEARANCE)
                   for low, high in _caption_bands(trial, edit, seams))

    if not touches(top, scale):
        return card.place
    for size in ((scale, round(max(0.6, scale * 0.85), 2)) if resizable else (scale,)):
        tall = tall_at_one * size
        bands = sorted(_caption_bands(card, edit, seams))
        spots = [low - CAPTION_CLEARANCE - tall for low, _high in bands] + [high + CAPTION_CLEARANCE for _low, high in bands]
        for y in sorted(spots, key=lambda spot: abs(spot - top)):  # the nearest clear spot
            if NAME_CARD_TOP_LIMIT <= y <= NAME_CARD_BOTTOM_LIMIT - tall and not touches(y, size):
                return f"{x:.0f},{y:.0f},{size:.2f},{align}"
    return card.place


def words_outside(words: list[Any], edit: ClipEdit) -> list[Any]:
    """The words whose bold captions stay: none that overlap a graphic, so a bold line
    never runs under a graphic as it comes in."""
    return [w for w in words if not any(_during(w, card) for card in edit.cards)]


def _ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _sentence_case(texts: list[str]) -> list[str]:
    """Transcripts without capitals (some speech models): 'i' -> 'I' and a capital
    after a full stop. Proper transcripts are left as they are."""
    if any(re.search(r"[A-Z]", t) for t in texts):
        return texts
    out, capital = [], True
    for text in texts:
        word = "I" + text[1:] if text in {"i", "i'm", "i've", "i'd", "i'll"} else text
        if capital and word:
            word = word[0].upper() + word[1:]
        out.append(word)
        capital = word.endswith((".", "?", "!"))
    return out


def landed(card: Any, edit: ClipEdit, rate: Fraction | float = 30) -> tuple[float, float]:
    """When a split chart's panel has landed and when it starts to leave (source seconds):
    the steady part between the moves, on the same frames compose_cards uses."""
    rate = Fraction(rate).limit_denominator(1001)
    count = max(2, round(MOVE * float(rate)))
    first_in = int(np.ceil((card.start - edit.start) * rate - 1e-6))
    last_out = int(np.ceil((card.end - edit.start) * rate - 1e-6))
    first_out = max(first_in + count, last_out - count)
    return edit.start + float((first_in + count) / rate), edit.start + float(first_out / rate)


def calm_captions(words: list[Any], edit: ClipEdit, path: Path,
                  seams: list[tuple[float, float]] | None = None, rate: Fraction | float = 30) -> Path:
    """Small sentence-case captions on a soft dark box, for the time graphics are up.
    ``seams``: the clip's two-person split spans (clip seconds). There, over the speakers
    (not under a chart), the line sits on the seam between the panels like the usual
    captions, never on a face. Under a split chart they show only while the panel is in
    place (not while it slides past them: Round 30)."""
    events: list[tuple[float, float, str]] = []
    on_seam: set[int] = set()
    on_split: set[int] = set()
    for card in edit.cards:
        first_event = len(events)
        limit = SPLIT_CAPTION_CHARS if MODES[card.template] == "split" else CAPTION_CHARS
        inside = [w for w in words if _during(w, card)]
        texts = _sentence_case([str(w.text).strip() for w in inside])
        group: list[tuple[Any, str]] = []

        def flush(group: list[tuple[Any, str]], opened: float) -> None:
            if group:
                start = max(float(group[0][0].start), opened)
                events.append((start, float(group[-1][0].end), " ".join(t for _, t in group)))
                group.clear()

        for word, text in zip(inside, texts):
            if group and len(" ".join(t for _, t in group)) + len(text) + 1 > limit:
                flush(group, card.start)
            group.append((word, text))
            if text.endswith((".", "?", "!", ",")) and len(" ".join(t for _, t in group)) > limit * 0.6:
                flush(group, card.start)
        flush(group, card.start)
        for k in range(len(events) - 1):  # each line stays until the next one (no flicker)
            start, end, text = events[k]
            if events[k + 1][0] - end < 0.6:
                events[k] = (start, events[k + 1][0], text)
        if len(events) > first_event:
            start, end, text = events[-1]
            events[-1] = (start, min(card.end, end + 0.4), text)
        if MODES[card.template] == "split":
            settled, leaving = landed(card, edit, rate)
            kept = []
            for start, end, text in events[first_event:]:
                start, end = max(start, settled), min(end, leaving)
                if end - start >= 0.25:
                    kept.append((start, end, text))
            events[first_event:] = kept
            on_split.update(range(first_event, len(events)))
        if MODES[card.template] == "overlay":
            for k in range(first_event, len(events)):
                middle = (events[k][0] + events[k][1]) / 2 - edit.start
                if any(a <= middle < b for a, b in seams or []):
                    on_seam.add(k)
    margin_v = FRAME_H - CAPTION_BOTTOM
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {FRAME_W}", f"PlayResY: {FRAME_H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Calm,Inter,{CALM_SIZE},&H00FFFFFF,&H00FFFFFF,&H64000000,&H64000000,-1,0,0,0,100,100,0,0,3,9,0,2,90,90,{margin_v},1",
        # Round 39: under a split chart, one bold line on the seam (the bottom of the speaker's
        # viewport), on a soft dark box: readable, and clear of the card and the apps' bars.
        f"Style: Split,Inter,{SPLIT_CAPTION_SIZE},&H00FFFFFF,&H00FFFFFF,&H50000000,&H50000000,-1,0,0,0,100,100,0,0,3,10,0,2,"
        f"70,70,{FRAME_H - SEAM_CAPTION_BOTTOM},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for k, (start, end, text) in enumerate(events):
        safe = text.replace("\\", "").replace("{", "(").replace("}", ")")
        style = "Calm"
        if k in on_split:
            safe, style = rf"{{\an2\pos(540,{SEAM_CAPTION_BOTTOM})}}" + safe, "Split"
        elif k in on_seam:
            safe = r"{\an5\pos(540,960)}" + safe
        lines.append(f"Dialogue: 0,{_ass_time(start - edit.start)},{_ass_time(end - edit.start)},{style},,0,0,0,,{safe}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _round_mask(width: int, height: int, radius: int, folder: Path) -> Path:
    """White rounded rectangle on black: where the frosted blur shows."""
    from PIL import Image, ImageDraw

    path = folder / f"mask_{width}x{height}_{radius}.png"
    if not path.is_file():
        image = Image.new("L", (width, height), 0)
        ImageDraw.Draw(image).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
        image.save(path)
    return path


def _frame_rate(path: Path) -> Fraction:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    try:
        rate = Fraction(out)
        return rate if rate > 0 else Fraction(30)
    except (ValueError, ZeroDivisionError):
        return Fraction(30)


TO_RGB = "scale=in_color_matrix=bt709"  # a clip's frame, as its Rec. 709 tag says
FROM_RGB = "scale=out_color_matrix=bt709,format=yuv420p"  # a move frame, back into the clip


def _decode(path: Path, start: float, count: int, rate: Fraction | None = None, chain: str = "",
            size: tuple[int, int] | None = None) -> list[np.ndarray]:
    """``count`` frames (RGB) from ``start`` seconds. ``rate``: sample the picture at the
    clip's frame rate (as the steady split view does), so a 30 fps camera in a 24 fps clip
    moves at its real speed during the move. ``chain``: FFmpeg filters to apply first,
    whose output is ``size`` (w, h)."""
    if size is None:
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height", "-of", "csv=p=0", str(path)],
                               capture_output=True, text=True).stdout.strip().split(",")
        size = (int(probe[0]), int(probe[1]))
    width, height = size
    # Round 32: read the picture as Rec. 709, as every clip is tagged (core.video_renderer),
    # and write the move frames back the same way (compose_cards). Newer FFmpeg builds no
    # longer pick the same matrix both ways on their own, and the move flashed a shade off.
    filters = ",".join(part for part in ((f"fps={rate}" if rate else ""), chain, TO_RGB) if part)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.4f}", "-i", str(path),
                          *(["-vf", filters] if filters else []), "-frames:v", str(count),
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    frame_bytes = width * height * 3
    frames = [np.frombuffer(raw[k * frame_bytes:(k + 1) * frame_bytes], np.uint8).reshape(height, width, 3)
              for k in range(len(raw) // frame_bytes)]
    while frames and len(frames) < count:
        frames.append(frames[-1])
    return frames


def _ease(p: float) -> float:
    """Ease-in-out (cubic): starts and lands softly."""
    p = min(1.0, max(0.0, p))
    return 4 * p ** 3 if p < 0.5 else 1 - (-2 * p + 2) ** 3 / 2


BAND_ASPECT = FRAME_W / SPLIT_TOP  # the speaker's viewport in a split view: 1080 x 1040 (Round 39)
BAND_HEADROOM = 0.35  # a source taller than the viewport's shape: the crop sits a little above centre (heads sit high)


def band_rect(width: int, height: int, cx: float | None = None) -> tuple[float, float, float, float]:
    """Round 39: the source rectangle the speaker's viewport shows in a split view (x, y,
    w, h; even, as FFmpeg crops it): the viewport's 1080:1040 shape at the camera's full
    height, centred on ``cx`` (the face: the centre of the full-screen crop) and kept in
    the frame. 16:9 1920x1080 -> 1120 x 1078 around the face, about 1.7x the face size of
    the old 2.35:1 strip."""
    w = int(min(float(width), height * BAND_ASPECT)) // 2 * 2
    h = int(round(w / BAND_ASPECT)) // 2 * 2
    centre = width / 2 if cx is None else cx
    x = int(min(max(centre - w / 2, 0.0), width - w)) // 2 * 2
    y = int(round((height - h) * BAND_HEADROOM)) // 2 * 2
    return float(x), float(y), float(w), float(h)


def strip_rect(width: int, height: int) -> tuple[float, float, float, float]:
    """The viewport's rectangle with no face to follow: centred (see band_rect)."""
    return band_rect(width, height)


def wide_fit(width: int, height: int) -> tuple[int, int, int, int]:
    """Round 39: a wide shot (no single face to follow) in the speaker's viewport: the whole
    picture, as large as fits (x, y, w, h in output px; even, as FFmpeg's scale and overlay
    place it), over its own blurred copy."""
    if width <= 0 or height <= 0:
        return 0, 0, FRAME_W, SPLIT_TOP
    tall = round(FRAME_W * height / width / 2) * 2  # scale=1080:-2
    if tall <= SPLIT_TOP:
        w, h = FRAME_W, tall
    else:
        w, h = round(SPLIT_TOP * width / height / 2) * 2, SPLIT_TOP  # scale=-2:1040
    return int((FRAME_W - w) / 2) // 2 * 2, int((SPLIT_TOP - h) / 2) // 2 * 2, w, h


def band_centres(crop_at: Callable[[float], tuple[float, float, float, float] | None] | None,
                 a: float, b: float, step: float = 0.2) -> list[tuple[float, float | None]]:
    """(clip second, face centre x in source px, None for a wide shot) through a split
    chart's window, in runs: a new entry only where the centre moves 8 px or more (or the
    shot changes kind). What the viewport's crop follows."""
    out: list[tuple[float, float | None]] = []
    t = a
    while t < b + 1e-9:
        rect = crop_at(t) if crop_at else None
        cx = None if rect is None else round((rect[0] + rect[2] / 2) / 8) * 8.0
        if not out or (cx is None) != (out[-1][1] is None) or (cx is not None and abs(cx - out[-1][1]) >= 8):
            out.append((round(t, 3), cx))
        t += step
    return out


def centre_at(centres: list[tuple[float, float | None]], t: float) -> float | None:
    """The viewport's face centre at clip second t (see band_centres)."""
    cx = centres[0][1] if centres else None
    for since, value in centres:
        if since <= t + 1e-9:
            cx = value
    return cx


def _x_expression(centres: list[tuple[float, float | None]], width: int, height: int) -> str:
    """FFmpeg's crop x for the viewport, per frame (t: clip seconds), from band_centres."""
    runs = [(since, int(band_rect(width, height, cx)[0])) for since, cx in centres]
    expr = str(runs[-1][1])
    for (_since, x), (until, _next) in reversed(list(zip(runs, runs[1:]))):
        expr = f"if(lt(t,{until:.3f}),{x},{expr})"
    return expr


# The blurred-background layouts (see video_renderer): the whole camera picture, 1080 wide,
# centred over a blurred copy of itself.
BAND_LAYOUTS = frozenset({"blurred_background", "wide_blurred_background"})
BLURRED_BACKDROP = ("scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
                    "boxblur=5:2,eq=brightness=-0.15,scale=1080:1920")


def picture_band(layout: str, width: int, height: int) -> tuple[float, float, float, float] | None:
    """Where a blurred-background clip shows the whole camera picture (output x, y, w, h);
    None for other layouts, or a non-widescreen source in the wide layout (its picture
    carries black bars)."""
    if layout not in BAND_LAYOUTS or width <= 0 or height <= 0:
        return None
    tall = round(FRAME_W * height / width / 2) * 2
    if layout == "wide_blurred_background" and abs(tall - 608) > 2:
        return None
    top = int((FRAME_H - tall) / 2) // 2 * 2  # FFmpeg's overlay keeps an even row for 4:2:0
    return 0.0, float(top), float(FRAME_W), float(tall)


def _band_frame(picture: np.ndarray, backdrop: np.ndarray, chart: np.ndarray | None, eased: float,
                band: tuple[float, float, float, float], canvas: str = CANVAS) -> np.ndarray:
    """One frame of the move for a blurred-background clip: the camera picture (1080 wide)
    glides up from its place in the middle to its place in the speaker's viewport (Round 39:
    the whole picture, centred in the top 1040 rows, as the steady wide view shows it), over
    the clip's own blurred background, while the panel slides up from the bottom."""
    pic_h = picture.shape[0]
    _x, band_top, _w, _band_h = band
    target = float(wide_fit(FRAME_W, pic_h)[1]) if pic_h <= SPLIT_TOP else 0.0
    top = band_top + (target - band_top) * eased
    out = backdrop.copy()
    first, last = int(np.floor(top)), min(FRAME_H, int(np.ceil(top + pic_h)))
    if last > first:
        matrix = np.float32([[1, 0, 0], [0, 1, top - first]])
        rows = cv2.warpAffine(picture, matrix, (FRAME_W, last - first), flags=cv2.INTER_LANCZOS4,
                              borderMode=cv2.BORDER_REPLICATE)
        out[first:last] = rows
    return _panel(out, chart, FRAME_H + (SPLIT_TOP - FRAME_H) * eased, canvas)


def _panel(out: np.ndarray, chart: np.ndarray | None, band: float, color: str = CANVAS) -> np.ndarray:
    """The panel from row ``band`` down (a soft sub-pixel top edge), its chart on it.
    ``color``: the panel (Round 40: the clip's palette canvas; CANVAS for classic)."""
    canvas = np.array([int(color[k:k + 2], 16) for k in (1, 3, 5)], np.uint8)
    edge = int(np.floor(band))
    if edge < FRAME_H:
        out[edge + 1:] = canvas
        cover = 1.0 - (band - edge)
        out[edge] = (out[edge] * (1 - cover) + canvas * cover).astype(np.uint8)
    if chart is not None and band < FRAME_H:
        shifted = cv2.warpAffine(chart, np.float32([[1, 0, 0], [0, 1, band]]), (FRAME_W, FRAME_H),
                                 flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
        alpha = shifted[:, :, 3:4].astype(np.float32) / 255
        out[:] = (out * (1 - alpha) + shifted[:, :, :3] * alpha).astype(np.uint8)
    return out


def _move_frame(source: np.ndarray, base: np.ndarray, chart: np.ndarray | None, eased: float,
                start_rect: tuple[float, float, float, float] | None,
                end_rect: tuple[float, float, float, float] | None = None, canvas: str = CANVAS) -> np.ndarray:
    """One frame of the move between the full-screen speaker (eased = 0) and the split
    view (eased = 1): the speaker's picture zooms out into the viewport (``end_rect``, the
    steady view's crop: band_rect around the face) while the panel slides up from the
    bottom. ``start_rect``: the source rectangle the clip shows full screen; None when it's
    not a plain crop (the whole frame or the two-person split), then the full-screen
    picture stays put and the panel slides up over it."""
    src_h, src_w = source.shape[:2]
    band = FRAME_H + (SPLIT_TOP - FRAME_H) * eased  # where the panel's top edge is
    out = np.empty((FRAME_H, FRAME_W, 3), np.uint8)
    if start_rect is None:
        out[:] = base
    else:
        x0, y0, w0, h0 = start_rect
        x1, y1, w1, h1 = end_rect or band_rect(src_w, src_h, x0 + w0 / 2)
        cx = (x0 + w0 / 2) + ((x1 + w1 / 2) - (x0 + w0 / 2)) * eased
        cy = (y0 + h0 / 2) + ((y1 + h1 / 2) - (y0 + h0 / 2)) * eased
        width = min(w0 * (w1 / w0) ** eased, src_h * FRAME_W / band, float(src_w))  # zoom out, stay in frame
        height = width * band / FRAME_W
        left = min(max(cx - width / 2, 0.0), src_w - width)
        top = min(max(cy - height / 2, 0.0), src_h - height)
        scale = FRAME_W / width
        matrix = np.float32([[scale, 0, -left * scale], [0, scale, -top * scale]])
        rows = min(FRAME_H, int(np.ceil(band)) + 1)
        out[:rows] = cv2.warpAffine(source, matrix, (FRAME_W, rows), flags=cv2.INTER_LANCZOS4,
                                    borderMode=cv2.BORDER_REPLICATE)
        out[rows:] = 0
    return _panel(out, chart, band, canvas)


def _size(path: Path) -> tuple[int, int]:
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip().split(",")
    try:
        return int(probe[0]), int(probe[1])
    except (ValueError, IndexError):
        return 0, 0


def split_moves(source_video: Path, base_clip: Path, edit_start: float, a: float, b: float, chart: Frames,
                crop_at: Callable[[float], tuple[float, float, float, float] | None] | None, folder: Path,
                rate: Fraction, layout: str = "",
                centres: list[tuple[float, float | None]] | None = None) -> list[tuple[int, int, str]]:
    """The frames of the move into (from ``a``) and out of (until ``b``) one split chart,
    as PNG sequences: [(first clip frame, frame count, pattern)]. Clip frames are
    n / ``rate``; the move takes MOVE seconds each way.

    Three kinds of move, by what the clip shows full screen: a plain crop of the camera
    (``crop_at``: the picture zooms out into the top band); the blurred-background layout
    (``layout``: the whole picture glides up from the middle into the band); anything else
    (the panel slides up over the clip)."""
    folder.mkdir(parents=True, exist_ok=True)
    count = max(2, round(MOVE * float(rate)))
    first_in = int(np.ceil(a * rate - 1e-6))
    last_out = int(np.ceil(b * rate - 1e-6))  # the first full-screen frame after the chart
    first_out = max(first_in + count, last_out - count)
    width, height = _size(source_video)
    band = picture_band(layout, width, height)
    made = []
    for name, start_frame, rising in (("in", first_in, True), ("out", first_out, False)):
        times = [float((start_frame + j) / rate) for j in range(count)]
        rect = crop_at(times[0] if rising else times[-1]) if crop_at else None
        mode = "crop" if rect is not None else ("band" if band is not None else "over")
        if mode == "band":
            tall = int(band[3])
            sources = _decode(source_video, edit_start + times[0], count, rate, "scale=1080:-2:flags=lanczos", (FRAME_W, tall))
            bases = _decode(source_video, edit_start + times[0], count, rate, BLURRED_BACKDROP, (FRAME_W, FRAME_H))
        else:
            sources = _decode(source_video, edit_start + times[0], count, rate) if mode == "crop" else []
            bases = _decode(base_clip, times[0], count)
        if not bases or (mode != "over" and not sources):
            return []
        for j, t in enumerate(times):
            # In: the last frame lands exactly on the chart view's own picture (eased 1). Out:
            # the last frame is exactly the clip's own picture (eased 0). No snap either way.
            progress = (j + 1) / count
            eased = _ease(progress if rising else 1 - progress)
            index = min(chart.count - 1, max(0, int(round((t - a) * FPS))))
            page = cv2.imread(chart.pattern % index, cv2.IMREAD_UNCHANGED)
            page_rgba = None
            if page is not None:
                page_rgba = np.zeros((FRAME_H, FRAME_W, 4), np.uint8)
                page_rgba[:page.shape[0], :, :] = cv2.cvtColor(page, cv2.COLOR_BGRA2RGBA)[:FRAME_H]
            if mode == "band":
                frame = _band_frame(sources[j], bases[j], page_rgba, eased, band, chart.canvas)
            else:
                end_rect = None
                if mode == "crop":  # land exactly on the steady view's crop (the face it follows then)
                    edge = times[-1] + 1 / float(rate) if rising else times[0] - 1 / float(rate)
                    cx = centre_at(centres, edge) if centres else rect[0] + rect[2] / 2
                    end_rect = band_rect(width, height, cx if cx is not None else rect[0] + rect[2] / 2)
                frame = _move_frame(sources[j] if sources else bases[j], bases[j], page_rgba, eased, rect, end_rect,
                                    chart.canvas)
            cv2.imwrite(str(folder / f"{name}_{j:03d}.png"), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        made.append((start_frame, count, str(folder / f"{name}_%03d.png")))
    return made


def compose_cards(
    base_clip: Path,
    source_video: Path,
    edit: ClipEdit,
    frames: list[Frames],
    words: list[Any],
    output: Path,
    seams: list[tuple[float, float]] | None = None,
    crop_at: Callable[[float], tuple[float, float, float, float] | None] | None = None,
    layout: str = "",
) -> Path:
    """Lay ``edit``'s rendered graphics over ``base_clip`` (edit.start-edit.end, ideally
    rendered without captions while graphics are up: words_outside()). ``layout``: the
    clip's layout (the blurred-background ones get their own move into a chart)."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg was not found. Install it with `brew install ffmpeg`.")
    if len(frames) != len(edit.cards):
        raise ValueError("One set of frames per card.")
    output = Path(output).resolve()
    base_clip, source_video, output = Path(base_clip).resolve(), Path(source_video).resolve(), Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="graphics-"))  # captions and masks; removed after
    duration = edit.end - edit.start
    windows = windows_of(edit)
    splits = [k for k, card in enumerate(edit.cards) if MODES[card.template] == "split"]
    rate = _frame_rate(base_clip)
    ass = calm_captions(words, edit, work / f"calm_{uuid.uuid4().hex}.ass", seams, rate)
    inputs = ["-i", str(base_clip)]
    graph = ["[0:v]format=yuv420p[v0]"]  # the clip's own frame rate: no repeated or dropped frames
    next_input = 1
    centres: dict[int, list[tuple[float, float | None]]] = {}
    if splits:
        inputs += ["-ss", f"{edit.start:.3f}", "-t", f"{duration:.3f}", "-i", str(source_video)]
        src_w, src_h = _size(source_video)
        labels = "".join(f"[src{k}]" for k in splits)
        graph.append(f"[1:v]fps={rate},split={len(splits)}{labels}")
        for k in splits:
            # Round 39: the speaker's viewport, 1080 x 1040 (54% of the frame). Following a face:
            # a crop of the camera around it (band_rect), its x following the full-screen crop
            # frame by frame. A wide shot: the whole picture over its own blurred copy.
            a, b = windows[k]
            centres[k] = band_centres(crop_at, a, b)
            if any(cx is not None for _t, cx in centres[k]) and src_w and src_h:
                runs = centres[k]
                first = next(cx for _t, cx in runs if cx is not None)
                runs = [(t, cx if cx is not None else first) for t, cx in runs]  # a wide moment keeps the face's place
                centres[k] = runs
                _x, y, w, h = band_rect(src_w, src_h, first)
                graph.append(f"[src{k}]crop={int(w)}:{int(h)}:'{_x_expression(runs, src_w, src_h)}':{int(y)},"
                             f"scale={FRAME_W}:{SPLIT_TOP}:flags=lanczos,setsar=1[sp{k}]")
            else:
                x, y, w, h = wide_fit(src_w, src_h)
                graph.append(f"[src{k}]split[wa{k}][wb{k}]")
                graph.append(f"[wa{k}]{BLURRED_BACKDROP},crop={FRAME_W}:{SPLIT_TOP}:0:0[wbg{k}]")
                graph.append(f"[wb{k}]scale={w}:{h}:flags=lanczos[wfg{k}]")
                graph.append(f"[wbg{k}][wfg{k}]overlay={x}:{y},setsar=1[sp{k}]")
        next_input = 2
    current, n = "v0", 0
    order = sorted(range(len(edit.cards)), key=lambda k: edit.cards[k].start)
    for k in order:
        card, seq, (a, b) = edit.cards[k], frames[k], windows[k]
        inputs += ["-framerate", str(FPS), "-i", seq.pattern]
        graphic = f"g{k}"
        graph.append(f"[{next_input}:v]format=rgba,setpts=PTS-STARTPTS+{a:.3f}/TB[{graphic}]")
        next_input += 1
        on = f"between(t\\,{a:.3f}\\,{b:.3f})"
        if MODES[card.template] == "overlay":
            reveals = [((row.reveal_at - edit.start) if row.reveal_at is not None else a) for row in card.rows]
            for box, (x, y, w, h, radius) in enumerate(seq.blur):
                x, y = x - x % 2, y - y % 2  # even sizes: the video's colour planes are half size
                w, h = w + w % 2, h + h % 2
                start = reveals[box] if box < len(reveals) else a
                graph.append(f"[{current}]split[m{n}][c{n}]")
                blurred = f"[c{n}]crop={w}:{h}:{x}:{y},{BLUR},format=rgba"
                if radius:  # rounded corners (the name card): blur only inside them
                    inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{duration:.3f}", "-i",
                               str(_round_mask(w, h, radius, work))]
                    graph.append(f"{blurred}[r{n}];[{next_input}:v]format=gray[k{n}];[r{n}][k{n}]alphamerge[q{n}]")
                    blurred = f"[q{n}]format=rgba"
                    next_input += 1
                graph.append(f"{blurred},fade=t=in:st={start:.3f}:d=0.3:alpha=1,"
                             f"fade=t=out:st={b - EXIT - 1 / FPS:.3f}:d={EXIT}:alpha=1[bl{n}]")
                graph.append(f"[m{n}][bl{n}]overlay={x}:{y}:enable='{on}'[v{n + 1}]")
                current, n = f"v{n + 1}", n + 1
            graph.append(f"[{current}][{graphic}]overlay=0:0:enable='{on}':eof_action=pass[v{n + 1}]")
            current, n = f"v{n + 1}", n + 1
        else:
            # The canvas at the clip's own frame rate: at the graphics' 30 fps, a 25 fps
            # camera picture repeated one frame in five (a small, steady judder).
            graph.append(f"color=c={_hex(seq.canvas)}:s={FRAME_W}x{FRAME_H}:r={rate}:d={duration:.3f}[cv{k}]")
            graph.append(f"[cv{k}][sp{k}]overlay=0:0[sb{k}]")
            graph.append(f"[sb{k}][{graphic}]overlay=0:{SPLIT_TOP}:eof_action=repeat[split{k}]")
            # The move in, the steady split view and the move out, joined into one stream
            # (frame-exact: the parts follow each other inside it), laid over the clip once.
            # Before, each part was its own overlay; at 24 fps one FFmpeg build showed one
            # frame of the plain clip between the move in and the chart.
            moves = split_moves(source_video, base_clip, edit.start, a, b, seq, crop_at, work / f"move{k}", rate, layout,
                                centres.get(k))
            if len(moves) == 2:
                (first_in, count_in, in_pattern), (first_out, count_out, out_pattern) = moves
                tick = f"{rate.denominator}/{rate.numerator}"
                parts = []
                for label, pattern in (("pi", in_pattern), ("po", out_pattern)):
                    inputs += ["-framerate", str(rate), "-i", pattern]
                    graph.append(f"[{next_input}:v]{FROM_RGB},setsar=1,settb={tick},setpts=N[{label}{k}]")
                    parts.append(f"[{label}{k}]")
                    next_input += 1
                steady_from, steady_to = first_in + count_in, first_out
                if steady_to > steady_from:
                    graph.append(f"[split{k}]trim=start_frame={steady_from}:end_frame={steady_to},"
                                 f"format=yuv420p,setsar=1,settb={tick},setpts=N[ps{k}]")
                    parts.insert(1, f"[ps{k}]")
                else:
                    graph.append(f"[split{k}]nullsink")
                end_frame = first_out + count_out
                graph.append(f"{''.join(parts)}concat=n={len(parts)}:v=1:a=0,settb={tick},setpts=N+{first_in}[span{k}]")
                window = f"between(t\\,{(first_in - 0.5) / rate:.4f}\\,{(end_frame - 0.5) / rate:.4f})"
                graph.append(f"[{current}][span{k}]overlay=0:0:eof_action=pass:enable='{window}'[v{n + 1}]")
            else:
                graph.append(f"[{current}][split{k}]overlay=0:0:enable='{on}'[v{n + 1}]")
            current, n = f"v{n + 1}", n + 1
    graph.append(f"[{current}]ass='{ass.name}':fontsdir='{FONTS}'[out]")
    # Round 35: the graphics' micro sound effects (core.audio_polish), one track with every cue
    # at its exact sample, mixed under the clip's own audio. Only the audio is re-encoded; the
    # video's frames pass through untouched (-fps_mode passthrough), so lip-sync can't drift.
    audio = ["-map", "0:a?", "-c:a", "copy"]
    from core import audio_polish

    if audio_polish.enabled() and _has_audio(base_clip):
        track = audio_polish.cue_track(audio_polish.clip_cues(edit, windows), duration, work / "sfx.wav")
        if track is not None:
            inputs += ["-i", str(track)]
            # Round 36: a 1 ms look-ahead limiter keeps a whoosh on a loud word under the -1.5 dBTP master
            graph.append(f"[0:a][{next_input}:a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                         f"{audio_polish.SFX_LIMITER}[aout]")
            audio = ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
            next_input += 1
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs,
        "-filter_complex", ";".join(graph), "-map", "[out]", *audio,
        *_video_encoder_args(ffmpeg), "-pix_fmt", "yuv420p", *PASSTHROUGH, "-movflags", "+faststart",
        "-t", f"{duration:.3f}", str(output),
    ]
    try:
        subprocess.run(command, check=True, cwd=str(work), capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Adding the graphics failed: {exc.stderr.strip()[-700:]}") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return output


def _has_audio(path: Path) -> bool:
    """The clip has a sound track."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(probe.stdout.strip())


PASSTHROUGH = ("-fps_mode", "passthrough")  # every video frame written as it comes: none dropped or doubled


def render_with_cards(
    source_video: Path,
    edit: ClipEdit,
    words: list[Any],
    output: Path,
    work_dir: Path,
    render_base: Callable[[list[Any]], Path],
    hook_seconds: float = 3.0,
    seams: list[tuple[float, float]] | None = None,
    crop_at: Callable[[float], tuple[float, float, float, float] | None] | None = None,
    layout: str = "",
) -> tuple[Path | None, list[Any]]:
    """Check every card, render the ones that pass, have ``render_base(words)`` render
    the clip with those words' captions (none during graphics), and compose.
    ``hook_seconds``: how long the title card shows, so no graphic starts on top of it.
    ``seams``: the clip's two-person split spans (clip seconds), for the calm captions.
    ``crop_at(t)``: the source rectangle the clip shows at clip second t (for the move into
    a split chart to start from exactly that picture); None where it isn't a plain crop.
    Returns (the clip with graphics, or None when no card passed; one CardCheck per card)."""
    from core.card_checks import check_edit
    from core.cards import CardRenderError, compact, render_card

    checks = check_edit(edit, words, hook_seconds)
    passed, frames = [], []
    for card, check in zip(edit.cards, checks):
        if not check.ok:
            continue
        place = clear_of_captions(card, edit, seams)
        if place != card.place:
            card = replace(card, place=place)
        try:
            frames.append(render_card(card, edit.brand, Path(work_dir) / "cards"))
        except CardRenderError as exc:
            shorter = compact(card) if str(exc).startswith("Too long") else None
            try:  # Round 33: text that doesn't fit costs the headline or a line, not the visual
                if shorter is None:
                    raise exc
                frames.append(render_card(shorter, edit.brand, Path(work_dir) / "cards"))
                card = shorter
            except CardRenderError as final:
                check.errors.append(str(final))
                continue
        passed.append(card)
    if not passed:
        return None, checks
    keep = ClipEdit(start=edit.start, end=edit.end, cards=passed, brand=edit.brand)
    # The bold captions give way (to the small ones) only under graphics that would sit on them.
    quiet = ClipEdit(start=edit.start, end=edit.end, brand=edit.brand,
                     cards=[card for card in passed if not keeps_bold_captions(card, edit, seams)])
    base = render_base(words_outside(words, quiet))
    # Round 41: under a newspaper tear-out the headline is the caption (its words highlight as said)
    calm = [w for w in words if any(_during(w, card) for card in quiet.cards)
            and not any(_during(w, card) for card in quiet.cards if card.template == "newspaper_tearout")]
    return compose_cards(Path(base), source_video, keep, frames, calm, output, seams, crop_at, layout), checks
