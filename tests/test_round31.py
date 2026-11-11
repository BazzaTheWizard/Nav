"""Round 31 checks: the broadcast-grade graphics engine. Chart libraries vendored for
offline renders (Observable Plot + D3, ECharts in SVG mode, TradingView Lightweight
Charts), frame-exact stepping (the same frame always comes out the same, in any order),
the Bento card (output y 492-1340, x 28-1052, above the small captions; 18/68/14 zones),
the type (Instrument Serif, Geist, JetBrains Mono), numbers that roll up as they're said,
the change pill, the footer, glass pills outside the card, no TradingView logo in the
video and its credit on the clip pages and the website.
Temp files only: no AI, YouTube, R2 or network.

Run from the project root:  .venv/bin/python tests/test_round31.py .
"""
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["NAV_NO_CLIP_PAGES"] = "1"
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


from PIL import Image  # noqa: E402

import core.cards as C  # noqa: E402
import core.dynamic_layout as D  # noqa: E402
from core.edl import MODES, TEMPLATES, BrandKit, Card, CardRow  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="round31-"))
S = 100.0


def row(label, value="", hl=False, at=None, **kw):
    return CardRow(label, value, highlight=hl, reveal_at=None if at is None else S + at, **kw)


def price_file(path):
    lines, price = ["date,open,high,low,close,volume"], 100.0
    for day in range(40):
        change = (1.6 if day % 5 else -2.4) + (day % 3) * 0.3
        lines.append(f"2026-09-{day % 28 + 1:02d},{price:.2f},{price + 2.5:.2f},{price - 2.0:.2f},{price + change:.2f},{1000 + 37 * day}")
        price += change
    path.write_text("\n".join(lines))
    return str(path)


CARDS = {
    "metric_bars": Card("metric_bars", S, S + 4, headline="Where the money goes",
                        rows=[row("Payroll", "$4.2M", at=0.9), row("Hosting", "$1.1M", at=1.5), row("Ads", "$0.9M–$1.3M", True, 2.0)]),
    "ranked_bars": Card("ranked_bars", S, S + 4, headline="Top markets", rows=[row("US", "$12B", at=1.0), row("Japan", "$6B", True, 1.8)]),
    "diverging_bar": Card("diverging_bar", S, S + 4, headline="Who pays", rows=[row("Buyers", "62%", True, 1.2), row("Sellers", "38%", at=1.8)]),
    "share_split": Card("share_split", S, S + 4, headline="The portfolio",
                        rows=[row("Software", "40%", True, 0.9), row("Fintech", "35%", at=1.4), row("Other", "25%", at=1.9)]),
    "trend_line": Card("trend_line", S, S + 4, headline="ARR", rows=[row("2022", "$5M", at=0.9), row("2023", "$11M", at=1.5),
                                                                    row("2024", "$19M", True, 2.1)]),
    "timeline": Card("timeline", S, S + 4, headline="From garage to IPO",
                     rows=[row("", "2014", at=0.9, detail="Founded"), row("", "2023", at=1.9, detail="Listed")]),
    "arr_bridge": Card("arr_bridge", S, S + 4.5, headline="ARR bridge", rows=[row("Start", "$10M", at=0.8), row("New", "+$4.5M", at=1.3),
                                                                            row("Churn", "−$1.5M", at=1.8), row("End", "$13M", True, 2.3)]),
    "sankey_flow": Card("sankey_flow", S, S + 4.5, headline="Each dollar", rows=[
        row("Revenue", "$10M", at=0.8), row("Costs", "$6M", at=1.4, parent="Revenue"), row("Profit", "$4M", True, 2.0, parent="Revenue")]),
    "cac_payback": Card("cac_payback", S, S + 4, headline="Payback", rows=[row("Cost to win one", "$1,200", at=0.9),
                                                                         row("Break-even", "14 months", at=1.9)]),
    "drawdown_curve": Card("drawdown_curve", S, S + 4.5, headline="The crash", rows=[row("From the peak", "−34%", at=1.0),
                                                                                    row("Back to new highs", "18 months", at=2.2)]),
    "candlestick_ema": Card("candlestick_ema", S, S + 3.5, headline="The S&P", source="Sample data",
                            data_path=price_file(tmp / "prices.csv")),
    # Round 35: real market data (date, value; a price file's closes work too), with its source
    "market_line": Card("market_line", S, S + 5.4, headline="The S&P since 2020", source="Yahoo Finance",
                        footnote="Sep 2026, daily", data_path=str(tmp / "prices.csv"),
                        rows=[CardRow("Sep 1", "$100"), CardRow("Sep 28", "$162"), CardRow("Change", "+62%", True)]),
    "speaker_title": Card("speaker_title", S, S + 3.3, rows=[CardRow("Jonathan Bragdon", detail="Founder")]),
    "stat_pill": Card("stat_pill", S, S + 3, rows=[row("ARR", "40M", at=0.4)]),
    "kinetic_range": Card("kinetic_range", S, S + 3, rows=[row("a year in fees", "$850–$1,700", True, 0.5)]),
    "buckets": Card("buckets", S, S + 3, rows=[row("Expected", at=0.4, dashed=True), row("Actual", at=1.2)]),
    # Round 32: the qualitative Bento cards (their checks are in test_round32)
    "comparison_card": Card("comparison_card", S, S + 4, headline="Marketing in growth", rows=[
        row("Venture firms", "Before", at=0.8, detail="didn't market themselves"),
        row("Then we did", "Now", True, 2.0, detail="and it worked")]),
    "step_sequence": Card("step_sequence", S, S + 4, rows=[row("Find the market", at=0.6), row("Hire the team", at=1.4),
                                                          row("Scale the brand", "", True, 2.2, detail="go tell your story")]),
    # Round 36: the multi-niche cards (their checks are in test_round36)
    "geo_map_card": Card("geo_map_card", S, S + 5, headline="Chips leave China",
                         rows=[row("China", at=0.5), row("United States", hl=True, at=0.8)]),
    "prediction_gauge_card": Card("prediction_gauge_card", S, S + 5, headline="A December cut?",
                                  rows=[row("Cut", "70%", True, 0.6)]),
    "flow_sankey_card": Card("flow_sankey_card", S, S + 5, headline="Who builds the chips",
                             rows=[row("ASML", at=0.5), row("TSMC", at=0.8), row("NVIDIA", hl=True, at=1.0)]),
    "framework_matrix_card": Card("framework_matrix_card", S, S + 5, headline="The flywheel", context={"layout": "flywheel"},
                                  rows=[row("Lower prices", at=0.4), row("More customers", at=0.6), row("More sellers", at=0.8)]),
    "headline_card": Card("headline_card", S, S + 5, eyebrow="The rule",
                          rows=[row("Never fight the Fed.", "fight the Fed", True, 0.5, detail="Marty Zweig")]),
}
check("A sample for every graphic", set(CARDS) == set(TEMPLATES), str(set(TEMPLATES) ^ set(CARDS)))

# ---------- offline: every library and font is in the project ----------
vendor = {name: (ROOT / "assets" / "vendor" / name) for lib in C.LIBRARIES.values() for name in lib}
check("Vendored: d3, Observable Plot, ECharts, Lightweight Charts, and (Round 36) TopoJSON with the world map (no CDN)",
      all(p.is_file() and p.stat().st_size > (5_000 if n == "topojson-client.min.js" else 100_000) for n, p in vendor.items())
      and set(vendor) == {"d3.min.js", "plot.min.js", "echarts.min.js", "lightweight-charts.standalone.production.js",
                          "topojson-client.min.js", "world-50m.js"}, str(vendor))
check("Their licenses travel with them", all((ROOT / "assets" / "vendor" / f).is_file() for f in
                                             ("LICENSE-d3.txt", "LICENSE-plot.txt", "LICENSE-echarts.txt", "LICENSE-lightweight-charts.txt")))
fonts = ["InstrumentSerif-Regular.woff2", "Geist-Variable.woff2", "JetBrainsMono-Variable.woff2"]
check("Fonts vendored with their licenses: Instrument Serif, Geist, JetBrains Mono",
      all((ROOT / "assets" / "fonts" / f).is_file() for f in fonts)
      and all((ROOT / "assets" / "fonts" / f).is_file() for f in ("OFL-InstrumentSerif.txt", "OFL-Geist.txt", "OFL-JetBrainsMono.txt")))

pages = {t: C.scene(card, BrandKit())[0] for t, card in CARDS.items()}
remote = {t: re.findall(r'(?:src|href)=["\']?(https?:[^"\'> ]+)|url\(["\']?(https?:[^)"\']+)', p) for t, p in pages.items()}
check("No page loads anything from the internet (scripts, fonts, styles)", not any(remote.values()),
      str({t: r for t, r in remote.items() if r}))
local = {t: re.findall(r'<script src="(file://[^"]+)"', p) for t, p in pages.items()}
check("Every script a page loads is a local file that exists",
      all(Path(re.sub("^file://", "", u).replace("%20", " ")).is_file() for urls in local.values() for u in urls))
uses = {t: {lib for lib, files in C.LIBRARIES.items() if all(f in p for f in files)} for t, p in pages.items()}  # a library is used when all its files load
check("Plot + D3 draw the bars, splits, shares, trends, timelines and the payback and drawdown curves",
      all(uses[t] == {"plot"} for t in ("metric_bars", "ranked_bars", "diverging_bar", "share_split", "trend_line", "timeline",
                                        "cac_payback", "drawdown_curve")), str(uses))
check("ECharts (SVG renderer) draws the ARR bridge and the Sankey",
      uses["arr_bridge"] == uses["sankey_flow"] == {"echarts"} and all("renderer: 'svg'" in pages[t] for t in ("arr_bridge", "sankey_flow")))
check("Lightweight Charts draws the price chart, with TradingView's logo off in the video",
      uses["candlestick_ema"] == {"lightweight"} and "attributionLogo: false" in pages["candlestick_ema"])
check("Over-the-speaker graphics load no chart library", all(not uses[t] and "d3.min.js" not in pages[t] for t in TEMPLATES if MODES[t] == "overlay"))
check("The map (Round 36) loads D3 with TopoJSON and the bundled world map", uses["geo_map_card"] == {"geo"})
check("No chart animates on its own clock (library animation off; one frame clock)",
      all("animation: false" in pages[t] for t in ("arr_bridge", "sankey_flow")) and "window.seekToFrame" in pages["metric_bars"]
      and "cubic-bezier(0.16, 1, 0.3, 1)" in C.EASE and "bezier(0.16, 1, 0.3, 1)" in C.RUNTIME)

# ---------- the Bento card ----------
check("The card (Round 39): output y 1056-1768, x 28-1052, right under the speaker's 1040 px viewport",
      (C.CARD_TOP, C.CARD_BOTTOM, C.CARD_X, C.CARD_X + C.CARD_W, C.SPLIT_TOP) == (1056, 1768, 28, 1052, 1040))
check("...it ends 152 px above the frame's bottom (the apps' bars); its captions sit on the seam above it",
      1920 - C.CARD_BOTTOM == 152 and D.SPLIT_CAPTIONS[1] < C.CARD_TOP, f"{C.CARD_BOTTOM} vs {D.SPLIT_CAPTIONS}")
check("Three zones: header 16%, chart 72%, footer 12% (Round 39: compact)",
      abs(C.HEADER_H / C.CARD_H - 0.16) < 0.005 and abs(C.FOOTER_H / C.CARD_H - 0.12) < 0.005
      and abs((C.CARD_H - C.HEADER_H - C.FOOTER_H) / C.CARD_H - 0.72) < 0.005)
check("Canvas #040507 around a #090C12 card with a 20 px radius and a top-lit 1 px rim",
      C.CANVAS == "#040507" and C.CARD_BG == "#090C12" and C.RADIUS == 20
      and "rgba(255,255,255,.12) 26%, rgba(255,255,255,.035) 100%" in pages["metric_bars"]  # Round 35: accent-lit corner
      and '<div class="spec-top"></div>' in pages["metric_bars"])
check("Studio texture: a 3.5% dot grid, the accent's glow at 6%, 2% noise",
      "rgba(255,255,255,.035)" in pages["trend_line"] and "rgba(244,196,48,0.06)" in pages["trend_line"]
      and "feTurbulence" in pages["trend_line"] and 'opacity=".02"' in pages["trend_line"])
check("Type: Instrument Serif headlines, Geist labels, JetBrains Mono numbers with tabular digits",
      'font-family: "Instrument Serif"' in pages["metric_bars"] and "font-family: Geist" in pages["metric_bars"]
      and '.num {{ font-family: "JetBrains Mono"'.replace("{{", "{") in pages["metric_bars"] and "tabular-nums" in pages["metric_bars"])
check("The eyebrow names the chart (tracked caps)", "Breakdown" in pages["metric_bars"] and "letter-spacing: .18em" in pages["metric_bars"])
check("Change pill: the punchline's signed change, rose for a fall (nothing computed)",
      'id="kpi"' in C.scene(Card("metric_bars", S, S + 4, headline="Sell-off", rows=[row("Nikkei", "−5%", at=1), row("SoftBank", "−10%", True, 2)]),
                            BrandKit())[0] and 'id="kpi"' not in pages["metric_bars"] and C._kpi_row(CARDS["share_split"]) is None)
check("The footer shows only with something in it (a context strip, the price chart's key); otherwise the chart takes its room",
      "display: none" in pages["metric_bars"].split(".ftr {")[1].split("}")[0]
      and "display: none" not in pages["candlestick_ema"].split(".ftr {")[1].split("}")[0] and "Source: Sample data" in pages["candlestick_ema"])

# ---------- drawing every graphic ----------
brand = BrandKit()
drawn = {}
for template, card in CARDS.items():
    try:
        drawn[template] = C.render_card(card, brand, tmp / "cards")
    except Exception as exc:  # noqa: BLE001
        drawn[template] = exc
broken = {t: str(f)[:200] for t, f in drawn.items() if isinstance(f, Exception)}
check(f"All {len(CARDS)} graphics draw on the new engine", not broken, str(broken))
good = {t: f for t, f in drawn.items() if not isinstance(f, Exception)}


def rgba(frames, k):
    return np.array(Image.open(frames.pattern % k).convert("RGBA")).astype(int)


def lit(px):
    return int(((px[:, :, :3].min(axis=2) > 200) & (px[:, :, 3] > 200)).sum())


cy0, cy1 = C.CARD_Y, C.CARD_Y + C.CARD_H
for t in ("metric_bars", "arr_bridge", "candlestick_ema"):
    if t in good:
        last = rgba(good[t], good[t].count - 2)
        inside = last[cy0 + 30:cy1 - 30, C.CARD_X + 30:C.CARD_X + C.CARD_W - 30, 3]
        outside = np.concatenate([last[:cy0 - 2, :, 3].ravel(), last[cy1 + 2:, :, 3].ravel(),
                                  last[cy0:cy1, :C.CARD_X - 2, 3].ravel(), last[cy0:cy1, C.CARD_X + C.CARD_W + 2:, 3].ravel()])
        check(f"{t}: the card fills exactly its box; nothing drawn outside it (so never on the captions)",
              int(inside.min()) == 255 and int(outside.max()) == 0, f"{inside.min()} {outside.max()}")
for t in ("metric_bars", "arr_bridge", "sankey_flow", "candlestick_ema", "drawdown_curve", "trend_line"):
    if t in good:
        f = good[t]
        early, late = lit(rgba(f, 2)), lit(rgba(f, f.count - 2))
        check(f"{t}: drawn as it's said (nothing on frame 2 but the card; the data by the end)", early < 60 and late > 1000,  # Round 39: the compact card has fewer lit pixels
              f"{early} -> {late}")
if "arr_bridge" in good:
    f = good["arr_bridge"]
    # Round 35: the entrance plays on a faster clock (core.cards' time-warp): the same story moments, sooner
    warp = C.scene(CARDS["arr_bridge"], brand)[1]
    mids = [lit(rgba(f, k)) for k in (round(warp.real(t) * 30) for t in (0.9, 1.5, 2.6))]
    check("ECharts waterfall: its bars grow frame by frame from our clock (more drawn at each step)",
          mids[0] < mids[1] < mids[2], str(mids))
if "candlestick_ema" in good:
    f = good["candlestick_ema"]
    warp = C.scene(CARDS["candlestick_ema"], brand)[1]
    steps = [lit(rgba(f, k)) + int((rgba(f, k)[:, :, 1] > 150).sum()) for k in (round(warp.real(t / 30) * 30) for t in (20, 40, 70))]
    check("Lightweight Charts: candles come in left to right from our clock", steps[0] < steps[1] < steps[2], str(steps))
    page_dom = pages["candlestick_ema"]
    check("...and no TradingView logo or link is drawn into the video", "tradingview.com" not in page_dom.lower())

# ---------- frame-exact: the same frame always comes out the same ----------
from playwright.sync_api import sync_playwright  # noqa: E402


def frame_hashes(template, order):
    card = CARDS[template]
    page, motion, width, height, _top = C.scene(card, brand)
    path = tmp / f"det_{template}.html"
    path.write_text(page, encoding="utf-8")
    out = {}
    requests = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        view = browser.new_page(viewport={"width": width, "height": height})
        view.on("request", lambda r: requests.append(r.url))
        view.goto(path.as_uri())
        view.evaluate(C._SETUP, motion.js(max(0.5, card.end - card.start)))
        for k in order:
            view.evaluate(C._SEEK, [k, C.FPS])
            out[k] = np.array(Image.open(__import__("io").BytesIO(view.screenshot(omit_background=True)))).astype(np.int16)
        # Round 33: odometer digits: what shows is each digit column's position (the text stays the final value)
        rolled = view.evaluate("() => [...document.querySelectorAll('[data-roll]')].map(e => [...e.childNodes].map(n =>"
                               " n.classList && n.classList.contains('od') ? String(Math.round(+getComputedStyle(n)"
                               ".getPropertyValue('--y') || 0) % 10) : n.textContent).join(''))")
        browser.close()
    return out, requests, rolled


def same_frame(a, b):
    """The same frame: every element where it was, in the same colour. Chrome's rasterizer
    can shade a few anti-aliased edge pixels a little differently from one browser session
    to the next (measured: at most 49 pixels of 1.6 million, by at most 34/255); anything
    bigger, like a box shifted by a pixel, fails."""
    diff = np.abs(a - b).max(axis=2)
    return int((diff > 0).sum()) <= 120 and int(diff.max()) <= 48, f"{int((diff > 0).sum())} px, max {int(diff.max())}"


for template in ("arr_bridge", "sankey_flow", "candlestick_ema", "trend_line", "buckets", "geo_map_card"):
    forward, requests, _ = frame_hashes(template, [10, 30, 50, 70])
    backward, _, _ = frame_hashes(template, [70, 50, 30, 10, 50])
    results = [same_frame(forward[k], backward[k]) for k in forward]
    check(f"{template}: frame-exact (the same frame on a second render, and stepping backwards or skipping)",
          all(ok for ok, _ in results), str([d for ok, d in results if not ok]))
    check(f"{template}: no network request while drawing", all(u.startswith(("file:", "data:", "about:")) for u in requests),
          str([u for u in requests if not u.startswith(("file:", "data:", "about:"))][:3]))
again_dir = tmp / "again"
if "metric_bars" in good:
    second = C.render_card(CARDS["metric_bars"], brand, again_dir)
    results = [same_frame(rgba(good["metric_bars"], k).astype(np.int16), rgba(second, k).astype(np.int16))
               for k in range(0, second.count, 7)]
    check("A whole render, done twice from scratch, gives the same frames", second.count == good["metric_bars"].count
          and all(ok for ok, _ in results), str([d for ok, d in results if not ok]))

# ---------- numbers roll up as they're said ----------
_h, _r, mid_roll = frame_hashes("metric_bars", [round(C.scene(CARDS["metric_bars"], brand)[1].real(0.9 + 0.1) * 30)])
_h, _r, end_roll = frame_hashes("metric_bars", [int(3.5 * 30)])
check("Numbers roll up to their value from the word where they're said, then hold it exactly",
      end_roll == ["$4.2M", "$1.1M", "$0.9M–$1.3M"] and mid_roll[0] != "$4.2M" and re.fullmatch(r"\$\d\.\dM", mid_roll[0])
      and mid_roll[1] == "$0.0M", f"{mid_roll} / {end_roll}")
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    view = browser.new_page()
    view.set_content(f"<script>{C.RUNTIME}</script>")
    formats = view.evaluate("() => [NAV.rollText('$1,700', 0.25), NAV.rollText('$1,700', 0.75), NAV.rollText('−$0.4M', 0.5),"
                            " NAV.rollText('$850–$1,700', 0.5), NAV.rollText('14 months', 1), NAV.prog(1, 0, 2) > 0.5]")
    browser.close()
check("...keeping the format: separators, decimals and signs ('$1,700' counts '$425', '$1,275'; '−$0.4M' via '−$0.2M')",
      formats == ["$425", "$1,275", "−$0.2M", "$425–$850", "14 months", True], str(formats))

# ---------- glass pills outside the card ----------
if "speaker_title" in good and "stat_pill" in good:
    check("Name card and micro-stat: glass pills over the video, blurred behind (rounded), outside any card",
          good["speaker_title"].blur and good["speaker_title"].blur[0][4] >= 20 and good["stat_pill"].blur
          and good["speaker_title"].height == 1920 and "1px" in pages["speaker_title"] and 'class="card"' not in pages["speaker_title"])
check("The blur behind glass is about CSS blur(12px) (two 31-px box passes: a Gaussian of ~12.6 px)",
      D.BLUR == "boxblur=15:2" and abs((2 * ((2 * 15 + 1) ** 2 - 1) / 12) ** 0.5 - 12.6) < 0.1)

# ---------- the TradingView credit: on the pages, not in the video ----------
worker = (ROOT / "cloudflare" / "clip-pages-worker.js").read_text()
check("Client preview pages credit TradingView with a link (its license), in the footer",
      'href="https://www.tradingview.com/"' in worker and "TradingView Lightweight Charts" in worker.split("<footer>")[1])

# ---------- speed: Chrome's fast PNG path ----------
check("Frames are captured with Chrome's fast PNG path (same pixels, about 4x quicker)",
      "optimizeForSpeed" in Path(C.__file__).read_text())

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
