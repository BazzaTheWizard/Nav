"""Round 41: the Visual Proof overlays, drawn over the 100 % full-screen speaker.

Pillar 2, floating proof cards (a lower third, the speaker never shrinks):
  brand_proof   a company or brand named (Nike, Oakberry, OpenAI): its real logo
                (core.entity_enricher: Brandfetch / logo.dev, cached; a clean monochrome
                monogram when there's none), its name in bold editorial type, an industry
                tag with the ticker, and a one-line context badge.
  wiki_proof    a notable person, event, book or idea: the Wikipedia REST summary's own
                thumbnail on the left, its title in an archival serif and its one-line
                description on the right, credited to Wikipedia (core.visual_proof fetches
                and caches both).
  stat_proof    a number as it's said: a pill whose figure rolls up from zero to the
                spoken value in 0.45 s, landing on the word.
  All three: 860 px wide, 220-260 px tall, inside y 1320-1580 (under the captions, above
  the apps' bottom bar), 20 px corners, a blurred backdrop (core.dynamic_layout blurs the
  video behind every .blur element) and a soft shadow; a spring slide-up in, the
  overlay's fade-out (core.cards.scene), 2.4-3.2 s on screen.

Pillar 3, newspaper_tearout: a warm newsprint card (#FDFBF7, paper grain, deckled torn
top and bottom edges, a 1.5° tilt, a soft drop shadow) over the dimmed, blurred speaker,
with a masthead ("THE INDUSTRY BRIEF // MARKET REALITY"), the speaker's thesis line as a
bold serif headline (Georgia, then the vendored Instrument Serif), and a #FDE047 marker
at 65 % (multiply) sweeping left to right across its key 4-6 words, each one exactly as
it's said (its Whisper start to its end). 1.8-2.5 s.

Every card wears the clip's palette (core.palettes): the pills are the palette's card
(near-opaque, so its AAA text holds on any footage) with its accent; the tear-out is
newsprint on every palette, with the palette's accent on its masthead rule (ink on
nyt_archival: zero red). Classic: dark glass with the brand accent.
"""
from __future__ import annotations

import base64
import html
import re
from pathlib import Path
from typing import Any

PROOF_TEMPLATES = ("brand_proof", "wiki_proof", "stat_proof", "newspaper_tearout")
PILL_TEMPLATES = ("brand_proof", "wiki_proof", "stat_proof")
PILL_W = 860
PILL_X = (1080 - PILL_W) // 2
BAND_TOP, BAND_BOTTOM = 1320, 1580  # every pill sits inside these rows
PILL_H = {"brand_proof": 240, "wiki_proof": 260, "stat_proof": 220}
PILL_RADIUS = 20
PILL_SECONDS = (2.4, 3.2)  # on screen, its way out included
TEAR_W, TEAR_H = 920, 340
TEAR_TOP = 960  # the lower/centre third: 960-1300
TEAR_TILT = -1.5  # degrees
TEAR_SECONDS = (1.8, 2.5)
NEWSPRINT = "#FDFBF7"
MARKER = "#FDE047"
MARKER_OPACITY = 0.65
ROLL_SECONDS = 0.45
KIND_LABELS = {"company": "Public company", "private": "Private company", "fund": "Investment firm",
               "crypto": "Crypto asset", "index": "Market index", "macro": "Economic indicator"}


def _e(text: object) -> str:
    return html.escape(str(text or ""))


def _rgba(color: str, alpha: float) -> str:
    color = color.lstrip("#")
    return f"rgba({int(color[0:2], 16)},{int(color[2:4], 16)},{int(color[4:6], 16)},{alpha:g})"


def pill_top(template: str) -> int:
    """A pill's top row: centred in the 1320-1580 band."""
    return BAND_TOP + (BAND_BOTTOM - BAND_TOP - PILL_H[template]) // 2


def look(brand: Any, entity_color: str | None = None) -> dict[str, str]:
    """The pill's colours on this clip's palette (classic: dark glass and the brand accent)."""
    from core import palettes

    palette = palettes.of(brand)
    if palette is None:
        accent = entity_color or brand.accent
        return {"surface": "rgba(12,15,21,.80)", "header": "#FFFFFF", "body": "#C9CED6", "rule": "rgba(255,255,255,.16)",
                "accent": accent, "tile": "#FFFFFF", "shadow": "rgba(0,0,0,.42)", "mono": "#FFFFFF", "dark": "1"}
    return {"surface": _rgba(palette.bg, 0.95), "header": palette.header, "body": palette.body,
            "rule": _rgba(palette.divider, 0.55), "accent": palette.card_accent(entity_color),
            "tile": "#FFFFFF", "shadow": "rgba(0,0,0,.30)" if not palette.dark else "rgba(0,0,0,.45)",
            "mono": palette.header, "dark": "1" if palette.dark else "0"}


def _pill_css(template: str, colors: dict[str, str]) -> str:
    top, height = pill_top(template), PILL_H[template]
    return f"""
.pf {{ position: absolute; left: {PILL_X}px; top: {top}px; width: {PILL_W}px; height: {height}px;
  border-radius: {PILL_RADIUS}px; background: {colors['surface']}; box-shadow: 0 18px 46px {colors['shadow']};
  display: flex; align-items: center; gap: 30px; padding: 0 40px 0 30px; overflow: hidden; }}
.pf .edge {{ position: absolute; left: 0; top: 0; bottom: 0; width: 6px; background: {colors['accent']}; }}
.pf .rule {{ position: absolute; left: 30px; right: 30px; bottom: 0; height: 1px; background: {colors['rule']}; }}
.pf .meta {{ flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; }}
.pf .eyebrow {{ font-size: 21px; font-weight: 640; letter-spacing: .16em; text-transform: uppercase; color: {colors['body']};
  white-space: nowrap; overflow: hidden; }}
.pf .eyebrow .tk {{ font-family: "JetBrains Mono", monospace; letter-spacing: .04em; color: {colors['accent']}; }}
.pf .name {{ font-size: 58px; font-weight: 720; letter-spacing: -.025em; line-height: 1.02; color: {colors['header']};
  white-space: nowrap; overflow: hidden; }}
.pf .serif {{ font-family: Georgia, "Instrument Serif", serif; font-weight: 700; letter-spacing: -.01em; }}
.pf .badge {{ font-size: 27px; font-weight: 520; line-height: 1.2; color: {colors['body']}; white-space: nowrap; overflow: hidden; }}
.pf .credit {{ position: absolute; right: 26px; bottom: 14px; font-size: 16px; letter-spacing: .12em;
  text-transform: uppercase; color: {colors['body']}; opacity: .8; }}
"""


def _enter(m: Any, selector: str = "#pf") -> None:
    from core.cards import SPRING

    m.tween(selector, "opacity", 0.0, 0.22, "0", "1")
    m.tween(selector, "transform", 0.0, 0.62, "translateY(70px)", "translateY(0px)", SPRING)


def _initials(name: str) -> str:
    parts = [p for p in re.split(r"[\s\-]+", name) if p and p[0].isalnum()]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def brand_proof(card: Any, brand: Any) -> tuple[str, str, Any]:
    from core import entity_enricher
    from core.cards import Motion

    row = card.rows[0]
    found = entity_enricher.cached(card.entity) if card.entity else None
    entity_color = found.color if found is not None and found.color else None
    colors = look(brand, entity_color)
    real_logo = found is not None and found.logo_uri and found.logo_kind not in {"", "initials"}
    if real_logo:
        logo = f'<div class="tile"><img src="{found.logo_uri}" alt=""></div>'
    else:  # a clean monochrome monogram, never a coloured stand-in
        logo = f'<div class="mono">{_e(_initials(row.label))}</div>'
    css = _pill_css("brand_proof", colors) + f"""
.pf .tile {{ flex: none; width: 168px; height: 168px; border-radius: 18px; background: {colors['tile']};
  display: flex; align-items: center; justify-content: center; box-shadow: inset 0 0 0 1px rgba(0,0,0,.08); }}
.pf .tile img {{ max-width: 128px; max-height: 128px; object-fit: contain; }}
.pf .mono {{ flex: none; width: 168px; height: 168px; border-radius: 50%; border: 3px solid {colors['mono']};
  color: {colors['mono']}; display: flex; align-items: center; justify-content: center;
  font-family: Georgia, "Instrument Serif", serif; font-size: 70px; font-weight: 700; letter-spacing: -.02em; }}
"""
    tag = _e(card.eyebrow or "")
    ticker = f' · <span class="tk">{_e(row.value)}</span>' if row.value else ""
    body = (f'<div class="pf blur round" id="pf"><div class="edge"></div>{logo}<div class="meta">'
            f'<div class="eyebrow fit" id="ey">{tag}{ticker}</div><div class="name fit" id="nm">{_e(row.label)}</div>'
            f'<div class="badge fit" id="bd">{_e(row.detail)}</div></div><div class="rule"></div></div>')
    m = Motion()
    _enter(m)
    m.fade_up("#nm", 0.10, rise=10, length=0.4)
    m.fade_up("#ey", 0.16, rise=8, length=0.4)
    m.fade_up("#bd", 0.24, rise=8, length=0.4)
    return css, body, m


def wiki_proof(card: Any, brand: Any) -> tuple[str, str, Any]:
    from core.cards import Motion

    row = card.rows[0]
    colors = look(brand)
    photo = ""
    path = Path(card.data_path) if card.data_path else None
    if path is not None and path.is_file():
        kind = "png" if path.suffix.lower() == ".png" else "webp" if path.suffix.lower() == ".webp" else "jpeg"
        photo = f'<div class="ph"><img src="data:image/{kind};base64,{base64.b64encode(path.read_bytes()).decode()}" alt=""></div>'
    else:
        photo = f'<div class="ph none">{_e(_initials(row.label))}</div>'
    css = _pill_css("wiki_proof", colors) + f"""
.pf {{ padding-left: 0; }}
.pf .ph {{ flex: none; width: 210px; height: {PILL_H['wiki_proof']}px; overflow: hidden; background: {colors['rule']}; }}
.pf .ph img {{ width: 100%; height: 100%; object-fit: cover; object-position: 50% 22%; filter: saturate(.85) contrast(1.04); }}
.pf .ph.none {{ display: flex; align-items: center; justify-content: center; color: {colors['header']};
  font-family: Georgia, "Instrument Serif", serif; font-size: 72px; font-weight: 700; }}
.pf .name {{ font-size: 54px; }}
.pf .badge {{ white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }}
"""
    body = (f'<div class="pf blur round" id="pf">{photo}<div class="meta">'
            f'<div class="eyebrow" id="ey">{_e(card.eyebrow or "From the archive")}</div>'
            f'<div class="name serif fit" id="nm">{_e(row.label)}</div>'
            f'<div class="badge" id="bd">{_e(row.detail)}</div></div><div class="credit">{_e(card.footnote or "Wikipedia")}</div></div>')
    m = Motion()
    _enter(m)
    m.tween(".ph img", "transform", 0.0, 3.2, "scale(1.08)", "scale(1.0)", "cubic-bezier(0.25, 0.1, 0.25, 1)")
    m.fade_up("#ey", 0.12, rise=8, length=0.4)
    m.fade_up("#nm", 0.18, rise=10, length=0.4)
    m.fade_up("#bd", 0.28, rise=8, length=0.4)
    return css, body, m


def stat_proof(card: Any, brand: Any) -> tuple[str, str, Any]:
    from core.cards import Motion, _roll

    row = card.rows[0]
    colors = look(brand)
    at = float(card.context.get("at", card.start + 0.25)) - card.start  # the word the number is said on
    m = Motion()
    _enter(m)
    figure = _roll(m, row.value, max(0.05, at - 0.12), ROLL_SECONDS, "left")
    css = _pill_css("stat_proof", colors) + f"""
.pf {{ gap: 34px; padding-left: 46px; }}
.pf .fig {{ flex: none; font-family: "JetBrains Mono", monospace; font-size: 108px; font-weight: 700; letter-spacing: -.04em;
  line-height: 1; color: {colors['accent']}; white-space: nowrap; --h: 1em; }}
.pf .lbl {{ font-size: 38px; font-weight: 620; letter-spacing: -.01em; line-height: 1.12; color: {colors['header']};
  white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
"""
    body = (f'<div class="pf blur round" id="pf"><div class="edge"></div><div class="fig num" id="fg">{figure}</div>'
            f'<div class="meta"><div class="eyebrow" id="ey">{_e(card.eyebrow or "By the numbers")}</div>'
            f'<div class="lbl" id="lb">{_e(row.label)}</div></div><div class="rule"></div></div>')
    m.fade_up("#ey", 0.14, rise=8, length=0.4)
    m.fade_up("#lb", max(0.2, at - 0.05), rise=10, length=0.4)
    return css, body, m


def headline_size(headline: str) -> int:
    """The headline's size: up to three lines on the 812 px measure, never overflowing the paper."""
    length = len(headline)
    return 64 if length <= 34 else 60 if length <= 52 else 56 if length <= 72 else 50


# A deckled, torn edge along the top and bottom of the newsprint (an SVG mask).
def _torn_mask(width: int, height: int, seed: int = 41) -> str:
    import random

    rng = random.Random(seed)
    step = 14
    top = [(0, 10)] + [(x, rng.uniform(1, 13)) for x in range(step, width, step)] + [(width, 8)]
    bottom = [(width, height - 9)] + [(x, height - rng.uniform(1, 14)) for x in range(width - step, 0, -step)] + [(0, height - 7)]
    points = " ".join(f"{x:.0f},{y:.1f}" for x, y in top + bottom)
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}'>"
           f"<polygon points='{points}' fill='black'/></svg>")
    return "url(\"data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode() + "\")"


def newspaper_tearout(card: Any, brand: Any) -> tuple[str, str, Any]:
    from core import palettes
    from core.cards import Motion, SPRING

    palette = palettes.of(brand)
    rule = palette.accent if palette is not None else brand.accent  # nyt_archival: ink (zero red)
    headline = str(card.rows[0].label)
    size = headline_size(headline)
    tokens = headline.split()
    key = list(card.context.get("key") or [])  # [[word, start, end], ...] in source seconds
    first = int(card.context.get("key_from", max(0, len(tokens) - len(key))))
    spans = []
    for k, token in enumerate(tokens):
        j = k - first
        if 0 <= j < len(key):
            spans.append(f'<span class="hw"><i class="mk" id="mk{j}"></i><span class="tx">{_e(token)}</span></span>')
        else:
            spans.append(f'<span class="tx">{_e(token)}</span>')
    css = f"""
.dim {{ position: absolute; left: 0; top: 0; width: 1080px; height: 1920px; background: rgba(6,7,9,.42); }}
.tear {{ position: absolute; left: {(1080 - TEAR_W) // 2}px; top: {TEAR_TOP}px; width: {TEAR_W}px; height: {TEAR_H}px;
  filter: drop-shadow(0 22px 34px rgba(0,0,0,.50)) drop-shadow(0 3px 6px rgba(0,0,0,.25)); transform: rotate({TEAR_TILT}deg); }}
.paper {{ position: absolute; inset: 0; background: {NEWSPRINT}; -webkit-mask-image: {_torn_mask(TEAR_W, TEAR_H)};
  -webkit-mask-size: 100% 100%; padding: 40px 54px 30px; color: #111111; }}
.paper .grain {{ position: absolute; inset: 0; opacity: .16; mix-blend-mode: multiply; }}
.paper .mast {{ display: flex; align-items: center; gap: 14px; font-size: 19px; font-weight: 700; letter-spacing: .22em;
  text-transform: uppercase; color: #111111; white-space: nowrap; }}
.paper .mast .sl {{ color: #555555; }}
.paper .rules {{ margin: 12px 0 18px; height: 5px; border-top: 3px solid {rule}; border-bottom: 1px solid #555555; }}
.paper .hl {{ font-family: Georgia, "Playfair Display", "Instrument Serif", serif; font-weight: 700; font-size: {size}px;
  line-height: 1.12; letter-spacing: -.015em; color: #111111; }}
.paper .hl .tx, .paper .hl .hw {{ position: relative; display: inline-block; margin-right: .24em; }}
.paper .hl .hw .tx {{ margin-right: 0; position: relative; }}
.paper .mk {{ position: absolute; left: -4px; right: -10px; top: 18%; bottom: 4%; background: {MARKER};
  opacity: {MARKER_OPACITY}; mix-blend-mode: multiply; transform: scaleX(0); transform-origin: 0 50%; border-radius: 3px; }}
"""
    grain = ('<svg class="grain" width="100%" height="100%"><filter id="np"><feTurbulence type="fractalNoise" '
             'baseFrequency=".9" numOctaves="2" seed="7"/><feColorMatrix type="saturate" values="0"/></filter>'
             '<rect width="100%" height="100%" filter="url(#np)"/></svg>')
    masthead, _sep, section = str(card.eyebrow or "THE BRIEF").partition("//")
    mast = (f'<div class="mast"><span>{_e(masthead.strip())}</span>'
            + (f'<span class="sl">//</span><span>{_e(section.strip())}</span>' if section else "") + "</div>")
    body = (f'<div class="dim blur" id="dim"></div><div class="tear" id="tear"><div class="paper">{grain}{mast}'
            f'<div class="rules"></div><div class="hl" id="hl">{"".join(spans)}</div></div></div>')
    m = Motion()
    m.tween("#dim", "opacity", 0.0, 0.25, "0", "1")
    m.tween("#tear", "opacity", 0.0, 0.16, "0", "1")
    m.tween("#tear", "transform", 0.0, 0.55, f"translateY(48px) rotate({TEAR_TILT}deg) scale(.96)",
            f"translateY(0px) rotate({TEAR_TILT}deg) scale(1)", SPRING)
    for j, (_word, start, end) in enumerate(key):
        t0 = max(0.05, float(start) - card.start)
        t1 = max(t0 + 0.12, float(end) - card.start)
        m.tween(f"#mk{j}", "transform", t0, t1, "scaleX(0)", "scaleX(1)", "linear")
        m.hold(t0, t1)
    return css, body, m


BUILDERS = {"brand_proof": brand_proof, "wiki_proof": wiki_proof, "stat_proof": stat_proof,
            "newspaper_tearout": newspaper_tearout}
