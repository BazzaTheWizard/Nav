"""Round 36: Bento cards for the conversations that aren't about a number.

Geopolitics, politics, tech ecosystems, VC and strategy clips get native visuals instead of
a forced bar chart. Each is a split chart (speaker on top, the card below, inside the card's
band: Round 39, y 1056-1768) built from the speaker's words, with real data only where it is sourced:

- geo_map_card: a map that zooms to the 1-3 nations named, lights them in the accent and
  draws dashed trade or capital arcs between them (an offline Natural Earth map, d3-geo).
  An optional World Bank figure per country (core.niche_data) carries its source.
- prediction_gauge_card: a tug-of-war probability bar from 50%: the odds the speaker gives,
  or a Polymarket market picked in the app (with its source and volume).
- flow_sankey_card: a value chain or supply chain (ASML -> TSMC -> NVIDIA) or where money
  goes, node by node, each with its logo; arrows draw themselves in (vivus-style
  stroke-dashoffset), thicker where more flows when the speaker gives amounts.
- framework_matrix_card: a 2x2 (the speaker's two axes, four quadrants, the point's one
  lit) or a 3-4 step flywheel whose arrows draw themselves round the loop.
- headline_card: an editorial tear-sheet: the speaker's thesis set like a newspaper pull
  quote, a highlighter sweeping over the key phrase, a dek line from Wikipedia when a law
  or event is named. Round 39: it comes in as the quote starts, lights each word as it's
  said and leaves 0.35 s after the last word.

Motion follows the Round 35 rules: everything in by 1.2 s (core.cards' time-warp), then
still to be read; ticks (core.audio_polish) as nodes and quadrants light up.
"""
from __future__ import annotations

import html
import json
import math

from core import cards as C
from core.edl import Card


def _e(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def _times(card: Card, gap: float = 0.3) -> list[float]:
    return [C._at(card, row.reveal_at, C.FIRST + 0.15 + gap * k) for k, row in enumerate(card.rows)]


def _logo(key: str, size: int = 56) -> str:
    """A company's logo tile (from the cache only), or "" when it has none."""
    if not key:
        return ""
    from core import entity_enricher

    found = entity_enricher.cached(key)
    if found is None or not found.logo_uri:
        return ""
    badge = " badge" if found.logo_kind == "initials" else ""
    inner = round(size * 0.72)
    return (f'<div class="logo{badge}" style="width:{size}px;height:{size}px;border-radius:{size // 4}px">'
            f'<img src="{found.logo_uri}" alt="" style="width:{inner if not badge else size}px;'
            f'height:{inner if not badge else size}px"></div>')


def _draw(m: C.Motion, selector: str, t: float, length: float = 0.55) -> None:
    """A path that draws itself in (pathLength 1: stroke-dashoffset 1 -> 0)."""
    m.tween(selector, "strokeDashoffset", t, t + length, "1", "0", C.EASE)


# ---------------------------------------------------------------------------- geo map
GEO_JS = """NAV.onBuild(() => {
  const D = __DATA__, W = __W__, H = __H__;
  const world = window.NAV_WORLD, all = topojson.feature(world, world.objects.countries).features;
  const picked = D.ids.map(id => all.find(f => f.id === id)).filter(Boolean);
  const mainPart = f => {  // a country's biggest piece: the US framed without Alaska, France without Guiana
    const parts = f.geometry.type === 'MultiPolygon' ? f.geometry.coordinates.map(c => ({type: 'Polygon', coordinates: c})) : [f.geometry];
    return parts.reduce((a, p) => d3.geoArea(p) > d3.geoArea(a) ? p : a, parts[0]); };
  const fc = {type: 'FeatureCollection', features: picked.map(f => ({type: 'Feature', geometry: mainPart(f)}))};
  const lons = picked.map(f => d3.geoCentroid(f)[0] * Math.PI / 180);
  const lon = Math.atan2(d3.mean(lons.map(Math.sin)), d3.mean(lons.map(Math.cos))) * 180 / Math.PI;
  const proj = d3.geoNaturalEarth1().rotate([-lon, 0]).fitExtent([[90, 70], [W - 90, H - 70]], fc);
  const zoom = picked.length > 1 ? 0.86 : 0.42;  // room around them: the neighbours give the place
  proj.scale(proj.scale() * zoom);
  let path = d3.geoPath(proj), b = path.bounds(fc), t = proj.translate();
  proj.translate([t[0] + W / 2 - (b[0][0] + b[1][0]) / 2, t[1] + H / 2 - (b[0][1] + b[1][1]) / 2]);
  path = d3.geoPath(proj);
  const svg = d3.select('#geo');
  svg.append('path').attr('d', path(d3.geoGraticule10())).attr('class', 'grat');
  svg.append('g').selectAll('path').data(all).join('path').attr('d', path).attr('class', 'land');
  const centers = picked.map(f => proj(d3.geoCentroid(mainPart(f))));  // a label sits on the biggest part
  picked.forEach((f, k) => svg.append('path').attr('d', path(f)).attr('id', 'hl' + k)
    .attr('class', 'hl' + (k === D.main ? ' main' : '')));
  D.arcs.forEach(([a, z], j) => {
    const [x1, y1] = centers[a], [x2, y2] = centers[z], dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1;
    const bend = Math.min(140, 0.28 * len), cx = (x1 + x2) / 2 - dy / len * bend, cy = (y1 + y2) / 2 + dx / len * bend;
    const d = `M${x1},${y1} Q${cx},${cy} ${x2},${y2}`;
    const mask = svg.append('mask').attr('id', 'am' + j).attr('maskUnits', 'userSpaceOnUse')
      .attr('x', 0).attr('y', 0).attr('width', W).attr('height', H);
    mask.append('path').attr('d', d).attr('id', 'amp' + j).attr('pathLength', 1).attr('class', 'reveal');
    svg.append('path').attr('d', d).attr('class', 'arc').attr('mask', `url(#am${j})`);
    svg.append('circle').attr('cx', x2).attr('cy', y2).attr('r', 9).attr('class', 'tip').attr('id', 'tip' + j);
  });
  const placed = [];  // labels never overlap: above the dot, else below, else nudged sideways
  const hits = (r) => placed.some(q => r.x < q.x + q.w + 10 && q.x < r.x + r.w + 10 && r.y < q.y + q.h + 8 && q.y < r.y + r.h + 8);
  centers.forEach(([x, y], k) => {
    svg.append('circle').attr('cx', x).attr('cy', y).attr('r', 18).attr('class', 'halo').attr('id', 'halo' + k);
    svg.append('circle').attr('cx', x).attr('cy', y).attr('r', 6.5).attr('class', 'dot').attr('id', 'dot' + k);
    const label = document.getElementById('gl' + k), w = label.offsetWidth, h = label.offsetHeight;
    const clampX = v => Math.max(12, Math.min(W - w - 12, v)), clampY = v => Math.max(8, Math.min(H - h - 8, v));
    const tries = [[x - w / 2, y - h - 24], [x - w / 2, y + 24], [x + 28, y - h / 2], [x - w - 28, y - h / 2],
                   [x - w / 2, y - 2 * h - 34], [x - w / 2, y + h + 34]].map(([a, b]) => ({x: clampX(a), y: clampY(b), w, h}));
    const spot = tries.find(r => !hits(r)) || tries[0];
    placed.push(spot);
    label.style.left = spot.x + 'px'; label.style.top = spot.y + 'px';
  });
});"""


def _geo_map(card: Card, accent: str, ph: int) -> C.Chart:
    from core import niche_data

    rows = card.rows
    places = [niche_data.country(row.label) for row in rows]
    ids = [place.atlas if place else "" for place in places]
    main = next((k for k, row in enumerate(rows) if row.highlight), 0)
    arcs = card.context.get("arcs") if isinstance(card.context.get("arcs"), list) else None
    if arcs is None:  # as said: the first place flows to each of the others
        arcs = [[0, k] for k in range(1, len(rows))]
    else:  # context arcs name items by number, from 1 (core.card_checks)
        arcs = [[int(a) - 1, int(z) - 1] for a, z in arcs]
    arcs = [[int(a), int(z)] for a, z in arcs if 0 <= int(a) < len(rows) and 0 <= int(z) < len(rows) and a != z][:3]
    times = _times(card)
    m = C.Motion()
    m.tween("#geo", "opacity", C.FIRST, C.FIRST + 0.35, "0", "1")
    labels = []
    for k, row in enumerate(rows):
        t = times[k]
        m.tween(f"#hl{k}", "opacity", t, t + 0.35, "0", "1")
        m.tween(f"#dot{k}", "opacity", t, t + 0.2, "0", "1")
        m.tween(f"#halo{k}", "transform", t, t + 0.5, "scale(0.2)", "scale(1)", C.SPRING)
        m.tween(f"#halo{k}", "opacity", t, t + 0.5, "0", "1")
        value = (f'<span class="gv num" style="color:{accent if k == main else C.INK}">'
                 f'{C._roll(m, row.value, t + 0.05, align="left")}</span>') if row.value.strip() else ""
        detail = f'<span class="gd">{_e(row.detail)}</span>' if row.detail.strip() else ""
        labels.append(f'<div class="glab{" main" if k == main else ""}" id="gl{k}"><span class="gn">{_e(row.label)}</span>'
                      f'{value}{detail}</div>')
        m.fade_up(f"#gl{k}", t + 0.05, rise=10, length=0.35)
        if k == main:
            m.cue(t, "tick")
    for j, (_a, z) in enumerate(arcs):
        t = max(times[z], times[_a]) + 0.05
        _draw(m, f"#amp{j}", t, 0.6)
        m.tween(f"#tip{j}", "opacity", t + 0.5, t + 0.65, "0", "1")
    data = {"ids": ids, "main": main, "arcs": arcs}
    js = C._js(GEO_JS, DATA=json.dumps(data), W=C.CARD_W, H=ph)
    css = f"""#geo {{ position: absolute; left: 0; top: 0; width: {C.CARD_W}px; height: {ph}px; overflow: hidden; }}
#geo .grat {{ fill: none; stroke: rgba(255,255,255,.035); stroke-width: 1; }}
#geo .land {{ fill: #121722; stroke: #222A39; stroke-width: 0.8; }}
#geo [id^=hl] {{ fill: {C._rgba(accent, 0.30)}; stroke: {accent}; stroke-width: 1.6; }}
#geo .hl.main {{ fill: {C._rgba(accent, 0.62)}; filter: drop-shadow(0 0 18px {C._rgba(accent, 0.55)}); }}
#geo .reveal {{ fill: none; stroke: #fff; stroke-width: 14; stroke-dasharray: 1; stroke-dashoffset: 1; }}
#geo .arc {{ fill: none; stroke: {C.INK}; stroke-width: 3; stroke-dasharray: 10 9; stroke-linecap: round; }}
#geo .tip {{ fill: {C.INK}; opacity: 0; }}
#geo .dot {{ fill: {C.INK}; stroke: {C.CARD_BG}; stroke-width: 2.5; }}
#geo .halo {{ fill: {C._rgba(accent, 0.22)}; stroke: {C._rgba(accent, 0.6)}; stroke-width: 1.5; transform-box: fill-box;
  transform-origin: center; }}
.glab {{ position: absolute; display: flex; flex-direction: column; align-items: center; padding: 10px 18px 12px;
  border-radius: 14px; background: rgba(9,12,18,.86); border: 1px solid rgba(255,255,255,.12); white-space: nowrap;
  box-shadow: 0 10px 30px rgba(0,0,0,.5); }}
.glab.main {{ border-color: {C._rgba(accent, 0.65)}; }}
.glab .gn {{ font-size: 30px; font-weight: 700; letter-spacing: -.01em; }}
.glab .gv {{ font-size: 34px; font-weight: 700; margin-top: 2px; }}
.glab .gd {{ font-size: 20px; color: {C.SOFT}; margin-top: 2px; }}"""
    body = f'<svg id="geo" viewBox="0 0 {C.CARD_W} {ph}"></svg>' + "".join(labels)
    key = ""
    if card.source:
        key = f'<div class="key" id="ckey"><span class="src">{_e("Source: " + card.source)}</span></div>'
        m.tween("#ckey", "opacity", 0.3, 0.6, "0", "1")
    return C.Chart(css, body, m, js, ("geo",), (C.CARD_W / 2, ph / 2), key=key)


# ---------------------------------------------------------------------------- prediction gauge
def _probability(text: str) -> float | None:
    numbers = C.parse_value(text).numbers
    return max(0.0, min(100.0, numbers[0])) if numbers else None


def _prediction_gauge(card: Card, accent: str, ph: int) -> C.Chart:
    """A tug-of-war from 50%: the knob slides from the middle to the odds."""
    yes, no = card.rows[0], card.rows[1] if len(card.rows) > 1 else None
    p = _probability(yes.value) or 50.0
    left, width = C.PAD + 10, C.IW - 20
    bar_y, bar_h = ph * 0.56, 72
    t = C._at(card, yes.reveal_at, C.FIRST + 0.15)
    m = C.Motion()
    knob_x = left + width * p / 100
    m.tween("#gyes", "width", t, t + 0.7, f"{width / 2:.1f}px", f"{width * p / 100:.1f}px", C.SPRING)
    m.tween("#gknob", "left", t, t + 0.7, f"{left + width / 2 - 22:.1f}px", f"{knob_x - 22:.1f}px", C.SPRING)
    m.tween("#gauge", "opacity", C.FIRST, C.FIRST + 0.3, "0", "1")
    m.cue(t + 0.6, "tick")
    no_value = no.value if no is not None and no.value.strip() else f"{100 - p:.0f}%"
    yes_side = f'{_e(yes.label or "Yes")}'
    no_side = f'{_e(no.label if no is not None and no.label else "No")}'
    badges = (f'<div class="gb yes" id="gby"><span class="gk">{yes_side}</span>'
              f'<span class="gp num">{C._roll(m, yes.value, t, 0.6, "left")}</span></div>'
              f'<div class="gb no" id="gbn"><span class="gk">{no_side}</span>'
              f'<span class="gp num">{C._roll(m, no_value, t, 0.6, "right")}</span></div>')
    m.fade_up("#gby", t, rise=10, length=0.35)
    m.fade_up("#gbn", t + 0.1, rise=10, length=0.35)
    css = f"""#gauge {{ position: absolute; left: {left}px; top: {bar_y:.0f}px; width: {width}px; height: {bar_h}px;
  border-radius: {bar_h // 2}px; background: {C._rgba(C.NEG, 0.26)}; border: 1px solid rgba(255,255,255,.10); overflow: hidden; }}
#gyes {{ position: absolute; left: 0; top: 0; bottom: 0; background: linear-gradient(90deg, {C._rgba(accent, 0.55)}, {accent}); }}
.gmid {{ position: absolute; left: {left + width / 2 - 1:.0f}px; top: {bar_y - 22:.0f}px; width: 2px; height: {bar_h + 44}px;
  background: rgba(255,255,255,.55); }}
.gmidl {{ position: absolute; left: {left + width / 2 - 60:.0f}px; width: 120px; text-align: center; top: {bar_y + bar_h + 28:.0f}px;
  font-size: 22px; color: {C.SOFT}; }}
#gknob {{ position: absolute; top: {bar_y + bar_h / 2 - 22:.0f}px; width: 44px; height: 44px; border-radius: 22px;
  background: {C.INK}; box-shadow: 0 0 0 6px {C._rgba(accent, 0.35)}, 0 8px 22px rgba(0,0,0,.55); }}
.gb {{ position: absolute; top: {bar_y - 160:.0f}px; display: flex; flex-direction: column; gap: 4px; max-width: {width / 2 - 20:.0f}px; }}
.gb.yes {{ left: {left}px; }}
.gb.no {{ right: {C.CARD_W - left - width}px; align-items: flex-end; text-align: right; }}
.gb .gk {{ font-size: 28px; font-weight: 600; color: {C.SOFT}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }}
.gb .gp {{ font-size: 76px; font-weight: 800; letter-spacing: -.03em; line-height: 1; }}
.gb.yes .gp {{ color: {accent}; }}
.gb.no .gp {{ color: {C.INK}; opacity: .78; }}"""
    body = (f'{badges}<div id="gauge"><div id="gyes" style="width:{width / 2:.0f}px"></div></div>'
            f'<div class="gmid"></div><div class="gmidl num">50%</div><div id="gknob" style="left:{left + width / 2 - 22:.0f}px"></div>')
    key = ""
    if card.source:
        key = (f'<div class="key" id="ckey"><span class="src">{_e("Source: " + card.source)}</span>'
               f'<span class="lg">{_e(card.footnote)}</span></div>')
        m.tween("#ckey", "opacity", 0.3, 0.6, "0", "1")
    return C.Chart(css, body, m, "", (), (knob_x, bar_y + bar_h / 2), key=key)


# ---------------------------------------------------------------------------- value chain / flow
def _flow_chain(card: Card, accent: str, ph: int) -> C.Chart:
    """Nodes in order, joined by arrows that draw themselves; with amounts, each link's
    width follows what flows through it."""
    from core import entity_enricher

    rows = card.rows
    n = len(rows)
    times = _times(card, 0.25)
    amounts = [C._num(row.value) for row in rows]
    sized = all(a > 0 for a in amounts[1:]) and n > 1
    peak = max(amounts[1:]) if sized else 1.0
    horizontal = n <= 3
    m = C.Motion()
    nodes, links = [], []
    if horizontal:
        box_w, box_h, gap = (C.IW - 2 * 70) / n, 210, 70
        boxes = [(C.PAD + k * (box_w + gap), ph / 2 - box_h / 2, box_w, box_h) for k in range(n)]
    else:
        box_w, box_h = C.IW * 0.72, min(104, (ph - 40 - (n - 1) * 26) / n)
        gap = (ph - 20 - n * box_h) / max(1, n - 1)
        boxes = [(C.PAD + (C.IW - box_w) / 2, 10 + k * (box_h + gap), box_w, box_h) for k in range(n)]
    for k, row in enumerate(rows):
        x, y, w, h = boxes[k]
        logo = _logo(entity_enricher.card_entity(row.label), 64 if horizontal else 52)
        main = " main" if row.highlight else ""
        role = f'<div class="fr">{_e(row.detail)}</div>' if row.detail.strip() else ""
        inside = (f'<div class="fnv num">{C._roll(m, row.value, times[k] + 0.1, align="right")}</div>'
                  if row.value.strip() and not horizontal else "")
        nodes.append(f'<div class="fnode{main}{" h" if horizontal else ""}" id="fn{k}" style="left:{x:.0f}px;top:{y:.0f}px;'
                     f'width:{w:.0f}px;height:{h:.0f}px">{logo}<div class="ft"><div class="fname fit">{_e(row.label)}</div>{role}</div>'
                     f'{inside}</div>')
        m.fade_up(f"#fn{k}", times[k], rise=12, length=0.4)
        if row.highlight:
            m.cue(times[k], "tick")
        if k == 0:
            continue
        px, py, pw, ph_ = boxes[k - 1]
        if horizontal:
            x1, y1, x2, y2 = px + pw + 6, py + ph_ / 2, x - 10, y + h / 2
            d = f"M{x1:.1f},{y1:.1f} C{x1 + 30:.1f},{y1:.1f} {x2 - 30:.1f},{y2:.1f} {x2:.1f},{y2:.1f}"
            mid = ((x1 + x2) / 2, y1 - 34)
        else:
            x1, y1, x2, y2 = px + pw / 2, py + ph_ + 4, x + w / 2, y - 8
            d = f"M{x1:.1f},{y1:.1f} L{x2:.1f},{y2:.1f}"
            mid = (x1 + 34, (y1 + y2) / 2 - 16)
        thick = 4 + 22 * amounts[k] / peak if sized else 4
        links.append(f'<path id="fl{k}" d="{d}" pathLength="1" class="flink{" sized" if sized else ""}" '
                     f'style="stroke-width:{thick:.1f}px"/>')
        if not sized:
            links.append(f'<path id="fa{k}" d="M{x2 - (14 if horizontal else 9):.1f},{y2 - (9 if horizontal else 14):.1f} '
                         f'L{x2:.1f},{y2:.1f} L{x2 - (14 if horizontal else -9):.1f},{y2 + (9 if horizontal else -14):.1f}" class="fhead"/>')
            m.tween(f"#fa{k}", "opacity", times[k] + 0.25, times[k] + 0.4, "0", "1")
        _draw(m, f"#fl{k}", times[k] - 0.12, 0.45)
        if row.value.strip() and horizontal:
            nodes.append(f'<div class="fval num" id="fv{k}" style="left:{mid[0] - 70:.0f}px;top:{mid[1]:.0f}px">'
                         f'{C._roll(m, row.value, times[k] + 0.1, align="center")}</div>')
            m.fade_up(f"#fv{k}", times[k] + 0.1, rise=6, length=0.3)
    css = f""".fnode {{ position: absolute; display: flex; align-items: center; gap: 18px; padding: 0 22px; border-radius: 18px;
  background: linear-gradient(180deg, rgba(255,255,255,.05), rgba(255,255,255,.018)); border: 1px solid rgba(255,255,255,.12); }}
.fnode.h {{ flex-direction: column; justify-content: center; text-align: center; gap: 14px; padding: 18px 14px; }}
.fnode.main {{ border-color: {C._rgba(accent, 0.7)}; box-shadow: 0 0 0 1px {C._rgba(accent, 0.35)}, 0 0 46px {C._rgba(accent, 0.22)}; }}
.fnode .ft {{ min-width: 0; max-width: 100%; flex: 1; }}
.fnv {{ flex: none; font-size: 34px; font-weight: 700; color: {C.INK}; }}
.fnode.main .fnv {{ color: {accent}; }}
.fname {{ font-size: 34px; font-weight: 700; letter-spacing: -.015em; white-space: nowrap; overflow: hidden; }}
.fnode.main .fname {{ color: {accent}; }}
.fr {{ font-size: 21px; color: {C.SOFT}; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
#flow {{ position: absolute; left: 0; top: 0; width: {C.CARD_W}px; height: {ph}px; overflow: visible; }}
.flink {{ fill: none; stroke: {C._rgba(accent, 0.85)}; stroke-linecap: round; stroke-dasharray: 1; stroke-dashoffset: 1; }}
.flink.sized {{ stroke: {C._rgba(accent, 0.55)}; stroke-linecap: butt; }}
.fhead {{ fill: none; stroke: {C._rgba(accent, 0.85)}; stroke-width: 4; stroke-linecap: round; stroke-linejoin: round; opacity: 0; }}
.fval {{ position: absolute; width: 140px; text-align: center; font-size: 26px; font-weight: 700; color: {C.INK}; }}"""
    body = f'<svg id="flow" viewBox="0 0 {C.CARD_W} {ph}">{"".join(links)}</svg>' + "".join(nodes)
    focal = next(((boxes[k][0] + boxes[k][2] / 2, boxes[k][1] + boxes[k][3] / 2) for k, r in enumerate(rows) if r.highlight),
                 (C.CARD_W / 2, ph / 2))
    return C.Chart(css, body, m, "", (), focal)


# ---------------------------------------------------------------------------- framework matrix / flywheel
def _framework(card: Card, accent: str, ph: int) -> C.Chart:
    layout = str(card.context.get("layout") or ("matrix" if len(card.rows) == 4 else "flywheel"))
    return _matrix(card, accent, ph) if layout == "matrix" else _flywheel(card, accent, ph)


def _matrix(card: Card, accent: str, ph: int) -> C.Chart:
    """A 2x2: axes draw themselves, then the quadrants in the order said (top left, top
    right, bottom left, bottom right), the point's quadrant lit."""
    rows = card.rows[:4]
    times = _times(card, 0.22)
    left, top, size_w, size_h = C.PAD + 46, 8, C.IW - 46, ph - 62
    cw, ch = size_w / 2, size_h / 2
    m = C.Motion()
    x_axis = str(card.context.get("x") or "")
    y_axis = str(card.context.get("y") or "")
    axis = (f'<path id="ax" d="M{left:.0f},{top + size_h:.0f} L{left + size_w:.0f},{top + size_h:.0f}" pathLength="1" class="mx"/>'
            f'<path id="ay" d="M{left:.0f},{top + size_h:.0f} L{left:.0f},{top:.0f}" pathLength="1" class="mx"/>'
            f'<path id="mh" d="M{left:.0f},{top + ch:.0f} L{left + size_w:.0f},{top + ch:.0f}" pathLength="1" class="mg"/>'
            f'<path id="mv" d="M{left + cw:.0f},{top:.0f} L{left + cw:.0f},{top + size_h:.0f}" pathLength="1" class="mg"/>')
    for selector in ("#ax", "#ay"):
        _draw(m, selector, C.FIRST, 0.45)
    for selector in ("#mh", "#mv"):
        _draw(m, selector, C.FIRST + 0.15, 0.45)
    cells = []
    for k, row in enumerate(rows):
        col, line = k % 2, k // 2
        x, y = left + col * cw + 10, top + line * ch + 10
        main = " main" if row.highlight else ""
        detail = f'<div class="qd fitv">{_e(row.detail)}</div>' if row.detail.strip() else ""
        cells.append(f'<div class="quad{main}" id="q{k}" style="left:{x:.0f}px;top:{y:.0f}px;width:{cw - 20:.0f}px;'
                     f'height:{ch - 20:.0f}px"><div class="ql fitv">{_e(row.label)}</div>{detail}</div>')
        m.fade_up(f"#q{k}", times[k], rise=10, length=0.35)
        if row.highlight:
            m.cue(times[k] + 0.2, "tick")
            m.tween(f"#q{k}", "backgroundColor", times[k] + 0.2, times[k] + 0.5, "rgba(255,255,255,0.02)",
                    C._rgba(accent, 0.16))
    labels = ""
    if x_axis:
        labels += f'<div class="axl x fit" id="axl">{_e(x_axis)} →</div>'
        m.tween("#axl", "opacity", C.FIRST + 0.3, C.FIRST + 0.6, "0", "1")
    if y_axis:
        labels += f'<div class="axl y" id="ayl"><span class="fit">{_e(y_axis)} →</span></div>'
        m.tween("#ayl", "opacity", C.FIRST + 0.3, C.FIRST + 0.6, "0", "1")
    css = f"""#mat {{ position: absolute; left: 0; top: 0; width: {C.CARD_W}px; height: {ph}px; }}
.mx {{ fill: none; stroke: {C.SOFT}; stroke-width: 2.5; stroke-dasharray: 1; stroke-dashoffset: 1; stroke-linecap: round; }}
.mg {{ fill: none; stroke: {C.RULE}; stroke-width: 1.5; stroke-dasharray: 1; stroke-dashoffset: 1; }}
.quad {{ position: absolute; border-radius: 14px; padding: 20px 22px; background: rgba(255,255,255,0.02);
  border: 1px solid rgba(255,255,255,.07); display: flex; flex-direction: column; justify-content: center; overflow: hidden; }}
.quad.main {{ border-color: {C._rgba(accent, 0.7)}; }}
.ql {{ font-size: 34px; font-weight: 700; letter-spacing: -.015em; line-height: 1.12; max-height: 116px; overflow: hidden; }}
.quad.main .ql {{ color: {accent}; }}
.qd {{ font-size: 22px; color: {C.SOFT}; margin-top: 8px; line-height: 1.25; max-height: 58px; overflow: hidden; }}
.axl {{ position: absolute; font-size: 21px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: {C.SOFT};
  white-space: nowrap; overflow: hidden; }}
.axl.x {{ left: {left}px; width: {size_w}px; top: {top + size_h + 14:.0f}px; text-align: right; }}
.axl.y {{ left: {C.PAD - 4}px; top: {top}px; width: 36px; height: {size_h}px; writing-mode: vertical-rl; transform: rotate(180deg);
  text-align: right; }}
.axl.y span {{ display: inline-block; max-height: {size_h}px; }}"""
    body = f'<svg id="mat" viewBox="0 0 {C.CARD_W} {ph}">{axis}</svg>' + "".join(cells) + labels
    main = next((k for k, r in enumerate(rows) if r.highlight), 1)
    focal = (left + (main % 2 + 0.5) * cw, top + (main // 2 + 0.5) * ch)
    return C.Chart(css, body, m, "", (), focal)


def _flywheel(card: Card, accent: str, ph: int) -> C.Chart:
    """3-4 steps round a loop: each arrow draws itself to the next step, the last one
    back to the first (the loop that compounds)."""
    rows = card.rows[:4]
    n = len(rows)
    times = _times(card, 0.25)
    cx, cy = C.CARD_W / 2, ph / 2
    r = min(ph / 2 - 74, 230)
    m = C.Motion()
    angles = [-math.pi / 2 + 2 * math.pi * k / n for k in range(n)]
    points = [(cx + r * math.cos(a), cy + r * math.sin(a)) for a in angles]
    arcs, nodes = [], []
    for k in range(n):
        a0, a1 = angles[k] + 0.30, angles[k] + 2 * math.pi / n - 0.30
        x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        arcs.append(f'<path id="wa{k}" d="M{x0:.1f},{y0:.1f} A{r:.1f},{r:.1f} 0 0 1 {x1:.1f},{y1:.1f}" pathLength="1" class="warc"/>')
        tangent = a1 + math.pi / 2
        hx, hy = x1 + 16 * math.cos(tangent - 2.6), y1 + 16 * math.sin(tangent - 2.6)
        gx, gy = x1 + 16 * math.cos(tangent + 2.6), y1 + 16 * math.sin(tangent + 2.6)
        arcs.append(f'<path id="wh{k}" d="M{hx:.1f},{hy:.1f} L{x1:.1f},{y1:.1f} L{gx:.1f},{gy:.1f}" class="whead"/>')
        _draw(m, f"#wa{k}", times[k] + 0.12, 0.4)
        m.tween(f"#wh{k}", "opacity", times[k] + 0.45, times[k] + 0.55, "0", "1")
        x, y = points[k]
        main = " main" if rows[k].highlight else ""
        nodes.append(f'<div class="wnode{main}" id="wn{k}" style="left:{x - 170:.0f}px;top:{y - 44:.0f}px">'
                     f'<span class="wi num">{k + 1:02d}</span><span class="wl fit">{_e(rows[k].label)}</span></div>')
        m.fade_up(f"#wn{k}", times[k], rise=10, length=0.35)
        m.cue(times[k], "tick")
    center = '<div class="wc" id="wc">↻</div>'
    m.tween("#wc", "opacity", C.FIRST, C.FIRST + 0.4, "0", "1")
    css = f"""#wheel {{ position: absolute; left: 0; top: 0; width: {C.CARD_W}px; height: {ph}px; }}
.warc {{ fill: none; stroke: {C._rgba(accent, 0.8)}; stroke-width: 4; stroke-linecap: round; stroke-dasharray: 1; stroke-dashoffset: 1; }}
.whead {{ fill: none; stroke: {C._rgba(accent, 0.8)}; stroke-width: 4; stroke-linecap: round; stroke-linejoin: round; opacity: 0; }}
.wnode {{ position: absolute; width: 340px; height: 88px; border-radius: 44px; display: flex; align-items: center; gap: 14px;
  padding: 0 26px 0 18px; background: #0E131C; border: 1px solid rgba(255,255,255,.14); box-shadow: 0 10px 28px rgba(0,0,0,.5); }}
.wnode.main {{ border-color: {C._rgba(accent, 0.75)}; }}
.wi {{ flex: none; width: 52px; height: 52px; border-radius: 26px; background: {C._rgba(accent, 0.16)}; color: {accent};
  font-size: 22px; font-weight: 700; display: flex; align-items: center; justify-content: center; }}
.wl {{ font-size: 29px; font-weight: 700; letter-spacing: -.01em; white-space: nowrap; overflow: hidden; min-width: 0; }}
.wc {{ position: absolute; left: {cx - 60:.0f}px; top: {cy - 60:.0f}px; width: 120px; height: 120px; display: flex;
  align-items: center; justify-content: center; font-size: 84px; color: {C._rgba(accent, 0.55)}; }}"""
    body = f'<svg id="wheel" viewBox="0 0 {C.CARD_W} {ph}">{"".join(arcs)}</svg>{center}' + "".join(nodes)
    return C.Chart(css, body, m, "", (), (cx, cy))


# ---------------------------------------------------------------------------- editorial tear-sheet
def _quote_words(quote: str, at: int, length: int) -> tuple[str, list[int]]:
    """The quote as one span per word (id qw0, qw1...), the words of the highlighted phrase
    (characters at..at+length) inside one highlighter span. Returns (markup, the phrase's
    word indexes)."""
    out, phrase, offset, open_ = [], [], 0, False
    tokens = quote.split()
    for k, token in enumerate(tokens):
        begin = quote.index(token, offset)
        offset = begin + len(token)
        inside = at >= 0 and begin < at + length and offset > at
        if inside and not open_:
            out.append('<span class="hlt" id="hlt">')
            open_ = True
        if not inside and open_:
            out[-1] = out[-1].rstrip()
            out.append("</span> ")
            open_ = False
        if inside:
            phrase.append(k)
        out.append(f'<span class="qw" id="qw{k}">{_e(token)}</span> ')
    if open_:
        out[-1] = out[-1].rstrip()
        out.append("</span>")
    return "".join(out).strip(), phrase


def _headline(card: Card, accent: str, ph: int) -> C.Chart:
    """The speaker's thesis as a newspaper pull quote. Round 39: compact (it sits in the
    bottom-anchored card under the speaker), in as the quote starts, every word lit as it's
    said (core.card_checks aligns the quote's words to the transcript: context["cadence"]),
    the highlighter sweeping its key phrase as that phrase is said; the attribution under
    it, and a dek from Wikipedia when an event is named."""
    row = card.rows[0]
    quote, phrase = row.label.strip(), row.value.strip()
    t = C._at(card, row.reveal_at, C.FIRST + 0.1)
    lower = quote.lower()
    at = lower.find(phrase.lower()) if phrase else -1
    text, in_phrase = _quote_words(quote, at, len(phrase))
    cadence = [float(x) for x in card.context.get("cadence") or []]
    synced = len(cadence) == len(quote.split())
    m = C.Motion()
    m.tween("#rule", "transform", C.FIRST, C.FIRST + 0.45, "scaleX(0)", "scaleX(1)", C.EASE)
    m.fade_up("#qt", t, rise=10, length=0.4)
    m.tween("#qmark", "opacity", C.FIRST, C.FIRST + 0.4, "0", "1")
    lit = []
    if synced:  # each word comes up from dim as it's said (the cadence is already WORD_LEAD early)
        lit = [C._at(card, x, t) for x in cadence]
        for k, when in enumerate(lit):
            m.tween(f"#qw{k}", "opacity", when, when + 0.16, ".26", "1")
    if at >= 0:
        sweep_from = lit[in_phrase[0]] if synced and in_phrase else t + 0.35
        sweep_to = max(sweep_from + 0.35, lit[in_phrase[-1]] + 0.25) if synced and in_phrase else t + 0.85
        m.tween("#hlt", "backgroundSize", sweep_from, sweep_to, "0% 100%", "100% 100%", C.EASE)
        m.cue(sweep_from, "tick")
    done = lit[-1] if lit else t
    byline = f'<div class="by" id="by">— {_e(row.detail)}</div>' if row.detail.strip() else ""  # right under the quote
    if byline:
        m.fade_up("#by", done + 0.2, rise=8, length=0.35)
    dek = ""
    if card.footnote.strip():
        dek = f'<div class="dek" id="dek"><b>Background</b> {_e(card.footnote)}</div>'
        m.fade_up("#dek", done + 0.3, rise=8, length=0.35)
    css = f"""#rule {{ position: absolute; left: {C.PAD}px; right: {C.PAD}px; top: 4px; height: 4px; background: {C.INK};
  transform-origin: left center; }}
#rule::after {{ content: ""; position: absolute; left: 0; right: 0; top: 9px; height: 1px; background: rgba(245,246,248,.45); }}
#qmark {{ position: absolute; left: {C.PAD - 6}px; top: 12px; font-family: "Instrument Serif", Georgia, serif; font-size: 120px;
  line-height: 1; color: {C._rgba(accent, 0.38)}; }}
#qbox {{ position: absolute; left: {C.PAD + 6}px; right: {C.PAD}px; top: 64px; bottom: {58 if card.footnote.strip() else 12}px;
  display: flex; flex-direction: column; justify-content: center; }}
#qt {{ font-family: "Instrument Serif", Georgia, serif; font-size: 58px; line-height: 1.1; letter-spacing: -.014em; color: {C.INK};
  max-height: {ph - 150}px; overflow: hidden; }}
.qw {{ opacity: {".26" if synced else "1"}; }}
.hlt {{ background-image: linear-gradient(transparent 52%, {C._rgba(accent, 0.55)} 52%, {C._rgba(accent, 0.55)} 92%, transparent 92%);
  background-repeat: no-repeat; background-size: 0% 100%; padding: 0 4px; margin: 0 -4px; -webkit-box-decoration-break: clone;
  box-decoration-break: clone; }}
.by {{ margin-top: 18px; font-size: 24px; font-weight: 600; letter-spacing: .02em; color: {C.SOFT}; }}
.dek {{ position: absolute; left: {C.PAD}px; right: {C.PAD}px; bottom: 4px; padding-top: 10px; border-top: 1px solid {C.RULE};
  font-size: 21px; color: {C.SOFT}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.dek b {{ color: {C.INK}; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; font-size: 17px; margin-right: 10px; }}"""
    body = f'<div id="rule"></div><div id="qmark">“</div><div id="qbox"><div id="qt" class="fitv">{text}</div>{byline}</div>{dek}'
    key = ""
    if card.source:
        key = f'<div class="key" id="ckey"><span class="src">{_e("Source: " + card.source)}</span></div>'
        m.tween("#ckey", "opacity", 0.3, 0.6, "0", "1")
    return C.Chart(css, body, m, "", (), (C.CARD_W * 0.4, ph * 0.4), key=key)


CHARTS = {
    "geo_map_card": _geo_map,
    "prediction_gauge_card": _prediction_gauge,
    "flow_sankey_card": _flow_chain,
    "framework_matrix_card": _framework,
    "headline_card": _headline,
}
