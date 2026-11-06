"""Round 27: putting the animated graphics on the clip.

- overlay graphics (a number or range, wireframe boxes): the speaker stays full-screen;
  the graphic's frames go over the video, and the video behind each box is blurred
  (the frosted-glass look) while the box is up.
- split charts: the frame cuts, on the sentence, to the speaker on top (the whole camera
  frame at 1080x608: no crop, two-person shots keep both people) and the chart below.
  The chart's lines and bars are already moving on the first frame: never an empty box.
- While a graphic is up, the bold word-by-word captions give way to small, calm,
  sentence-case captions near the bottom, so the number isn't shown twice.
- Leaving: every graphic slides down and fades over its last 0.25 s, and the split view
  dissolves back into the full-screen speaker at the same time: no snap back.

render_with_cards() checks every card (core.card_checks), renders the ones that pass
(core.cards), asks for a base clip without captions during the graphics, then composes.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from core.cards import CANVAS, EXIT, FPS, SPLIT_TOP, Frames
from core.edl import MODES, ClipEdit
from core.video_renderer import _video_encoder_args

FRAME_W, FRAME_H = 1080, 1920
FONTS = Path(__file__).resolve().parents[1] / "assets" / "fonts"
ENTER = 0.3  # seconds (9 frames): the split view dissolves in
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


def calm_captions(words: list[Any], edit: ClipEdit, path: Path) -> Path:
    """Small sentence-case captions on a soft dark box, for the time graphics are up."""
    events: list[tuple[float, float, str]] = []
    for card in edit.cards:
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
    for start, end, text in events:
        safe = text.replace("\\", "").replace("{", "(").replace("}", ")")
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


def compose_cards(
    base_clip: Path,
    source_video: Path,
    edit: ClipEdit,
    frames: list[Frames],
    words: list[Any],
    output: Path,
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
    ass = calm_captions(words, edit, work / f"calm_{uuid.uuid4().hex}.ass")

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
            graph.append(f"color=c={_hex(CANVAS)}:s={FRAME_W}x{FRAME_H}:r={FPS}:d={duration:.3f}[cv{k}]")
            graph.append(f"[cv{k}][sp{k}]overlay=0:0[sb{k}]")
            # Coming in, the split view dissolves in over ENTER seconds (the chart then draws
            # itself); leaving, it dissolves back into the full-screen speaker over EXIT
            # seconds while the chart slides down and fades. No snap either way.
            graph.append(f"[sb{k}][{graphic}]overlay=0:{SPLIT_TOP}:eof_action=repeat,format=rgba,"
                         f"fade=t=in:st={a:.3f}:d={ENTER}:alpha=1,"
                         f"fade=t=out:st={b - EXIT - 1 / FPS:.3f}:d={EXIT}:alpha=1[split{k}]")
            graph.append(f"[{current}][split{k}]overlay=0:0:enable='{on}'[v{n + 1}]")
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
) -> tuple[Path | None, list[Any]]:
    """Check every card, render the ones that pass, have ``render_base(words)`` render
    the clip with those words' captions (none during graphics), and compose.
    ``hook_seconds``: how long the title card shows, so no graphic starts on top of it.
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
    base = render_base(words_outside(words, keep))
    return compose_cards(Path(base), source_video, keep, frames, words, output), checks
