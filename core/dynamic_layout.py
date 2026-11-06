"""Round 27: putting the animated graphics on the clip.

- overlay graphics (a number or range, wireframe boxes): the speaker stays full-screen;
  the graphic's frames go over the video, and the video behind each box is blurred
  (the frosted-glass look) while the box is up.
- split charts (24/76): the speaker in a band on top (the camera's whole width, so
  two-person shots keep both people) and the chart below. Getting there is one camera
  move, never a cross-fade (two sizes of the same face at once): the picture zooms out
  and shrinks into the band while the dark panel slides up from the bottom edge; leaving,
  the reverse. Those frames are drawn here with sub-pixel resampling (no FFmpeg rounding
  wobble); the rest of the split view is FFmpeg.
- While a graphic is up, the bold word-by-word captions give way to small, calm,
  sentence-case captions near the bottom, so the number isn't shown twice.
- Leaving: every graphic slides down and fades over its last 0.4 s: no snap back.

render_with_cards() checks every card (core.card_checks), renders the ones that pass
(core.cards), asks for a base clip without captions during the graphics, then composes.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import subprocess
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from core.cards import CANVAS, EXIT, FPS, NAME_CARD_SIZE, SPLIT_TOP, Frames, parse_place
from core.edl import MODES, ClipEdit
from core.subtitle_engine import CAPTION_MARGIN_V
from core.video_renderer import _video_encoder_args

FRAME_W, FRAME_H = 1080, 1920
FONTS = Path(__file__).resolve().parents[1] / "assets" / "fonts"
MOVE = 0.4  # seconds (10 frames at 25 fps, 12 at 30): the move into and out of the split view
CAPTION_BOTTOM = 1440  # calm captions sit with their bottom here (above where apps cover the screen)
CAPTION_CHARS = 30
BLUR = "boxblur=18:2"


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


def keeps_bold_captions(card: Any, edit: ClipEdit, seams: list[tuple[float, float]] | None = None) -> bool:
    """A name card placed clear of the bold captions (above the head on a close-up, or in
    the guest's own panel) leaves them as they are: the hook's captions don't switch to the
    small ones and back for it. A data graphic, or a name card in the standard lower spot,
    still gets the small captions."""
    if card.template != "speaker_title" or not card.place:
        return False
    _x, top, scale, _align = parse_place(card.place)
    bottom = top + NAME_CARD_SIZE[1] * scale
    a, b = card.start - edit.start, card.end - edit.start
    spans = seams or []
    bands = ([SEAM_CAPTIONS] if any(s < b and a < e for s, e in spans) else []) \
        + ([] if any(s <= a and b <= e for s, e in spans) else [BOLD_CAPTIONS])
    return all(bottom + CAPTION_CLEARANCE <= low or top >= high + CAPTION_CLEARANCE for low, high in bands)


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


def calm_captions(words: list[Any], edit: ClipEdit, path: Path,
                  seams: list[tuple[float, float]] | None = None) -> Path:
    """Small sentence-case captions on a soft dark box, for the time graphics are up.
    ``seams``: the clip's two-person split spans (clip seconds). There, over the speakers
    (not under a chart), the line sits on the seam between the panels like the usual
    captions, never on a face."""
    events: list[tuple[float, float, str]] = []
    on_seam: set[int] = set()
    for card in edit.cards:
        first_event = len(events)
        inside = [w for w in words if _during(w, card)]
        texts = _sentence_case([str(w.text).strip() for w in inside])
        group: list[tuple[Any, str]] = []

        def flush() -> None:
            if group:
                start = max(float(group[0][0].start), card.start)
                events.append((start, float(group[-1][0].end), " ".join(t for _, t in group)))
                group.clear()

        for word, text in zip(inside, texts):
            if group and len(" ".join(t for _, t in group)) + len(text) + 1 > CAPTION_CHARS:
                flush()
            group.append((word, text))
            if text.endswith((".", "?", "!", ",")) and len(" ".join(t for _, t in group)) > CAPTION_CHARS * 0.6:
                flush()
        flush()
        for k in range(len(events) - 1):  # each line stays until the next one (no flicker)
            start, end, text = events[k]
            if events[k + 1][0] - end < 0.6:
                events[k] = (start, events[k + 1][0], text)
        if events:
            start, end, text = events[-1]
            events[-1] = (start, min(card.end, end + 0.4), text)
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
        f"Style: Calm,Inter,40,&H00FFFFFF,&H00FFFFFF,&H64000000,&H64000000,-1,0,0,0,100,100,0,0,3,9,0,2,90,90,{margin_v},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for k, (start, end, text) in enumerate(events):
        safe = text.replace("\\", "").replace("{", "(").replace("}", ")")
        if k in on_seam:
            safe = r"{\an5\pos(540,960)}" + safe
        lines.append(f"Dialogue: 0,{_ass_time(start - edit.start)},{_ass_time(end - edit.start)},Calm,,0,0,0,,{safe}")
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


def _decode(path: Path, start: float, count: int) -> list[np.ndarray]:
    """``count`` frames (RGB, full size) from ``start`` seconds."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip().split(",")
    width, height = int(probe[0]), int(probe[1])
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.4f}", "-i", str(path), "-frames:v",
                          str(count), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    size = width * height * 3
    frames = [np.frombuffer(raw[k * size:(k + 1) * size], np.uint8).reshape(height, width, 3)
              for k in range(len(raw) // size)]
    while frames and len(frames) < count:
        frames.append(frames[-1])
    return frames


def _ease(p: float) -> float:
    """Ease-in-out (cubic): starts and lands softly."""
    p = min(1.0, max(0.0, p))
    return 4 * p ** 3 if p < 0.5 else 1 - (-2 * p + 2) ** 3 / 2


def strip_rect(width: int, height: int) -> tuple[float, float, float, float]:
    """The source rectangle the top band shows (the camera's whole width; see compose_cards)."""
    scaled = round(FRAME_W * height / width / 2) * 2
    top = max(0.0, (scaled - SPLIT_TOP) * 0.35)
    return 0.0, top * width / FRAME_W, float(width), SPLIT_TOP * width / FRAME_W


def _move_frame(source: np.ndarray, base: np.ndarray, chart: np.ndarray | None, eased: float,
                start_rect: tuple[float, float, float, float] | None) -> np.ndarray:
    """One frame of the move between the full-screen speaker (eased = 0) and the split
    view (eased = 1): the speaker's picture zooms out and shrinks into the top band while
    the panel slides up from the bottom. ``start_rect``: the source rectangle the clip
    shows full screen; None when it's not a plain crop (the whole frame or the two-person
    split), then the full-screen picture stays put and the panel slides up over it."""
    src_h, src_w = source.shape[:2]
    band = FRAME_H + (SPLIT_TOP - FRAME_H) * eased  # where the panel's top edge is
    out = np.empty((FRAME_H, FRAME_W, 3), np.uint8)
    if start_rect is None:
        out[:] = base
    else:
        x0, y0, w0, h0 = start_rect
        x1, y1, w1, h1 = strip_rect(src_w, src_h)
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
    canvas = np.array([int(CANVAS[k:k + 2], 16) for k in (1, 3, 5)], np.uint8)
    edge = int(np.floor(band))
    if edge < FRAME_H:
        out[edge + 1:] = canvas
        cover = 1.0 - (band - edge)  # the edge row: partly covered (a soft, sub-pixel edge)
        out[edge] = (out[edge] * (1 - cover) + canvas * cover).astype(np.uint8)
    if chart is not None and band < FRAME_H:
        shifted = cv2.warpAffine(chart, np.float32([[1, 0, 0], [0, 1, band]]), (FRAME_W, FRAME_H),
                                 flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
        alpha = shifted[:, :, 3:4].astype(np.float32) / 255
        out[:] = (out * (1 - alpha) + shifted[:, :, :3] * alpha).astype(np.uint8)
    return out


def split_moves(source_video: Path, base_clip: Path, edit_start: float, a: float, b: float, chart: Frames,
                crop_at: Callable[[float], tuple[float, float, float, float] | None] | None, folder: Path,
                rate: Fraction) -> list[tuple[int, int, str]]:
    """The frames of the move into (from ``a``) and out of (until ``b``) one split chart,
    as PNG sequences: [(first clip frame, frame count, pattern)]. Clip frames are
    n / ``rate``; the move takes MOVE seconds each way."""
    folder.mkdir(parents=True, exist_ok=True)
    count = max(2, round(MOVE * float(rate)))
    first_in = int(np.ceil(a * rate - 1e-6))
    last_out = int(np.ceil(b * rate - 1e-6))  # the first full-screen frame after the chart
    first_out = max(first_in + count, last_out - count)
    made = []
    for name, start_frame, rising in (("in", first_in, True), ("out", first_out, False)):
        times = [float((start_frame + j) / rate) for j in range(count)]
        sources = _decode(source_video, edit_start + times[0], count)
        bases = _decode(base_clip, times[0], count)
        if not sources or not bases:
            return []
        rect = crop_at(times[0] if rising else times[-1]) if crop_at else None
        for j, t in enumerate(times):
            progress = (j + 1) / (count + 1)
            eased = _ease(progress if rising else 1 - progress)
            index = min(chart.count - 1, max(0, int(round((t - a) * FPS))))
            page = cv2.imread(chart.pattern % index, cv2.IMREAD_UNCHANGED)
            page_rgba = None
            if page is not None:
                page_rgba = np.zeros((FRAME_H, FRAME_W, 4), np.uint8)
                page_rgba[:page.shape[0], :, :] = cv2.cvtColor(page, cv2.COLOR_BGRA2RGBA)[:FRAME_H]
            frame = _move_frame(sources[j], bases[j], page_rgba, eased, rect)
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
) -> Path:
    """Lay ``edit``'s rendered graphics over ``base_clip`` (edit.start-edit.end, ideally
    rendered without captions while graphics are up: words_outside())."""
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
    ass = calm_captions(words, edit, work / f"calm_{uuid.uuid4().hex}.ass", seams)

    rate = _frame_rate(base_clip)
    inputs = ["-i", str(base_clip)]
    graph = ["[0:v]format=yuv420p[v0]"]  # the clip's own frame rate: no repeated or dropped frames
    next_input = 1
    if splits:
        inputs += ["-ss", f"{edit.start:.3f}", "-t", f"{duration:.3f}", "-i", str(source_video)]
        labels = "".join(f"[sp{k}]" for k in splits)
        # The speaker's strip: a 2.35:1 band of the frame, a little above centre so heads
        # (which sit high in an interview frame) aren't cut.
        graph.append(f"[1:v]scale={FRAME_W}:{SPLIT_TOP}:force_original_aspect_ratio=increase:flags=lanczos,"
                     f"crop={FRAME_W}:{SPLIT_TOP}:0:'(ih-oh)*0.35',setsar=1,split={len(splits)}{labels}")
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
            graph.append(f"color=c={_hex(CANVAS)}:s={FRAME_W}x{FRAME_H}:r={rate}:d={duration:.3f}[cv{k}]")
            graph.append(f"[cv{k}][sp{k}]overlay=0:0[sb{k}]")
            graph.append(f"[sb{k}][{graphic}]overlay=0:{SPLIT_TOP}:eof_action=repeat[split{k}]")
            # The move in and out (drawn frames, see split_moves); the steady split view
            # between them. Frame n of the clip is at n / rate; each part owns whole frames.
            moves = split_moves(source_video, base_clip, edit.start, a, b, seq, crop_at, work / f"move{k}", rate)
            if len(moves) == 2:
                steady = (moves[0][0] + moves[0][1], moves[1][0])
                on = f"between(t\\,{(steady[0] - 0.5) / rate:.4f}\\,{(steady[1] - 0.5) / rate:.4f})"
            graph.append(f"[{current}][split{k}]overlay=0:0:enable='{on}'[v{n + 1}]")
            current, n = f"v{n + 1}", n + 1
            for first, count, pattern in moves:
                inputs += ["-framerate", str(rate), "-i", pattern]
                graph.append(f"[{next_input}:v]setpts=PTS-STARTPTS+{float(first / rate):.4f}/TB[mv{n}]")
                window = f"between(t\\,{(first - 0.5) / rate:.4f}\\,{(first + count - 0.5) / rate:.4f})"
                graph.append(f"[{current}][mv{n}]overlay=0:0:eof_action=pass:enable='{window}'[v{n + 1}]")
                next_input += 1
                current, n = f"v{n + 1}", n + 1
    graph.append(f"[{current}]ass='{ass.name}':fontsdir='{FONTS}'[out]")
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs,
        "-filter_complex", ";".join(graph), "-map", "[out]", "-map", "0:a?",
        *_video_encoder_args(ffmpeg), "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
        "-t", f"{duration:.3f}", str(output),
    ]
    try:
        subprocess.run(command, check=True, cwd=str(work), capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Adding the graphics failed: {exc.stderr.strip()[-700:]}") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return output


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
) -> tuple[Path | None, list[Any]]:
    """Check every card, render the ones that pass, have ``render_base(words)`` render
    the clip with those words' captions (none during graphics), and compose.
    ``hook_seconds``: how long the title card shows, so no graphic starts on top of it.
    ``seams``: the clip's two-person split spans (clip seconds), for the calm captions.
    ``crop_at(t)``: the source rectangle the clip shows at clip second t (for the move into
    a split chart to start from exactly that picture); None where it isn't a plain crop.
    Returns (the clip with graphics, or None when no card passed; one CardCheck per card)."""
    from core.card_checks import check_edit
    from core.cards import CardRenderError, render_card

    checks = check_edit(edit, words, hook_seconds)
    passed, frames = [], []
    for card, check in zip(edit.cards, checks):
        if not check.ok:
            continue
        try:
            frames.append(render_card(card, edit.brand, Path(work_dir) / "cards"))
        except CardRenderError as exc:
            check.errors.append(str(exc))
            continue
        passed.append(card)
    if not passed:
        return None, checks
    keep = ClipEdit(start=edit.start, end=edit.end, cards=passed, brand=edit.brand)
    # The bold captions give way (to the small ones) only under graphics that would sit on them.
    quiet = ClipEdit(start=edit.start, end=edit.end, brand=edit.brand,
                     cards=[card for card in passed if not keeps_bold_captions(card, edit, seams)])
    base = render_base(words_outside(words, quiet))
    calm = [w for w in words if any(_during(w, card) for card in quiet.cards)]
    return compose_cards(Path(base), source_video, keep, frames, calm, output, seams, crop_at), checks
