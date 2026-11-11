"""Round 40: content-adaptive editorial palettes for the graphics.

Until Round 39 every creator's cards wore one dark palette. Now each clip gets one of
four publication-grade palettes, chosen from what the clip is about and how its studio
is lit (or set for every clip in the app's 🎨 brand kit: palette_mode):

  nyt_archival        light editorial: warm parchment #FDFBF7, matte ink #111111 headers,
                      slate #555555 body and dividers. Monochrome: zero red, no company
                      colours; gains and losses are told apart by ▲ / ▼ and tone.
  bloomberg_obsidian  dark financial / tech: deep carbon #0D0F12, #F3F4F6 headers, muted
                      #9CA3AF body, electric amber #F59E0B data accents (cyan #38BDF8 for
                      tech and AI talk).
  economist_slate     dark executive: charcoal-navy #12161F, #FFFFFF primary, #94A3B8
                      secondary, warm champagne gold #D4AF37 accents.
  studio_adaptive     sampled: the studio's dominant background tone (from the speaker's
                      own frames) becomes the card, with WCAG AAA (7:1) headers and body
                      and a harmonious accent (its complementary hue) at 4.5:1 or more.
  classic             Round 39's single dark palette with the brand kit's accent (also
                      what a card with no palette at all gets: every older render and test).

How a palette reaches every card type without 23 rewrites: the card pages are still built
in the Round 39 house tokens, then Palette.remap() moves every colour onto the palette by
its role. Neutrals (surfaces, rules, text, glass highlights) follow a tonal ramp anchored
on the house tokens (canvas -> card -> divider -> body -> header), so a track that sat
between the card and a rule still sits between them, light or dark. Data neutrals (the
quiet bars next to the accent) get the palette's own data ramp when it has one (the light
palette needs them light, not ink-dark), gains and losses its own pos / neg, and the
accent goes in through a sentinel colour, so it is never mistaken for a neutral. Black
shadows and masks are left alone. The four over-the-speaker glass pills (kinetic_range,
buckets, stat_pill, speaker_title) keep their dark glass on any palette, so they read on
any footage, and wear the palette's overlay accent.
"""
from __future__ import annotations

import colorsys
import json
import logging
import os
import re
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

PALETTE_MODES = ("auto", "nyt_archival", "bloomberg_obsidian", "economist_slate", "studio_adaptive", "classic")
PALETTE_LABELS = {
    "auto": "Auto: picked per clip from its topic and studio lighting",
    "nyt_archival": "NYT archival (light editorial parchment, ink, slate)",
    "bloomberg_obsidian": "Bloomberg obsidian (dark carbon, amber or cyan data)",
    "economist_slate": "Economist slate (charcoal-navy, champagne gold)",
    "studio_adaptive": "Studio adaptive (sampled from the speaker's set)",
    "classic": "Classic (Round 39's dark palette with your accent)",
}
AAA = 7.0  # WCAG AAA for body text
AA = 4.5  # the accent (large numbers, rules and borders): at least AA
SENTINEL = "#FE01FD"  # the accent while a page is built: replaced last, never remapped
_SENTINEL_RGB = (254, 1, 253)

# The Round 39 house tokens the pages are built in (core.cards), by role.
HOUSE_RAMP = ("#040507", "#090C12", "#2A3140", "#94A3B8", "#F5F6F8", "#FFFFFF")  # canvas, card, divider, body, header, white
HOUSE_DATA = {"#E6E9EE": 0, "#C9CED6": 0, "#A3ACBA": 1, "#9AA2AF": 1, "#6E7787": 2, "#5B6474": 2, "#5A6272": 2,
              "#465062": 3}
HOUSE_POS, HOUSE_NEG = "#34D399", "#FB7185"
HOUSE_CARD = "#090C12"


# ------------------------------------------------------------------ colour maths
def rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def hexcolor(r: float, g: float, b: float) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, round(c))) for c in (r, g, b)))


def luminance(color: str) -> float:
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (v / 255 for v in rgb(color))]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(first: str, second: str) -> float:
    """WCAG contrast ratio (1 to 21)."""
    a, b = sorted((luminance(first), luminance(second)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def lightness(color: str) -> float:
    """CIE L* (0-100): perceptual lightness, what the tonal ramp is measured in."""
    y = luminance(color)
    return 116 * y ** (1 / 3) - 16 if y > 216 / 24389 else y * 24389 / 27


def mix(first: str, second: str, amount: float) -> str:
    """``amount`` of the way from ``first`` to ``second`` (sRGB)."""
    a, b = rgb(first), rgb(second)
    return hexcolor(*(x + (y - x) * amount for x, y in zip(a, b)))


def fit_contrast(color: str, background: str, target: float) -> str:
    """``color`` with its hue and saturation kept, moved away from the background's
    lightness (lighter on a dark card, darker on a light one) until it reads at
    ``target``: 4.5 for AA, 7 for AAA."""
    if contrast(color, background) >= target:
        return color
    r, g, b = (c / 255 for c in rgb(color))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    step = 0.01 if luminance(background) < 0.18 else -0.01
    for _ in range(110):
        l = min(1.0, max(0.0, l + step))
        out = hexcolor(*(c * 255 for c in colorsys.hls_to_rgb(h, l, s)))
        if contrast(out, background) >= target:
            return out
    return "#FFFFFF" if step > 0 else "#000000"


def _neutral(color: str) -> bool:
    channels = rgb(color)
    return max(channels) - min(channels) <= 48


# ------------------------------------------------------------------ the palettes
@dataclass(frozen=True)
class Palette:
    name: str
    canvas: str  # the panel around the card
    bg: str  # the card
    divider: str  # rules, axes, dividers (and, between it and the card, tracks and grids)
    body: str  # body text, labels, axes text
    header: str  # headlines and the numbers
    accent: str  # the punchline: highlighted bar, active box, rim
    pos: str  # a gain
    neg: str  # a loss
    overlay_accent: str  # the accent on the over-the-speaker glass pills (dark glass)
    data: tuple[str, ...] = ()  # quiet data next to the accent, most to least prominent (empty: the tonal ramp)
    entity_colors: bool = True  # a card about one company may wear its colour
    reason: str = ""

    @property
    def dark(self) -> bool:
        return luminance(self.bg) < 0.18

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["data"] = list(self.data)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Palette":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        known["data"] = tuple(known.get("data") or ())
        return cls(**known)

    # -------------------------------------------------------------- the remap
    def _ramp(self) -> list[tuple[float, str]]:
        targets = (self.canvas, self.bg, self.divider, self.body, self.header, self.header)
        return [(lightness(house), target) for house, target in zip(HOUSE_RAMP, targets)]

    def color(self, house: str) -> str:
        """Where a Round 39 house colour lands on this palette (see the module docstring)."""
        house = house.upper()
        if house == SENTINEL:
            return house
        if house in HOUSE_DATA and self.data:
            return self.data[min(HOUSE_DATA[house], len(self.data) - 1)]
        if house == HOUSE_POS:
            return self.pos
        if house == HOUSE_NEG:
            return self.neg
        if not _neutral(house):
            return house  # a hue of its own (a glow's tint): kept
        ramp = self._ramp()
        value = lightness(house)
        if value <= ramp[0][0]:
            return ramp[0][1]
        for (low, low_color), (high, high_color) in zip(ramp, ramp[1:]):
            if value <= high:
                return mix(low_color, high_color, (value - low) / ((high - low) or 1.0))
        return ramp[-1][1]

    def remap(self, text: str, accent: str) -> str:
        """A card's page (or its animation tracks) in this palette: every #RRGGBB and
        rgba() moved by role, the sentinel accent replaced with ``accent``."""
        cache: dict[str, str] = {}

        def hex_sub(match: re.Match) -> str:
            found = match.group(0).upper()
            if found == SENTINEL:
                return accent
            if found not in cache:
                cache[found] = self.color(found)
            return cache[found]

        def rgba_sub(match: re.Match) -> str:
            r, g, b, alpha = int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4)
            if (r, g, b) == (0, 0, 0):
                return match.group(0)  # shadows and masks
            if (r, g, b) == _SENTINEL_RGB:
                ar, ag, ab = rgb(accent)
            else:
                ar, ag, ab = rgb(self.color(hexcolor(r, g, b)))
            return f"rgba({ar},{ag},{ab},{alpha})"

        text = re.sub(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([.\d]+)\s*\)", rgba_sub, text)
        return re.sub(r"#[0-9A-Fa-f]{6}(?![0-9A-Za-z])", hex_sub, text)

    def remap_json(self, value: Any, accent: str) -> Any:
        """The same, for anything JSON (a Motion's tracks)."""
        return json.loads(self.remap(json.dumps(value), accent))

    def card_accent(self, entity_color: str | None) -> str:
        """The accent a Bento card wears: the company's colour made readable on this
        card, or the palette's."""
        if entity_color and self.entity_colors:
            return fit_contrast(entity_color, self.bg, AA)
        return self.accent

    def checks(self) -> dict[str, float]:
        """The contrast ratios this palette promises (WCAG)."""
        return {"header": round(contrast(self.header, self.bg), 2), "body": round(contrast(self.body, self.bg), 2),
                "accent": round(contrast(self.accent, self.bg), 2),
                "overlay_accent": round(contrast(self.overlay_accent, HOUSE_CARD), 2)}


NYT_ARCHIVAL = Palette(
    name="nyt_archival", canvas="#F2EEE5", bg="#FDFBF7", divider="#555555", body="#555555", header="#111111",
    accent="#111111", pos="#111111", neg="#555555", overlay_accent="#FDFBF7",
    data=("#A39C90", "#BDB6AA", "#D1CBC0", "#E2DDD3"), entity_colors=False,
)
BLOOMBERG_OBSIDIAN = Palette(
    name="bloomberg_obsidian", canvas="#07080A", bg="#0D0F12", divider="#2C3038", body="#9CA3AF", header="#F3F4F6",
    accent="#F59E0B", pos="#34D399", neg="#FB7185", overlay_accent="#F59E0B",
)
BLOOMBERG_CYAN = "#38BDF8"
ECONOMIST_SLATE = Palette(
    name="economist_slate", canvas="#0B0E14", bg="#12161F", divider="#2B3445", body="#94A3B8", header="#FFFFFF",
    accent="#D4AF37", pos="#7DD3A8", neg="#F19A8E", overlay_accent="#D4AF37",
)
BUILT_IN = {p.name: p for p in (NYT_ARCHIVAL, BLOOMBERG_OBSIDIAN, ECONOMIST_SLATE)}


def bloomberg(cyan: bool = False) -> Palette:
    """Bloomberg obsidian with its amber data accent, or cyan for tech and AI."""
    if not cyan:
        return BLOOMBERG_OBSIDIAN
    return replace(BLOOMBERG_OBSIDIAN, accent=BLOOMBERG_CYAN, overlay_accent=BLOOMBERG_CYAN)


def adaptive(tone: str) -> Palette:
    """studio_adaptive: a card in the studio's own background tone. A light studio gets
    a tinted paper card, a dark one a deep card of the same hue (its saturation kept
    low: editorial, never neon); headers and body are pushed to AAA (7:1) on it, and the
    accent is the tone's complementary hue at 4.5:1 or more (the harmonious border)."""
    r, g, b = (c / 255 for c in rgb(tone))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    soft = min(s, 0.32)

    def hls(lightness_: float, saturation: float, hue: float = h) -> str:
        return hexcolor(*(c * 255 for c in colorsys.hls_to_rgb(hue % 1.0, lightness_, saturation)))

    light = luminance(tone) > 0.40
    if light:
        bg, canvas, header = hls(0.965, soft * 0.6), hls(0.915, soft * 0.6), hls(0.07, min(soft, 0.2))
    else:
        bg, canvas, header = hls(0.075, soft), hls(0.04, soft), hls(0.965, min(soft, 0.12))
    header = fit_contrast(header, bg, AAA + 3)
    body = mix(header, bg, 0.30)
    while contrast(body, bg) < AAA:
        body = mix(body, header, 0.12)
    divider = mix(header, bg, 0.62 if light else 0.78)
    hue = (h + 0.5) if s >= 0.08 else (42 / 360)  # a grey set: a warm gold accent
    accent = fit_contrast(hls(0.5, 0.72, hue), bg, AA)
    overlay = fit_contrast(hls(0.62, 0.72, hue), HOUSE_CARD, AA)
    data = tuple(mix(header, bg, f) for f in (0.55, 0.68, 0.78, 0.86)) if light else ()
    pos = fit_contrast("#1F9D6A" if light else "#34D399", bg, AA)
    neg = fit_contrast("#C2410C" if light else "#FB7185", bg, AA)
    return Palette(name="studio_adaptive", canvas=canvas, bg=bg, divider=divider, body=body, header=header,
                   accent=accent, pos=pos, neg=neg, overlay_accent=overlay, data=data,
                   reason=f"sampled from the studio's {tone}")


# ------------------------------------------------------------------ the studio
@dataclass(frozen=True)
class Studio:
    """What the speaker's frames show: the dominant background ``tone``, how much of the
    background it covers (``share``), its ``saturation`` and the frame's mean brightness
    (relative luminance, 0-1)."""

    tone: str
    share: float
    saturation: float
    brightness: float

    @property
    def distinct(self) -> bool:
        """A set with its own colour (a branded wall, a coloured backdrop), not grey."""
        return self.share >= 0.35 and self.saturation >= 0.28 and 0.04 < luminance(self.tone) < 0.85


def sample_studio(source: str | Path, start: float, end: float, samples: int = 3) -> Studio | None:
    """Read a few small frames of the clip and measure its background: everything but
    the middle of the picture, where the speaker usually is. None if they can't be read."""
    import numpy as np

    width, height = 96, 54
    pixels = []
    for k in range(samples):
        t = start + (end - start) * (k + 1) / (samples + 1)
        try:
            raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(source), "-frames:v", "1",
                                  "-vf", f"scale={width}:{height}:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                  "-"], capture_output=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if len(raw) != width * height * 3:
            continue
        frame = np.frombuffer(raw, np.uint8).reshape(height, width, 3)
        mask = np.ones((height, width), bool)
        mask[int(height * 0.28):, int(width * 0.26):int(width * 0.74)] = False  # the speaker's middle
        pixels.append(frame[mask])
    if not pixels:
        return None
    every = np.concatenate(pixels).astype(np.int32)
    bins = (every[:, 0] >> 5) * 64 + (every[:, 1] >> 5) * 8 + (every[:, 2] >> 5)
    counts = np.bincount(bins, minlength=512)
    top = int(np.argmax(counts))
    tone = hexcolor(*every[bins == top].mean(axis=0))
    _h, _l, saturation = colorsys.rgb_to_hls(*(c / 255 for c in rgb(tone)))
    lin = np.where(every / 255 <= 0.04045, every / 255 / 12.92, ((every / 255 + 0.055) / 1.055) ** 2.4)
    brightness = float((lin @ np.array([0.2126, 0.7152, 0.0722])).mean())
    return Studio(tone=tone, share=round(float(counts[top] / len(bins)), 3), saturation=round(saturation, 3),
                  brightness=round(brightness, 3))


# ------------------------------------------------------------------ the selector
FINANCE = frozenset("""stock stocks market markets invest investing investor investors fund funds portfolio
valuation revenue revenues earnings margin margins profit profits interest rates inflation fed bond bonds equity
trading trader traders crypto bitcoin price prices pricing capital ipo dividend dividends cash debt arr mrr ebitda
multiple multiples vc venture raise raised valuation bank banks banking hedge yield yields recession""".split())
TECH = frozenset("""ai model models software saas cloud data chip chips gpu gpus nvidia startup startups engineering
code coding developer developers platform api apps compute openai tech technology algorithm algorithms agents agent
llm llms robotics automation semiconductor semiconductors""".split())
EXECUTIVE = frozenset("""strategy strategic leadership leader leaders ceo ceos executive executives board management
manager managers organization organizations culture hire hiring consulting consultant client clients decision
decisions policy government geopolitics geopolitical trade tariffs tariff economy economic economics macro global
regulation negotiation operations scale scaling competition competitive""".split())
EDITORIAL = frozenset("""history historical story stories book books writing writer novel life family childhood art
music science scientist health brain love meaning philosophy school century ancient kids parents faith nature
travel food design memory war museum archive journalism essay culture human humans happiness""".split())


def _rate(tokens: list[str], vocabulary: frozenset[str]) -> float:
    return 100.0 * sum(1 for t in tokens if t in vocabulary) / max(1, len(tokens))


def select(text: str, title: str = "", studio: Studio | None = None) -> Palette:
    """The palette for a clip, from its words and title (hits per 100 words, the title
    counting three times) and its studio: a bright set leans to the light archival card,
    a dark one to the dark palettes, and a set with a strong colour of its own gets the
    studio-adaptive card. Ties go to Bloomberg obsidian (the house's own register)."""
    tokens = re.findall(r"[a-z$]+", (text + (" " + title) * 3).lower())
    finance, tech = _rate(tokens, FINANCE), _rate(tokens, TECH)
    scores = {
        "bloomberg_obsidian": finance + tech,
        "economist_slate": _rate(tokens, EXECUTIVE),
        "nyt_archival": 1.2 * _rate(tokens, EDITORIAL),
        "studio_adaptive": 0.0,
    }
    lit = ""
    if studio is not None:
        if studio.brightness >= 0.55:
            scores["nyt_archival"] += 1.5
            lit = "a bright set"
        elif studio.brightness <= 0.28:
            scores["bloomberg_obsidian"] += 0.5
            scores["economist_slate"] += 0.5
            lit = "a dark set"
        if studio.distinct:
            topical = max(scores["bloomberg_obsidian"], scores["economist_slate"], scores["nyt_archival"])
            scores["studio_adaptive"] = 1.5 + 4.0 * studio.share * studio.saturation + (1.0 if topical < 1.5 else 0.0)
    order = ("bloomberg_obsidian", "economist_slate", "nyt_archival", "studio_adaptive")
    name = max(order, key=lambda key: (round(scores[key], 6), -order.index(key)))
    why = f"{name.replace('_', ' ')}: " + ", ".join(f"{k.split('_')[0]} {v:.1f}" for k, v in scores.items())
    why += f" ({lit})" if lit else ""
    if name == "studio_adaptive" and studio is not None:
        return replace(adaptive(studio.tone), reason=why)
    if name == "bloomberg_obsidian":
        return replace(bloomberg(cyan=tech > finance), reason=why)
    return replace(BUILT_IN[name], reason=why)


def mode() -> str:
    """PALETTE_MODE in .env, if set, overrides the brand kit's palette_mode."""
    found = (os.getenv("PALETTE_MODE") or "").strip().lower()
    return found if found in PALETTE_MODES else ""


def resolve(palette_mode: str, text: str = "", title: str = "", source: str | Path | None = None,
            start: float = 0.0, end: float = 0.0) -> Palette | None:
    """The palette for one clip (None: classic). ``palette_mode``: auto, a palette's
    name, or classic. Auto and studio_adaptive read the studio's frames from ``source``."""
    palette_mode = mode() or (palette_mode or "auto").strip().lower()
    if palette_mode == "classic" or palette_mode not in PALETTE_MODES:
        return None
    studio = None
    if palette_mode in {"auto", "studio_adaptive"} and source is not None and end > start:
        try:
            studio = sample_studio(source, start, end)
        except Exception:  # noqa: BLE001 - no frames: the topic decides
            logger.warning("Studio sampling failed; the topic picks the palette.", exc_info=True)
    if palette_mode == "auto":
        return select(text, title, studio)
    if palette_mode == "studio_adaptive":
        if studio is None:
            return replace(BLOOMBERG_OBSIDIAN, reason="studio adaptive: no frames to sample, Bloomberg obsidian instead")
        return adaptive(studio.tone)
    if palette_mode == "bloomberg_obsidian":
        tokens = re.findall(r"[a-z$]+", (text + " " + title).lower())
        return bloomberg(cyan=_rate(tokens, TECH) > _rate(tokens, FINANCE))
    return BUILT_IN[palette_mode]


def of(brand: Any) -> Palette | None:
    """The palette a brand kit carries (BrandKit.palette), or None (classic)."""
    data = getattr(brand, "palette", None) or {}
    if not data or data.get("name") in {None, "", "classic"}:
        return None
    try:
        return Palette.from_dict(data)
    except (TypeError, ValueError, KeyError):
        return None


def for_clip(brand: Any, words: Iterable[Any], title: str = "", source: str | Path | None = None,
             start: float = 0.0, end: float = 0.0) -> tuple[Any, str]:
    """(the brand kit with this clip's palette on it, a note saying which and why)."""
    text = " ".join(str(getattr(w, "text", w)) for w in words)
    palette = resolve(getattr(brand, "palette_mode", "auto"), text, title, source, start, end)
    if palette is None:
        return replace(brand, palette={}), "Palette: classic (Round 39's dark palette with the brand accent)."
    return (replace(brand, palette=palette.to_dict()),
            f"Palette: {palette.name}" + (f" ({palette.reason})" if palette.reason else "") + ".")
