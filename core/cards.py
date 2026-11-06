"""Round 27: the graphics library. Animated HTML/SVG, rendered frame by frame.

Every graphic is a small web page with a motion timeline (Web Animations): lines draw,
bars grow, rows dim, focus frames appear, each on the word it belongs to. Playwright
steps through that timeline at 30 fps and saves transparent PNG frames (frames where
nothing moves are hard links, so holds cost nothing); core.dynamic_layout lays them
over the clip.

One design system for all of them (the TOKENS below): a deep obsidian canvas, unlit
tracks one step lighter, crisp white, muted slate labels, one accent (gold by default),
desaturated editorial green and red for gains and losses. Inter for all text with
tabular digits; sharp corners on data, a rounded glass card only for names. No
wordmarks or footnotes: the graphic is the message. The one exception is a chart drawn
from a file you supply (candlestick_ema): it shows its source, because nobody said it.

Needs Playwright:  .venv/bin/pip install playwright  then  .venv/bin/playwright install chromium
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.card_checks import parse_value, signed_value
from core.edl import MODES, BrandKit, Card

FPS = 30
FRAME_W, FRAME_H = 1080, 1920
SPLIT_TOP = 460  # 24/76 split: the speaker's strip (a 2.35:1 band of the frame), the chart below
SPLIT_H = FRAME_H - SPLIT_TOP  # the chart page (76% of the frame)
CHART_H = 1312  # the builders' drawing area; content stays in its top 642 px
CHART_PAD = 136  # the drawing area starts this far below the strip: content at y 596-1238, centred
# between the strip and the captions (the bottom fifth stays clear: apps cover it)
W = FRAME_W

# Design tokens (the whole library uses only these).
CANVAS = "#050608"  # deep obsidian: reads as black, keeps unlit tracks and grid visible
TRACK = "#12151C"  # unlit bar tracks
GRID = "#1B1F28"
RULE = "#2A2F3A"  # axes and baselines
INK = "#F5F6F8"
SOFT = "#B4BAC5"  # labels
AXIS = "#7C8494"  # axis text (4.9:1 on the canvas)
GRAY = "#5B6270"  # neutral bars and flows
LIGHT = "#E6E9EE"
POS = "#4FB286"  # gains (desaturated editorial green)
NEG = "#E06C5F"  # losses (desaturated editorial red)
SHADES = [LIGHT, "#9AA2AF", "#6B7280", "#434955"]  # parts of a whole, after the accent
STROKE = 4  # lines and axes
FOCUS = 2  # focus frames
FONTS = Path(__file__).resolve().parents[1] / "assets" / "fonts"
MIN_CONTRAST = 4.5  # WCAG AA for text
EASE = "cubic-bezier(0.22, 1, 0.36, 1)"  # entrances: fast in, gentle settle
EASE_IN = "cubic-bezier(0.55, 0, 0.75, 0.2)"  # accelerating
EASE_OUT_EXIT = "cubic-bezier(0.65, 0, 0.35, 1)"  # exits: damped ease-in-out, no snap at either end
POP = "cubic-bezier(0.34, 1.56, 0.64, 1)"  # points popping in, a touch of overshoot
EXIT = 0.4  # seconds (12 frames): every graphic slides and fades out over its last 0.4 s


@dataclass
class Frames:
    """A rendered graphic: PNG frames (``pattern`` for FFmpeg, ``count``, 30 fps), its
    size and where it sits, and boxes to blur behind it (x, y, w, h, corner radius)."""

    pattern: str
    count: int
    width: int
    height: int
    top: int
    blur: list[tuple[int, int, int, int, int]] = field(default_factory=list)


class CardRenderError(RuntimeError):
    """The graphic doesn't fit or can't be drawn (the message says what to change)."""


def _luminance(color: str) -> float:
    color = color.lstrip("#")
    channels = [int(color[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first: str, second: str) -> float:
    """WCAG contrast ratio between two #RRGGBB colors (1 to 21)."""
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


class Motion:
    """A timeline of property changes (seconds from the graphic's start), one Web
    Animation per element and property."""

    def __init__(self) -> None:
        self.tracks: dict[tuple[str, str], list[tuple[float, str, str]]] = {}
        self.spans: list[tuple[float, float]] = []

    def tween(self, selector: str, prop: str, t0: float, t1: float, a: str, b: str, easing: str = EASE) -> None:
        t0, t1 = max(0.0, t0), max(0.0, t1)
        points = self.tracks.setdefault((selector, prop), [])
        points.append((t0, a, easing))
        points.append((t1, b, "linear"))
        self.spans.append((t0, t1))

    def fade_up(self, selector: str, t: float, rise: int = 14, length: float = 0.35) -> None:
        self.tween(selector, "opacity", t, t + length * 0.8, "0", "1")
        self.tween(selector, "transform", t, t + length, f"translateY({rise}px)", "translateY(0px)")

    def wipe_x(self, selector: str, t0: float, t1: float, x_from: float, x_to: float, width: float = W,
               easing: str = EASE) -> None:
        """Reveal left to right, from x_from to x_to (page pixels)."""
        self.tween(selector, "clipPath", t0, t1, f"inset(0 {100 * (width - x_from) / width:.3f}% 0 0)",
                   f"inset(0 {100 * (width - x_to) / width:.3f}% 0 0)", easing)

    def last(self) -> float:
        return max((t1 for _, t1 in self.spans), default=0.0)

    def moving(self, t: float) -> bool:
        return any(t0 - 0.5 / FPS <= t <= t1 + 1.5 / FPS for t0, t1 in self.spans)

    def js(self, total: float) -> list[dict]:
        out = []
        for (selector, prop), points in self.tracks.items():
            points = sorted(points, key=lambda p: p[0])
            frames = [{"offset": 0.0, prop: points[0][1], "easing": "linear"}]
            for t, value, easing in points:
                frames.append({"offset": min(1.0, t / total), prop: value, "easing": easing})
            frames.append({"offset": 1.0, prop: points[-1][1]})
            out.append({"sel": selector, "kf": frames, "ms": total * 1000})
        return out


def _base_css(width: int, height: int, stage_top: int = 0, stage_h: int | None = None) -> str:
    stage_h = stage_h or height
    return f"""
@font-face {{ font-family: Inter; src: url("{(FONTS / 'Inter-Variable.ttf').as_uri()}"); font-weight: 100 900; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ width: {width}px; height: {height}px; background: transparent; overflow: hidden; }}
body {{ font-family: Inter, sans-serif; color: {INK}; -webkit-font-smoothing: antialiased;
  font-variant-numeric: tabular-nums; font-feature-settings: "tnum" 1, "calt" 0; position: relative; }}
.stage {{ position: absolute; left: 0; top: {stage_top}px; width: {width}px; height: {stage_h}px; }}
.lyr {{ position: absolute; left: 0; top: 0; width: {width}px; height: {stage_h}px; }}
.title {{ position: absolute; left: 76px; right: 76px; top: 40px; text-align: center; font-size: 44px;
  font-weight: 760; letter-spacing: -.012em; white-space: nowrap; overflow: hidden; }}
.lab {{ font-size: 30px; font-weight: 560; color: {SOFT}; white-space: nowrap; }}
.val {{ font-size: 50px; font-weight: 760; letter-spacing: -.025em; white-space: nowrap; }}
.axis {{ font-size: 24px; font-weight: 520; color: {AXIS}; white-space: nowrap; }}
.src {{ position: absolute; left: 76px; top: 604px; font-size: 21px; font-weight: 500; color: {AXIS}; }}
"""


def _e(text: object) -> str:
    return html.escape(str(text or ""))


def _rel(card: Card, time: float | None, fallback: float) -> float:
    return max(0.0, (time - card.start) if time is not None else fallback)


def _title(card: Card, motion: Motion) -> str:
    if not card.headline:
        return ""
    motion.fade_up(".title", 0.0, rise=10, length=0.35)
    return f'<div class="title fit">{_e(card.headline)}</div>'


def _layer(layer_id: str, inner: str, width: int = W, height: int = CHART_H) -> str:
    return (f'<div class="lyr" id="{layer_id}"><svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f"{inner}</svg></div>")


def _split_range(value: str) -> tuple[str, str]:
    """'$850–$1,700' -> ('$850', '$1,700'); '96 hrs' -> ('96 hrs', '')."""
    parts = re.split(r"\s*[–—]\s*|\s+-\s+|(?<=\d)-(?=[$\d~])", value, maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (value.strip(), "")


def _top(card: Card) -> float:
    return max((max(parse_value(r.value).numbers or [0.0]) for r in card.rows), default=0.0) or 1.0


# ------------------------------------------------------------ overlays (speaker full-screen)
ARROW = re.compile(r"\s*(?:→|->|=>)\s*")


def _kinetic_range(card: Card, accent: str) -> tuple[str, str, Motion]:
    row = card.rows[0]
    change = ARROW.search(row.value)
    first, second = ([part.strip() for part in ARROW.split(row.value, maxsplit=1)] if change
                     else _split_range(row.value))
    color = accent if row.highlight else INK
    css = f"""
.scrim {{ position: absolute; left: 0; right: 0; top: 860px; bottom: 0;
  background: linear-gradient(180deg, rgba(0,0,0,0) 0%, rgba(0,0,0,.42) 42%, rgba(0,0,0,.55) 100%); }}
.wrap {{ position: absolute; left: 60px; right: 60px; top: 1010px; text-align: center; }}
.nums {{ display: inline-flex; align-items: baseline; gap: 26px; white-space: nowrap; font-weight: 800;
  font-size: 150px; letter-spacing: -.045em; line-height: 1; color: {color}; text-shadow: 0 6px 28px rgba(0,0,0,.45);
  max-width: 960px; overflow: hidden; }}
.dash {{ font-weight: 500; opacity: .9; }}
.ctx {{ margin-top: 26px; font-size: 46px; font-weight: 600; line-height: 1.18; color: rgba(255,255,255,.94);
  text-shadow: 0 3px 16px rgba(0,0,0,.5); }}
"""
    joiner = "→" if change else "–"
    rest = f'<span class="rest"><span class="dash">{joiner}</span><span>{_e(second)}</span></span>' if second else ""
    body = (f'<div class="scrim"></div><div class="wrap"><div class="nums fit"><span class="a">{_e(first)}</span>'
            f'{rest}</div><div class="ctx">{_e(row.label)}</div></div>')
    m = Motion()
    t = _rel(card, row.reveal_at, 0.0)
    m.tween(".scrim", "opacity", 0.0, 0.3, "0", "1")
    m.fade_up(".a", t, rise=22, length=0.45)
    last = t
    if second:
        t2 = max(t + 0.25, _rel(card, row.reveal_to, t + 0.5))
        m.tween(".rest", "clipPath", t2, t2 + 0.45, "inset(0 100% 0 0)", "inset(0 0% 0 0)")
        last = t2
    if row.label:
        m.fade_up(".ctx", last + 0.45, rise=12, length=0.45)
    return css, body, m


def _buckets(card: Card, accent: str) -> tuple[str, str, Motion]:
    css = f"""
.row {{ position: absolute; left: 40px; right: 40px; top: 1200px; display: flex; justify-content: center; gap: 22px; }}
.box {{ min-width: 190px; white-space: nowrap; padding: 20px 32px; border: {FOCUS}px solid rgba(255,255,255,.96);
  background: rgba(255,255,255,.12); color: #FFFFFF; font-size: 40px; font-weight: 650; line-height: 1.08;
  text-align: center; text-shadow: 0 2px 10px rgba(0,0,0,.35); }}
.box.dashed {{ border-style: dashed; }}
"""
    boxes = "".join(f'<div class="box blur{" dashed" if row.dashed else ""}" id="b{k}">{_e(row.label)}</div>'
                    for k, row in enumerate(card.rows))
    m = Motion()
    times = [_rel(card, row.reveal_at, 0.15 + 0.5 * k) for k, row in enumerate(card.rows)]
    for k, t in enumerate(times):
        m.fade_up(f"#b{k}", t, rise=16, length=0.4)
        m.tween(f"#b{k}", "borderColor", t, t + 0.22, "rgba(255,255,255,.96)", accent)
        m.tween(f"#b{k}", "fontWeight", t, t + 0.22, "650", "800")
        if k + 1 < len(times):  # the box being talked about is gold; the one before goes back to white
            m.tween(f"#b{k}", "borderColor", times[k + 1], times[k + 1] + 0.22, accent, "rgba(255,255,255,.96)")
            m.tween(f"#b{k}", "fontWeight", times[k + 1], times[k + 1] + 0.22, "800", "650")
    return css, f'<div class="row">{boxes}</div>', m


NAME_CARD_TOP = 1150  # the standard spot: lower-middle, above the captions
NAME_CARD_SIZE = (940, 166)  # the largest card (width, height) at scale 1


def name_card_size(name: str, role: str, scale: float = 1.0) -> tuple[int, int]:
    """About how big a name card is, in output pixels (to place it before drawing)."""
    width = 120 + max(len(name) * 31.0, len(role) * 16.6)
    return int(min(NAME_CARD_SIZE[0], max(560, width)) * scale), int(NAME_CARD_SIZE[1] * scale)


def parse_place(place: str) -> tuple[float, float, float, str]:
    """(x, y, scale, align) from Card.place; the standard spot when empty or unreadable."""
    try:
        x, y, scale, align = place.split(",")
        if align in {"center", "left", "right"}:
            return float(x), float(y), max(0.5, min(1.0, float(scale))), align
    except (AttributeError, ValueError):
        pass
    return W / 2, NAME_CARD_TOP, 1.0, "center"


def _speaker_title(card: Card, accent: str) -> tuple[str, str, Motion]:
    row = card.rows[0]
    x, y, scale, align = parse_place(card.place)
    if align == "left":
        wrap = f"left: {x:.0f}px; justify-content: flex-start; transform-origin: top left;"
    elif align == "right":
        wrap = f"right: {W - x:.0f}px; justify-content: flex-end; transform-origin: top right;"
    else:
        wrap = f"left: {x - W / 2:.0f}px; width: {W}px; justify-content: center; transform-origin: top center;"
    css = (f".ltwrap {{ position: absolute; top: {y:.0f}px; display: flex; {wrap} transform: scale({scale:.3f}); }}"
           + """
.lt { position: relative; min-width: 560px; max-width: 940px; padding: 30px 60px 32px; border-radius: 30px;
  background: rgba(16,18,24,.40); box-shadow: 0 14px 44px rgba(0,0,0,.32); text-align: left; }
.lt svg { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
.name { font-size: 56px; font-weight: 720; letter-spacing: -.015em; color: #FFFFFF; white-space: nowrap; line-height: 1.05; }
.role { margin-top: 8px; font-size: 30px; font-weight: 520; color: #CDD2DB; white-space: nowrap; }
""")
    border = ('<svg><defs><linearGradient id="spec" x1="0" y1="0" x2="1" y2="1">'
              '<stop offset="0" stop-color="#fff" stop-opacity=".7"/><stop offset=".45" stop-color="#fff" stop-opacity=".1"/>'
              '<stop offset="1" stop-color="#fff" stop-opacity=".4"/></linearGradient></defs>'
              '<rect x="0" y="0" width="100%" height="100%" rx="30" fill="none" '
              'stroke="url(#spec)" stroke-width="1.5"/></svg>')
    body = (f'<div class="ltwrap"><div class="lt blur round" id="lt">{border}<div class="name fit">{_e(row.label)}</div>'
            f'<div class="role">{_e(row.detail)}</div></div></div>')
    m = Motion()
    m.tween("#lt", "opacity", 0.0, 0.3, "0", "1")
    m.tween("#lt", "transform", 0.0, 0.42, "translateY(18px) scale(.98)", "translateY(0px) scale(1)")
    m.fade_up(".role", 0.12, rise=8, length=0.4)
    return css, body, m


# ------------------------------------------------------------ split charts
def _timeline(card: Card, accent: str) -> tuple[str, str, Motion]:
    n = len(card.rows)
    left, right, axis_y = 190, W - 190, 318
    xs = [left + (right - left) * k / (n - 1) for k in range(n)]
    css = f"""
.track, .progress {{ position: absolute; top: {axis_y - 2}px; height: {STROKE}px; left: {left}px; width: {right - left}px; }}
.track {{ background: {RULE}; }} .progress {{ background: {INK}; }}
.node {{ position: absolute; top: {axis_y - 13}px; width: 26px; height: 26px; margin-left: -13px; border-radius: 50%;
  background: {CANVAS}; border: {STROKE}px solid {INK}; }}
.ring {{ position: absolute; top: {axis_y - 30}px; width: 60px; height: 60px; margin-left: -30px; border-radius: 50%;
  background: {accent}; opacity: 0; }}
.when {{ position: absolute; top: {axis_y - 112}px; width: 320px; margin-left: -160px; text-align: center;
  font-size: 46px; font-weight: 780; letter-spacing: -.02em; white-space: nowrap; }}
.what {{ position: absolute; top: {axis_y + 46}px; width: 300px; margin-left: -150px; text-align: center;
  font-size: 31px; font-weight: 520; line-height: 1.22; color: {SOFT}; max-height: 80px; overflow: hidden; }}
"""
    m = Motion()
    parts = [_title(card, m), '<div class="track"></div><div class="progress"></div>']
    for k, (x, row) in enumerate(zip(xs, card.rows)):
        parts.append(f'<div class="ring" id="r{k}" style="left:{x:.0f}px"></div><div class="node" id="n{k}" '
                     f'style="left:{x:.0f}px"></div><div class="when fit" id="w{k}" style="left:{x:.0f}px">{_e(row.value)}'
                     f'</div><div class="what fitv" id="d{k}" style="left:{x:.0f}px">{_e(row.detail)}</div>')
    m.tween(".track", "clipPath", 0.0, 0.7, "inset(0 100% 0 0)", "inset(0 0% 0 0)")
    times = [_rel(card, row.reveal_at, 0.2 + 0.7 * k) for k, row in enumerate(card.rows)]
    span = right - left
    for k, t in enumerate(times):
        m.tween(f"#n{k}", "transform", t, t + 0.35, "scale(0)", "scale(1)", POP)
        m.fade_up(f"#w{k}", t)
        m.fade_up(f"#d{k}", t + 0.15, rise=8)
        m.tween(f"#n{k}", "backgroundColor", t, t + 0.25, CANVAS, accent)
        m.tween(f"#n{k}", "borderColor", t, t + 0.25, INK, accent)
        m.tween(f"#w{k}", "color", t, t + 0.25, INK, accent)
        m.tween(f"#r{k}", "opacity", t, t + 0.3, "0", ".22")
        if k + 1 < n:  # the newest point is the active one
            nxt = times[k + 1]
            m.tween(f"#n{k}", "backgroundColor", nxt, nxt + 0.25, accent, INK)
            m.tween(f"#n{k}", "borderColor", nxt, nxt + 0.25, accent, INK)
            m.tween(f"#w{k}", "color", nxt, nxt + 0.25, accent, INK)
            m.tween(f"#r{k}", "opacity", nxt, nxt + 0.25, ".22", "0")
        if k:
            m.tween(".progress", "clipPath", max(t - 0.45, times[k - 1] + 0.05), t,
                    f"inset(0 {100 - 100 * (xs[k - 1] - left) / span:.2f}% 0 0)",
                    f"inset(0 {100 - 100 * (xs[k] - left) / span:.2f}% 0 0)")
        else:
            m.tween(".progress", "clipPath", 0.0, 0.01, "inset(0 100% 0 0)", "inset(0 100% 0 0)")
    return css, "".join(parts), m


def _bars_css(accent: str) -> str:
    return f"""
.rows {{ position: absolute; left: 76px; right: 76px; top: 128px; }}
.r {{ margin-bottom: 30px; }}
.line {{ display: flex; justify-content: space-between; align-items: baseline; gap: 24px; }}
.r .lab {{ overflow: hidden; }}
.r.hl .val {{ color: {accent}; }}
.bar {{ position: relative; height: 16px; margin-top: 12px; background: {TRACK}; }}
.fill {{ position: absolute; left: 0; top: 0; bottom: 0; }}
.solid {{ position: absolute; left: 0; top: 0; bottom: 0; background: {LIGHT}; }}
.r.hl .solid {{ background: {accent}; }}
.range {{ position: absolute; top: 0; bottom: 0;
  background: repeating-linear-gradient(135deg, rgba(230,233,238,.6) 0 3px, transparent 3px 7px); }}
.r.hl .range {{ background: repeating-linear-gradient(135deg, {accent}B0 0 3px, transparent 3px 7px); }}
"""


def _bar_rows(card: Card) -> str:
    peak, out = _top(card), []
    for k, row in enumerate(card.rows):
        numbers = parse_value(row.value).numbers or [0.0]
        low, high = min(numbers), max(numbers)
        solid = 100 * low / high if high else 0
        out.append(f'<div class="r{" hl" if row.highlight else ""}" id="row{k}"><div class="line">'
                   f'<span class="lab fit">{_e(row.label)}</span><span class="val" id="v{k}">{_e(row.value)}</span></div>'
                   f'<div class="bar"><div class="fill" id="f{k}" style="width:{100 * high / peak:.3f}%">'
                   f'<div class="solid" style="width:{solid:.3f}%"></div>'
                   f'<div class="range" style="left:{solid:.3f}%;width:{100 - solid:.3f}%"></div></div></div></div>')
    return "".join(out)


def _metric_bars(card: Card, accent: str) -> tuple[str, str, Motion]:
    m = Motion()
    body = _title(card, m) + f'<div class="rows">{_bar_rows(card)}</div>'
    for k, row in enumerate(card.rows):
        m.tween(f"#row{k} .lab", "opacity", 0.05 * k, 0.3 + 0.05 * k, "0", "1")
        m.tween(f"#row{k} .bar", "clipPath", 0.05 * k, 0.5 + 0.05 * k, "inset(0 100% 0 0)", "inset(0 0% 0 0)")
        t = _rel(card, row.reveal_at, 0.3 + 0.5 * k)
        m.fade_up(f"#v{k}", t)
        m.tween(f"#f{k}", "clipPath", t, t + 0.8, "inset(0 100% 0 0)", "inset(0 0% 0 0)")
    return _bars_css(accent), body, m


def _ranked_bars(card: Card, accent: str) -> tuple[str, str, Motion]:
    m = Motion()
    body = _title(card, m) + f'<div class="rows">{_bar_rows(card)}</div>'
    n = len(card.rows)
    for k in range(n):
        m.tween(f"#row{k}", "opacity", 0.06 * k, 0.3 + 0.06 * k, "0", "1")
        m.tween(f"#f{k}", "clipPath", 0.06 * k, 0.8 + 0.06 * k, "inset(0 100% 0 0)", "inset(0 0% 0 0)")
    focus = sorted((max(_rel(card, r.reveal_at, 0.0), 0.9), k) for k, r in enumerate(card.rows) if r.reveal_at is not None)
    for step, (t, active) in enumerate(focus):  # the row being said at full strength, the rest at 25%
        for k in range(n):
            before = "1" if step == 0 or focus[step - 1][1] == k else ".25"
            after = "1" if k == active else ".25"
            if before != after:
                m.tween(f"#row{k}", "opacity", t, t + 0.3, before, after)
    return _bars_css(accent), body, m


def _diverging_bar(card: Card, accent: str) -> tuple[str, str, Motion]:
    first, second = card.rows
    a, b = max(parse_value(first.value).numbers or [0.0]), max(parse_value(second.value).numbers or [0.0])
    share = 100 * a / (a + b) if a + b else 50.0
    main = 0 if first.highlight or not second.highlight else 1
    color = [accent if main == 0 else "#C9CED6", accent if main == 1 else "#C9CED6"]
    focus_left = 76 - 12 if main == 0 else 76 + 928 * share / 100 - 9
    focus_w = (928 * share / 100 + 21) if main == 0 else (928 * (100 - share) / 100 + 21)
    css = f"""
.sides {{ position: absolute; left: 76px; right: 76px; top: 150px; display: flex; justify-content: space-between; }}
.side {{ display: flex; flex-direction: column; gap: 6px; }} .side.r {{ text-align: right; }}
#s0 .val {{ color: {color[0]}; font-size: 64px; }} #s1 .val {{ color: {color[1]}; font-size: 64px; }}
.dbar {{ position: absolute; left: 76px; right: 76px; top: 330px; height: 76px; }}
.seg {{ position: absolute; top: 0; bottom: 0; }}
#g0 {{ left: 0; width: calc({share:.3f}% - 3px); background: {color[0]}; }}
#g1 {{ right: 0; width: calc({100 - share:.3f}% - 3px); background: {color[1]}; }}
.focus {{ position: absolute; top: 314px; height: 108px; border: {FOCUS}px solid {INK}; opacity: 0;
  left: {focus_left:.0f}px; width: {focus_w:.0f}px; }}
"""
    m = Motion()
    body = (_title(card, m) + f'<div class="sides"><div class="side" id="s0"><span class="lab">{_e(first.label)}</span>'
            f'<span class="val">{_e(first.value)}</span></div><div class="side r" id="s1"><span class="lab">'
            f'{_e(second.label)}</span><span class="val">{_e(second.value)}</span></div></div>'
            '<div class="dbar"><div class="seg" id="g0"></div><div class="seg" id="g1"></div></div><div class="focus"></div>')
    m.tween("#s0", "opacity", 0.1, 0.4, "0", "1")
    m.tween("#s1", "opacity", 0.25, 0.55, "0", "1")
    m.tween("#g0", "clipPath", 0.0, 0.8, "inset(0 100% 0 0)", "inset(0 0% 0 0)")
    m.tween("#g1", "clipPath", 0.15, 0.95, "inset(0 0 0 100%)", "inset(0 0 0 0%)")
    t = max(1.1, _rel(card, card.rows[main].reveal_at, 1.1))
    m.tween(f"#g{1 - main}", "opacity", t, t + 0.35, "1", ".3")
    m.tween(f"#s{1 - main}", "opacity", t, t + 0.35, "1", ".35")
    m.tween(".focus", "opacity", t, t + 0.25, "0", "1")
    m.tween(".focus", "transform", t, t + 0.35, "scale(1.04)", "scale(1)")
    return css, body, m


def _share_split(card: Card, accent: str) -> tuple[str, str, Motion]:
    values = [max(parse_value(r.value).numbers or [0.0]) for r in card.rows]
    total = sum(values) or 1.0
    shades = iter(SHADES)
    colors = [accent if r.highlight else next(shades) for r in card.rows]
    css = f"""
.sbar {{ position: absolute; left: 76px; width: 928px; top: 150px; height: 86px; }}
.sseg {{ position: absolute; top: 0; bottom: 0; }}
.legend {{ position: absolute; left: 76px; right: 76px; top: 290px; }}
.li {{ display: flex; align-items: center; gap: 22px; height: 70px; border-bottom: 1px solid {GRID}; }}
.sw {{ width: 22px; height: 22px; flex: none; }}
.li .lab {{ flex: 1; color: {INK}; font-weight: 600; overflow: hidden; }}
.li .val {{ font-size: 44px; }}
.sfocus {{ position: absolute; top: 138px; height: 110px; border: {FOCUS}px solid {INK}; opacity: 0; }}
"""
    m = Motion()
    parts, x = [_title(card, m), '<div class="sbar">'], 0.0
    hl = next((k for k, r in enumerate(card.rows) if r.highlight), None)
    focus = ""
    for k, (value, color) in enumerate(zip(values, colors)):
        width = 928 * value / total
        parts.append(f'<div class="sseg" id="q{k}" style="left:{x:.1f}px;width:{max(0.0, width - 3):.1f}px;background:{color}"></div>')
        if k == hl:
            focus = f'<div class="sfocus" style="left:{76 + x - 10:.0f}px;width:{width + 17:.0f}px"></div>'
        x += width
    parts.append("</div>" + focus + '<div class="legend">')
    for k, (row, color) in enumerate(zip(card.rows, colors)):
        parts.append(f'<div class="li" id="li{k}"><span class="sw" style="background:{color}"></span>'
                     f'<span class="lab fit">{_e(row.label)}</span><span class="val">{_e(row.value)}</span></div>')
    parts.append("</div>")
    times = [_rel(card, r.reveal_at, 0.2 + 0.45 * k) for k, r in enumerate(card.rows)]
    for k, t in enumerate(times):
        m.tween(f"#q{k}", "clipPath", t, t + 0.5, "inset(0 100% 0 0)", "inset(0 0% 0 0)")
        m.fade_up(f"#li{k}", t, rise=10)
    if hl is not None:
        t = times[hl] + 0.6
        for k in range(len(card.rows)):
            if k != hl:
                m.tween(f"#q{k}", "opacity", t, t + 0.35, "1", ".35")
                m.tween(f"#li{k} .lab", "opacity", t, t + 0.35, "1", ".45")
        m.tween(".sfocus", "opacity", t, t + 0.25, "0", "1")
    return css, "".join(parts), m


def _trend_line(card: Card, accent: str) -> tuple[str, str, Motion]:
    values = [max(parse_value(r.value).numbers or [0.0]) for r in card.rows]
    n = len(values)
    left, right, base, top = 130, 950, 500, 190
    peak = max(values) * 1.12 or 1.0
    xs = [left + (right - left) * k / (n - 1) for k in range(n)]
    ys = [base - (base - top) * v / peak for v in values]
    line = " ".join(f"{'M' if k == 0 else 'L'}{x:.1f},{y:.1f}" for k, (x, y) in enumerate(zip(xs, ys)))
    area = line + f" L{xs[-1]:.1f},{base} L{xs[0]:.1f},{base} Z"
    css = f"""
.pt {{ position: absolute; width: 22px; height: 22px; margin: -11px 0 0 -11px; border-radius: 50%; background: {INK};
  border: 5px solid {CANVAS}; }}
.pt.hl {{ background: {accent}; }}
.pv {{ position: absolute; width: 240px; margin-left: -120px; text-align: center; font-size: 40px; font-weight: 760;
  letter-spacing: -.02em; white-space: nowrap; }}
.pv.hl {{ color: {accent}; }}
.px {{ position: absolute; top: {base + 22}px; width: 200px; margin-left: -100px; text-align: center; }}
"""
    m = Motion()
    parts = [_title(card, m),
             _layer("base", f'<line x1="{left - 30}" y1="{base}" x2="{right + 30}" y2="{base}" stroke="{RULE}" stroke-width="2"/>'),
             _layer("area", f'<defs><linearGradient id="ag" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{accent}" '
                            f'stop-opacity=".22"/><stop offset="1" stop-color="{accent}" stop-opacity="0"/></linearGradient></defs>'
                            f'<path d="{area}" fill="url(#ag)"/>'),
             _layer("line", f'<path d="{line}" fill="none" stroke="{INK}" stroke-width="{STROKE}" stroke-linejoin="round"/>')]
    for k, (row, x, y) in enumerate(zip(card.rows, xs, ys)):
        hl = " hl" if row.highlight else ""
        parts.append(f'<div class="pt{hl}" id="p{k}" style="left:{x:.0f}px;top:{y:.0f}px"></div>'
                     f'<div class="pv{hl}" id="pv{k}" style="left:{x:.0f}px;top:{y - 74:.0f}px">{_e(row.value)}</div>'
                     f'<div class="px axis" id="px{k}" style="left:{x:.0f}px">{_e(row.label)}</div>')
    m.tween("#base", "clipPath", 0.0, 0.5, "inset(0 100% 0 0)", "inset(0 0% 0 0)")
    times = [_rel(card, r.reveal_at, 0.3 + 0.6 * k) for k, r in enumerate(card.rows)]
    for k, t in enumerate(times):
        m.tween(f"#p{k}", "transform", t, t + 0.35, "scale(0)", "scale(1)", POP)
        m.fade_up(f"#pv{k}", t + 0.05)
        m.tween(f"#px{k}", "opacity", t, t + 0.3, "0", "1")
        if k:
            t0 = max(t - 0.5, times[k - 1] + 0.05)
            for layer in ("#line", "#area"):
                m.wipe_x(layer, t0, t, xs[k - 1] + 1, xs[k] + 1)
        else:
            for layer in ("#line", "#area"):
                m.wipe_x(layer, 0.0, 0.01, xs[0], xs[0])
    return css, "".join(parts), m


def _arr_bridge(card: Card, accent: str) -> tuple[str, str, Motion]:
    rows = card.rows
    n = len(rows)
    start = max(parse_value(rows[0].value).numbers or [0.0])
    end = max(parse_value(rows[-1].value).numbers or [0.0])
    steps = [signed_value(r.value) for r in rows[1:-1]]
    levels, running = [start], start
    for step in steps:
        running += step
        levels.append(running)
    peak = max(levels + [end]) * 1.08 or 1.0
    base, top = 470, 150
    scale = (base - top) / peak
    slot = 928 / n
    bar_w = slot * 0.58
    css = f"""
.wb {{ position: absolute; }}
.wv {{ position: absolute; width: {slot:.0f}px; text-align: center; font-size: 34px; font-weight: 760;
  letter-spacing: -.02em; white-space: nowrap; }}
.wl {{ position: absolute; top: {base + 18}px; width: {slot - 8:.0f}px; text-align: center; font-size: 25px; font-weight: 560;
  color: {SOFT}; line-height: 1.15; max-height: 62px; overflow: hidden; }}
"""
    m = Motion()
    parts = [_title(card, m), _layer("wbase", f'<line x1="66" y1="{base}" x2="1014" y2="{base}" stroke="{RULE}" stroke-width="2"/>')]
    m.tween("#wbase", "clipPath", 0.0, 0.5, "inset(0 100% 0 0)", "inset(0 0% 0 0)")
    times = [_rel(card, r.reveal_at, 0.3 + 0.55 * k) for k, r in enumerate(rows)]
    previous_top = None
    for k, row in enumerate(rows):
        x = 76 + slot * k + (slot - bar_w) / 2
        if k == 0:
            y0, y1, color, grow = base - start * scale, base, GRAY, "up"
        elif k == n - 1:
            y0, y1, color, grow = base - end * scale, base, accent, "up"
        else:
            before, after = levels[k - 1], levels[k]
            y0, y1 = base - max(before, after) * scale, base - min(before, after) * scale
            color, grow = (POS, "up") if after >= before else (NEG, "down")
        clip_from = "inset(100% 0 0 0)" if grow == "up" else "inset(0 0 100% 0)"
        parts.append(f'<div class="wb" id="wb{k}" style="left:{x:.1f}px;top:{y0:.1f}px;width:{bar_w:.1f}px;'
                     f'height:{max(2.0, y1 - y0):.1f}px;background:{color}"></div>')
        value_color = INK if k in (0, n - 1) else color
        parts.append(f'<div class="wv" id="wv{k}" style="left:{76 + slot * k:.1f}px;top:{y0 - 50:.1f}px;color:{value_color}">'
                     f'{_e(row.value)}</div><div class="wl fitv" id="wl{k}" style="left:{76 + slot * k + 4:.1f}px">{_e(row.label)}</div>')
        t = times[k]
        m.tween(f"#wb{k}", "clipPath", t, t + 0.5, clip_from, "inset(0 0 0 0)")
        m.fade_up(f"#wv{k}", t + 0.1, rise=10)
        m.tween(f"#wl{k}", "opacity", t, t + 0.3, "0", "1")
        if previous_top is not None:  # dashed connector from the last bar's top to this bar
            level_y = previous_top
            x_from, x_to = 76 + slot * (k - 1) + (slot + bar_w) / 2, x
            parts.append(_layer(f"wc{k}", f'<line x1="{x_from:.1f}" y1="{level_y:.1f}" x2="{x_to:.1f}" y2="{level_y:.1f}" '
                                          f'stroke="#596070" stroke-width="2" stroke-dasharray="6 6"/>'))
            m.wipe_x(f"#wc{k}", t - 0.25, t, x_from, x_to + 1)
        previous_top = base - (levels[k] if k < n - 1 else end) * scale
    return css, "".join(parts), m


def _sankey_flow(card: Card, accent: str) -> tuple[str, str, Motion]:
    rows = card.rows
    values = [max(parse_value(r.value).numbers or [0.0]) for r in rows]
    by_label = {r.label: k for k, r in enumerate(rows)}
    parent = [by_label.get(r.parent, -1) if r.parent else -1 for r in rows]
    depth = [0] * len(rows)
    for k in range(len(rows)):
        p, d = parent[k], 0
        while p >= 0 and d < 4:
            d, p = d + 1, parent[p]
        depth[k] = d
    root = next(k for k in range(len(rows)) if parent[k] < 0)
    unit = 330 / (values[root] or 1.0)
    xs = {0: 220, 1: 560, 2: 870}
    node_w = 16
    tops: dict[int, float] = {root: 390.0 - 165.0}
    heights = {k: max(3.0, values[k] * unit) for k in range(len(rows))}
    for level in (1, 2):
        members = [k for k in range(len(rows)) if depth[k] == level]
        for p in sorted({parent[k] for k in members}):
            kids = [k for k in members if parent[k] == p]
            gap = 34 if level == 1 else 16
            total = sum(heights[k] for k in kids) + gap * (len(kids) - 1)
            centre = tops[p] + heights[p] / 2
            y = min(max(190.0 if level == 1 else 120.0, centre - total / 2), 610.0 - total)
            for k in kids:
                tops[k] = y
                y += heights[k] + gap
    # Leaf labels sit on one line beside their node, pushed apart so they never touch.
    label_y: dict[int, float] = {}
    for column in (1, 2):
        leaves = sorted((k for k in range(len(rows)) if depth[k] == column and not any(parent[j] == k for j in range(len(rows)))),
                        key=lambda k: tops[k])
        y = -1e9
        for k in leaves:
            y = max(tops[k] + heights[k] / 2 - 22, y + 48)
            label_y[k] = y
        overflow = (y + 44) - 620 if leaves else 0
        if overflow > 0:
            for k in leaves:
                label_y[k] -= overflow
    css = f"""
.nd {{ position: absolute; width: {node_w}px; }}
.sl {{ position: absolute; white-space: nowrap; }}
.sl .lab {{ font-size: 25px; display: block; }} .sl .val {{ font-size: 34px; display: block; }}
"""
    m = Motion()
    parts = [_title(card, m)]
    out_offset = {k: 0.0 for k in range(len(rows))}
    times = {}
    for k, row in enumerate(rows):
        t = _rel(card, row.reveal_at, 0.2 + 0.5 * k)
        times[k] = max(t, times.get(parent[k], 0.0) + 0.3) if parent[k] >= 0 else t
    for k in sorted(range(len(rows)), key=lambda j: (depth[j], tops[j])):
        x = xs[depth[k]]
        color = accent if rows[k].highlight else "#9AA2AF"
        parts.append(f'<div class="nd" id="nd{k}" style="left:{x}px;top:{tops[k]:.1f}px;height:{heights[k]:.1f}px;background:{color}"></div>')
        leaf = not any(parent[j] == k for j in range(len(rows)))
        if k == root:
            label_style = f"right:{W - x + 18}px;top:{tops[k] + heights[k] / 2 - 40:.0f}px;text-align:right"
        elif leaf:
            label_style = f"left:{x + node_w + 14}px;top:{label_y.get(k, tops[k]):.0f}px;display:flex;gap:12px;align-items:baseline"
        else:
            label_style = f"left:{x - 60}px;top:{tops[k] - 84:.0f}px"
        value_color = accent if rows[k].highlight else INK
        inline = ' style="display:inline"' if leaf and k != root else ""
        parts.append(f'<div class="sl" id="sl{k}" style="{label_style}"><span class="lab"{inline}>{_e(rows[k].label)}</span>'
                     f'<span class="val"{inline} style="color:{value_color}">{_e(rows[k].value)}</span></div>')
        t = times[k]
        if parent[k] >= 0:
            p = parent[k]
            x0, x1 = xs[depth[p]] + node_w, x
            y0a = tops[p] + out_offset[p]
            y0b = y0a + heights[k]
            out_offset[p] += heights[k]
            y1a, y1b = tops[k], tops[k] + heights[k]
            mid = (x0 + x1) / 2
            band = (f"M{x0},{y0a:.1f} C{mid},{y0a:.1f} {mid},{y1a:.1f} {x1},{y1a:.1f} L{x1},{y1b:.1f} "
                    f"C{mid},{y1b:.1f} {mid},{y0b:.1f} {x0},{y0b:.1f} Z")
            fill = accent if rows[k].highlight else GRAY
            parts.insert(1, _layer(f"bd{k}", f'<path d="{band}" fill="{fill}" fill-opacity="{.55 if rows[k].highlight else .42}"/>'))
            m.wipe_x(f"#bd{k}", t, t + 0.6, x0, x1 + 1)
            m.tween(f"#nd{k}", "transform", t + 0.45, t + 0.75, "scaleY(0)", "scaleY(1)")
            m.fade_up(f"#sl{k}", t + 0.45, rise=8)
        else:
            m.tween(f"#nd{k}", "transform", t, t + 0.45, "scaleY(0)", "scaleY(1)")
            m.fade_up(f"#sl{k}", t + 0.1, rise=8)
    return css, "".join(parts), m


def _cac_payback(card: Card, accent: str) -> tuple[str, str, Motion]:
    cost, months = card.rows
    left, right, base, top = 130, 1000, 520, 140
    x_be = (left + right) / 2  # break-even sits halfway: the axis runs to twice the payback period
    y_be = (base + top) / 2  # and the threshold halfway up
    css = f"""
.tlab {{ position: absolute; left: {left + 14}px; top: {y_be - 92:.0f}px; }}
.tlab .lab {{ display: block; font-size: 26px; }} .tlab .val {{ display: block; font-size: 40px; }}
.zone {{ position: absolute; font-size: 26px; font-weight: 600; letter-spacing: .02em; color: {SOFT}; }}
.be {{ position: absolute; left: {x_be:.0f}px; top: {y_be:.0f}px; width: 24px; height: 24px; margin: -12px 0 0 -12px;
  border-radius: 50%; background: {accent}; box-shadow: 0 0 0 10px {accent}33; }}
.betag {{ position: absolute; left: {x_be - 140:.0f}px; top: {y_be - 150:.0f}px; width: 280px; text-align: center;
  padding: 10px 0 12px; border: {FOCUS}px solid {accent}; background: {CANVAS}; }}
.betag .lab {{ display: block; font-size: 25px; color: {accent}; font-weight: 700; }}
.betag .val {{ display: block; font-size: 36px; }}
.xt {{ position: absolute; top: {base + 16}px; width: 160px; margin-left: -80px; text-align: center; }}
"""
    under = f"M{left},{base} L{x_be:.0f},{y_be:.0f} L{left},{y_be:.0f} Z"
    profit = f"M{x_be:.0f},{y_be:.0f} L{right},{top} L{right},{y_be:.0f} Z"
    m = Motion()
    parts = [_title(card, m),
             _layer("ax", f'<line x1="{left}" y1="{top - 20}" x2="{left}" y2="{base}" stroke="{RULE}" stroke-width="2"/>'
                          f'<line x1="{left}" y1="{base}" x2="{right + 20}" y2="{base}" stroke="{RULE}" stroke-width="2"/>'),
             _layer("zu", f'<path d="{under}" fill="{NEG}" fill-opacity=".16"/>'),
             _layer("zp", f'<path d="{profit}" fill="{POS}" fill-opacity=".16"/>'),
             _layer("th", f'<line x1="{left}" y1="{y_be:.0f}" x2="{right}" y2="{y_be:.0f}" stroke="{NEG}" stroke-width="2.5" '
                          f'stroke-dasharray="8 8"/>'),
             _layer("c1", f'<line x1="{left}" y1="{base}" x2="{x_be:.0f}" y2="{y_be:.0f}" stroke="{POS}" stroke-width="{STROKE + 1}"'
                          f' stroke-linecap="round"/>'),
             _layer("c2", f'<line x1="{x_be:.0f}" y1="{y_be:.0f}" x2="{right}" y2="{top}" stroke="{accent}" '
                          f'stroke-width="{STROKE + 1}" stroke-linecap="round"/>'),
             f'<div class="tlab" id="tl"><span class="lab">{_e(cost.label)}</span><span class="val">{_e(cost.value)}</span></div>',
             f'<div class="zone" id="zu1" style="left:{left + 40}px;top:{(y_be + base) / 2 + 20:.0f}px">Underwater</div>',
             f'<div class="zone" id="zp1" style="left:{x_be + 200:.0f}px;top:{y_be - 70:.0f}px">In profit</div>',
             f'<div class="be" id="be"></div><div class="betag" id="bt"><span class="lab">{_e(months.label or "Break-even")}</span>'
             f'<span class="val">{_e(months.value)}</span></div>',
             f'<div class="xt axis" style="left:{left}px">0</div><div class="xt axis" id="xp" style="left:{x_be:.0f}px">'
             f'{_e(months.value)}</div>']
    t_cost = _rel(card, cost.reveal_at, 0.3)
    t_be = max(t_cost + 0.6, _rel(card, months.reveal_at, t_cost + 1.0))
    m.tween("#ax", "opacity", 0.0, 0.3, "0", "1")
    m.wipe_x("#th", t_cost, t_cost + 0.6, left, right + 2)
    m.fade_up("#tl", t_cost)
    m.wipe_x("#c1", t_cost + 0.2, t_be, left - 4, x_be + 4, easing="cubic-bezier(0.4, 0, 0.2, 1)")
    m.wipe_x("#zu", t_cost + 0.2, t_be, left, x_be)
    m.tween("#zu1", "opacity", t_be - 0.4, t_be, "0", "1")
    m.tween("#be", "transform", t_be, t_be + 0.35, "scale(0)", "scale(1)", POP)
    m.fade_up("#bt", t_be + 0.1, rise=12)
    m.tween("#xp", "opacity", t_be, t_be + 0.3, "0", "1")
    m.wipe_x("#c2", t_be + 0.2, t_be + 1.0, x_be - 4, right + 6)
    m.wipe_x("#zp", t_be + 0.2, t_be + 1.0, x_be, right)
    m.tween("#zp1", "opacity", t_be + 0.7, t_be + 1.0, "0", "1")
    return css, "".join(parts), m


def read_prices(path: str) -> list[dict[str, float]]:
    """Rows of a price file (CSV with open, high, low, close and optional volume
    columns, oldest first). The last 60 rows are used."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            item = {k.strip().lower(): v for k, v in raw.items() if k}
            try:
                rows.append({key: float(str(item[key]).replace(",", "")) for key in ("open", "high", "low", "close")}
                            | {"volume": float(str(item.get("volume") or 0).replace(",", "") or 0)})
            except (KeyError, ValueError):
                continue
    return rows[-60:]


def _candlestick_ema(card: Card, accent: str) -> tuple[str, str, Motion]:
    prices = read_prices(card.data_path)
    n = len(prices)
    low = min(p["low"] for p in prices)
    high = max(p["high"] for p in prices)
    pad = (high - low) * 0.06 or 1.0
    low, high = low - pad, high + pad
    left, right, top, base = 76, 930, 140, 450
    vol_base, vol_h = 580, 80
    slot = (right - left) / n
    y = lambda v: base - (base - top) * (v - low) / (high - low)  # noqa: E731
    vmax = max(p["volume"] for p in prices) or 1.0
    candles, volume = [], []
    for k, p in enumerate(prices):
        cx = left + slot * (k + 0.5)
        color = POS if p["close"] >= p["open"] else NEG
        body_top, body_bottom = y(max(p["open"], p["close"])), y(min(p["open"], p["close"]))
        candles.append(f'<line x1="{cx:.1f}" y1="{y(p["high"]):.1f}" x2="{cx:.1f}" y2="{y(p["low"]):.1f}" stroke="{color}" stroke-width="2"/>'
                       f'<rect x="{cx - slot * 0.32:.1f}" y="{body_top:.1f}" width="{slot * 0.64:.1f}" '
                       f'height="{max(1.5, body_bottom - body_top):.1f}" fill="{color}"/>')
        h = vol_h * p["volume"] / vmax
        volume.append(f'<rect x="{cx - slot * 0.32:.1f}" y="{vol_base - h:.1f}" width="{slot * 0.64:.1f}" height="{h:.1f}" fill="{GRAY}"/>')
    period = 20 if n >= 30 else max(3, n // 3)
    alpha, ema, points = 2 / (period + 1), prices[0]["close"], []
    for k, p in enumerate(prices):
        ema = p["close"] if k == 0 else alpha * p["close"] + (1 - alpha) * ema
        points.append(f"{'M' if k == 0 else 'L'}{left + slot * (k + 0.5):.1f},{y(ema):.1f}")
    step = _nice_step((high - low) / 4)
    ticks, tick = [], math.ceil(low / step) * step
    while tick <= high:
        ticks.append(tick)
        tick += step
    grid = "".join(f'<line x1="{left}" y1="{y(t):.1f}" x2="{right}" y2="{y(t):.1f}" stroke="{GRID}" stroke-width="1"/>' for t in ticks)
    labels = "".join(f'<div class="axis" style="position:absolute;left:{right + 14}px;top:{y(t) - 15:.0f}px">{_fmt(t)}</div>'
                     for t in ticks)
    css = ""
    m = Motion()
    parts = [_title(card, m), _layer("cg", grid), _layer("cv", "".join(volume)), _layer("cc", "".join(candles)),
             _layer("ce", f'<path d="{" ".join(points)}" fill="none" stroke="{accent}" stroke-width="{STROKE}" stroke-linejoin="round"/>'),
             f'<div id="cl">{labels}<div class="axis" style="position:absolute;left:{right - 150}px;top:{top - 46}px;color:{accent}">'
             f'EMA ({period})</div></div>', f'<div class="src" id="cs">{_e("Source: " + card.source)}</div>']
    m.tween("#cg", "opacity", 0.0, 0.4, "0", "1")
    m.tween("#cl", "opacity", 0.1, 0.5, "0", "1")
    m.tween("#cs", "opacity", 0.2, 0.6, "0", "1")
    for layer in ("#cc", "#cv"):
        m.wipe_x(layer, 0.15, 1.75, left, right + 2, easing="cubic-bezier(0.4, 0, 0.2, 1)")
    m.wipe_x("#ce", 0.6, 2.3, left, right + 2, easing="cubic-bezier(0.4, 0, 0.2, 1)")
    return css, "".join(parts), m


def _nice_step(raw: float) -> float:
    exponent = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    return next(s * exponent for s in (1, 2, 2.5, 5, 10) if s * exponent >= raw)


def _drawdown_curve(card: Card, accent: str) -> tuple[str, str, Motion]:
    """A fall and the recovery, shown rather than told: a line climbs to its peak, drops
    by the percentage said (bracketed, in red) as it's said, then climbs back past the
    old peak as the recovery is said. The shape is a sketch: no axis, no dates; the only
    claims on it are the two the speaker made."""
    fall, recovery = card.rows
    left, peak_x, trough_x, cross_x, right = 120, 330, 560, 810, 960
    peak_y, trough_y = 250, 470
    end_y = peak_y - 0.22 * (trough_y - peak_y)
    start_y = peak_y + 70
    rise = f"M{left},{start_y} C{left + 90},{start_y - 10} {peak_x - 90},{peak_y} {peak_x},{peak_y}"
    drop = f"M{peak_x},{peak_y} C{peak_x + 80},{peak_y} {trough_x - 90},{trough_y} {trough_x},{trough_y}"
    back = (f"M{trough_x},{trough_y} C{trough_x + 110},{trough_y} {cross_x - 90},{peak_y + 10} {cross_x},{peak_y} "
            f"S{right - 40},{end_y} {right},{end_y}")
    middle = (peak_x + trough_x) / 2
    css = f"""
.seg {{ fill: none; stroke-width: {STROKE + 1}; stroke-linecap: round; stroke-dasharray: 1; stroke-dashoffset: 1; }}
.fallv {{ position: absolute; left: {middle - 170:.0f}px; width: 340px; top: {peak_y - 112}px; text-align: center;
  font-size: 72px; font-weight: 800; letter-spacing: -.03em; color: {NEG}; white-space: nowrap; }}
.falll {{ position: absolute; left: {middle - 220:.0f}px; width: 440px; top: {peak_y - 150}px; text-align: center;
  font-size: 30px; font-weight: 560; color: {SOFT}; white-space: nowrap; }}
.backl {{ position: absolute; right: {W - right}px; top: {end_y - 92:.0f}px; text-align: right; font-size: 38px;
  font-weight: 720; color: {POS}; white-space: nowrap; max-width: 520px; }}
.backv {{ font-size: 30px; font-weight: 560; color: {SOFT}; }}
"""
    m = Motion()
    parts = [
        _title(card, m),
        _layer("ref", f'<line x1="{left}" y1="{peak_y}" x2="{right}" y2="{peak_y}" stroke="{RULE}" stroke-width="2" '
                      f'stroke-dasharray="10 10"/>'),
        _layer("bracket", f'<path d="M{trough_x - 14},{peak_y} H{trough_x + 14} M{trough_x},{peak_y} V{trough_y - 12}" '
                          f'fill="none" stroke="{NEG}" stroke-width="3" stroke-dasharray="6 6"/>'),
        _layer("curve", f'<path class="seg" id="s1" pathLength="1" stroke="{INK}" d="{rise}"/>'
                        f'<path class="seg" id="s2" pathLength="1" stroke="{NEG}" d="{drop}"/>'
                        f'<path class="seg" id="s3" pathLength="1" stroke="{POS}" d="{back}"/>'),
        f'<div class="fallv fit" id="fv">{_e(fall.value)}</div>',
        f'<div class="falll" id="fl">{_e(fall.label)}</div>' if fall.label else "",
        f'<div class="backl" id="bl">{_e(recovery.label)}'
        + (f'<div class="backv">{_e(recovery.value)}</div>' if recovery.value else "") + "</div>",
    ]
    t_fall = _rel(card, fall.reveal_at, 0.9)
    t_back = max(_rel(card, recovery.reveal_at, t_fall + 1.4), t_fall + 0.8)
    m.tween("#ref", "opacity", 0.0, 0.4, "0", "1")
    m.tween("#s1", "strokeDashoffset", 0.05, min(0.75, max(0.3, t_fall - 0.1)), "1", "0")
    m.tween("#s2", "strokeDashoffset", t_fall, t_fall + 0.7, "1", "0")
    m.tween("#bracket", "clipPath", t_fall + 0.1, t_fall + 0.6, "inset(0 0 100% 0)", "inset(0 0 0% 0)")
    m.fade_up("#fv", t_fall + 0.25)
    if fall.label:
        m.fade_up("#fl", t_fall + 0.15, rise=8)
    m.tween("#s3", "strokeDashoffset", t_back, t_back + 1.0, "1", "0")
    m.fade_up("#bl", t_back + 0.5)
    return css, "".join(parts), m


def _fmt(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.2f}".rstrip("0").rstrip(".")


BUILDERS = {
    "drawdown_curve": _drawdown_curve,
    "kinetic_range": _kinetic_range, "buckets": _buckets, "speaker_title": _speaker_title,
    "timeline": _timeline, "metric_bars": _metric_bars, "ranked_bars": _ranked_bars,
    "diverging_bar": _diverging_bar, "share_split": _share_split, "trend_line": _trend_line,
    "arr_bridge": _arr_bridge, "sankey_flow": _sankey_flow, "cac_payback": _cac_payback,
    "candlestick_ema": _candlestick_ema,
}

_SETUP = """(tracks) => {
  const fit = el => { let size = parseFloat(getComputedStyle(el).fontSize), floor = size * 0.62;
    while (el.scrollWidth > el.clientWidth + 1 && size > floor) { size -= 2; el.style.fontSize = size + 'px'; } };
  document.querySelectorAll('.fit').forEach(fit);
  const over = [];
  for (const el of document.querySelectorAll('.fit')) if (el.scrollWidth > el.clientWidth + 1) over.push(el.textContent);
  for (const el of document.querySelectorAll('.fitv')) if (el.scrollHeight > el.clientHeight + 1) over.push(el.textContent);
  for (const t of tracks) document.querySelectorAll(t.sel).forEach(el => {
    const a = el.animate(t.kf, {duration: t.ms, fill: 'both'}); a.pause(); a.currentTime = 0; });
  return over;
}"""
_SEEK = "(ms) => { for (const a of document.getAnimations()) a.currentTime = ms; }"
_BLUR = """() => [...document.querySelectorAll('.blur')].map(el => { const r = el.getBoundingClientRect();
  return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height),
          el.classList.contains('round') ? Math.round(parseInt(getComputedStyle(el).borderTopLeftRadius)
            * r.width / Math.max(1, el.offsetWidth)) : 0]; })"""


def scene(card: Card, brand: BrandKit) -> tuple[str, Motion, int, int, int]:
    """(page, motion, width, height, top) for a card, with its exit (slide and fade)."""
    css, body, motion = BUILDERS[card.template](card, brand.accent)
    overlay = MODES[card.template] == "overlay"
    width, height, top = (FRAME_W, FRAME_H, 0) if overlay else (FRAME_W, SPLIT_H, SPLIT_TOP)
    duration = max(0.5, card.end - card.start)
    gone = duration - 1 / FPS  # fully faded on the last frame, so nothing pops off
    motion.tween(".stage", "opacity", gone - EXIT, gone, "1", "0", EASE_OUT_EXIT)
    motion.tween(".stage", "transform", gone - EXIT, gone, "translateY(0px)", "translateY(18px)", EASE_OUT_EXIT)
    stage_top, stage_h = (0, height) if overlay else (CHART_PAD, CHART_H)
    page = (f"<!doctype html><html><head><meta charset='utf-8'><style>{_base_css(width, height, stage_top, stage_h)}{css}</style>"
            f"</head><body><div class='stage'>{body}</div></body></html>")
    return page, motion, width, height, top


def render_card(card: Card, brand: BrandKit, out_dir: Path) -> Frames:
    """Render the card's animation to PNG frames in ``out_dir`` (reused when nothing
    changed). Raises CardRenderError when text doesn't fit or the accent can't be read."""
    if contrast(brand.accent, CANVAS) < MIN_CONTRAST:
        raise CardRenderError(f"The accent {brand.accent} is too dark to read on the card "
                              f"(contrast {contrast(brand.accent, CANVAS):.1f}; needs {MIN_CONTRAST}). Pick a lighter one.")
    if card.template == "candlestick_ema" and len(read_prices(card.data_path) if Path(card.data_path).is_file() else []) < 10:
        raise CardRenderError("The price chart needs a price file with at least 10 rows (open, high, low, close).")
    page, motion, width, height, top = scene(card, brand)
    duration = max(0.5, card.end - card.start)
    count = math.ceil(duration * FPS)
    tracks = motion.js(duration)
    key = hashlib.sha1(json.dumps([page, tracks, count]).encode()).hexdigest()[:14]
    folder = Path(out_dir).resolve() / f"card_{key}"
    meta = folder / "frames.json"
    if meta.is_file():
        data = json.loads(meta.read_text())
        return Frames(str(folder / "f_%05d.png"), data["count"], width, height, top, [tuple(b) for b in data["blur"]])
    folder.mkdir(parents=True, exist_ok=True)
    page_file = folder / "card.html"
    page_file.write_text(page, encoding="utf-8")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        view = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
        view.goto(page_file.as_uri())
        view.evaluate("document.fonts.ready")
        over = view.evaluate(_SETUP, tracks)
        if over:
            browser.close()
            raise CardRenderError("Too long to fit, shorten: " + "; ".join(f'"{t.strip()}"' for t in over))
        view.evaluate(_SEEK, max(0.0, duration - EXIT - 0.05) * 1000)
        blur = [tuple(b) for b in view.evaluate(_BLUR)]
        previous = None
        for index in range(count):
            path = folder / f"f_{index:05d}.png"
            t = index / FPS
            if previous is not None and not motion.moving(t):
                os.link(previous, path)  # nothing moves: the same frame again, for free
            else:
                view.evaluate(_SEEK, t * 1000)
                view.screenshot(path=str(path), omit_background=True)
            previous = path
        browser.close()
    meta.write_text(json.dumps({"count": count, "blur": blur}))
    return Frames(str(folder / "f_%05d.png"), count, width, height, top, blur)
