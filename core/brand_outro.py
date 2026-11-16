"""Round 58: a client's branding on a finished clip, and a call-to-action end card.

finish() takes a rendered clip (core.pipeline.render_selected_clip) and writes ``<clip>_final.mp4``:
- the client's wordmark (or logo PNG) as a small pill just under the captions (y ~1300), from 0.3 s:
  clear of the topic header (top), the lower third (y 930), the captions (1010-1240) and the bottom
  ~400 px that Reels, Shorts and TikTok cover with their own buttons and text;
- then an end card (default 3 s): the client's accent colour, logo or wordmark, the CTA and a
  sub-line, fading in after the clip's last sentence; the sound fades out under the cut.
The .srt is copied alongside under the final name (the card comes after the last caption).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FONTS = ROOT / "assets" / "fonts"
DISPLAY = FONTS / "NavExec-InterDisplay-SemiBold.ttf"
TEXT = FONTS / "NavExec-InterText-Regular.ttf"
W, H = 1080, 1920
BUG_CENTER_Y = 1300


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    value = hex_colour.lstrip("#")
    return tuple(int(value[k:k + 2], 16) for k in (0, 2, 4))  # type: ignore[return-value]


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def _tracked(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, font, fill, tracking: float) -> float:
    """Draw letter-spaced text from its left edge; returns its width."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x - xy[0] - tracking


def _tracked_width(draw, text, font, tracking) -> float:
    return sum(draw.textlength(ch, font=font) for ch in text) + tracking * (len(text) - 1)


def bug_png(client: str, path: Path, logo: str = "") -> Path:
    """The small brand pill, on a transparent 1080x1920 frame."""
    image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if logo and Path(logo).is_file():
        mark = Image.open(logo).convert("RGBA")
        mark.thumbnail((300, 64))
        pad_x, pad_y = 26, 14
        box = (W // 2 - mark.width // 2 - pad_x, BUG_CENTER_Y - mark.height // 2 - pad_y,
               W // 2 + mark.width // 2 + pad_x, BUG_CENTER_Y + mark.height // 2 + pad_y)
        draw.rounded_rectangle(box, radius=(box[3] - box[1]) // 2, fill=(10, 12, 16, 150))
        image.alpha_composite(mark, (W // 2 - mark.width // 2, BUG_CENTER_Y - mark.height // 2))
    else:
        font, tracking = _font(DISPLAY, 28), 4.0
        label = client.upper()
        width = _tracked_width(draw, label, font, tracking)
        top, bottom = font.getbbox(label)[1], font.getbbox(label)[3]
        pad_x, pad_y = 28, 14
        box = (W / 2 - width / 2 - pad_x, BUG_CENTER_Y - (bottom - top) / 2 - pad_y,
               W / 2 + width / 2 + pad_x, BUG_CENTER_Y + (bottom - top) / 2 + pad_y)
        draw.rounded_rectangle(box, radius=(box[3] - box[1]) / 2, fill=(10, 12, 16, 150))
        _tracked(draw, (W / 2 - width / 2, BUG_CENTER_Y - (bottom - top) / 2 - top), label, font,
                 (255, 255, 255, 235), tracking)
    image.save(path)
    return path


def card_png(client: str, cta: str, sub: str, accent: str, path: Path, logo: str = "") -> Path:
    """The end card: accent background (a slight darkening towards the bottom), the mark, a short
    rule, the CTA (up to 3 lines) and the sub-line."""
    r, g, b = _rgb(accent)
    image = Image.new("RGB", (W, H), (r, g, b))
    shade = Image.new("L", (1, H))
    shade.putdata([int(90 * y / H) for y in range(H)])
    image.paste(Image.new("RGB", (W, H), (0, 0, 0)), (0, 0), shade.resize((W, H)))
    draw = ImageDraw.Draw(image)
    y = 640
    if logo and Path(logo).is_file():
        mark = Image.open(logo).convert("RGBA")
        mark.thumbnail((560, 220))
        image.paste(mark, (W // 2 - mark.width // 2, y - mark.height // 2), mark)
        y += mark.height // 2 + 70
    else:
        font, tracking = _font(DISPLAY, 46), 9.0
        label = client.upper()
        width = _tracked_width(draw, label, font, tracking)
        _tracked(draw, (W / 2 - width / 2, y - 30), label, font, (255, 255, 255), tracking)
        y += 70
    draw.rectangle((W / 2 - 40, y, W / 2 + 40, y + 4), fill=(255, 255, 255))
    y += 70
    font = _font(DISPLAY, 70)
    for line in textwrap.wrap(cta, 22)[:3]:
        width = draw.textlength(line, font=font)
        draw.text((W / 2 - width / 2, y), line, font=font, fill=(255, 255, 255))
        y += 88
    if sub:
        y += 30
        font = _font(TEXT, 40)
        for line in textwrap.wrap(sub, 36)[:2]:
            width = draw.textlength(line, font=font)
            draw.text((W / 2 - width / 2, y), line, font=font, fill=(225, 230, 238))
            y += 54
    image.save(path)
    return path


def _probe(path: Path) -> tuple[float, str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=r_frame_rate",
                          "-select_streams", "v:0", "-of", "json", str(path)], capture_output=True, text=True, check=True)
    info = json.loads(out.stdout)
    return float(info["format"]["duration"]), info["streams"][0]["r_frame_rate"]


def finish(clip: Path, client: str, cta: str, *, sub: str = "", accent: str = "#1F3A5F", logo: str = "",
           card_seconds: float = 3.0) -> Path:
    """``<clip>_final.mp4``: the clip with the brand pill, then the end card. Returns its path."""
    from core.video_renderer import _video_encoder_args

    clip = Path(clip)
    work = clip.parent / f".{clip.stem}_brand"
    work.mkdir(exist_ok=True)
    try:
        bug = bug_png(client, work / "bug.png", logo)
        card = card_png(client, cta, sub, accent, work / "card.png", logo)
        duration, rate = _probe(clip)
        final = clip.with_name(f"{clip.stem}_final.mp4")
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        fade_at = max(0.0, duration - 0.3)
        graph = ";".join([
            "[1:v]format=rgba,fade=in:st=0.3:d=0.4:alpha=1[bug]",
            "[0:v][bug]overlay=0:0:shortest=1,setsar=1,format=yuv420p[main]",
            f"[2:v]scale={W}:{H},fps={rate},format=yuv420p,fade=in:st=0:d=0.35,setsar=1[card]",
            f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,afade=t=out:st={fade_at:.3f}:d=0.3[a0]",
            f"anullsrc=r=48000:cl=stereo,atrim=0:{card_seconds:.3f}[a1]",
            "[main][a0][card][a1]concat=n=2:v=1:a=1[v][a]",
        ])
        command = [ffmpeg, "-y", "-v", "error", "-i", str(clip), "-loop", "1", "-i", str(bug),
                   "-loop", "1", "-t", f"{card_seconds:.3f}", "-i", str(card), "-filter_complex", graph,
                   "-map", "[v]", "-map", "[a]", *_video_encoder_args(ffmpeg), "-pix_fmt", "yuv420p",
                   "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                   "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(final)]
        subprocess.run(command, check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=900)
        srt = clip.with_suffix(".srt")
        if srt.is_file():
            shutil.copy2(srt, final.with_suffix(".srt"))
        return final
    finally:
        shutil.rmtree(work, ignore_errors=True)
