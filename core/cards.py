"""Round 31: the broadcast-grade graphics engine.

Every graphic is still a small web page rendered frame by frame (Playwright steps it at
30 fps and saves transparent PNG frames; frames where nothing moves are hard links), but
the charts are now drawn by real chart libraries, vendored in assets/vendor so a render
never touches the internet:

- Observable Plot + D3: bars, splits, shares, trends, timelines, and the payback and
  drawdown curves (gradient shading on either side of a threshold).
- Apache ECharts (SVG renderer): the ARR bridge (waterfall) and the Sankey flow.
- TradingView Lightweight Charts: the price chart (candles, EMA, volume). Its logo is
  off in the video; the credit is on the clip pages and the website instead.

Nothing animates on its own clock. ECharts and Lightweight Charts have their animation
off, and window.seekToFrame(frame, fps) seeks every Web Animation and calls each chart's
frame hook, which redraws it for that exact moment (eased with cubic-bezier(0.16, 1,
0.3, 1)). The same frame always comes out the same. Numbers roll up to their value from
the word where they're said.

Split charts sit in a Bento card: output y 492-1340, x 28-1052 (above the small
captions at 1370 and clear of the apps' bottom bars), in three zones: a header (18%:
eyebrow, editorial headline, the punchline's change as a pill), the chart (68%) and a
footer (14%: the "In context" strip, or the chart's key). With nothing for the footer
the chart takes its room, so the card never shows an empty band. The empty card rides
in with the panel, its contents draw once the panel lands, and it leaves with the panel.

Round 32: two qualitative Bento cards for talk with no chartable numbers, drawn in plain
HTML in the same card: the comparison (two sides of the speaker's contrast) and the step
sequence (2-4 steps of their framework, a rail drawing down as each is said).

Name cards, micro-stats and the over-the-speaker graphics stay on the video as glass
pills. The blur behind them is FFmpeg's (core.dynamic_layout), measured from the page:
a transparent PNG can't blur the video under it.

Type: Instrument Serif headlines, Geist labels, JetBrains Mono for every number
(tabular digits). The one chart with a footnote is the price chart: it shows its
source, because nobody said it.

Needs Playwright:  .venv/bin/pip install playwright  then  .venv/bin/playwright install chromium
"""
from __future__ import annotations

import base64
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

ENGINE = "r31"  # part of every cached render's key: a new engine draws everything again
FPS = 30
FRAME_W, FRAME_H = 1080, 1920
SPLIT_TOP = 460  # 24/76 split: the speaker's strip (a 2.35:1 band of the frame), the chart below
SPLIT_H = FRAME_H - SPLIT_TOP  # the chart page (76% of the frame)
W = FRAME_W

# The Bento card, in output pixels. It ends above the small captions (core.dynamic_layout
# CALM_CAPTIONS, from y 1370) and the apps' bottom bars (from about y 1540).
CARD_X, CARD_TOP, CARD_W, CARD_BOTTOM = 28, 492, 1024, 1340
CARD_H = CARD_BOTTOM - CARD_TOP  # 848
CARD_Y = CARD_TOP - SPLIT_TOP  # where it sits on the chart page (32)
HEADER_H, FOOTER_H = 153, 118  # 18% and 14% of the card; the chart zone is the 68% between
HEADER_SLIM = 84  # no headline: just the eyebrow
PAD = 44  # the card's inner margin
IW = CARD_W - 2 * PAD  # the width content uses (936)
RADIUS = 20

# Design tokens (the whole library uses only these).
CANVAS = "#040507"  # the panel around the card
CARD_BG = "#090C12"  # the card's surface
TRACK = "#161B26"  # unlit bar tracks
GRID = "#1A1F2B"
RULE = "#2A3140"  # axes and baselines
INK = "#F5F6F8"
SOFT = "#94A3B8"  # labels and axes (7.4:1 on the card)
AXIS = SOFT
GRAY = "#5B6474"  # neutral bars and flows
LIGHT = "#E6E9EE"
POS = "#34D399"  # emerald: gains
NEG = "#FB7185"  # rose: losses
SHADES = [LIGHT, "#A3ACBA", "#6E7787", "#465062"]  # parts of a whole, after the accent
STROKE = 4  # lines and axes
FOCUS = 2  # focus frames
ASSETS = Path(__file__).resolve().parents[1] / "assets"
FONTS = ASSETS / "fonts"
VENDOR = ASSETS / "vendor"
LIBRARIES = {
    "plot": ("d3.min.js", "plot.min.js"),
    "echarts": ("echarts.min.js",),
    "lightweight": ("lightweight-charts.standalone.production.js",),
}
MIN_CONTRAST = 4.5  # WCAG AA for text
EASE = "cubic-bezier(0.16, 1, 0.3, 1)"  # entrances: fast in, long gentle settle
EASE_IN = "cubic-bezier(0.55, 0, 0.75, 0.2)"  # accelerating
EASE_OUT_EXIT = "cubic-bezier(0.65, 0, 0.35, 1)"  # exits: damped ease-in-out, no snap at either end
POP = "cubic-bezier(0.34, 1.56, 0.64, 1)"  # points popping in, a touch of overshoot
EXIT = 0.4  # seconds (12 frames): over-the-speaker graphics slide and fade out over their last 0.4 s
LAND = 0.4  # the panel's move (core.dynamic_layout.MOVE): a split chart's card rides in with it
FIRST = 0.35  # nothing inside the card starts before this (it would draw while the panel slides)
ROLL = 0.8  # seconds a number takes to roll up to its value

EYEBROWS = {
    "timeline": "Timeline", "metric_bars": "Breakdown", "ranked_bars": "Ranking", "diverging_bar": "Head to head",
    "share_split": "Share of the total", "trend_line": "Trend", "arr_bridge": "ARR bridge", "sankey_flow": "Cash flow",
    "cac_payback": "CAC payback", "drawdown_curve": "Drawdown", "candlestick_ema": "Price action",
    "comparison_card": "The contrast", "step_sequence": "Step by step",
}
KEYED = {"candlestick_ema"}  # charts whose footer always holds their key (a source, a legend)


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


def _rgba(color: str, alpha: float) -> str:
    color = color.lstrip("#")
    r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha:g})"


class Motion:
    """A timeline of property changes (seconds from the graphic's start), one Web
    Animation per element and property. ``spans`` also holds the times a chart library
    redraws (its frame hook) and numbers roll, so those frames are drawn, not reused."""

    def __init__(self) -> None:
        self.tracks: dict[tuple[str, str], list[tuple[float, str, str]]] = {}
        self.spans: list[tuple[float, float]] = []

    def tween(self, selector: str, prop: str, t0: float, t1: float, a: str, b: str, easing: str = EASE) -> None:
        t0, t1 = max(0.0, t0), max(0.0, t1)
        points = self.tracks.setdefault((selector, prop), [])
        points.append((t0, a, easing))
        points.append((t1, b, "linear"))
        self.spans.append((t0, t1))

    def fade_up(self, selector: str, t: float, rise: int = 14, length: float = 0.4) -> None:
        self.tween(selector, "opacity", t, t + length * 0.8, "0", "1")
        self.tween(selector, "transform", t, t + length, f"translateY({rise}px)", "translateY(0px)")

    def wipe_x(self, selector: str, t0: float, t1: float, x_from: float, x_to: float, width: float = CARD_W,
               easing: str = EASE) -> None:
        """Reveal left to right, from x_from to x_to (pixels of a layer ``width`` wide)."""
        self.tween(selector, "clipPath", t0, t1, f"inset(0 {100 * (width - x_from) / width:.3f}% 0 0)",
                   f"inset(0 {100 * (width - x_to) / width:.3f}% 0 0)", easing)

    def hold(self, t0: float, t1: float) -> None:
        """A chart library redraws between t0 and t1 (its frame hook): draw those frames."""
        self.spans.append((max(0.0, t0), max(0.0, t1)))

    def merge(self, other: "Motion") -> None:
        for key, points in other.tracks.items():
            self.tracks.setdefault(key, []).extend(points)
        self.spans.extend(other.spans)

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


@dataclass
class Chart:
    """What a split chart's builder hands the Bento card: its zone's markup and motion,
    the script that draws it (``js``), where its focal point is (for the glow), what
    goes in the footer when there's no context layer (``key``), and a note for the
    header's right side when there's no change pill (``note``)."""

    css: str
    body: str
    motion: Motion
    js: str = ""
    libs: tuple[str, ...] = ()
    focal: tuple[float, float] | None = None
    key: str = ""
    note: str = ""


def _font_faces() -> str:
    faces = [("Inter", "Inter-Variable.ttf", "100 900", "normal"), ("Geist", "Geist-Variable.woff2", "100 900", "normal"),
             ("Instrument Serif", "InstrumentSerif-Regular.woff2", "400", "normal"),
             ("Instrument Serif", "InstrumentSerif-Italic.woff2", "400", "italic"),
             ("JetBrains Mono", "JetBrainsMono-Variable.woff2", "100 800", "normal")]
    return "".join(f'@font-face {{ font-family: "{name}"; src: url("{(FONTS / file).as_uri()}"); font-weight: {weight}; '
                   f"font-style: {style}; }}\n" for name, file, weight, style in faces)


def _base_css(width: int, height: int) -> str:
    return _font_faces() + f"""
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ width: {width}px; height: {height}px; background: transparent; overflow: hidden; }}
body {{ font-family: Geist, Inter, sans-serif; color: {INK}; -webkit-font-smoothing: antialiased;
  font-variant-numeric: tabular-nums; font-feature-settings: "tnum" 1; position: relative; }}
.stage {{ position: absolute; left: 0; top: 0; width: {width}px; height: {height}px; }}
.num {{ font-family: "JetBrains Mono", monospace; font-variant-numeric: tabular-nums; font-feature-settings: "tnum" 1, "zero" 0; }}
.lyr {{ position: absolute; left: 0; top: 0; width: 100%; height: 100%; }}
.lyr svg {{ position: absolute; left: 0; top: 0; overflow: visible; }}
.lab {{ font-size: 30px; font-weight: 520; color: {SOFT}; white-space: nowrap; }}
.val {{ font-size: 40px; font-weight: 700; letter-spacing: -.02em; white-space: nowrap; }}
.axis {{ font-family: "JetBrains Mono", monospace; font-size: 22px; font-weight: 500; color: {AXIS}; white-space: nowrap; }}
"""


def _e(text: object) -> str:
    return html.escape(str(text or ""))


def _rel(card: Card, time: float | None, fallback: float) -> float:
    return max(0.0, (time - card.start) if time is not None else fallback)


def _at(card: Card, time: float | None, fallback: float) -> float:
    """When something inside the card appears: as said, but never while the panel slides."""
    return max(FIRST, _rel(card, time, fallback))


def _num(text: str) -> float:
    return max(parse_value(text).numbers or [0.0])


def _roll(m: Motion, text: str, t0: float, length: float = ROLL, align: str = "right") -> str:
    """A number that rolls up to its value from t0 (every number in the text, keeping its
    format: '$1,700' counts 0, 425, ... 1,700). Text without digits doesn't roll."""
    if not re.search(r"\d", str(text or "")):
        return _e(text)
    m.hold(t0, t0 + length)
    return (f'<span data-roll="{_e(text)}" data-t0="{t0:.3f}" data-t1="{t0 + length:.3f}" '
            f'data-align="{align}">{_e(text)}</span>')


def _split_range(value: str) -> tuple[str, str]:
    """'$850–$1,700' -> ('$850', '$1,700'); '96 hrs' -> ('96 hrs', '')."""
    parts = re.split(r"\s*[–—]\s*|\s+-\s+|(?<=\d)-(?=[$\d~])", value, maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (value.strip(), "")


def _top(card: Card) -> float:
    return max((max(parse_value(r.value).numbers or [0.0]) for r in card.rows), default=0.0) or 1.0


def _data(value: object) -> str:
    """JSON for a page script (safe inside <script>)."""
    return json.dumps(value).replace("</", "<\\/")


# ------------------------------------------------------------ overlays (speaker full-screen)
ARROW = re.compile(r"\s*(?:→|->|=>)\s*")
GLASS = "rgba(14,16,22,.40)"  # the tint on the blurred video behind a glass pill
SPECULAR = ('<svg class="spec"><defs><linearGradient id="spec{k}" x1="0" y1="0" x2="1" y2="1">'
            '<stop offset="0" stop-color="#fff" stop-opacity=".62"/><stop offset=".45" stop-color="#fff" stop-opacity=".08"/>'
            '<stop offset="1" stop-color="#fff" stop-opacity=".32"/></linearGradient></defs>'
            '<rect x=".5" y=".5" width="calc(100% - 1px)" height="calc(100% - 1px)" rx="{r}" fill="none" '
            'stroke="url(#spec{k})" stroke-width="1"/></svg>')


def _specular(k: int, radius: int) -> str:
    return SPECULAR.replace("{k}", str(k)).replace("{r}", str(radius))


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
.nums {{ display: inline-flex; align-items: baseline; justify-content: center; gap: 22px; white-space: nowrap; font-weight: 700;
  font-size: 138px; letter-spacing: -.05em; line-height: 1; color: {color}; text-shadow: 0 6px 28px rgba(0,0,0,.45);
  max-width: 960px; overflow: hidden; }}
.dash {{ font-weight: 400; opacity: .85; }}
.ctx {{ margin-top: 28px; font-size: 46px; font-weight: 600; letter-spacing: -.01em; line-height: 1.18; color: rgba(255,255,255,.95);
  text-shadow: 0 3px 16px rgba(0,0,0,.5); }}
"""
    m = Motion()
    t = _rel(card, row.reveal_at, 0.0)
    joiner = "→" if change else "–"
    t2 = max(t + 0.25, _rel(card, row.reveal_to, t + 0.5)) if second else t
    rest = (f'<span class="rest"><span class="dash">{joiner}</span><span>{_roll(m, second, t2, align="left")}</span></span>'
            if second else "")
    body = (f'<div class="scrim"></div><div class="wrap"><div class="nums num fit"><span class="a">'
            f'{_roll(m, first, t, align="right" if second else "center")}</span>{rest}</div>'
            f'<div class="ctx">{_e(row.label)}</div></div>')
    m.tween(".scrim", "opacity", 0.0, 0.3, "0", "1")
    m.fade_up(".a", t, rise=22, length=0.45)
    last = t
    if second:
        m.tween(".rest", "clipPath", t2, t2 + 0.45, "inset(0 100% 0 0)", "inset(0 0% 0 0)")
        last = t2
    if row.label:
        m.fade_up(".ctx", last + 0.45, rise=12, length=0.45)
    return css, body, m


def _buckets(card: Card, accent: str) -> tuple[str, str, Motion]:
    css = f"""
.row {{ position: absolute; left: 40px; right: 40px; top: 1200px; display: flex; justify-content: center; gap: 22px; }}
.box {{ position: relative; min-width: 190px; white-space: nowrap; padding: 22px 34px; border-radius: 18px;
  border: {FOCUS}px solid rgba(255,255,255,.88); background: rgba(14,16,22,.30); color: #FFFFFF; font-size: 40px;
  font-weight: 600; letter-spacing: -.01em; line-height: 1.08; text-align: center; text-shadow: 0 2px 10px rgba(0,0,0,.35); }}
.box.dashed {{ border-style: dashed; }}
"""
    boxes = "".join(f'<div class="box blur round{" dashed" if row.dashed else ""}" id="b{k}">{_e(row.label)}</div>'
                    for k, row in enumerate(card.rows))
    m = Motion()
    times = [_rel(card, row.reveal_at, 0.15 + 0.5 * k) for k, row in enumerate(card.rows)]
    for k, t in enumerate(times):
        m.fade_up(f"#b{k}", t, rise=16, length=0.4)
        # The active box: gold edge and a gold tint. Not bolder text: that widens the box and
        # nudges the centred row sideways (Round 31).
        m.tween(f"#b{k}", "borderColor", t, t + 0.22, "rgba(255,255,255,.88)", accent)
        m.tween(f"#b{k}", "backgroundColor", t, t + 0.22, "rgba(14,16,22,.30)", _rgba(accent, 0.22))
        if k + 1 < len(times):  # the box being talked about is gold; the one before goes back to white
            m.tween(f"#b{k}", "borderColor", times[k + 1], times[k + 1] + 0.22, accent, "rgba(255,255,255,.88)")
            m.tween(f"#b{k}", "backgroundColor", times[k + 1], times[k + 1] + 0.22, _rgba(accent, 0.22), "rgba(14,16,22,.30)")
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
           + f"""
.lt {{ position: relative; min-width: 560px; max-width: 940px; padding: 30px 60px 32px; border-radius: 30px;
  background: {GLASS}; box-shadow: 0 14px 44px rgba(0,0,0,.30); text-align: left; }}
.spec {{ position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }}
.name {{ font-size: 56px; font-weight: 650; letter-spacing: -.02em; color: #FFFFFF; white-space: nowrap; line-height: 1.05; }}
.role {{ margin-top: 8px; font-size: 30px; font-weight: 500; color: #CDD2DB; white-space: nowrap; }}
.tick {{ display: inline-block; width: 10px; height: 10px; margin: 0 14px 4px 0; border-radius: 50%; background: {accent}; }}
""")
    body = (f'<div class="ltwrap"><div class="lt blur round" id="lt">{_specular(0, 30)}<div class="name fit">{_e(row.label)}</div>'
            f'<div class="role">{_e(row.detail)}</div></div></div>')
    m = Motion()
    m.tween("#lt", "opacity", 0.0, 0.3, "0", "1")
    m.tween("#lt", "transform", 0.0, 0.45, "translateY(18px) scale(.98)", "translateY(0px) scale(1)")
    m.fade_up(".role", 0.12, rise=8, length=0.4)
    return css, body, m


STAT_PILL_TOP = 190  # the micro-stat's default spot: top left, below the apps' top bar
STAT_PILL_SIZE = (500, 150)  # about how big it is (width, height), to place it


def _stat_pill(card: Card, accent: str) -> tuple[str, str, Motion]:
    """A micro-stat (Round 30): one number with a few of the speaker's words, or a short
    trend (2-4 numbers, each labelled with when) as a sparkline ending on its latest
    value. Small, on glass, in a corner clear of the face: it backs up what's said
    without taking the frame."""
    x, y, _scale, align = parse_place(card.place) if card.place else (56, STAT_PILL_TOP, 1.0, "left")
    side = f"left: {x:.0f}px;" if align != "right" else f"right: {W - x:.0f}px;"
    last = card.rows[-1]
    color = accent if (last.highlight or len(card.rows) > 1) else INK
    css = f"""
.pill {{ position: absolute; top: {y:.0f}px; {side} max-width: 620px; padding: 22px 30px 22px; border-radius: 26px;
  background: {GLASS}; box-shadow: 0 12px 36px rgba(0,0,0,.28); display: flex; align-items: center; gap: 22px; }}
.spec {{ position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }}
.pill .v {{ font-size: 58px; font-weight: 700; letter-spacing: -.04em; line-height: 1; color: {color}; white-space: nowrap; }}
.pill .l {{ font-size: 28px; font-weight: 540; color: #D5D9E0; line-height: 1.15; max-width: 300px; }}
.pill .when {{ font-size: 22px; font-weight: 500; color: {SOFT}; white-space: nowrap; }}
.spark {{ width: 210px; height: 84px; overflow: visible; }}
"""
    m = Motion()
    m.tween(".pill", "opacity", 0.0, 0.28, "0", "1")
    m.tween(".pill", "transform", 0.0, 0.42, f"translateX({-20 if align != 'right' else 20}px)", "translateX(0px)")
    if len(card.rows) == 1:
        t = _rel(card, last.reveal_at, 0.1)
        body = (f'<div class="pill blur round">{_specular(0, 26)}<span class="v num fit">{_roll(m, last.value, t, align="left")}</span>'
                + (f'<span class="l">{_e(last.label)}</span>' if last.label else "") + "</div>")
        return css, body, m
    values = [signed_value(row.value) for row in card.rows]  # a fall stays a fall
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    points = [(10 + 190 * k / (len(values) - 1), 74 - 64 * (v - low) / span) for k, v in enumerate(values)]
    path = " ".join(f"{'M' if k == 0 else 'L'}{px:.1f},{py:.1f}" for k, (px, py) in enumerate(points))
    dots = "".join(f'<circle class="dot" id="d{k}" cx="{px:.1f}" cy="{py:.1f}" r="{7 if k == len(points) - 1 else 5}" '
                   f'fill="{accent if k == len(points) - 1 else "#C9CED6"}"/>' for k, (px, py) in enumerate(points))
    times = [_rel(card, row.reveal_at, 0.2 + 0.5 * k) for k, row in enumerate(card.rows)]
    body = (f'<div class="pill blur round">{_specular(0, 26)}<svg class="spark" viewBox="0 0 210 84"><path class="ln" d="{path}" '
            f'pathLength="1" fill="none" stroke="#C9CED6" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" '
            f'stroke-dasharray="1" stroke-dashoffset="1"/>{dots}</svg><span><div class="v num fit">'
            f'{_roll(m, last.value, times[-1], align="left")}</div>'
            f'<div class="when">{_e(card.rows[0].label)} → {_e(last.label)}</div></span></div>')
    m.tween(".ln", "strokeDashoffset", times[0], max(times[-1], times[0] + 0.6), "1", "0")
    for k, t in enumerate(times):
        m.tween(f"#d{k}", "opacity", t, t + 0.2, "0", "1")
    m.tween(".pill .v", "opacity", 0.0, 0.01, "0", "0")
    m.tween(".pill .v", "opacity", times[-1], times[-1] + 0.3, "0", "1")
    return css, body, m


# ------------------------------------------------------------ the Bento card's parts
def _is_change(text: str) -> bool:
    text = str(text or "").strip()
    return bool(re.search(r"\d", text)) and text[:1] in "+-−–"


def _kpi_row(card: Card) -> int | None:
    """The header's change pill: the punchline (highlighted) item, when it's a signed
    change the speaker said ('+28%', '−$0.4M'). Nothing computed goes in it."""
    return next((k for k, row in enumerate(card.rows) if row.highlight and _is_change(row.value)), None)


def _context_footer(card: Card, accent: str) -> tuple[str, str, Motion]:
    """The chart's "In context" strip (Card.context, checked by core.card_checks): what
    its numbers mean, in the card's footer. A figure and a short line; for a ratio, the
    two sizes as blocks; for a share, how much of the whole; for an earlier number, when
    in the video it was said."""
    context = card.context
    value, text, kind = str(context.get("value") or ""), str(context.get("text") or ""), str(context.get("kind") or "")
    reveal = _at(card, context.get("reveal"), 1.6)
    m = Motion()
    extra = ""
    if kind in {"ratio", "share"}:
        first, second = (int(n) for n in context.get("of") or [1, 2])
        a, b = card.rows[first - 1], card.rows[second - 1]
        x, y = _num(a.value), _num(b.value)
        if kind == "ratio":  # the smaller one as one block, the larger as that many blocks
            small, large = (a, b) if x < y else (b, a)
            times = max(x, y) / (min(x, y) or 1.0)
            whole = min(10, int(times))
            blocks = "".join(f'<span class="blk" id="bk{k}" style="background:{accent}"></span>' for k in range(whole))
            part = times - whole if times < 10 else 0
            if part >= 0.08:
                blocks += f'<span class="blk" id="bk{whole}" style="width:{22 * part:.0f}px;background:{accent}"></span>'
            extra = (f'<span class="unit"><span class="blk" style="background:#8A93A3"></span><span class="cl">{_e(small.label)}'
                     f'</span></span><span class="unit"><span class="blks">{blocks}</span><span class="cl">{_e(large.label)}</span></span>')
        else:
            extra = (f'<span class="unit"><span class="cl">{_e(b.label)}</span><span class="ct"><span class="cf" id="cf0" '
                     f'style="width:{100 * x / (y or 1.0):.1f}%;background:{accent}"></span></span></span>')
    elif kind == "earlier":
        at = float(context.get("at") or 0.0)
        extra = f'<span class="when">Said earlier, at <span class="num">{int(at // 60)}:{int(at % 60):02d}</span></span>'
    css = f"""
.ctx {{ position: absolute; inset: 0; padding: 0 {PAD}px; }}
.ctx .r1 {{ position: absolute; left: {PAD}px; right: {PAD}px; top: 20px; height: 26px; display: flex; align-items: center;
  justify-content: space-between; gap: 24px; }}
.ctx .eye {{ font-size: 17px; font-weight: 650; letter-spacing: .18em; color: {SOFT}; white-space: nowrap; }}
.ctx .ex {{ display: flex; align-items: center; gap: 26px; min-width: 0; overflow: hidden; }}
.ctx .r2 {{ position: absolute; left: {PAD}px; right: {PAD}px; top: 52px; height: 52px; display: flex; align-items: baseline; gap: 20px; }}
.ctx .big {{ font-size: 44px; font-weight: 700; letter-spacing: -.03em; color: {accent}; white-space: nowrap; }}
.ctx .txt {{ flex: 1; min-width: 0; font-size: 28px; font-weight: 520; color: {LIGHT}; white-space: nowrap; overflow: hidden; }}
.unit {{ display: flex; align-items: center; gap: 10px; min-width: 0; }}
.blks {{ display: flex; gap: 5px; }}
.blk {{ display: inline-block; width: 22px; height: 22px; border-radius: 4px; flex: none; }}
.cl {{ max-width: 190px; font-size: 20px; font-weight: 500; color: {SOFT}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.ct {{ width: 220px; height: 8px; background: {TRACK}; border-radius: 4px; overflow: hidden; flex: none; }}
.cf {{ display: block; height: 100%; border-radius: 4px; }}
.ctx .when {{ font-size: 20px; color: {SOFT}; white-space: nowrap; }}
"""
    body = (f'<div class="ctx" id="ctx"><div class="r1"><span class="eye">IN CONTEXT</span><span class="ex">{extra}</span></div>'
            f'<div class="r2"><span class="big num">{_roll(m, value, reveal + 0.1, 0.7, "left")}</span>'
            f'<span class="txt fit">{_e(text)}</span></div></div>')
    m.fade_up("#ctx", reveal, rise=10, length=0.45)
    if 'id="cf0"' in body:
        m.tween("#cf0", "clipPath", reveal + 0.15, reveal + 0.8, "inset(0 100% 0 0)", "inset(0 0% 0 0)")
    for k in range(11):
        if f'id="bk{k}"' in body:
            m.tween(f"#bk{k}", "opacity", reveal + 0.2 + 0.1 * k, reveal + 0.35 + 0.1 * k, "0", "1")
    return css, body, m


def _card_css(accent: str, header_h: int, plot_h: int, footer: bool) -> str:
    return f"""
.card {{ position: absolute; left: {CARD_X}px; top: {CARD_Y}px; width: {CARD_W}px; height: {CARD_H}px;
  border-radius: {RADIUS}px; background: {CARD_BG}; overflow: hidden; }}
.rim {{ position: absolute; inset: 0; border-radius: {RADIUS}px; padding: 1px; pointer-events: none;
  background: linear-gradient(180deg, rgba(255,255,255,.16) 0%, rgba(255,255,255,.04) 100%);
  -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0); -webkit-mask-composite: xor;
  mask-composite: exclude; }}
.grain {{ position: absolute; inset: 0; width: {CARD_W}px; height: {CARD_H}px; pointer-events: none; }}
.hdr {{ position: absolute; left: 0; top: 0; width: {CARD_W}px; height: {header_h}px; }}
.eye0 {{ position: absolute; left: {PAD}px; top: 34px; font-size: 18px; font-weight: 650; letter-spacing: .18em;
  color: {SOFT}; white-space: nowrap; text-transform: uppercase; }}
.eye0::before {{ content: ""; display: inline-block; width: 8px; height: 8px; margin: 0 12px 2px 0; border-radius: 2px;
  background: {accent}; }}
.hrow {{ position: absolute; left: {PAD}px; right: {PAD}px; top: 66px; height: 64px; display: flex; align-items: center; gap: 24px; }}
.title {{ flex: 1; min-width: 0; font-family: "Instrument Serif", Georgia, serif; font-size: 60px; font-weight: 400;
  letter-spacing: -.012em; line-height: 1; white-space: nowrap; overflow: hidden; padding-bottom: 4px; }}
.kpi {{ flex: none; display: flex; align-items: center; gap: 10px; height: 52px; padding: 0 20px 0 16px; border-radius: 26px;
  border: 1px solid; font-size: 28px; font-weight: 700; letter-spacing: -.02em; white-space: nowrap; }}
.kpi svg {{ width: 16px; height: 14px; }}
.note {{ flex: none; max-width: 420px; font-size: 20px; font-weight: 500; color: {SOFT}; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }}
.plot {{ position: absolute; left: 0; top: {header_h}px; width: {CARD_W}px; height: {plot_h}px; }}
.dots {{ position: absolute; inset: 10px 24px; background-image: radial-gradient(circle, rgba(255,255,255,.035) 1.3px,
  transparent 1.8px); background-size: 24px 24px; background-position: 0 0; }}
.glow {{ position: absolute; width: 640px; height: 640px; border-radius: 50%;
  background: radial-gradient(closest-side, {_rgba(accent, 0.06)}, {_rgba(accent, 0)}); }}
.ftr {{ position: absolute; left: 0; bottom: 0; width: {CARD_W}px; height: {FOOTER_H}px; border-top: 1px solid rgba(255,255,255,.06);
  {'' if footer else 'display: none;'} }}
.key {{ position: absolute; left: {PAD}px; right: {PAD}px; top: 0; bottom: 0; display: flex; align-items: center;
  justify-content: space-between; gap: 24px; font-size: 21px; font-weight: 500; color: {SOFT}; white-space: nowrap; }}
.key .lg {{ display: flex; align-items: center; gap: 28px; }}
.key i {{ display: inline-block; vertical-align: middle; margin-right: 10px; }}
"""


GRAIN = ('<svg class="grain"><filter id="grain"><feTurbulence type="fractalNoise" baseFrequency=".85" numOctaves="2" '
         'seed="11" stitchTiles="stitch"/><feColorMatrix type="saturate" values="0"/></filter>'
         '<rect width="100%" height="100%" filter="url(#grain)" opacity=".02"/></svg>')
TRIANGLE = {True: '<svg viewBox="0 0 16 14"><path d="M8 0 L16 14 H0 Z" fill="currentColor"/></svg>',
            False: '<svg viewBox="0 0 16 14"><path d="M0 0 H16 L8 14 Z" fill="currentColor"/></svg>'}


# ------------------------------------------------------------ split charts (inside the card)
def _plot_tags(svg: str, tags: list[tuple[str, str]]) -> str:
    return "".join(f"NAV.tag({svg}, {json.dumps(cls)}, {json.dumps(prefix)});" for cls, prefix in tags)


def _bar_rows(card: Card, accent: str, m: Motion, ph: int, times: list[float]) -> tuple[str, str, list[float]]:
    """Labelled bars (metric_bars, ranked_bars): label left, value right, a bar under them
    (a range drawn solid to its low end and hatched to its high end). Plot draws the bars
    on a linear scale from zero to the largest value."""
    n = len(card.rows)
    block = 88  # label line 52 + gap 14 + bar 22
    pitch = min(170.0, (ph - 70) / n)
    top = (ph - ((n - 1) * pitch + block)) / 2
    ys = [top + k * pitch for k in range(n)]
    peak = _top(card)
    value_w = max(len(row.value) for row in card.rows) * 28 + 32  # JetBrains Mono at 46px: 28 px a character
    data, labels = [], []
    for k, row in enumerate(card.rows):
        numbers = parse_value(row.value).numbers or [0.0]
        low, high = min(numbers), max(numbers)
        data.append({"y": round(ys[k] + 66, 1), "low": low, "high": high,
                     "solid": accent if row.highlight else LIGHT, "hatch": f"url(#hatch{1 if row.highlight else 0})"})
        labels.append(f'<div class="lab fit" id="l{k}" style="left:{PAD}px;top:{ys[k]:.0f}px;width:{IW - value_w}px;'
                      f'color:{INK if row.highlight else SOFT}">{_e(row.label)}</div>'
                      f'<div class="val num" id="v{k}" style="right:{PAD}px;top:{ys[k] - 3:.0f}px;'
                      f'color:{accent if row.highlight else INK}">{_roll(m, row.value, times[k])}</div>')
    hatch = (f'<pattern id="hatch0" patternUnits="userSpaceOnUse" width="7" height="7" patternTransform="rotate(45)">'
             f'<rect width="3" height="7" fill="rgba(230,233,238,.6)"/></pattern>'
             f'<pattern id="hatch1" patternUnits="userSpaceOnUse" width="7" height="7" patternTransform="rotate(45)">'
             f'<rect width="3" height="7" fill="{_rgba(accent, 0.7)}"/></pattern>')
    js = (f"NAV.onBuild(() => {{ const rows = {_data(data)};"
          f" const svg = NAV.plot('#bars', {{width: {CARD_W}, height: {ph}, x: {{domain: [0, {peak}], range: [{PAD}, {CARD_W - PAD}]}},"
          f" y: {{type: 'identity'}}, color: {{type: 'identity'}}, marks: [NAV.defs({json.dumps(hatch)}),"
          f" Plot.rect(rows, {{x1: 0, x2: {peak}, y1: 'y', y2: d => d.y + 22, fill: {json.dumps(TRACK)}, className: 'trk'}}),"
          f" Plot.rect(rows, {{x1: 0, x2: 'low', y1: 'y', y2: d => d.y + 22, fill: 'solid', className: 'sol'}}),"
          f" Plot.rect(rows, {{x1: 'low', x2: 'high', y1: 'y', y2: d => d.y + 22, fill: 'hatch', className: 'rng'}})]}});"
          + _plot_tags("svg", [("trk", "t"), ("sol", "s"), ("rng", "r")]) + " });")
    return '<div class="lyr" id="bars"></div>' + "".join(labels), js, ys


BARS_CSS = f"""
#bars g.trk > rect, #bars g.sol > rect, #bars g.rng > rect {{ transform-box: view-box; transform-origin: {PAD}px 0px; }}
.plot .lab, .plot .val {{ position: absolute; }}
.plot .lab {{ height: 52px; line-height: 52px; font-size: 34px; overflow: hidden; }}
.plot .val {{ height: 56px; line-height: 56px; font-size: 46px; text-align: right; }}
"""


def _focal_bar(card: Card, ys: list[float]) -> tuple[float, float]:
    peak = _top(card)
    k = next((k for k, r in enumerate(card.rows) if r.highlight), 0)
    return PAD + IW * _num(card.rows[k].value) / peak, ys[k] + 77


def _metric_bars(card: Card, accent: str, ph: int) -> Chart:
    m = Motion()
    times = [_at(card, row.reveal_at, 0.55 + 0.5 * k) for k, row in enumerate(card.rows)]
    body, js, ys = _bar_rows(card, accent, m, ph, times)
    for k, t in enumerate(times):
        t_in = FIRST + 0.05 * k
        m.tween(f"#l{k}", "opacity", t_in, t_in + 0.3, "0", "1")
        m.tween(f"#t{k}", "transform", t_in, t_in + 0.55, "scaleX(0)", "scaleX(1)")
        m.fade_up(f"#v{k}", t, rise=10)
        m.tween(f"#s{k}, #r{k}", "transform", t, t + ROLL, "scaleX(0)", "scaleX(1)")
    return Chart(BARS_CSS, body, m, js, ("plot",), _focal_bar(card, ys))


def _ranked_bars(card: Card, accent: str, ph: int) -> Chart:
    m = Motion()
    n = len(card.rows)
    starts = [FIRST + 0.06 * k for k in range(n)]
    body, js, ys = _bar_rows(card, accent, m, ph, starts)
    for k, t_in in enumerate(starts):
        m.tween(f"#l{k}, #v{k}", "opacity", t_in, t_in + 0.3, "0", "1")
        m.tween(f"#t{k}", "transform", t_in, t_in + 0.55, "scaleX(0)", "scaleX(1)")
        m.tween(f"#s{k}, #r{k}", "transform", t_in, t_in + ROLL, "scaleX(0)", "scaleX(1)")
    focus = sorted((max(_rel(card, r.reveal_at, 0.0), 0.95), k) for k, r in enumerate(card.rows) if r.reveal_at is not None)
    for step, (t, active) in enumerate(focus):  # the row being said at full strength, the rest at 28%
        for k in range(n):
            before = "1" if step == 0 or focus[step - 1][1] == k else ".28"
            after = "1" if k == active else ".28"
            if before != after:
                m.tween(f"#l{k}, #v{k}, #s{k}, #r{k}", "opacity", t, t + 0.3, before, after)
    return Chart(BARS_CSS, body, m, js, ("plot",), _focal_bar(card, ys))


def _diverging_bar(card: Card, accent: str, ph: int) -> Chart:
    first, second = card.rows
    a, b = _num(first.value), _num(second.value)
    share = 100 * a / (a + b) if a + b else 50.0
    main = 0 if first.highlight or not second.highlight else 1
    color = [accent if main == 0 else "#C9CED6", accent if main == 1 else "#C9CED6"]
    top = (ph - 308) / 2 - 6
    bar_y = top + 188
    split_x = PAD + IW * share / 100
    focus_left, focus_w = ((PAD - 12, IW * share / 100 + 21) if main == 0 else (split_x - 9, IW * (100 - share) / 100 + 21))
    css = f"""
.sides {{ position: absolute; left: {PAD}px; right: {PAD}px; top: {top:.0f}px; display: flex; justify-content: space-between; }}
.side {{ display: flex; flex-direction: column; gap: 4px; }} .side.r {{ text-align: right; align-items: flex-end; }}
.side .lab {{ font-size: 34px; }}
.side .val {{ font-size: 88px; letter-spacing: -.04em; max-width: {IW / 2 - 24:.0f}px; overflow: hidden; }}
#g0 {{ transform-box: fill-box; transform-origin: 0% 50%; }} #g1 {{ transform-box: fill-box; transform-origin: 100% 50%; }}
.focus {{ position: absolute; top: {bar_y - 14:.0f}px; height: 148px; border: {FOCUS}px solid {INK}; opacity: 0;
  left: {focus_left:.0f}px; width: {focus_w:.0f}px; }}
"""
    m = Motion()
    t_main = max(1.1, _rel(card, card.rows[main].reveal_at, 1.1))
    body = (f'<div class="sides"><div class="side" id="s0"><span class="lab">{_e(first.label)}</span>'
            f'<span class="val num fit" style="color:{color[0]}">{_roll(m, first.value, 0.45, align="left")}</span></div>'
            f'<div class="side r" id="s1"><span class="lab">{_e(second.label)}</span>'
            f'<span class="val num fit" style="color:{color[1]}">{_roll(m, second.value, 0.6)}</span></div></div>'
            '<div class="lyr" id="dv"></div><div class="focus"></div>')
    js = (f"NAV.onBuild(() => {{ const svg = NAV.plot('#dv', {{width: {CARD_W}, height: {ph}, x: {{domain: [0, 100], "
          f"range: [{PAD}, {CARD_W - PAD}]}}, y: {{type: 'identity'}}, marks: ["
          f" Plot.rect([0], {{x1: 0, x2: {share:.4f}, y1: {bar_y:.1f}, y2: {bar_y + 120:.1f}, insetRight: 3, fill: {json.dumps(color[0])}, className: 'g0'}}),"
          f" Plot.rect([0], {{x1: {share:.4f}, x2: 100, y1: {bar_y:.1f}, y2: {bar_y + 120:.1f}, insetLeft: 3, fill: {json.dumps(color[1])}, className: 'g1'}})]}});"
          " NAV.tag(svg, 'g0', 'g0', true); NAV.tag(svg, 'g1', 'g1', true); });")
    m.tween("#s0", "opacity", 0.4, 0.7, "0", "1")
    m.tween("#s1", "opacity", 0.55, 0.85, "0", "1")
    m.tween("#g0", "transform", FIRST + 0.05, FIRST + 0.9, "scaleX(0)", "scaleX(1)")
    m.tween("#g1", "transform", FIRST + 0.2, FIRST + 1.05, "scaleX(0)", "scaleX(1)")
    m.tween(f"#g{1 - main}", "opacity", t_main, t_main + 0.35, "1", ".3")
    m.tween(f"#s{1 - main}", "opacity", t_main, t_main + 0.35, "1", ".35")
    m.tween(".focus", "opacity", t_main, t_main + 0.25, "0", "1")
    m.tween(".focus", "transform", t_main, t_main + 0.4, "scale(1.04)", "scale(1)")
    focal = (PAD + IW * share / 200, bar_y + 60) if main == 0 else (split_x + IW * (100 - share) / 200, bar_y + 60)
    return Chart(css, body, m, js, ("plot",), focal)


def _share_split(card: Card, accent: str, ph: int) -> Chart:
    values = [_num(r.value) for r in card.rows]
    total = sum(values) or 1.0
    shades = iter(SHADES)
    colors = [accent if r.highlight else next(shades) for r in card.rows]
    n = len(card.rows)
    top = (ph - (110 + 50 + n * 74)) / 2
    legend_top = top + 160
    css = f"""
#sh g.seg > rect {{ transform-box: fill-box; transform-origin: 0% 50%; }}
.legend {{ position: absolute; left: {PAD}px; right: {PAD}px; top: {legend_top:.0f}px; }}
.li {{ display: flex; align-items: center; gap: 22px; height: 74px; border-bottom: 1px solid {GRID}; }}
.sw {{ width: 22px; height: 22px; flex: none; border-radius: 4px; }}
.li .lab {{ flex: 1; min-width: 0; font-size: 32px; color: {INK}; font-weight: 560; overflow: hidden; }}
.li .val {{ font-size: 42px; }}
.sfocus {{ position: absolute; top: {top - 12:.0f}px; height: 134px; border: {FOCUS}px solid {INK}; opacity: 0; }}
"""
    m = Motion()
    times = [_at(card, r.reveal_at, 0.5 + 0.45 * k) for k, r in enumerate(card.rows)]
    data, x = [], 0.0
    hl = next((k for k, r in enumerate(card.rows) if r.highlight), None)
    focus, focal = "", None
    for k, (value, color) in enumerate(zip(values, colors)):
        data.append({"x1": x, "x2": x + value, "c": color})
        if k == hl:
            left = PAD + IW * x / total
            focus = f'<div class="sfocus" style="left:{left - 10:.0f}px;width:{IW * value / total + 17:.0f}px"></div>'
            focal = (left + IW * value / total / 2, top + 55)
        x += value
    legend = "".join(f'<div class="li" id="li{k}"><span class="sw" style="background:{color}"></span>'
                     f'<span class="lab fit">{_e(row.label)}</span><span class="val num">{_roll(m, row.value, times[k])}</span></div>'
                     for k, (row, color) in enumerate(zip(card.rows, colors)))
    body = f'<div class="lyr" id="sh"></div>{focus}<div class="legend">{legend}</div>'
    js = (f"NAV.onBuild(() => {{ const segs = {_data(data)}; const svg = NAV.plot('#sh', {{width: {CARD_W}, height: {ph},"
          f" x: {{domain: [0, {total}], range: [{PAD}, {CARD_W - PAD}]}}, y: {{type: 'identity'}}, color: {{type: 'identity'}},"
          f" marks: [Plot.rect(segs, {{x1: 'x1', x2: 'x2', y1: {top:.1f}, y2: {top + 110:.1f}, insetRight: 3, fill: 'c', className: 'seg'}})]}});"
          + _plot_tags("svg", [("seg", "q")]) + " });")
    for k, t in enumerate(times):
        m.tween(f"#q{k}", "transform", t, t + 0.6, "scaleX(0)", "scaleX(1)")
        m.fade_up(f"#li{k}", t, rise=10)
    if hl is not None:
        t = times[hl] + 0.6
        for k in range(n):
            if k != hl:
                m.tween(f"#q{k}", "opacity", t, t + 0.35, "1", ".35")
                m.tween(f"#li{k} .lab", "opacity", t, t + 0.35, "1", ".45")
        m.tween(".sfocus", "opacity", t, t + 0.25, "0", "1")
    return Chart(css, body, m, js, ("plot",), focal)


def _trend_line(card: Card, accent: str, ph: int) -> Chart:
    values = [_num(r.value) for r in card.rows]
    n = len(values)
    left, right, top, base = PAD + 70, CARD_W - PAD - 70, 96, ph - 86
    peak = max(values) * 1.12 or 1.0
    xs = [left + (right - left) * k / (n - 1) for k in range(n)]
    ys = [base - (base - top) * v / peak for v in values]
    data = [{"i": k, "v": v, "c": accent if row.highlight else INK} for k, (v, row) in enumerate(zip(values, card.rows))]
    css = f"""
.pv {{ position: absolute; width: 260px; margin-left: -130px; text-align: center; font-size: 36px; font-weight: 700;
  letter-spacing: -.03em; white-space: nowrap; }}
.px {{ position: absolute; top: {base + 22}px; width: 200px; margin-left: -100px; text-align: center; }}
"""
    common = (f"width: {CARD_W}, height: {ph}, x: {{type: 'point', domain: d3.range({n}), range: [{left}, {right}], padding: 0}},"
              f" y: {{domain: [0, {peak}], range: [{base}, {top}]}}, color: {{type: 'identity'}}")
    grad = (f'<linearGradient id="ag" gradientUnits="userSpaceOnUse" x1="0" y1="{top}" x2="0" y2="{base}">'
            f'<stop offset="0" stop-color="{accent}" stop-opacity=".26"/><stop offset="1" stop-color="{accent}" stop-opacity="0"/>'
            f"</linearGradient>")
    js = (f"NAV.onBuild(() => {{ const pts = {_data(data)};"
          f" NAV.plot('#tbase', {{{common}, marks: [Plot.ruleY([0], {{stroke: {json.dumps(RULE)}, strokeWidth: 2,"
          f" insetLeft: {left - 30}, insetRight: {CARD_W - right - 30}}})]}});"
          f" NAV.plot('#tarea', {{{common}, marks: [NAV.defs({json.dumps(grad)}), Plot.areaY(pts, {{x: 'i', y: 'v', fill: 'url(#ag)', curve: 'monotone-x'}})]}});"
          f" NAV.plot('#tline', {{{common}, marks: [Plot.lineY(pts, {{x: 'i', y: 'v', stroke: {json.dumps(INK)}, strokeWidth: {STROKE},"
          f" curve: 'monotone-x', strokeLinejoin: 'round'}})]}});"
          f" const dots = NAV.plot('#tdots', {{{common}, marks: [Plot.dot(pts, {{x: 'i', y: 'v', r: 9, fill: 'c', stroke: {json.dumps(CARD_BG)},"
          f" strokeWidth: 5, className: 'pt'}})]}});" + _plot_tags("dots", [("pt", "p")]) + " });")
    m = Motion()
    parts = ['<div class="lyr" id="tbase"></div><div class="lyr" id="tarea"></div><div class="lyr" id="tline"></div>'
             '<div class="lyr" id="tdots"></div>']
    times = [_at(card, r.reveal_at, 0.55 + 0.6 * k) for k, r in enumerate(card.rows)]
    for k, (row, x, y) in enumerate(zip(card.rows, xs, ys)):
        parts.append(f'<div class="pv num" id="pv{k}" style="left:{x:.0f}px;top:{y - 70:.0f}px;color:{accent if row.highlight else INK}">'
                     f'{_roll(m, row.value, times[k] + 0.05, align="center")}</div>'
                     f'<div class="px axis" id="px{k}" style="left:{x:.0f}px">{_e(row.label)}</div>')
    m.wipe_x("#tbase", FIRST, FIRST + 0.5, left - 30, right + 31)
    for k, t in enumerate(times):
        m.tween(f"#p{k}", "r", t, t + 0.35, "0px", "9px", POP)  # the radius, not a scale: drawn as geometry every frame
        m.fade_up(f"#pv{k}", t + 0.05)
        m.tween(f"#px{k}", "opacity", t, t + 0.3, "0", "1")
        if k:
            t0 = max(t - 0.5, times[k - 1] + 0.05)
            for layer in ("#tline", "#tarea"):
                m.wipe_x(layer, t0, t, xs[k - 1] + 1, xs[k] + 1)
        else:
            for layer in ("#tline", "#tarea"):
                m.wipe_x(layer, 0.0, 0.01, xs[0], xs[0])
    hl = next((k for k, r in enumerate(card.rows) if r.highlight), n - 1)
    return Chart(css, "".join(parts), m, js, ("plot",), (xs[hl], ys[hl]))


def _timeline(card: Card, accent: str, ph: int) -> Chart:
    n = len(card.rows)
    left, right, axis_y = PAD + 150, CARD_W - PAD - 150, ph * 0.5 + 10
    xs = [left + (right - left) * k / (n - 1) for k in range(n)]
    css = f"""
.when {{ position: absolute; top: {axis_y - 136:.0f}px; width: 320px; margin-left: -160px; text-align: center;
  font-size: 56px; font-weight: 700; letter-spacing: -.03em; white-space: nowrap; }}
.what {{ position: absolute; top: {axis_y + 56:.0f}px; width: 300px; margin-left: -150px; text-align: center;
  font-size: 32px; font-weight: 500; line-height: 1.22; color: {SOFT}; max-height: 84px; overflow: hidden; }}
"""
    common = (f"width: {CARD_W}, height: {ph}, x: {{type: 'point', domain: d3.range({n}), range: [{left}, {right}], padding: 0}},"
              f" y: {{type: 'identity'}}")
    js = (f"NAV.onBuild(() => {{ const pts = d3.range({n});"
          f" NAV.plot('#tltrack', {{{common}, marks: [Plot.ruleY([{axis_y:.1f}], {{stroke: {json.dumps(RULE)}, strokeWidth: {STROKE},"
          f" insetLeft: {left}, insetRight: {CARD_W - right}}})]}});"
          f" NAV.plot('#tlprog', {{{common}, marks: [Plot.ruleY([{axis_y:.1f}], {{stroke: {json.dumps(INK)}, strokeWidth: {STROKE},"
          f" insetLeft: {left}, insetRight: {CARD_W - right}}})]}});"
          f" const nodes = NAV.plot('#tlnodes', {{{common}, marks: ["
          f" Plot.dot(pts, {{x: d => d, y: {axis_y:.1f}, r: 38, fill: {json.dumps(accent)}, className: 'rg'}}),"
          f" Plot.dot(pts, {{x: d => d, y: {axis_y:.1f}, r: 16, fill: {json.dumps(CARD_BG)}, stroke: {json.dumps(INK)},"
          f" strokeWidth: {STROKE + 1}, className: 'nd'}})]}});" + _plot_tags("nodes", [("rg", "r"), ("nd", "n")]) + " });")
    m = Motion()
    parts = ['<div class="lyr" id="tltrack"></div><div class="lyr" id="tlprog"></div><div class="lyr" id="tlnodes"></div>']
    for k, (x, row) in enumerate(zip(xs, card.rows)):
        parts.append(f'<div class="when num fit" id="w{k}" style="left:{x:.0f}px">{_e(row.value)}</div>'
                     f'<div class="what fitv" id="d{k}" style="left:{x:.0f}px">{_e(row.detail)}</div>')
    m.wipe_x("#tltrack", FIRST, FIRST + 0.7, left, right + 1)
    times = [_at(card, row.reveal_at, 0.5 + 0.7 * k) for k, row in enumerate(card.rows)]
    for k, t in enumerate(times):
        m.tween(f"#n{k}", "r", t, t + 0.35, "0px", "16px", POP)
        m.fade_up(f"#w{k}", t)
        m.fade_up(f"#d{k}", t + 0.15, rise=8)
        m.tween(f"#n{k}", "fill", t, t + 0.25, CARD_BG, accent)
        m.tween(f"#n{k}", "stroke", t, t + 0.25, INK, accent)
        m.tween(f"#w{k}", "color", t, t + 0.25, INK, accent)
        m.tween(f"#r{k}", "opacity", t, t + 0.3, "0", ".22")
        if k + 1 < n:  # the newest point is the active one
            nxt = times[k + 1]
            m.tween(f"#n{k}", "fill", nxt, nxt + 0.25, accent, INK)
            m.tween(f"#n{k}", "stroke", nxt, nxt + 0.25, accent, INK)
            m.tween(f"#w{k}", "color", nxt, nxt + 0.25, accent, INK)
            m.tween(f"#r{k}", "opacity", nxt, nxt + 0.25, ".22", "0")
        if k:
            m.wipe_x("#tlprog", max(t - 0.45, times[k - 1] + 0.05), t, xs[k - 1], xs[k])
        else:
            m.wipe_x("#tlprog", 0.0, 0.01, left, left)
    return Chart(css, "".join(parts), m, js, ("plot",), (xs[-1], axis_y))


def _curve_layers(names: list[str]) -> str:
    return "".join(f'<div class="lyr" id="{name}"></div>' for name in names)


DASH_SETUP = ("for (const id of IDS) { const el = document.getElementById(id); if (el) { el.setAttribute('pathLength', '1');"
              " el.style.strokeDasharray = '1'; el.style.strokeDashoffset = '1'; } }")


def _drawdown_curve(card: Card, accent: str, ph: int) -> Chart:
    """A fall and the recovery, shown rather than told: a line climbs to its peak, drops
    by the percentage said (bracketed, in rose) as it's said, then climbs back past the
    old peak as the recovery is said. The ground lost below the old peak fills rose and
    the new ground above it emerald. The shape is a sketch: no axis, no dates; the only
    claims on it are the two the speaker made."""
    fall, recovery = card.rows
    left, peak_x, trough_x, cross_x, right = PAD + 76, 330, 560, 800, CARD_W - PAD - 40
    peak_y, trough_y = round(ph * 0.36), round(ph * 0.86)
    end_y = peak_y - 0.24 * (trough_y - peak_y)
    start_y = peak_y + 70

    def bezier(p0, p1, p2, p3, count=28):
        out = []
        for j in range(count + 1):
            t = j / count
            out.append({"x": round((1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0] + 3 * (1 - t) * t * t * p2[0] + t ** 3 * p3[0], 2),
                        "y": round((1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1] + 3 * (1 - t) * t * t * p2[1] + t ** 3 * p3[1], 2)})
        return out

    rise = bezier((left, start_y), (left + 90, start_y - 10), (peak_x - 90, peak_y), (peak_x, peak_y))
    drop = bezier((peak_x, peak_y), (peak_x + 80, peak_y), (trough_x - 90, trough_y), (trough_x, trough_y))
    back = bezier((trough_x, trough_y), (trough_x + 110, trough_y), (cross_x - 90, peak_y + 10), (cross_x, peak_y))
    tail = bezier((cross_x, peak_y), (cross_x + 90, peak_y - 10), (right - 40, end_y), (right, end_y))
    every = rise + drop[1:] + back[1:] + tail[1:]
    middle = (peak_x + trough_x) / 2
    grads = (f'<linearGradient id="under" gradientUnits="userSpaceOnUse" x1="0" y1="{peak_y}" x2="0" y2="{trough_y}">'
             f'<stop offset="0" stop-color="{NEG}" stop-opacity="0"/><stop offset="1" stop-color="{NEG}" stop-opacity=".30"/>'
             f'</linearGradient><linearGradient id="over" gradientUnits="userSpaceOnUse" x1="0" y1="{peak_y}" x2="0" y2="{end_y:.0f}">'
             f'<stop offset="0" stop-color="{POS}" stop-opacity="0"/><stop offset="1" stop-color="{POS}" stop-opacity=".26"/>'
             "</linearGradient>")
    common = f"width: {CARD_W}, height: {ph}, x: {{type: 'identity'}}, y: {{type: 'identity'}}"
    seg = f"strokeWidth: {STROKE + 1}, strokeLinecap: 'round', curve: 'linear'"
    js = (f"NAV.onBuild(() => {{ const all = {_data(every)}; const IDS = ['c1', 'c2', 'c3'];"
          f" NAV.plot('#dref', {{{common}, marks: [Plot.ruleY([{peak_y}], {{stroke: {json.dumps(RULE)}, strokeWidth: 2,"
          f" strokeDasharray: '10 10', insetLeft: {left}, insetRight: {CARD_W - right}}})]}});"
          # the threshold: the area between the old peak and the curve, below it (lost) and above it (new highs)
          f" NAV.plot('#dunder', {{{common}, marks: [NAV.defs({json.dumps(grads)}), Plot.areaY(all, {{x: 'x', y1: {peak_y},"
          f" y2: d => Math.max(d.y, {peak_y}), fill: 'url(#under)', curve: 'linear'}})]}});"
          f" NAV.plot('#dover', {{{common}, marks: [NAV.defs({json.dumps(grads.replace('id=\"', 'id=\"o'))}), Plot.areaY(all, {{x: 'x', y1: {peak_y},"
          f" y2: d => Math.min(d.y, {peak_y}), fill: 'url(#oover)', curve: 'linear'}})]}});"
          f" NAV.plot('#dbracket', {{{common}, marks: [Plot.ruleX([{trough_x}], {{y1: {peak_y}, y2: {trough_y - 12}, stroke: {json.dumps(NEG)},"
          f" strokeWidth: 3, strokeDasharray: '6 6'}}), Plot.ruleY([{peak_y}], {{x1: {trough_x - 14}, x2: {trough_x + 14},"
          f" stroke: {json.dumps(NEG)}, strokeWidth: 3}})]}});"
          f" const c = NAV.plot('#dcurve', {{{common}, marks: ["
          f" Plot.line({_data(rise)}, {{x: 'x', y: 'y', stroke: {json.dumps(INK)}, {seg}, className: 'c1'}}),"
          f" Plot.line({_data(drop)}, {{x: 'x', y: 'y', stroke: {json.dumps(NEG)}, {seg}, className: 'c2'}}),"
          f" Plot.line({_data(back + tail[1:])}, {{x: 'x', y: 'y', stroke: {json.dumps(POS)}, {seg}, className: 'c3'}})]}});"
          " NAV.tag(c, 'c1', 'c1', true); NAV.tag(c, 'c2', 'c2', true); NAV.tag(c, 'c3', 'c3', true); " + DASH_SETUP + " });")
    css = f"""
.fallv {{ position: absolute; left: {middle - 180:.0f}px; width: 360px; top: {peak_y - 112}px; text-align: center;
  font-size: 68px; font-weight: 700; letter-spacing: -.04em; color: {NEG}; white-space: nowrap; }}
.falll {{ position: absolute; left: {middle - 220:.0f}px; width: 440px; top: {peak_y - 152}px; text-align: center;
  font-size: 28px; font-weight: 520; color: {SOFT}; white-space: nowrap; }}
.backl {{ position: absolute; right: {CARD_W - right}px; top: {end_y - 96:.0f}px; text-align: right; font-size: 36px;
  font-weight: 650; letter-spacing: -.01em; color: {POS}; white-space: nowrap; max-width: 520px; }}
.backv {{ font-size: 28px; font-weight: 520; color: {SOFT}; }}
"""
    m = Motion()
    t_fall = _at(card, fall.reveal_at, 0.95)
    t_back = max(_rel(card, recovery.reveal_at, t_fall + 1.4), t_fall + 0.8)
    parts = [
        _curve_layers(["dref", "dunder", "dover", "dbracket", "dcurve"]),
        f'<div class="fallv num fit" id="fv">{_roll(m, fall.value, t_fall + 0.25, align="center")}</div>',
        f'<div class="falll" id="fl">{_e(fall.label)}</div>' if fall.label else "",
        f'<div class="backl" id="bl">{_e(recovery.label)}'
        + (f'<div class="backv num">{_e(recovery.value)}</div>' if recovery.value else "") + "</div>",
    ]
    m.tween("#dref", "opacity", FIRST - 0.05, FIRST + 0.35, "0", "1")
    m.tween("#c1", "strokeDashoffset", FIRST, max(FIRST + 0.3, min(FIRST + 0.7, t_fall - 0.1)), "1", "0")
    m.tween("#c2", "strokeDashoffset", t_fall, t_fall + 0.7, "1", "0")
    m.wipe_x("#dunder", t_fall, t_fall + 0.7, peak_x, trough_x)
    m.wipe_x("#dunder", t_back, t_back + 0.55, trough_x, cross_x + 1)
    m.tween("#dbracket", "clipPath", t_fall + 0.1, t_fall + 0.6, "inset(0 0 100% 0)", "inset(0 0 0% 0)")
    m.fade_up("#fv", t_fall + 0.25)
    if fall.label:
        m.fade_up("#fl", t_fall + 0.15, rise=8)
    m.tween("#c3", "strokeDashoffset", t_back, t_back + 1.0, "1", "0")
    m.wipe_x("#dover", t_back + 0.5, t_back + 1.0, cross_x, right + 2)
    m.fade_up("#bl", t_back + 0.5)
    return Chart(css, "".join(parts), m, js, ("plot",), (trough_x, (peak_y + trough_y) / 2))


def _cac_payback(card: Card, accent: str, ph: int) -> Chart:
    """The cash a customer brings in, from minus what it cost to win them, through zero at
    the payback month, to the same again in profit. Underwater shades rose below the
    zero line, profit emerald above it."""
    cost, months = card.rows
    left, right, top, base = PAD + 76, CARD_W - PAD - 24, 60, ph - 70
    x_be = (left + right) / 2  # break-even sits halfway: the axis runs to twice the payback period
    y_be = (base + top) / 2  # zero, halfway up
    grads = (f'<linearGradient id="uw" gradientUnits="userSpaceOnUse" x1="0" y1="{y_be}" x2="0" y2="{base}">'
             f'<stop offset="0" stop-color="{NEG}" stop-opacity=".06"/><stop offset="1" stop-color="{NEG}" stop-opacity=".30"/>'
             f'</linearGradient><linearGradient id="pf" gradientUnits="userSpaceOnUse" x1="0" y1="{y_be}" x2="0" y2="{top}">'
             f'<stop offset="0" stop-color="{POS}" stop-opacity=".06"/><stop offset="1" stop-color="{POS}" stop-opacity=".28"/>'
             "</linearGradient>")
    common = f"width: {CARD_W}, height: {ph}, x: {{domain: [0, 2], range: [{left}, {right}]}}, y: {{domain: [-1, 1], range: [{base}, {top}]}}"
    line = [{"x": 0, "y": -1}, {"x": 1, "y": 0}, {"x": 2, "y": 1}]
    js = (f"NAV.onBuild(() => {{ const L = {_data(line)}; const IDS = ['c1', 'c2'];"
          f" NAV.plot('#cax', {{{common}, marks: [Plot.ruleX([0], {{y1: -1, y2: 1, stroke: {json.dumps(RULE)}, strokeWidth: 2, insetTop: -20}}),"
          f" Plot.ruleY([-1], {{x1: 0, x2: 2, stroke: {json.dumps(RULE)}, strokeWidth: 2, insetRight: -20}})]}});"
          f" NAV.plot('#cuw', {{{common}, marks: [NAV.defs({json.dumps(grads)}), Plot.areaY(L.slice(0, 2), {{x: 'x', y1: 0, y2: 'y', fill: 'url(#uw)'}})]}});"
          f" NAV.plot('#cpf', {{{common}, marks: [NAV.defs({json.dumps(grads.replace('id=\"', 'id=\"p'))}), Plot.areaY(L.slice(1), {{x: 'x', y1: 0, y2: 'y', fill: 'url(#ppf)'}})]}});"
          f" NAV.plot('#cth', {{{common}, marks: [Plot.ruleY([0], {{x1: 0, x2: 2, stroke: {json.dumps(NEG)}, strokeWidth: 2.5, strokeDasharray: '8 8'}})]}});"
          f" const c = NAV.plot('#ccurve', {{{common}, marks: ["
          f" Plot.line(L.slice(0, 2), {{x: 'x', y: 'y', stroke: {json.dumps(INK)}, strokeWidth: {STROKE + 1}, strokeLinecap: 'round', className: 'c1'}}),"
          f" Plot.line(L.slice(1), {{x: 'x', y: 'y', stroke: {json.dumps(POS)}, strokeWidth: {STROKE + 1}, strokeLinecap: 'round', className: 'c2'}})]}});"
          " NAV.tag(c, 'c1', 'c1', true); NAV.tag(c, 'c2', 'c2', true); " + DASH_SETUP + " });")
    css = f"""
.tlab {{ position: absolute; left: {left + 22}px; top: {top + 2}px; }}
.tlab .lab {{ display: block; font-size: 26px; }} .tlab .val {{ display: block; font-size: 38px; }}
.zone {{ position: absolute; font-size: 24px; font-weight: 600; letter-spacing: .04em; color: {SOFT}; }}
.be {{ position: absolute; left: {x_be:.0f}px; top: {y_be:.0f}px; width: 24px; height: 24px; margin: -12px 0 0 -12px;
  border-radius: 50%; background: {accent}; box-shadow: 0 0 0 10px {_rgba(accent, 0.2)}; }}
.betag {{ position: absolute; left: {x_be - 140:.0f}px; top: {y_be - 146:.0f}px; width: 280px; text-align: center;
  padding: 10px 0 12px; border: {FOCUS}px solid {accent}; border-radius: 10px; background: {CARD_BG}; }}
.betag .lab {{ display: block; font-size: 24px; color: {accent}; font-weight: 650; }}
.betag .val {{ display: block; font-size: 34px; }}
.xt {{ position: absolute; top: {base + 16}px; width: 200px; margin-left: -100px; text-align: center; }}
"""
    m = Motion()
    t_cost = _at(card, cost.reveal_at, 0.5)
    t_be = max(t_cost + 0.6, _rel(card, months.reveal_at, t_cost + 1.0))
    parts = [
        _curve_layers(["cax", "cuw", "cpf", "cth", "ccurve"]),
        f'<div class="tlab" id="tl"><span class="lab">{_e(cost.label)}</span><span class="val num">'
        f'{_roll(m, cost.value, t_cost, align="left")}</span></div>',
        f'<div class="zone" id="zu1" style="left:{left + 22}px;top:{y_be + 22:.0f}px">Underwater</div>',
        f'<div class="zone" id="zp1" style="right:{CARD_W - right + 6}px;top:{y_be - 52:.0f}px">In profit</div>',
        f'<div class="be" id="be"></div><div class="betag" id="bt"><span class="lab">{_e(months.label or "Break-even")}</span>'
        f'<span class="val num">{_roll(m, months.value, t_be + 0.1, align="center")}</span></div>',
        f'<div class="xt axis" style="left:{left}px">0</div><div class="xt axis" id="xp" style="left:{x_be:.0f}px">'
        f'{_e(months.value)}</div>',
    ]
    m.tween("#cax", "opacity", FIRST - 0.05, FIRST + 0.3, "0", "1")
    m.wipe_x("#cth", t_cost, t_cost + 0.6, left, right + 2)
    m.fade_up("#tl", t_cost)
    m.tween("#c1", "strokeDashoffset", t_cost + 0.2, t_be, "1", "0", "cubic-bezier(0.4, 0, 0.2, 1)")
    m.wipe_x("#cuw", t_cost + 0.2, t_be, left, x_be, easing="cubic-bezier(0.4, 0, 0.2, 1)")
    m.tween("#zu1", "opacity", t_be - 0.4, t_be, "0", "1")
    m.tween("#be", "transform", t_be, t_be + 0.35, "scale(0)", "scale(1)", POP)
    m.fade_up("#bt", t_be + 0.1, rise=12)
    m.tween("#xp", "opacity", t_be, t_be + 0.3, "0", "1")
    m.tween("#c2", "strokeDashoffset", t_be + 0.2, t_be + 1.0, "1", "0")
    m.wipe_x("#cpf", t_be + 0.2, t_be + 1.0, x_be, right)
    m.tween("#zp1", "opacity", t_be + 0.7, t_be + 1.0, "0", "1")
    return Chart(css, "".join(parts), m, js, ("plot",), (x_be, y_be))


def _js(template: str, **values: object) -> str:
    """A page script from a template: each __NAME__ replaced by its value."""
    for name, value in values.items():
        template = template.replace(f"__{name}__", str(value))
    return template


ARR_JS = """NAV.onBuild(() => {
  const D = __DATA__;
  const chart = echarts.init(document.getElementById('ec'), null, {renderer: 'svg', width: __W__, height: __H__});
  const draw = t => {
    const base = [], val = [], lines = [];
    D.items.forEach((it, k) => {
      const p = NAV.prog(t, it.t0, it.t0 + 0.6); let b, h;
      if (it.kind === 'step') { const s = it.after - it.before; if (s >= 0) { b = it.before; h = s * p; } else { b = it.before + s * p; h = -s * p; } }
      else { b = 0; h = it.after * p; }
      const lp = NAV.prog(t, it.t0 + 0.1, it.t0 + 0.1 + __ROLL__);
      base.push(b);
      val.push({value: h, itemStyle: {color: it.color}, label: {show: lp > 0.001, formatter: NAV.rollText(it.text, lp),
        color: NAV.alpha(it.tcolor, Math.min(1, lp * 1.6))}});
      if (k > 0) { const level = D.items[k - 1].after, cp = NAV.prog(t, it.t0 - 0.25, it.t0 + 0.1);
        lines.push([{coord: [k - 1, level], lineStyle: {opacity: cp}}, {coord: [k, level]}]); }
    });
    chart.setOption({animation: false, silent: true,
      grid: {left: __PAD__, right: __PAD__, top: 70, bottom: 92},
      xAxis: {type: 'category', data: D.labels, axisTick: {show: false}, axisLine: {lineStyle: {color: __RULE__, width: 2}},
        axisLabel: {interval: 0, color: __SOFT__, fontFamily: 'Geist', fontSize: 24, fontWeight: 500, lineHeight: 28,
          width: __SLOT__, overflow: 'break', margin: 20}},
      yAxis: {type: 'value', show: false, min: 0, max: D.peak},
      series: [
        {type: 'bar', stack: 'w', data: base, barWidth: '58%', z: 1, itemStyle: {color: 'transparent'},
         markLine: {silent: true, symbol: 'none', label: {show: false}, animation: false,
           lineStyle: {type: [6, 6], color: '#5A6272', width: 2}, data: lines}},
        {type: 'bar', stack: 'w', data: val, barWidth: '58%', z: 3,
         label: {position: 'top', distance: 12, fontFamily: 'JetBrains Mono', fontSize: 30, fontWeight: 700}}]}, true);
    chart.getZr().refreshImmediately();
  };
  NAV.onFrame(draw); draw(0);
});"""

SANKEY_JS = """NAV.onBuild(() => {
  const D = __DATA__;
  const chart = echarts.init(document.getElementById('sk'), null, {renderer: 'svg', width: __W__, height: __H__});
  const side = pos => pos === 'left' ? 'right' : (pos === 'top' ? 'center' : 'left');
  const draw = t => {
    const nodes = D.nodes.map(nd => {
      if (nd.dummy) return {name: nd.id, depth: nd.depth, itemStyle: {opacity: 0}, label: {show: false}};
      const p = NAV.prog(t, nd.t0, nd.t0 + 0.4), lp = NAV.prog(t, nd.t0 + 0.05, nd.t0 + 0.05 + __ROLL__), a = Math.min(1, lp * 1.6);
      return {name: nd.id, depth: nd.depth, itemStyle: {color: nd.color, opacity: p, borderWidth: 0},
        label: {show: lp > 0.001, position: nd.pos, distance: 14, align: side(nd.pos),
          formatter: '{l|' + nd.label + '}\\n{v|' + NAV.rollText(nd.value, lp) + '}',
          rich: {l: {fontFamily: 'Geist', fontSize: 24, fontWeight: 500, lineHeight: 30, color: NAV.alpha(__SOFT__, a), align: side(nd.pos)},
                 v: {fontFamily: 'JetBrains Mono', fontSize: 32, fontWeight: 700, lineHeight: 40, color: NAV.alpha(nd.vcolor, a), align: side(nd.pos)}}}};
    });
    const links = D.links.map(ln => ({source: ln.s, target: ln.t, value: ln.v,
      lineStyle: {color: ln.color, opacity: ln.dummy ? 0 : ln.op * NAV.prog(t, ln.t0, ln.t0 + 0.6), curveness: 0.5}}));
    chart.setOption({animation: false, silent: true, series: [{type: 'sankey', left: 200, right: 236, top: 104, bottom: 40,
      nodeWidth: 16, nodeGap: 30, nodeAlign: 'left', layoutIterations: 0, draggable: false, emphasis: {disabled: true},
      data: nodes, links: links}]}, true);
    chart.getZr().refreshImmediately();
  };
  NAV.onFrame(draw); draw(0);
});"""

PRICE_JS = """NAV.onBuild(() => {
  const D = __DATA__;
  const chart = LightweightCharts.createChart(document.getElementById('lw'), {width: __W__, height: __H__, autoSize: false,
    layout: {background: {type: 'solid', color: 'rgba(0,0,0,0)'}, textColor: __SOFT__, fontFamily: 'JetBrains Mono', fontSize: 21,
      attributionLogo: false},
    grid: {vertLines: {visible: false}, horzLines: {color: 'rgba(255,255,255,0.06)'}},
    rightPriceScale: {borderVisible: false, scaleMargins: {top: 0.06, bottom: 0.24}, entireTextOnly: true},
    timeScale: {visible: false, borderVisible: false, rightOffset: 0},
    crosshair: {vertLine: {visible: false, labelVisible: false}, horzLine: {visible: false, labelVisible: false}},
    handleScroll: false, handleScale: false});
  const fixed = (lo, hi) => () => ({priceRange: {minValue: lo, maxValue: hi}});
  const candles = chart.addSeries(LightweightCharts.CandlestickSeries, {upColor: __POS__, downColor: __NEG__, wickUpColor: __POS__,
    wickDownColor: __NEG__, borderVisible: false, lastValueVisible: false, priceLineVisible: false, autoscaleInfoProvider: fixed(D.low, D.high)});
  const ema = chart.addSeries(LightweightCharts.LineSeries, {color: __ACCENT__, lineWidth: 3, lastValueVisible: false,
    priceLineVisible: false, crosshairMarkerVisible: false, autoscaleInfoProvider: fixed(D.low, D.high)});
  const vol = chart.addSeries(LightweightCharts.HistogramSeries, {priceScaleId: 'vol', lastValueVisible: false,
    priceLineVisible: false, priceFormat: {type: 'volume'}, autoscaleInfoProvider: fixed(0, D.vmax)});
  chart.priceScale('vol').applyOptions({scaleMargins: {top: 0.82, bottom: 0}, visible: false});
  NAV.canvas();
  let shown = [-1, -1];
  const draw = t => {
    const c = Math.round(D.n * NAV.prog(t, __T0__, __T1__)), e = Math.round(D.n * NAV.prog(t, __E0__, __E1__));
    if (c === shown[0] && e === shown[1]) return;
    shown = [c, e];
    candles.setData(D.candles.map((d, i) => i < c ? d : {time: d.time}));
    vol.setData(D.volume.map((d, i) => i < c ? d : {time: d.time}));
    ema.setData(D.ema.map((d, i) => i < e ? d : {time: d.time}));
    chart.timeScale().setVisibleLogicalRange({from: -0.6, to: D.n - 0.4});
  };
  NAV.onFrame(draw); draw(0);
});"""

ECHARTS_TEXT = re.compile(r"[{}|]")


def _ec_text(text: str) -> str:
    """Text for an ECharts label (its template characters dropped)."""
    return ECHARTS_TEXT.sub("", str(text or ""))


def _arr_bridge(card: Card, accent: str, ph: int) -> Chart:
    """The ARR bridge as an ECharts waterfall (SVG): start, each + and − step, the end.
    Each bar grows from the level before it as it's said, its value rolling up; dashed
    connectors carry the level across."""
    rows = card.rows
    n = len(rows)
    start, end = _num(rows[0].value), _num(rows[-1].value)
    levels, running = [start], start
    for row in rows[1:-1]:
        running += signed_value(row.value)
        levels.append(running)
    peak = max(levels + [end]) * 1.12 or 1.0
    times = [_at(card, r.reveal_at, 0.5 + 0.55 * k) for k, r in enumerate(rows)]
    items = []
    for k, row in enumerate(rows):
        if k == 0:
            kind, before, after, color, text_color = "total", 0.0, start, GRAY, INK
        elif k == n - 1:
            kind, before, after, color, text_color = "total", 0.0, end, accent, INK
        else:
            before, after = levels[k - 1], levels[k]
            kind, color = "step", (POS if after >= before else NEG)
            text_color = color
        items.append({"kind": kind, "before": before, "after": after, "text": _ec_text(row.value), "color": color,
                      "tcolor": text_color, "t0": round(times[k], 3)})
    slot = IW / n
    data = {"items": items, "labels": [_ec_text(r.label) for r in rows], "peak": peak}
    js = _js(ARR_JS, DATA=_data(data), W=CARD_W, H=ph, PAD=PAD, RULE=json.dumps(RULE), SOFT=json.dumps(SOFT),
             SLOT=f"{slot - 10:.0f}", ROLL=ROLL)
    m = Motion()
    m.hold(min(times) - 0.3, max(times) + 0.1 + ROLL)
    hl = n - 1
    x_end = PAD + slot * (hl + 0.5)
    y_end = 70 + (ph - 162) * (1 - end / peak) + 40
    return Chart("#ec { position: absolute; left: 0; top: 0; }", '<div id="ec"></div>', m, js, ("echarts",), (x_end, y_end))


def _sankey_flow(card: Card, accent: str, ph: int) -> Chart:
    """The flow of money as an ECharts Sankey (SVG), in the order the rows were given
    (no reshuffling). Each flow fades in as it's said, then its node and label, the
    value rolling up. A remainder the speaker didn't split out is kept as invisible
    flow, so every node's height matches the number on it."""
    rows = card.rows
    values = [_num(r.value) for r in rows]
    by_label = {r.label: k for k, r in enumerate(rows)}
    parent = [by_label.get(r.parent, -1) if r.parent else -1 for r in rows]
    depth = [0] * len(rows)
    for k in range(len(rows)):
        p, d = parent[k], 0
        while p >= 0 and d < 4:
            d, p = d + 1, parent[p]
        depth[k] = d
    times: dict[int, float] = {}
    for k, row in enumerate(rows):
        t = _at(card, row.reveal_at, 0.5 + 0.5 * k)
        times[k] = max(t, times.get(parent[k], 0.0) + 0.3) if parent[k] >= 0 else t
    nodes, links = [], []
    for k, row in enumerate(rows):
        leaf = not any(parent[j] == k for j in range(len(rows)))
        position = "left" if parent[k] < 0 else ("right" if leaf else "top")
        node_t = times[k] + (0.45 if parent[k] >= 0 else 0.0)
        nodes.append({"id": f"n{k}", "depth": depth[k], "label": _ec_text(row.label), "value": _ec_text(row.value),
                      "color": accent if row.highlight else "#9AA2AF", "vcolor": accent if row.highlight else INK,
                      "pos": position, "t0": round(node_t, 3), "dummy": False})
        if parent[k] >= 0:
            links.append({"s": f"n{parent[k]}", "t": f"n{k}", "v": max(values[k], 1e-9),
                          "color": "gradient", "op": 0.5 if row.highlight else 0.3,
                          "t0": round(times[k], 3), "dummy": False})
    for k in range(len(rows)):  # what the speaker didn't split out of a node: kept, unseen
        kids = [j for j in range(len(rows)) if parent[j] == k]
        rest = values[k] - sum(values[j] for j in kids)
        if kids and rest > values[k] * 0.02:
            nodes.append({"id": f"r{k}", "depth": depth[k] + 1, "label": "", "value": "", "color": CARD_BG, "vcolor": CARD_BG,
                          "pos": "right", "t0": 0, "dummy": True})
            links.append({"s": f"n{k}", "t": f"r{k}", "v": rest, "color": CARD_BG, "op": 0, "t0": 0, "dummy": True})
    data = {"nodes": nodes, "links": links}
    js = _js(SANKEY_JS, DATA=_data(data), W=CARD_W, H=ph, SOFT=json.dumps(SOFT), ROLL=ROLL)
    m = Motion()
    real = [n for n in nodes if not n["dummy"]]
    m.hold(min(times.values()) - 0.05, max(n["t0"] for n in real) + 0.05 + ROLL)
    return Chart("#sk { position: absolute; left: 0; top: 0; }", '<div id="sk"></div>', m, js, ("echarts",), (CARD_W / 2, ph / 2))


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


def _candlestick_ema(card: Card, accent: str, ph: int) -> Chart:
    """The price chart, drawn by TradingView Lightweight Charts from the file you
    supplied: candles and volume come in left to right, the EMA trailing them. Its scales
    are fixed to the whole file first, so nothing rescales as candles arrive."""
    prices = read_prices(card.data_path)
    n = len(prices)
    low = min(p["low"] for p in prices)
    high = max(p["high"] for p in prices)
    pad = (high - low) * 0.06 or 1.0
    period = 20 if n >= 30 else max(3, n // 3)
    alpha, ema = 2 / (period + 1), prices[0]["close"]
    candles, volume, line = [], [], []
    for k, p in enumerate(prices):
        time = 1_700_000_000 + 86_400 * k  # the file's dates aren't drawn: only the order matters
        ema = p["close"] if k == 0 else alpha * p["close"] + (1 - alpha) * ema
        candles.append({"time": time, "open": p["open"], "high": p["high"], "low": p["low"], "close": p["close"]})
        volume.append({"time": time, "value": p["volume"], "color": _rgba(POS if p["close"] >= p["open"] else NEG, 0.28)})
        line.append({"time": time, "value": round(ema, 6)})
    data = {"n": n, "candles": candles, "volume": volume, "ema": line, "low": low - pad, "high": high + pad,
            "vmax": max(p["volume"] for p in prices) or 1.0}
    t0, t1, e0, e1 = 0.45, 2.05, 0.9, 2.6
    js = _js(PRICE_JS, DATA=_data(data), W=IW, H=ph - 36, SOFT=json.dumps(SOFT), POS=json.dumps(POS), NEG=json.dumps(NEG),
             ACCENT=json.dumps(accent), T0=t0, T1=t1, E0=e0, E1=e1)
    m = Motion()
    m.hold(t0, e1)
    m.tween("#lw", "opacity", t0 - 0.05, t0 + 0.3, "0", "1")
    key = (f'<div class="key" id="ckey"><span class="src">{_e("Source: " + card.source)}</span><span class="lg">'
           f'<span><i style="width:26px;height:3px;background:{accent}"></i>EMA ({period})</span>'
           f'<span><i style="width:12px;height:18px;background:{_rgba(SOFT, 0.45)}"></i>Volume</span></span></div>')
    m.tween("#ckey", "opacity", 0.3, 0.6, "0", "1")
    css = f"#lw {{ position: absolute; left: {PAD}px; top: 14px; width: {IW}px; height: {ph - 36}px; }}"
    return Chart(css, '<div id="lw"></div>', m, js, ("lightweight",), (CARD_W * 0.7, ph * 0.4), key=key)


def _comparison_card(card: Card, accent: str, ph: int) -> Chart:
    """Round 32: the speaker's contrast as two sides of one card ("We didn't market" vs
    "Then we did, and it worked"). Each side lands on its words; the side the point is
    about (highlight, else the second) takes the accent and the other steps back."""
    main = next((k for k, row in enumerate(card.rows) if row.highlight), len(card.rows) - 1)
    gap, top = 64, 30
    side_w, side_h = (IW - gap) / 2, ph - 2 * top
    css = f"""
.cmp {{ position: absolute; top: {top}px; width: {side_w:.0f}px; height: {side_h:.0f}px; border-radius: 16px;
  border: 1px solid rgba(255,255,255,.10); background: rgba(255,255,255,.025); padding: 40px 34px 34px; }}
.cmp .tag {{ display: inline-block; max-width: 100%; height: 46px; line-height: 44px; padding: 0 18px; border-radius: 23px;
  border: 1px solid rgba(255,255,255,.18); font-size: 24px; font-weight: 650; color: {SOFT}; white-space: nowrap;
  overflow: hidden; margin-bottom: 26px; }}
.cmp .head {{ font-family: "Instrument Serif", Georgia, serif; font-size: 58px; line-height: 1.04; letter-spacing: -.01em;
  max-height: 126px; overflow: hidden; }}
.cmp .line {{ margin-top: 22px; font-size: 31px; font-weight: 500; line-height: 1.28; color: {SOFT}; max-height: 160px;
  overflow: hidden; }}
.cmp .edge {{ position: absolute; left: 0; top: 22px; bottom: 22px; width: 4px; border-radius: 2px; background: {accent};
  opacity: 0; }}
.vs {{ position: absolute; left: {PAD + side_w + gap / 2 - 30:.0f}px; top: {ph / 2 - 30:.0f}px; width: 60px; height: 60px;
  border-radius: 30px; background: {CARD_BG}; border: 1px solid {RULE}; color: {SOFT}; font-size: 20px; font-weight: 700;
  letter-spacing: .08em; display: flex; align-items: center; justify-content: center; }}
"""
    m = Motion()
    parts = []
    times = [_at(card, row.reveal_at, 0.5 + 0.9 * k) for k, row in enumerate(card.rows)]
    for k, row in enumerate(card.rows):
        left = PAD + k * (side_w + gap)
        tag = f'<div class="tag num fit" id="ct{k}">{_e(row.value)}</div>' if row.value.strip() else ""
        line = f'<div class="line fitv">{_e(row.detail)}</div>' if row.detail.strip() else ""
        parts.append(f'<div class="cmp" id="c{k}" style="left:{left:.0f}px"><div class="edge" id="ce{k}"></div>{tag}'
                     f'<div class="head fitv" id="ch{k}">{_e(row.label)}</div>{line}</div>')
        m.fade_up(f"#c{k}", times[k], rise=18, length=0.45)
    parts.append('<div class="vs" id="vs">VS</div>')
    m.tween("#vs", "opacity", times[0] + 0.2, times[0] + 0.5, "0", "1")
    t = times[main] + 0.35
    m.tween(f"#c{main}", "borderColor", t, t + 0.3, "rgba(255,255,255,.10)", _rgba(accent, 0.55))
    m.tween(f"#c{main}", "backgroundColor", t, t + 0.3, "rgba(255,255,255,.025)", _rgba(accent, 0.08))
    m.tween(f"#ce{main}", "opacity", t, t + 0.3, "0", "1")
    m.tween(f"#ch{main}", "color", t, t + 0.3, INK, accent)
    if row_other := [k for k in range(len(card.rows)) if k != main]:
        m.tween(f"#c{row_other[0]}", "opacity", max(t, times[row_other[0]] + 0.5), max(t, times[row_other[0]] + 0.5) + 0.35,
                "1", ".55")
    focal = (PAD + main * (side_w + gap) + side_w / 2, ph / 2)
    return Chart(css, "".join(parts), m, "", (), focal)


def _step_sequence(card: Card, accent: str, ph: int) -> Chart:
    """Round 32: the speaker's framework as 2-4 steps in the order said. A rail draws down
    from step to step as each one is said; the newest step is the active one."""
    n = len(card.rows)
    pitch = min(176.0, (ph - 60) / n)
    top = (ph - pitch * n) / 2
    dot_x = PAD + 34
    text_x = PAD + 100
    css = f"""
.step {{ position: absolute; left: {text_x}px; right: {PAD}px; height: {pitch:.0f}px; display: flex; flex-direction: column;
  justify-content: center; }}
.step .lbl {{ font-size: 48px; font-weight: 650; letter-spacing: -.015em; line-height: 1.08; white-space: nowrap; overflow: hidden; }}
.step .dtl {{ margin-top: 8px; font-size: 30px; font-weight: 500; color: {SOFT}; white-space: nowrap; overflow: hidden; }}
.dot {{ position: absolute; left: {dot_x - 30}px; width: 60px; height: 60px; border-radius: 30px; border: {STROKE}px solid {RULE};
  background: {CARD_BG}; color: {SOFT}; font-size: 24px; font-weight: 700; display: flex; align-items: center;
  justify-content: center; }}
.rail {{ position: absolute; left: {dot_x - STROKE / 2:.0f}px; width: {STROKE}px; background: {RULE}; }}
.rail.on {{ background: {accent}; transform-origin: 50% 0%; }}
"""
    m = Motion()
    parts = []
    centers = [top + pitch * (k + 0.5) for k in range(n)]
    parts.append(f'<div class="rail" style="top:{centers[0]:.0f}px;height:{centers[-1] - centers[0]:.0f}px"></div>')
    times = [_at(card, row.reveal_at, 0.5 + 0.8 * k) for k, row in enumerate(card.rows)]
    for k in range(1, n):
        parts.append(f'<div class="rail on" id="rl{k}" style="top:{centers[k - 1]:.0f}px;'
                     f'height:{centers[k] - centers[k - 1]:.0f}px"></div>')
        m.tween(f"#rl{k}", "transform", max(times[k - 1] + 0.1, times[k] - 0.45), times[k], "scaleY(0)", "scaleY(1)")
    for k, row in enumerate(card.rows):
        detail = f'<div class="dtl fit">{_e(row.detail)}</div>' if row.detail.strip() else ""
        parts.append(f'<div class="dot num" id="d{k}" style="top:{centers[k] - 30:.0f}px">{k + 1:02d}</div>'
                     f'<div class="step" id="s{k}" style="top:{centers[k] - pitch / 2:.0f}px">'
                     f'<div class="lbl fit" id="l{k}">{_e(row.label)}</div>{detail}</div>')
        t = times[k]
        m.fade_up(f"#s{k}", t, rise=14, length=0.4)
        m.tween(f"#d{k}", "borderColor", t, t + 0.25, RULE, accent)
        m.tween(f"#d{k}", "color", t, t + 0.25, SOFT, accent)
        m.tween(f"#l{k}", "color", t, t + 0.25, INK, accent)
        if k + 1 < n:  # the newest step is the active one
            nxt = times[k + 1]
            m.tween(f"#l{k}", "color", nxt, nxt + 0.25, accent, INK)
            m.tween(f"#d{k}", "color", nxt, nxt + 0.25, accent, INK)
    focal = (text_x + 200, centers[-1])
    return Chart(css, "".join(parts), m, "", (), focal)


def _nice_step(raw: float) -> float:
    exponent = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    return next(s * exponent for s in (1, 2, 2.5, 5, 10) if s * exponent >= raw)


def _fmt(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.2f}".rstrip("0").rstrip(".")


OVERLAYS = {"stat_pill": _stat_pill, "kinetic_range": _kinetic_range, "buckets": _buckets, "speaker_title": _speaker_title}
CHARTS = {
    "drawdown_curve": _drawdown_curve, "timeline": _timeline, "metric_bars": _metric_bars, "ranked_bars": _ranked_bars,
    "diverging_bar": _diverging_bar, "share_split": _share_split, "trend_line": _trend_line, "arr_bridge": _arr_bridge,
    "sankey_flow": _sankey_flow, "cac_payback": _cac_payback, "candlestick_ema": _candlestick_ema,
    "comparison_card": _comparison_card, "step_sequence": _step_sequence,
}
BUILDERS = {**OVERLAYS, **CHARTS}

# The page's runtime: easing, rolling numbers, Plot helpers and the frame clock.
RUNTIME = r"""
window.NAV = (() => {
  const bezier = (x1, y1, x2, y2) => {
    const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx, cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
    const sx = t => ((ax * t + bx) * t + cx) * t, sy = t => ((ay * t + by) * t + cy) * t;
    return x => { if (x <= 0) return 0; if (x >= 1) return 1; let lo = 0, hi = 1, t = x;
      for (let i = 0; i < 40; i++) { const v = sx(t); if (Math.abs(v - x) < 1e-7) break; if (v < x) lo = t; else hi = t; t = (lo + hi) / 2; }
      return sy(t); };
  };
  const ease = bezier(0.16, 1, 0.3, 1);
  const prog = (t, t0, t1) => t <= t0 ? 0 : t >= t1 ? 1 : ease((t - t0) / (t1 - t0));
  const digits = /\d[\d,]*(?:\.\d+)?/g;
  const rollText = (text, p) => p >= 1 ? text : String(text).replace(digits, m => {
    const places = (m.split('.')[1] || '').length; let s = (parseFloat(m.replace(/,/g, '')) * p).toFixed(places);
    if (m.includes(',')) { const [i, f] = s.split('.'); s = i.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (f !== undefined ? '.' + f : ''); }
    return s; });
  const rolls = t => { for (const el of document.querySelectorAll('[data-roll]'))
    el.textContent = rollText(el.dataset.roll, prog(t, +el.dataset.t0, +el.dataset.t1)); };
  const alpha = (hex, a) => { const h = hex.replace('#', ''); const n = parseInt(h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${Math.max(0, Math.min(1, a))})`; };
  const plot = (sel, opts) => { opts = Object.assign({margin: 0, style: 'overflow: visible; background: transparent;'}, opts);
    for (const s of ['x', 'y']) opts[s] = Object.assign({axis: null}, opts[s]);  // the card draws its own labels
    const svg = Plot.plot(opts);
    document.querySelector(sel).append(svg); return svg; };
  const defs = markup => () => { const d = document.createElementNS('http://www.w3.org/2000/svg', 'defs'); d.innerHTML = markup; return d; };
  const tag = (svg, cls, prefix, single) => svg.querySelectorAll('g.' + cls + ' > *').forEach((el, i) => { el.id = single ? prefix : prefix + i; });
  const builds = [], hooks = []; let paint = false;
  return {ease, prog, rollText, rolls, alpha, plot, defs, tag, builds, hooks,
          onBuild: f => builds.push(f), onFrame: f => hooks.push(f), canvas: () => { paint = true; }, needsPaint: () => paint};
})();
window.seekToFrame = async (frameIndex, fps) => {
  const ms = frameIndex * 1000 / fps, t = ms / 1000;
  for (const a of document.getAnimations()) a.currentTime = ms;
  NAV.rolls(t);
  for (const hook of NAV.hooks) await hook(t);
  if (NAV.needsPaint()) await new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
};
"""

_SETUP = """async (tracks) => {
  await document.fonts.ready;
  await Promise.all(['400 60px "Instrument Serif"', '500 24px Geist', '650 30px Geist', '700 40px "JetBrains Mono"',
                     '500 22px "JetBrains Mono"'].map(f => document.fonts.load(f)));
  for (const build of NAV.builds) await build();
  const fit = el => { let size = parseFloat(getComputedStyle(el).fontSize), floor = size * 0.62;
    while (el.scrollWidth > el.clientWidth + 1 && size > floor) { size -= 2; el.style.fontSize = size + 'px'; } };
  document.querySelectorAll('.fit').forEach(fit);
  const over = [];
  for (const el of document.querySelectorAll('.fit')) if (el.scrollWidth > el.clientWidth + 1) over.push(el.textContent);
  for (const el of document.querySelectorAll('.fitv')) if (el.scrollHeight > el.clientHeight + 1) over.push(el.textContent);
  for (const el of document.querySelectorAll('[data-roll]')) {  // a rolling number keeps its final width: nothing shifts
    el.style.display = 'inline-block'; el.style.minWidth = el.offsetWidth + 'px'; el.style.textAlign = el.dataset.align; }
  for (const t of tracks) document.querySelectorAll(t.sel).forEach(el => {
    const a = el.animate(t.kf, {duration: t.ms, fill: 'both'}); a.pause(); a.currentTime = 0; });
  return over;
}"""
_SEEK = "([frame, fps]) => window.seekToFrame(frame, fps)"
_BLUR = """() => [...document.querySelectorAll('.blur')].map(el => { const r = el.getBoundingClientRect();
  return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height),
          el.classList.contains('round') ? Math.round(parseInt(getComputedStyle(el).borderTopLeftRadius)
            * r.width / Math.max(1, el.offsetWidth)) : 0]; })"""


def _scripts(libs: tuple[str, ...]) -> str:
    files = [name for lib in ("plot", "echarts", "lightweight") if lib in libs for name in LIBRARIES[lib]]
    return "".join(f'<script src="{(VENDOR / name).as_uri()}"></script>' for name in files)


def _split_scene(card: Card, brand: BrandKit) -> tuple[str, str, str, Motion, tuple[str, ...]]:
    """The Bento card for a split chart: (css, body, script, motion, libraries)."""
    accent = brand.accent
    context = bool(card.context.get("value"))
    footer = context or card.template in KEYED
    header_h = HEADER_H if card.headline or _kpi_row(card) is not None else HEADER_SLIM
    plot_h = CARD_H - header_h - (FOOTER_H if footer else 0)
    chart = CHARTS[card.template](card, accent, plot_h)
    m = chart.motion
    eyebrow = card.eyebrow or EYEBROWS.get(card.template, "")
    header = [f'<div class="eye0" id="eye">{_e(eyebrow)}</div>']
    m.tween("#eye", "opacity", 0.2, 0.5, "0", "1")
    right = ""
    k = _kpi_row(card)
    if k is not None:
        row = card.rows[k]
        up = not row.value.strip().startswith(("-", "−", "–"))
        color = POS if up else NEG
        t = _at(card, row.reveal_at, 0.5)
        right = (f'<div class="kpi num" id="kpi" style="color:{color};background:{_rgba(color, 0.12)};'
                 f'border-color:{_rgba(color, 0.34)}">{TRIANGLE[up]}{_roll(m, row.value, t + 0.05, 0.7, "left")}</div>')
        m.fade_up("#kpi", t, rise=8, length=0.4)
    elif chart.note:
        right = f'<div class="note" id="note">{_e(chart.note)}</div>'
        m.tween("#note", "opacity", 0.3, 0.6, "0", "1")
    if card.headline or right:
        title = f'<div class="title fit" id="ttl">{_e(card.headline)}</div>' if card.headline else '<div class="title"></div>'
        header.append(f'<div class="hrow">{title}{right}</div>')
        if card.headline:
            m.fade_up("#ttl", 0.25, rise=10, length=0.45)
    fx, fy = chart.focal or (CARD_W / 2, plot_h / 2)
    glow = f'<div class="glow" id="glow" style="left:{fx - 320:.0f}px;top:{fy - 320:.0f}px"></div>'
    m.tween("#glow", "opacity", FIRST, FIRST + 0.8, "0", "1")
    css = _card_css(accent, header_h, plot_h, footer) + chart.css
    foot = ""
    if context:
        extra_css, foot, extra = _context_footer(card, accent)
        css += extra_css
        m.merge(extra)
    elif footer:
        foot = chart.key
    body = (f'<div class="card">{GRAIN}<div class="hdr">{"".join(header)}</div><div class="plot"><div class="dots"></div>'
            f'{glow}{chart.body}</div><div class="ftr">{foot}</div><div class="rim"></div></div>')
    return css, body, chart.js, m, chart.libs


def scene(card: Card, brand: BrandKit) -> tuple[str, Motion, int, int, int]:
    """(page, motion, width, height, top) for a card. An over-the-speaker graphic slides
    and fades out over its last 0.4 s; a split chart's card stays whole and leaves with
    the panel (core.dynamic_layout slides it down)."""
    duration = max(0.5, card.end - card.start)
    if MODES[card.template] == "overlay":
        css, body, motion = OVERLAYS[card.template](card, brand.accent)
        script, libs = "", ()
        width, height, top = FRAME_W, FRAME_H, 0
        gone = duration - 1 / FPS  # fully faded on the last frame, so nothing pops off
        motion.tween(".stage", "opacity", gone - EXIT, gone, "1", "0", EASE_OUT_EXIT)
        motion.tween(".stage", "transform", gone - EXIT, gone, "translateY(0px)", "translateY(18px)", EASE_OUT_EXIT)
    else:
        css, body, script, motion, libs = _split_scene(card, brand)
        width, height, top = FRAME_W, SPLIT_H, SPLIT_TOP
    page = (f"<!doctype html><html><head><meta charset='utf-8'><style>{_base_css(width, height)}{css}</style>"
            f"<script>{RUNTIME}</script>{_scripts(libs)}</head><body><div class='stage'>{body}</div>"
            f"<script>{script}</script></body></html>")
    return page, motion, width, height, top


def render_card(card: Card, brand: BrandKit, out_dir: Path) -> Frames:
    """Render the card's animation to PNG frames in ``out_dir`` (reused when nothing
    changed). Raises CardRenderError when text doesn't fit or the accent can't be read."""
    if contrast(brand.accent, CARD_BG) < MIN_CONTRAST:
        raise CardRenderError(f"The accent {brand.accent} is too dark to read on the card "
                              f"(contrast {contrast(brand.accent, CARD_BG):.1f}; needs {MIN_CONTRAST}). Pick a lighter one.")
    if card.template == "candlestick_ema" and len(read_prices(card.data_path) if Path(card.data_path).is_file() else []) < 10:
        raise CardRenderError("The price chart needs a price file with at least 10 rows (open, high, low, close).")
    page, motion, width, height, top = scene(card, brand)
    duration = max(0.5, card.end - card.start)
    count = math.ceil(duration * FPS)
    tracks = motion.js(duration)
    key = hashlib.sha1(json.dumps([ENGINE, page, tracks, count]).encode()).hexdigest()[:14]
    folder = Path(out_dir).resolve() / f"card_{key}"
    meta = folder / "frames.json"
    if meta.is_file():
        data = json.loads(meta.read_text())
        return Frames(str(folder / "f_%05d.png"), data["count"], width, height, top, [tuple(b) for b in data["blur"]])
    folder.mkdir(parents=True, exist_ok=True)
    page_file = folder / "card.html"
    page_file.write_text(page, encoding="utf-8")
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            view = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            errors: list[str] = []
            view.on("pageerror", lambda exc: errors.append(str(exc)))
            view.goto(page_file.as_uri())
            try:
                over = view.evaluate(_SETUP, tracks)
            except PlaywrightError as exc:
                raise CardRenderError(f"The chart couldn't be drawn ({(errors or [str(exc)])[0][:160]}).") from exc
            if errors:
                raise CardRenderError(f"The chart couldn't be drawn ({errors[0][:160]}).")
            if over:
                raise CardRenderError("Too long to fit, shorten: " + "; ".join(f'"{t.strip()}"' for t in over))
            view.evaluate(_SEEK, [max(0, round((duration - EXIT - 0.05) * FPS)), FPS])
            blur = [tuple(b) for b in view.evaluate(_BLUR)]
            # Chrome's fast PNG path: the same pixels as page.screenshot, about 4x quicker.
            cdp = view.context.new_cdp_session(view)
            cdp.send("Emulation.setDefaultBackgroundColorOverride", {"color": {"r": 0, "g": 0, "b": 0, "a": 0}})
            previous = None
            for index in range(count):
                path = folder / f"f_{index:05d}.png"
                t = index / FPS
                if previous is not None and not motion.moving(t):
                    os.link(previous, path)  # nothing moves: the same frame again, for free
                else:
                    view.evaluate(_SEEK, [index, FPS])
                    shot = cdp.send("Page.captureScreenshot", {"format": "png", "optimizeForSpeed": True})
                    path.write_bytes(base64.b64decode(shot["data"]))
                previous = path
        finally:
            browser.close()
    meta.write_text(json.dumps({"count": count, "blur": blur}))
    return Frames(str(folder / "f_%05d.png"), count, width, height, top, blur)
