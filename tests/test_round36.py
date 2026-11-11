"""Round 36 checks: the multi-niche Bento cards (a map, odds, a value chain, a framework,
a tear-sheet) inside the card's band with their data sources, YouTube replay-heatmap
scoring, broadcast camera physics (cut snapping, an 8% dead band with hysteresis, a
critically damped spring), semantic caption keywords with no caption ever on a Bento
card, and broadcast loudness mastering (-14 LUFS, -1.5 dBTP) with dead-air flags.
No network: World Bank, Polymarket and Wikipedia answers are faked.

Run from the project root:  .venv/bin/python tests/test_round36.py .
"""
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp(prefix="round36-"))
os.environ.update({"NAV_NO_CLIP_PAGES": "1", "NAV_LOGO_DIR": str(tmp / "logos"), "NAV_NICHE_CACHE": str(tmp / "niche"),
                   "NAV_MARKET_CACHE": str(tmp / "market"), "NAV_SFX_DIR": str(tmp / "sfx")})
for name in ("NICHE_DATA", "MARKET_DATA", "NAV_OFFLINE", "KEYWORD_EMPHASIS", "NUMBER_EMPHASIS", "AUDIO_CLEANUP",
             "AUDIO_TARGET_LUFS", "EMPHASIS_INTENSITY", "CAPTION_STYLE", "SFX"):
    os.environ.pop(name, None)
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


import numpy as np  # noqa: E402

import core.audio_polish as A  # noqa: E402
import core.camera_crop as CC  # noqa: E402
import core.captions as CAP  # noqa: E402
import core.card_checks as K  # noqa: E402
import core.cards as C  # noqa: E402
import core.entity_enricher as E  # noqa: E402
import core.face_tracker as F  # noqa: E402
import core.heatmap_scorer as H  # noqa: E402
import core.niche_data as N  # noqa: E402
import core.niche_director as ND  # noqa: E402
from core.edl import MODES, TEMPLATES, BrandKit, Card, CardRow, ClipEdit, sentences  # noqa: E402
from core.transcriber import Word  # noqa: E402

FFMPEG = "ffmpeg"


def words_from(text, start=100.0, step=0.3):
    return [Word(t, round(start + k * step, 3), round(start + k * step + 0.25, 3)) for k, t in enumerate(text.split())]


# ======================= 1. five new Bento archetypes =======================
NEW = ["geo_map_card", "prediction_gauge_card", "flow_sankey_card", "framework_matrix_card", "headline_card"]
check("Five new card types, all split-view Bento cards", all(t in TEMPLATES and MODES[t] == "split" for t in NEW))
check("...each with a builder", all(t in C.CHARTS for t in NEW))
check("The offline world map is bundled (Natural Earth via world-atlas, with its licence)",
      (ROOT / "assets/vendor/world-50m.js").is_file() and (ROOT / "assets/vendor/topojson-client.min.js").is_file()
      and (ROOT / "assets/vendor/LICENSE-world-atlas.txt").is_file())

check("Countries are found in the order named, by alias, each once (South Korea isn't Korea twice)",
      [c.name for c in N.countries_in("Chips go from Taiwan to the US, then America ships to South Korea and the US")]
      == ["Taiwan", "United States", "South Korea"])
check("...and a lowercase 'us' is not the United States", N.countries_in("let us talk about it") == [])

S = 100.0


def row(label, value="", hl=False, at=None, **kw):
    return CardRow(label, value, highlight=hl, reveal_at=None if at is None else S + at, **kw)


SAMPLES = {
    "geo2": Card("geo_map_card", S, S + 5.4, headline="Chips leave China", context={"arcs": [[1, 2]]},
                 rows=[row("China", at=0.5), row("United States", hl=True, at=0.8)]),
    "geo3": Card("geo_map_card", S, S + 5.4, headline="Where the chips go", source="World Bank (2024)",
                 rows=[row("Taiwan", "$0.8T", at=0.5, detail="GDP"), row("United States", "$29.2T", True, 0.7, detail="GDP"),
                       row("China", "$18.7T", at=0.9, detail="GDP")]),
    "geo1": Card("geo_map_card", S, S + 5.4, headline="The Iran question", rows=[row("Iran", hl=True, at=0.5)]),
    "gauge": Card("prediction_gauge_card", S, S + 5.4, headline="A December rate cut?", source="Polymarket",
                  footnote="$9.5M traded", rows=[row("Cut", "68%", True, 0.5), row("No cut", "32%")]),
    "chain": Card("flow_sankey_card", S, S + 5.4, headline="Who builds the chips",
                  rows=[row("ASML", at=0.5, detail="Lithography"), row("TSMC", at=0.75, detail="Fabs"),
                        row("NVIDIA", hl=True, at=1.0, detail="Design")]),
    "chain4": Card("flow_sankey_card", S, S + 5.4, headline="Where a dollar goes",
                   rows=[row("Revenue", "$10B", at=0.4), row("Cloud", "$6B", at=0.6), row("Ads", "$3B", True, at=0.8),
                         row("Devices", "$1B", at=1.0)]),
    "matrix": Card("framework_matrix_card", S, S + 5.4, headline="Where to invest",
                   context={"layout": "matrix", "x": "Market size", "y": "Growth"},
                   rows=[row("Hidden gems", at=0.4, detail="Small but fast"), row("Category kings", hl=True, at=0.6),
                         row("Lifestyle", at=0.8), row("Cash cows", at=1.0, detail="Big but slow")]),
    "wheel": Card("framework_matrix_card", S, S + 5.4, headline="The flywheel", context={"layout": "flywheel"},
                  rows=[row("Lower prices", at=0.4), row("More customers", at=0.6), row("More sellers", hl=True, at=0.8)]),
    "headline": Card("headline_card", S, S + 5.4, eyebrow="The rule", source="Wikipedia",
                     footnote="1933 US law separating commercial and investment banking",
                     rows=[row("Never fight the Fed, because the Fed always gets the last word.", "always gets the last word",
                               True, 0.5, detail="Steve Eisman")]),
}
for key in ("asml", "tsmc", "nvidia"):
    E.enrich(key, offline=True)

from playwright.sync_api import sync_playwright  # noqa: E402

PROBE = """() => {
  const cardEl = document.querySelector('.card'), card = cardEl.getBoundingClientRect();
  const out = [];  // every piece of text or picture (the card clips its glows and backgrounds)
  for (const el of document.querySelectorAll('.card *')) {
    if (el.closest('svg')) continue;
    const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    if (!own && el.tagName.toLowerCase() !== 'img') continue;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (!r.width || !r.height || s.display === 'none' || s.visibility === 'hidden') continue;
    out.push([el.id || el.className || el.tagName, r.top, r.bottom, r.left, r.right]);
  }
  const labels = [...document.querySelectorAll('.glab')].map(l => l.getBoundingClientRect())
    .map(r => [r.left, r.top, r.right, r.bottom]);
  const dash = sel => [...document.querySelectorAll(sel)].map(p => parseFloat(getComputedStyle(p).strokeDashoffset));
  return {card: [card.top, card.bottom, card.left, card.right], clips: getComputedStyle(cardEl).overflow, parts: out, labels,
          geoOverflow: document.querySelector('#geo') ? getComputedStyle(document.querySelector('#geo')).overflow : '',
          hl: [...document.querySelectorAll('[id^=hl]')].length,
          arcs: dash('[id^=amp]'), axes: dash('#ax, #ay, #mh, #mv'), wheel: dash('[id^=wa]'), links: dash('[id^=fl]'),
          sweep: document.querySelector('.hlt') ? getComputedStyle(document.querySelector('.hlt')).backgroundSize : '',
          yes: document.querySelector('#gyes') ? document.querySelector('#gyes').getBoundingClientRect().width : 0,
          bar: document.querySelector('#gauge') ? document.querySelector('#gauge').getBoundingClientRect().width : 0,
          logos: document.querySelectorAll('.fnode img').length,
          footer: document.querySelector('.ftr, .key') ? document.querySelector('.ftr, .key').textContent : ''};
}"""
results, errors = {}, {}
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for name, card in SAMPLES.items():
        page, motion, width, height, top = C.scene(card, BrandKit())
        html = tmp / f"{name}.html"
        html.write_text(page)
        view = browser.new_page(viewport={"width": width, "height": height})
        errs = []
        view.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
        view.goto(html.as_uri())
        over = view.evaluate(C._SETUP, motion.js(card.end - card.start))
        view.evaluate(C._SEEK, [0, 30])
        start = view.evaluate(PROBE)
        view.evaluate(C._SEEK, [int(3.0 * 30), 30])
        end = view.evaluate(PROBE)
        results[name] = (top, over, start, end)
        errors[name] = errs
        view.close()
    browser.close()

check("Every new card renders without a script error", not any(errors.values()), str({k: v[:1] for k, v in errors.items() if v}))
check("...and with no text overflowing", not any(r[1] for r in results.values()),
      str({k: r[1] for k, r in results.items() if r[1]}))
outside = {}
for name, (top, _over, _start, end) in results.items():
    box = end["card"]
    bad = [(p[0], round(top + p[1]), round(top + p[2])) for p in end["parts"]
           if p[1] < box[0] - 0.5 or p[2] > box[1] + 0.5 or p[3] < box[2] - 0.5 or p[4] > box[3] + 0.5]
    if bad or end["clips"] != "hidden" or not (C.CARD_TOP - 0.5 <= top + box[0] and top + box[1] <= C.CARD_BOTTOM + 0.5
                                               and box[2] >= 0 and box[3] <= 1080):
        outside[name] = bad[:3] or end["card"]
check("Every new card stays inside the Bento band, y 1056-1768, clipped to it, with every word and logo inside it",
      not outside, str(outside))
check("The map clips to its chart area (a single zoomed-in country never draws over the title)",
      all(results[n][3]["geoOverflow"] == "hidden" for n in ("geo1", "geo2", "geo3")))
check("...the named countries are lit (one highlight path per country)",
      [results[n][3]["hl"] for n in ("geo1", "geo2", "geo3")] == [1, 2, 3], str([results[n][3]["hl"] for n in ("geo1", "geo2", "geo3")]))


def overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


labels = results["geo3"][3]["labels"]
check("...country labels never overlap each other (Taiwan sits right next to China)",
      len(labels) == 3 and not any(overlap(a, b) for i, a in enumerate(labels) for b in labels[i + 1:]), str(labels))
check("...the trade line draws itself in (dashed, animated) and is fully drawn by the hold",
      results["geo2"][2]["arcs"] and all(v > 0.9 for v in results["geo2"][2]["arcs"])
      and all(abs(v) < 1e-3 for v in results["geo2"][3]["arcs"]), f"{results['geo2'][2]['arcs']} -> {results['geo2'][3]['arcs']}")
check("...World Bank figures show with their source", "World Bank" in results["geo3"][3]["footer"], results["geo3"][3]["footer"])
gauge = results["gauge"]
check("Odds gauge: a tug-of-war from 50% that settles on the odds (68% of the bar)",
      abs(gauge[2]["yes"] / gauge[2]["bar"] - 0.5) < 0.02 and abs(gauge[3]["yes"] / gauge[3]["bar"] - 0.68) < 0.02,
      f"{gauge[2]['yes'] / gauge[2]['bar']:.2f} -> {gauge[3]['yes'] / gauge[3]['bar']:.2f}")
check("...with its source (Polymarket) and volume", "Polymarket" in gauge[3]["footer"] and "$9.5M" in gauge[3]["footer"],
      gauge[3]["footer"])
check("Value chain: ASML -> TSMC -> NVIDIA, each node wearing its logo, links drawing themselves",
      results["chain"][3]["logos"] == 3 and results["chain"][3]["links"] and all(abs(v) < 1e-3 for v in results["chain"][3]["links"]),
      f"{results['chain'][3]['logos']} logos, links {results['chain'][3]['links']}")
check("2x2 matrix: axes and grid draw themselves (stroke-dashoffset 1 -> 0)",
      results["matrix"][2]["axes"] and all(v > 0.9 for v in results["matrix"][2]["axes"])
      and all(abs(v) < 1e-3 for v in results["matrix"][3]["axes"]), f"{results['matrix'][2]['axes']} -> {results['matrix'][3]['axes']}")
check("Flywheel: its arrows draw themselves round the loop",
      len(results["wheel"][3]["wheel"]) == 3 and all(abs(v) < 1e-3 for v in results["wheel"][3]["wheel"])
      and all(v > 0.9 for v in results["wheel"][2]["wheel"]), str(results["wheel"][3]["wheel"]))
check("Tear-sheet: the highlighter sweeps across the phrase (0% -> 100%)",
      results["headline"][2]["sweep"].startswith("0%") and results["headline"][3]["sweep"].startswith("100%"),
      f"{results['headline'][2]['sweep']} -> {results['headline'][3]['sweep']}")
timing = {}
for name, card in SAMPLES.items():
    sample = Card(card.template, S, S + 4, headline=card.headline, rows=card.rows, context=dict(card.context),
                  source=card.source, footnote=card.footnote, eyebrow=card.eyebrow)
    K.pace(sample, 1000.0, [])
    settle = C.settle_time(sample)
    last = max(t for t in C.spoken_times(sample)) - sample.start
    timing[name] = (round(settle, 2), round(sample.end - sample.start - settle - C.EXIT, 2), round(sample.end - sample.start, 2),
                    round(max(C.ENTRANCE_END, last + 0.9), 2))
check("Each lands by 1.2 s (Round 39: or just after its last item's word), then holds still 3.8 s or more, on screen 5-6.5 s",
      all(s <= b + 1e-6 and h >= K.READ_HOLD - 1e-6 and 5.0 <= d <= 6.5 for s, h, d, b in timing.values()), str(timing))

# ---- the checks: every name, number and quote is what was said (or fetched, with its source)
said = words_from("so here is the thing that matters a lot today folks ok China makes most of the chips. Then they ship "
                  "them to the United States and Taiwan. There's a 70% chance the Fed cuts in December. ASML makes the "
                  "machines, TSMC builds the chips and NVIDIA designs them. Never fight the Fed because the Fed always gets "
                  "the last word. Lower prices bring more customers which bring more sellers. "
                  + "and that is really all there is to it my friends " * 3)
CS, CE = said[0].start, said[-1].end


def at(word):
    return next(w.start for w in said if w.text.strip(".,").lower() == word.lower())


def judged(card):
    return K.check_card(card, said, CS, CE, 3.0)


geo = Card("geo_map_card", at("China") - 0.1, at("China") + 6, rows=[CardRow("China"), CardRow("United States", highlight=True)],
           context={"arcs": [[1, 2]]})
result = judged(geo)
check("A map of countries the speaker names passes; each lights up as it's named",
      result.ok and geo.rows[0].reveal_at < geo.rows[1].reveal_at, str(result.errors))
check("...a country nobody names is refused", not judged(Card("geo_map_card", geo.start, geo.end, rows=[CardRow("France")])).ok)
check("...a figure nobody said is refused", not judged(Card("geo_map_card", geo.start, geo.end,
                                                            rows=[CardRow("China", "$18.7T")])).ok)
fed = Card("geo_map_card", geo.start, geo.end, source="World Bank (2024)", context={"data": "world_bank"},
           rows=[CardRow("China", "$18.7T", detail="GDP"), CardRow("United States", "$29.2T", detail="GDP")])
check("...unless the code fetched it from the World Bank and shows the source", judged(fed).ok)
check("...a 'fetched' figure with any other source is refused (the model can't claim a feed)",
      not judged(Card("geo_map_card", geo.start, geo.end, source="Bloomberg", context={"data": "world_bank"},
                      rows=[CardRow("China", "$18.7T")])).ok)
odds = Card("prediction_gauge_card", at("70%") - 0.2, at("70%") + 5.5, headline="A December cut?",
            rows=[CardRow("Cut", "70%", True), CardRow("No cut", "")])
check("Odds the speaker states pass (the other side is the rest)", judged(odds).ok, str(judged(odds).errors))
check("...odds nobody said are refused", not judged(Card("prediction_gauge_card", odds.start, odds.end,
                                                         rows=[CardRow("Cut", "65%")])).ok)
check("...and two sides must add to 100%", not judged(Card("prediction_gauge_card", odds.start, odds.end,
                                                           rows=[CardRow("Cut", "70%"), CardRow("No", "40%")])).ok)
chain = Card("flow_sankey_card", at("ASML") - 0.1, at("ASML") + 6,
             rows=[CardRow("ASML", detail="Machines"), CardRow("TSMC"), CardRow("NVIDIA", highlight=True)])
result = judged(chain)
check("A value chain in the speaker's words passes, each step on its word, in order",
      result.ok and chain.rows[0].reveal_at < chain.rows[1].reveal_at < chain.rows[2].reveal_at, str(result.errors))
quote = Card("headline_card", at("Never") - 0.1, at("Never") + 6,
             rows=[CardRow("Never fight the Fed because the Fed always gets the last word.", "always gets the last word")])
check("A tear-sheet of the speaker's exact line passes", judged(quote).ok, str(judged(quote).errors))
check("...a highlight that isn't in the quote is refused", not judged(Card("headline_card", quote.start, quote.end, rows=[
    CardRow("Never fight the Fed because the Fed always gets the last word.", "fight the ECB")])).ok)
check("...a line the speaker didn't say is refused", not judged(Card("headline_card", quote.start, quote.end, rows=[
    CardRow("Always buy the dip when markets fall.", "buy the dip")])).ok)
wheel = Card("framework_matrix_card", at("Lower") - 0.1, at("Lower") + 6, context={"layout": "flywheel"},
             rows=[CardRow("Lower prices"), CardRow("More customers"), CardRow("More sellers")])
check("A flywheel of the speaker's steps passes", judged(wheel).ok, str(judged(wheel).errors))
check("...a 2x2 needs exactly four boxes", not judged(Card("framework_matrix_card", wheel.start, wheel.end,
                                                           context={"layout": "matrix"}, rows=wheel.rows)).ok)

# ---- data: World Bank, Polymarket, Wikipedia (all faked), filled by the code with sources
calls = []


def fake_get(url):
    calls.append(url)
    if "worldbank" in url:
        value = {"CHN": 18.74e12, "USA": 29.18e12}[re.search(r"country/(\w+)/", url).group(1)]
        return [{"page": 1}, [{"value": value, "date": "2024"}]]
    if "polymarket" in url:
        return {"events": [{"markets": [{"question": "Fed cut in December?", "outcomes": '["Yes", "No"]',
                                         "outcomePrices": '["0.62", "0.38"]', "volume": "9500000", "slug": "fed"}]}]}
    if "search/title" in url:
        return {"pages": [{"key": "Glass–Steagall_legislation"}]}
    return {"title": "Glass–Steagall legislation", "description": "1933 US banking law",
            "extract": "The Glass–Steagall legislation separated commercial and investment banking."}


N._get_json = fake_get
os.environ["NICHE_DATA"] = "on"
mapped = Card("geo_map_card", geo.start, geo.end, rows=[CardRow("China"), CardRow("United States")],
              context=ND.niche_context("geo_map_card", {"stat": "gdp", "arcs": [[1, 2]], "data": "world_bank"}))
check("The model can ask for a World Bank figure but never set one ('data' is dropped)",
      mapped.context == {"arcs": [[1, 2]], "stat": "gdp"}, str(mapped.context))
ND.enrich(mapped)
check("World Bank GDP fills each country, with its year as the source",
      [r.value for r in mapped.rows] == ["$18.7T", "$29.2T"] and mapped.source == "World Bank (2024)"
      and mapped.context.get("data") == "world_bank" and judged(mapped).ok, f"{[r.value for r in mapped.rows]} {mapped.source}")
asked = Card("prediction_gauge_card", odds.start, odds.end, rows=[CardRow("Cut"), CardRow("Hold")],
             context=ND.niche_context("prediction_gauge_card", {"market": "fed december cut"}))
ND.enrich(asked)
check("Polymarket's live odds fill a gauge the speaker put no number on, with source and volume",
      [r.value for r in asked.rows] == ["62%", "38%"] and asked.source == "Polymarket" and "$9.5M" in asked.footnote
      and judged(asked).ok, f"{[(r.label, r.value) for r in asked.rows]} {asked.footnote} {judged(asked).errors}")
sheet = Card("headline_card", quote.start, quote.end, rows=list(quote.rows),
             context=ND.niche_context("headline_card", {"topic": "Glass-Steagall"}))
ND.enrich(sheet)
check("A Wikipedia line goes under a tear-sheet, with its source", sheet.footnote == "1933 US banking law"
      and sheet.source == "Wikipedia" and judged(sheet).ok, f"{sheet.footnote!r} {sheet.source}")
cached = len(calls)
ND.enrich(Card("geo_map_card", geo.start, geo.end, rows=[CardRow("China")], context={"stat": "gdp"}))
check("...answers are cached (no second call for the same figure)", len(calls) == cached)
os.environ["NICHE_DATA"] = "off"
offline = Card("geo_map_card", geo.start, geo.end, rows=[CardRow("China")], context={"stat": "gdp"})
ND.enrich(offline)
check("NICHE_DATA=off: nothing fetched, the card shows only what was said", offline.rows[0].value == "" and not offline.source)
os.environ.pop("NICHE_DATA")

spans = sentences(said)
found = ND.moments(said, spans, CS, CE, 3.0)
check("From the transcript alone: stated odds become a gauge, countries around chips become a map",
      {c.template for _s, c, _r in found} >= {"prediction_gauge_card"}
      and any(c.template == "geo_map_card" for _s, c, _r in ND.moments(said[:29], sentences(said[:29]), CS, CE, 3.0)),
      str([(c.template, r) for _s, c, r in found]))
from core import graphics_director as G  # noqa: E402

direction = G.direct(said, CS, CE, info=G.VideoInfo("t"), hook_seconds=3.0, ask=lambda p, s: None)
check("With no AI key the director still finds a niche Bento card in the transcript (it passes every check)",
      any(c.template in ND.NICHE_TEMPLATES for c in direction.edit.cards), "\n".join(direction.notes))
check("The director's prompt teaches the five new cards", all(t in G.SYSTEM_PROMPT for t in NEW))
app_source = (ROOT / "app.py").read_text()
check("The app lets you add each one by hand (guide, rows, map/odds/framework/Wikipedia options)",
      all(f'"{t}"' in app_source.split("CARD_GUIDE")[1].split("NAME_CARD_SECONDS")[0] for t in NEW)
      and "_niche_context(template, key)" in app_source)

# ======================= 2. replay heatmap =======================
heatmap = [{"start_time": float(s), "end_time": float(s + 10), "value": v} for s, v in
           [(0, 1.0), (10, 0.4), (90, 0.3), (100, 0.7), (110, 0.92), (120, 0.5), (300, 0.66), (500, 0.2)]]
spikes = H.spikes(heatmap)
check("Replay spikes: buckets at 0.65+ merged (100-120 s, top 0.92), the video's opening skipped",
      spikes == [(100.0, 120.0, 0.92), (300.0, 310.0, 0.66)], str(spikes))
check("A clip whose hook lands on a spike scores the spike's value",
      H.peak_retention_score(104.0, 140.0, spikes) == 0.92 and H.hook_on_spike(104.0, 140.0, spikes))
check("...a spike later in the clip counts half (not a High Replay hook)",
      H.peak_retention_score(85.0, 125.0, spikes) == 0.46 and not H.hook_on_spike(85.0, 125.0, spikes))
check("...no spike, no score", H.peak_retention_score(400.0, 440.0, spikes) == 0.0)
check("The boost lifts a spike hook's virality (capped at 100)", H.boost(80, 104.0, 140.0, spikes) == 91
      and H.boost(80, 400.0, 440.0, spikes) == 80 and H.boost(95, 104.0, 140.0, spikes) == 100)

from core.hook_selector import ClipResult  # noqa: E402

plain = ClipResult(start_time=400.0, end_time=440.0, viral_title="a", caption="", hashtags=[], virality_score=85)
hot = ClipResult(start_time=104.0, end_time=140.0, viral_title="b", caption="", hashtags=[], virality_score=80)
ranked = H.score_clips([plain, hot], spikes)
check("A spike hook outranks a slightly better-scored clip without one, and carries peak_retention_score",
      ranked[0].viral_title == "b" and ranked[0].peak_retention_score == 0.92 and H.high_replay(ranked[0])
      and not H.high_replay(ranked[1]), str([(c.viral_title, c.virality_score, c.peak_retention_score) for c in ranked]))
check("peak_retention_score is saved with the clip, and never asked of the AI (not in the response schema)",
      "peak_retention_score" in ranked[0].model_dump() and "peak_retention_score" not in json.dumps(ClipResult.model_json_schema()))

import core.agent_brain as AB  # noqa: E402
from core import pipeline as P  # noqa: E402

os.environ["OPENAI_API_KEY"] = "test"
os.environ.pop("GEMINI_API_KEY", None)
AB.load_dotenv = lambda *a, **k: None
transcript, t = [], 0.0
for i in range(300):
    for j in range(10):
        transcript.append(Word(f"w{i}{'.' if j == 9 else ''}", t, t + 0.3)); t += 0.32
    t += 0.5
picks = [{"start_time": s, "end_time": s + 40, "hook_title": f"T{s}", "reason": "r", "point": "a point", "usual_take": ""}
         for s in (100, 400, 700)]
answer = {"clips": picks, "short_topic": "x", "format": "podcast", "host_first_name": "dana"}
AB._request_openai = lambda key, prompt, system_prompt=AB._SYSTEM_PROMPT: json.dumps(answer)
replay = [{"start_time": 690.0, "end_time": 712.0, "value": 0.9}]
clips, _email, _subject = P.pick_clips_with_agent(transcript, t, 3, "Dana", "Video", heatmap=replay)
by_start = {round(c.start_time / 100): c for c in clips}
check("The app's and worker's picker (pick_clips_with_agent) boosts and badges the clip on the replay spike",
      by_start.get(7) is not None and by_start[7].peak_retention_score >= H.HOT_VALUE
      and all(c.peak_retention_score == 0 for k, c in by_start.items() if k != 7), str([(c.start_time, c.virality_score,
                                                                                          c.peak_retention_score) for c in clips]))
worker_source = (ROOT / "autonomous_worker.py").read_text()
check("Parity: the worker passes the video's heatmap exactly like the app, and the app shows the badge",
      'heatmap=list(video_info.get("heatmap") or []) or None' in worker_source
      and 'heatmap=st.session_state.get("agent_heatmap") or None' in app_source
      and "heatmap_scorer.BADGE" in app_source and H.BADGE == "🔥 High Replay Moment")

# ======================= 3. camera physics =======================
frame_w, crop_w = 2560, 810


def evaluate(expression, t):
    """FFmpeg's expression language (the parts the crop uses), evaluated in Python."""
    code = (expression.replace("between(", "_bt(").replace("if(", "_if(").replace("lt(", "_lt(").replace("exp(", "_exp("))
    return eval(code, {"_bt": lambda v, a, b: a <= v <= b, "_if": lambda c, a, b: a if c else b,
                       "_lt": lambda a, b: a < b, "_exp": math.exp, "t": t})


plan = F.CropPlan(frame_w, 1440, crop_w, 1440, 0, (F.Shot(100.0, 104.0, 600, True, False), F.Shot(104.0, 110.0, 1500, True, False)),
                  cuts=(104.0,))
frames = [n / 30 for n in range(0, 300)]
xs = [plan.x_at(t, 100.0, 110.0) for t in frames]
jumps = [(frames[k], xs[k - 1], xs[k]) for k in range(1, len(xs)) if xs[k] != xs[k - 1]]
check("Hard cut: the crop snaps to the new angle on the cut frame itself, with no pan across angles",
      jumps == [(4.0, 600.0, 1500.0)], str(jumps))
check("...the render (FFmpeg expression) and the preview (x_at) agree on every frame",
      all(abs(evaluate(plan.x_expression(100.0, 110.0), t) - x) < 1e-6 for t, x in zip(frames, xs)))
late = F.CropPlan(frame_w, 1440, crop_w, 1440, 0, (F.Shot(100.0, 104.0, 600, True, False, None, ((103.5, 900),)),
                                                    F.Shot(104.0, 110.0, 1500, True, False)), cuts=(104.0,))
late_xs = [late.x_at(t, 100.0, 110.0) for t in frames]
check("...a re-frame that would still be moving at the cut is never started (no pan into a cut)",
      sorted(set(late_xs)) == [600.0, 1500.0], str(sorted(set(late_xs))[:5]))
gray = [np.full((36, 64, 3), 20, np.uint8)] * 10 + [np.full((36, 64, 3), 230, np.uint8)] * 10
times = [n / 30 for n in range(20)]
cut = F._cut_times(np.array(gray), times)
check("A cut is placed exactly between the last frame of the old angle and the first of the new",
      len(cut) == 1 and times[9] < cut[0] < times[10] and abs(cut[0] - (times[9] + times[10]) / 2) < 1e-9, str(cut))

center = 600 + crop_w / 2
band = CC.DEAD_BAND * frame_w
check("Dead band: 8% of the frame width (hysteresis back inside 5%)", CC.DEAD_BAND == 0.08 and CC.RELEASE_BAND == 0.05
      and F.DEAD_ZONE == CC.DEAD_BAND)
rng = np.random.default_rng(36)
jitter = [(k / 10, center + rng.uniform(-0.07, 0.07) * frame_w) for k in range(100)]
check("Stable: 10 s of head movement within ±7% of the frame never moves the crop", F._reframes(jitter, 600, crop_w, frame_w, 100.0) == ())
edge = [(k / 10, center + (0.079 if k % 2 else 0.06) * frame_w) for k in range(100)]
check("...nor a face resting just inside the band's edge", F._reframes(edge, 600, crop_w, frame_w, 100.0) == ())
moved = [(k / 10, center + (0.15 * frame_w if k >= 20 else 0)) for k in range(80)]
moves = F._reframes(moved, 600, crop_w, frame_w, 100.0)
check("A real move (out of the band and staying out 2 s) re-frames once, from when it started",
      len(moves) == 1 and abs(moves[0][0] - 102.0) < 1e-6 and abs(moves[0][1] + crop_w / 2 - (center + 0.15 * frame_w)) <= 2,
      str(moves))
hover = [(k / 10, center + (0.10 if k % 2 else 0.06) * frame_w) for k in range(40)]
check("Hysteresis: hovering across the band's edge (10% / 6%) still counts as moved (no flicker in the decision)",
      len(F._reframes(hover, 600, crop_w, frame_w, 100.0)) == 1)
back = [(k / 10, center + (0.12 * frame_w if (k // 5) % 2 == 0 else 0.02 * frame_w)) for k in range(60)]
check("...but coming right back to centre (inside 5%) resets the timer: a lean is never a move",
      F._reframes(back, 600, crop_w, frame_w, 100.0) == ())
curve = [CC.spring(u / 100) for u in range(101)]
check("Spring: critically damped (starts at rest, never overshoots or bounces, lands exactly)",
      curve[0] == 0 and abs(curve[-1] - 1) < 1e-12 and all(b >= a for a, b in zip(curve, curve[1:]))
      and max(curve) <= 1 + 1e-12 and curve[1] < 0.003, f"{curve[1]:.4f} {curve[50]:.3f}")
pan = F.CropPlan(frame_w, 1440, crop_w, 1440, 0, (F.Shot(100.0, 110.0, 600, True, False, None, ((103.0, 930),)),))
expression = pan.x_expression(100.0, 110.0)
check("...the pan is that spring in FFmpeg (exp), the same as the preview on every frame",
      "exp(" in expression and all(abs(evaluate(expression, t) - pan.x_at(t, 100.0, 110.0)) < 0.01 for t in frames))
probe = subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={frame_w}x1440:r=30:d=5", "-vf",
                        f"crop={crop_w}:1440:'{expression}':0", "-f", "null", "-"], capture_output=True, text=True)
check("...and FFmpeg accepts the expression", probe.returncode == 0, probe.stderr[-200:])

# ======================= 4. semantic captions, never on a Bento card =======================
tokens = "NVIDIA made $10B last year, 40% more than China and the United States. $NVDA never looked back.".split()
kinds = dict(zip(tokens, CAP.keyword_kinds(tokens)))
check("Keywords: figures, tickers, companies and countries (every word of 'United States'), power words",
      kinds["$10B"] == kinds["40%"] == CAP.FIGURE and kinds["$NVDA"] == CAP.TICKER and kinds["NVIDIA"] == kinds["China"]
      == kinds["United"] == kinds["States."] == CAP.ENTITY and kinds["never"] == CAP.POWER and kinds["made"] is None
      and kinds["last"] is None, str(kinds))
check("...a spelled figure keeps its scale ('ten billion')", CAP.keyword_kinds(["ten", "billion", "dollars"])[:2] == [CAP.FIGURE] * 2)
from core.subtitle_engine import create_ass_subtitles  # noqa: E402

timed = [Word(x, k * 0.4, k * 0.4 + 0.35) for k, x in enumerate(tokens)]
# Round 45 made sentence-case executive captions the default (CAPTION_STYLE is cleared above);
# these checks are about the bold style's semantic colours, so they ask for it by name.
ass = create_ass_subtitles(timed, tmp / "k.ass", clip_duration=12, accent="#FF5A1F", style="bold").read_text()
events = [line for line in ass.splitlines() if line.startswith("Dialogue")]
accent, emerald, gold = CAP.ass_colour("#FF5A1F"), CAP.ass_colour(CAP.EMERALD), CAP.ass_colour(CAP.GOLD)
check("Colours: figures emerald, tickers and power words gold, names in the brand accent, for the whole line",
      f"\\c{emerald}}}$10B" in ass and f"\\c{gold}}}$NVDA" in ass and f"\\c{gold}}}NEVER" in ass
      and f"\\c{accent}}}NVIDIA" in ass and f"\\c{accent}}}UNITED" in ass, events[1][-200:])
spoken = next(e for e in events if re.search(r"\\fscx108\\fscy108[^}]*\}\$10B", e))
check("The spoken keyword pops to 1.08x and settles back within the word", "\\t(0," in spoken and "\\fscx100\\fscy100" in spoken)
check("Other spoken words: yellow, height-only pulse (the line never shifts sideways)",
      any("\\c&H00FFFF&\\fscy108" in e and "MADE" in e for e in events))
os.environ["KEYWORD_EMPHASIS"] = "off"
quiet = create_ass_subtitles(timed, tmp / "q.ass", clip_duration=12, accent="#FF5A1F", style="bold").read_text()
os.environ.pop("KEYWORD_EMPHASIS")
check("KEYWORD_EMPHASIS=off: only figures stay emerald (Round 33)", accent not in quiet and f"\\c{emerald}}}$10B" in quiet)

from core import dynamic_layout as DL  # noqa: E402

check("Bold captions sit inside the Bento band, so they're never drawn while a card is up; the split view's sit on the seam above it",
      CAP.overlaps(CAP.caption_band("bold")) and not CAP.overlaps(CAP.caption_band("split")), str(CAP.caption_band("split")))
clip_words = words_from("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
                        "seventeen eighteen nineteen twenty " * 3, 100.0, 0.3)
edit = ClipEdit(100.0, 118.0, cards=[Card("metric_bars", 104.0, 109.5, rows=[CardRow("A", "$1M"), CardRow("B", "$2M")])])
kept = DL.words_outside(clip_words, edit)
check("No bold caption word while the card is up (none even partly under it)",
      not any(w.start < 109.5 and w.end > 104.0 for w in kept) and kept == CAP.clear_of_bento(clip_words, edit.cards))
calm = DL.calm_captions(clip_words, edit, tmp / "calm.ass").read_text()
style = next(line for line in calm.splitlines() if line.startswith("Style: Split"))
size = int(style.split(",")[2])
landed = DL.landed(edit.cards[0], edit)
lines = [line for line in calm.splitlines() if line.startswith("Dialogue")]
times = [tuple(float(v) for v in re.findall(r"(\d+):(\d\d):(\d\d\.\d\d)", line)[0][1:]) for line in lines]
check("Captions under a chart (Round 39): one bold line on the seam, above the card (its bottom over y 1056), "
      "only once the card has landed",
      size >= 38 and DL.SEAM_CAPTION_BOTTOM < C.CARD_TOP and lines and all(f"\\pos(540,{DL.SEAM_CAPTION_BOTTOM})" in l for l in lines)
      and all(m * 60 + s >= landed[0] - 100.0 - 0.011 for m, s in times),
      f"size {size}, bottom {DL.SEAM_CAPTION_BOTTOM}, landed {landed[0] - 100:.2f}, first {times[:1]}")

# ======================= 5. loudness mastering and dead air =======================
import core.video_renderer as V  # noqa: E402

check("Mastering chain: 80 Hz high-pass, denoise, +2 dB at 3 kHz, then loudnorm -14 LUFS / -1.5 dBTP / LRA 11",
      "highpass=f=80" in A.MASTERING_CHAIN and "equalizer=f=3000:t=q:w=1:g=2" in A.MASTERING_CHAIN
      and A.loudnorm().startswith("loudnorm=I=-14:TP=-1.5:LRA=11") and V.TRUE_PEAK_DB == -1.5 and V.DEFAULT_TARGET_LUFS == -14)
voice = tmp / "voice.wav"
subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=220:r=48000:d=8", "-f", "lavfi", "-i",
                "anoisesrc=r=48000:a=0.02:d=8", "-filter_complex",
                "[0:a]volume=3,tremolo=f=3:d=0.7[s];[s][1:a]amix=inputs=2:normalize=0,lowpass=f=6000[out]",
                "-map", "[out]", str(voice)], check=True)
chain = V._audio_filter(FFMPEG, voice, 0.0, 8.0)
check("The two-pass filtergraph: high-pass < denoise < presence < measured loudnorm to -14 / -1.8 (AAC headroom)",
      chain.index("highpass=f=80") < chain.index("afftdn") < chain.index("equalizer=f=3000") < chain.index("loudnorm=I=-14")
      and "TP=-1.8" in chain and "measured_I=" in chain and "linear=true" in chain, chain[:200])
mastered = tmp / "mastered.m4a"
subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(voice), "-af", chain, "-c:a", "aac", "-b:a", "192k", str(mastered)], check=True)
meter = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", str(mastered), "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
loud = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", meter)[-1])
peak = float(re.findall(r"Peak:\s+(-?[\d.]+) dBFS", meter)[-1])
check("Delivered: -14 LUFS and at or under -1.5 dBTP", abs(loud + 14) <= 1.0 and peak <= -1.5 + 0.05, f"{loud} LUFS, {peak} dBTP")
dl_source = (ROOT / "core/dynamic_layout.py").read_text()
check("After the micro-SFX mix, a 1 ms look-ahead limiter holds the ceiling",
      "audio_polish.SFX_LIMITER" in dl_source and "attack=1" in A.SFX_LIMITER and abs(float(re.search(r"limit=([\d.]+)",
                                                                                                         A.SFX_LIMITER).group(1)) - 10 ** (-1.8 / 20)) < 1e-3)

talk = [Word("a", 10.0, 10.3), Word("b", 10.35, 10.6), Word("c", 11.6, 11.9), Word("d", 11.95, 12.2), Word("e", 12.6, 12.9)]
gaps = A.dead_air(talk, 10.0, 13.0)
check("Dead air: pauses over 650 ms found from the word timings (1.0 s found, 0.4 s not)", gaps == [(0.6, 1.6)], str(gaps))
ranges = A.keep_ranges(gaps, 3.0)
check("...a trim would keep 0.3 s of the pause and never touch a word's tail or attack",
      len(ranges) == 2 and ranges[0][1] >= 0.6 + A.TAIL_GUARD and ranges[1][0] <= 1.6 - A.LEAD_GUARD
      and abs((ranges[1][0] - ranges[0][1]) - (1.0 - A.KEEP_PAUSE)) < 1e-6, str(ranges))
gap_wav = tmp / "gap.wav"
subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:r=48000:d=1", "-f", "lavfi", "-i",
                "anullsrc=r=48000:cl=mono:d=1", "-f", "lavfi", "-i", "sine=f=300:r=48000:d=1", "-filter_complex",
                "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]", "-map", "[out]", str(gap_wav)], check=True)
heard = A.silences(FFMPEG, gap_wav, 0.0, 3.0)
check("...and FFmpeg's silencedetect confirms it in the sound", len(heard) == 1 and abs(heard[0][0] - 1.0) < 0.05
      and abs(heard[0][1] - 2.0) < 0.05, str(heard))
flagged = A.flag_dead_air([ClipResult(start_time=10.0, end_time=13.0, viral_title="", caption="", hashtags=[],
                                      virality_score=50)], talk)
check("Clips carry their dead air as a flag (never cut automatically), shown in the app",
      flagged[0].dead_air == [[0.6, 1.6]] and "_dead_air_note(clip.dead_air)" in app_source
      and "dead_air" not in json.dumps(ClipResult.model_json_schema()))
run_all = (ROOT / "tests/run_all.py").read_text()
check("tests/run_all.py gives each test file its own temp folder and deletes it afterwards",
      '"TMPDIR": str(scratch)' in run_all and "shutil.rmtree(scratch" in run_all)
print("\nAll checks passed." if not fails else f"\n{fails} check(s) failed.")
sys.exit(1 if fails else 0)
