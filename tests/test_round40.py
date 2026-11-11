"""Round 40 checks: the climax teaser hook (hinge-word detection on Whisper timings, a
2.5-4.0 s window from 15 s in, cut before the answer with a 0.06 s fade; the stitch with
a 1.05x punch-in, the title banner, a two-frame #FDFBF7 flash at 85 % / 40 % and a
45-80 Hz sub-bass hit under the limiter; the clip after it exactly N frames later),
the editorial palettes (nyt_archival, bloomberg_obsidian, economist_slate,
studio_adaptive; WCAG AAA; picked from the topic and the studio's lighting; palette_mode
in app.py), and Round 28's two recap checks (a graphic checked twice gives the same
answer).

Run from the project root:  .venv/bin/python tests/test_round40.py .
"""
import colorsys
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import numpy as np

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp(prefix="round40-"))
os.environ.update({"NAV_NO_CLIP_PAGES": "1", "NAV_LOGO_DIR": str(tmp / "logos"), "NAV_SFX_DIR": str(tmp / "sfx")})
for name in ("SFX", "ENABLE_CLIMAX_HOOK", "PALETTE_MODE", "HOOK_PULL_IN"):
    os.environ.pop(name, None)
os.environ.update({"SIGNATURE_EDIT": "0", "CAMERA_FLOW": "0", "OPENING_HOOK": "0"})  # later rounds' editing off: this suite checks its own round's
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


import core.audio_polish as A  # noqa: E402
import core.card_checks as K  # noqa: E402
import core.cards as C  # noqa: E402
import core.climax_hook as H  # noqa: E402
import core.palettes as P  # noqa: E402
from core.edl import MODES, TEMPLATES, BrandKit, Card, CardRow, ClipEdit  # noqa: E402
from core.transcriber import Word  # noqa: E402


def words_from(text, start=0.0, step=0.38):
    out, t = [], start
    for token in text.split():
        out.append(Word(token, round(t, 3), round(t + step - 0.03, 3)))
        t += step
    return out


def first(words, token, after=0.0):
    return next(k for k, w in enumerate(words) if w.text.strip(".,:").lower() == token and w.start >= after)


# ============================================ 1. hinge-word detection ============================================
STORY = ("So we spent the first two years building the wrong product for the wrong people, and we burned most of the money. "
         "Our investors were patient, the team was great, the market was there, and still nothing worked for us. "
         "Then one Tuesday a customer called me. And what nobody tells you about product market fit is that it feels "
         "like being pulled, not pushing, and every metric moves at once. That is when we knew we had it.")
CLIP_START = 2.0
story = words_from(STORY, CLIP_START)
clip_end = round(story[-1].end + 0.4, 3)
teaser = H.find_climax(story, CLIP_START, clip_end, rate=30.0)
check("A 28 s+ clip with a high-stakes line in its back half gets a teaser", teaser is not None and clip_end - CLIP_START >= 28,
      f"{teaser} / {clip_end - CLIP_START:.1f} s")
hinge_k = first(story, "that", after=CLIP_START + 15)
reveal = story[hinge_k + 1]
what = story[first(story, "what")]
check("...cut on the hinge word right before the answer (\"...fit is that\" | \"it feels like...\")",
      teaser and teaser.hinge.endswith("is that") and story[hinge_k].end <= teaser.cut + 1e-6 < reveal.start
      and "it" not in teaser.line.split()[-1:], f"cut {teaser and teaser.cut} / answer at {reveal.start}")
check("...starting at the beginning of that climax clause (\"What nobody tells you...\")",
      teaser and teaser.line.lower().startswith("what nobody tells you") and teaser.start <= what.start + 1e-6
      and teaser.start > story[first(story, "what") - 1].start, teaser and teaser.line)
check("...from 15 s into the clip or later", teaser and teaser.start - CLIP_START >= H.MIN_TEASER_AT - 1e-9,
      f"{teaser and teaser.start - CLIP_START:.2f} s in")
check("...2.5-4.0 s long, a whole number of frames, the cut frame-exact on the hinge",
      teaser and H.TEASER_MIN - 1e-9 <= teaser.offset <= H.TEASER_MAX + 1e-9 and teaser.frames == round(teaser.offset * 30)
      and abs(teaser.start + teaser.frames / 30 - teaser.cut) < 1e-3, f"{teaser and teaser.frames} frames")
check("Configurable: the fade on the cut word is 0.06 s, the punch-in 1.05x, the flash #FDFBF7 at 85 % then 40 %",
      H.CUT_FADE == 0.06 and H.PUNCH_IN == 1.05 and H.FLASH_COLOR == "#FDFBF7" and H.FLASH_OPACITY == (0.85, 0.40))
short = words_from(STORY, CLIP_START)[:60]
check("No teaser under 28 s", H.find_climax(short, CLIP_START, short[-1].end + 0.2) is None)
early = words_from("Here is what nobody tells you about hiring is that it is all about the first ten people. " + " ".join(
    ["and then we kept going with the plan for months"] * 6) + ".", CLIP_START)
check("No teaser from the first 15 s (the climax must come from the back half)",
      H.find_climax(early, CLIP_START, early[-1].end + 0.3) is None, str(H.find_climax(early, CLIP_START, early[-1].end + 0.3)))
flat = words_from(" ".join(["we talked about the weather and the trains and the food in the city that week"] * 7) + ".", CLIP_START)
check("No strong line: no teaser (the clip opens as before)", H.find_climax(flat, CLIP_START, flat[-1].end + 0.3) is None)
closed = words_from(STORY.replace("is that it feels like being pulled, not pushing, and every metric moves at once.",
                                  "is underrated. It feels great when you find it, believe me, really great."), CLIP_START)
check("A setup with no answer after it in its sentence is no cliffhanger: skipped",
      (lambda t: t is None or "underrated" not in t.line)(H.find_climax(closed, CLIP_START, closed[-1].end + 0.4)))
contrarian = words_from(" ".join(["we raised money and we hired people and we built the product for two years"] * 4)
                        + ". And the thing I learned the hard way, it's not about the money at all, it's the people "
                          "you hire in the first year that decide everything.", CLIP_START)
found = H.find_climax(contrarian, CLIP_START, contrarian[-1].end + 0.4)
check("A contrarian turn (\"it's not about the money, it's ...\") is a hinge too",
      found is not None and found.line.split()[-1].strip(",").lower() == "it's", found and found.line)
long_clip = words_from(STORY + " " + " ".join(["and we kept on building it every day after that"] * 8) + ".", CLIP_START)
check("Teaser + clip stay under YouTube's 59 s Shorts limit (a 57 s clip gets none)",
      H.find_climax(long_clip, CLIP_START, CLIP_START + 57.0) is None and H.MAX_TOTAL_SECONDS == 59.0)

# ============================================ 2. the downstream offset ============================================
off = 3.8
moved = H.shift_words([Word("a", 0.5, 0.9)], off)
check("Caption words move by +T_teaser", (moved[0].start, moved[0].end) == (4.3, 4.7), str(moved))
card = Card("kinetic_range", 110.0, 115.0, rows=[CardRow("x", "$5M", reveal_at=110.4, reveal_to=111.0)])
edit = ClipEdit(start=100.0, end=130.0, cards=[card])
fake = types.SimpleNamespace(offset=off)
shifted = H.shift_edit(edit, fake)
check("Card triggers (start, end, every reveal) land at clip time + T_teaser in the file",
      (shifted.cards[0].start, shifted.cards[0].end, shifted.cards[0].rows[0].reveal_at, shifted.cards[0].rows[0].reveal_to)
      == (13.8, 18.8, 14.2, 14.8) and shifted.end == 33.8 and edit.cards[0].start == 110.0, str(shifted.cards[0]))
expr = "if(lt(t,2.469),22,if(lt(t,3.203),1288,22))"
late = H.shift_expression(expr, off)


def ev(e, t):
    e = re.sub(r"(?<![\w.])t(?![\w(])", repr(t), e).replace("if(", "_if(").replace("lt(", "_lt(")
    return eval(e, {"_if": lambda c, a, b: a if c else b, "_lt": lambda a, b: a < b})


check("Crop keyframes (an FFmpeg x expression in t) move by +T_teaser",
      all(ev(late, t + off) == ev(expr, t) for t in (0.0, 2.4, 2.5, 3.0, 3.3, 9.0)), late)
check("...and spans", H.shift_spans([(1.0, 2.0)], off) == [(4.8, 5.8)])
check("to_output: the clip's first source second is the file's T_teaser", H.to_output(100.0, 100.0, fake) == off
      and H.to_output(100.0, 100.0, None) == 0.0)

# ============================================ 3. the sub-bass hit ============================================
hit = A.sub_hit()
spectrum = np.abs(np.fft.rfft(hit, 48000)) ** 2
band = spectrum[30:121].sum() / spectrum.sum()  # a 180 ms sound: its own length smears a pure tone ~10 Hz wide


def peak_hz(segment):
    spec = np.abs(np.fft.rfft(segment * np.hanning(len(segment)), 48000 * 4))
    return float(np.argmax(spec[:1600]) / 4)
check("Sub-bass hit: 180 ms, -9 dBFS peak", len(hit) == round(0.18 * 48000) and abs(20 * np.log10(np.abs(hit).max()) + 9.0) < 0.05)
check("...pure sub-bass: 95 %+ of its energy between 30 and 120 Hz", band > 0.95, f"{band:.3f}")
early_hz, late_hz = peak_hz(hit[:2880]), peak_hz(hit[-2880:])
check("...an 80 -> 45 Hz sine drop (its pitch in its first 60 ms vs its last)",
      55 <= early_hz <= 80 and 40 <= late_hz <= 52 and early_hz > late_hz + 8 and (A.SUB_HIT_FROM, A.SUB_HIT_TO) == (80.0, 45.0),
      f"{early_hz:.1f} Hz -> {late_hz:.1f} Hz")
envelope_end = np.abs(hit[-120:]).max() / np.abs(hit).max()
check("...a tight 180 ms exponential decay (under -40 dB at its end), no click in or out",
      envelope_end < 0.01 and abs(hit[0]) < 1e-3 and abs(hit[-1]) < 1e-6 and np.abs(hit[:24]).max() < 0.2 * np.abs(hit).max(),
      f"{20 * np.log10(envelope_end + 1e-12):.1f} dB")
check("...routed through the 1 ms limiter under the master's ceiling", "attack=1" in A.SFX_LIMITER
      and "SFX_LIMITER" in (ROOT / "core" / "climax_hook.py").read_text())

# ============================================ 4. Round 28: a graphic checked twice ============================================
hook_line = words_from("I came from being a C student in math to an A student overnight. Because it was more "
                       "analytical and less focused on numbers, and that changed everything for me.", 100.0, 0.3)
recap = Card("kinetic_range", 100.0, hook_line[-1].end, rows=[CardRow("overnight", "C → A")])
one = K.check_card(recap, hook_line, 100.0, 130.0, hook_seconds=4.0)
state = (recap.start, recap.end, recap.rows[0].reveal_at, recap.rows[0].reveal_to)
two = K.check_card(recap, hook_line, 100.0, 130.0, hook_seconds=4.0)
check("Round 28 fix: a change said under the title comes in right after it (104.2 s) on the first check and the second",
      one.ok and two.ok and abs(state[0] - 104.2) < 1e-6
      and (recap.start, recap.end, recap.rows[0].reveal_at, recap.rows[0].reveal_to) == state, str(state))
said = words_from("And honestly it was about five trillion dollars of market value. That moved the whole market that "
                  "week and nobody saw it coming at all, not one person on the street.", 100.0, 0.3)
late_card = Card("kinetic_range", said[12].start, said[-1].end, rows=[CardRow("", "~$5T")])
a = K.check_card(late_card, said, 95.0, 130.0)
snap = (late_card.start, late_card.rows[0].reveal_at)
b = K.check_card(late_card, said, 95.0, 130.0)
check("...any recap (a number said just before its card) is checked the same way twice: no drift",
      a.ok and b.ok and (late_card.start, late_card.rows[0].reveal_at) == snap, f"{snap} -> {(late_card.start, late_card.rows[0].reveal_at)}")

# ============================================ 5. the palettes ============================================
n, b, e = P.NYT_ARCHIVAL, P.BLOOMBERG_OBSIDIAN, P.ECONOMIST_SLATE
check("nyt_archival: parchment #FDFBF7, ink #111111 headers, slate #555555 body and dividers",
      (n.bg, n.header, n.body, n.divider) == ("#FDFBF7", "#111111", "#555555", "#555555"))
check("bloomberg_obsidian: carbon #0D0F12, #F3F4F6 headers, #9CA3AF body, amber #F59E0B (cyan #38BDF8 for tech)",
      (b.bg, b.header, b.body, b.accent, P.bloomberg(cyan=True).accent) == ("#0D0F12", "#F3F4F6", "#9CA3AF", "#F59E0B", "#38BDF8"))
check("economist_slate: charcoal-navy #12161F, #FFFFFF primary, #94A3B8 secondary, champagne gold #D4AF37",
      (e.bg, e.header, e.body, e.accent) == ("#12161F", "#FFFFFF", "#94A3B8", "#D4AF37"))
tones = ["#2E5E8C", "#D9C7A8", "#6B6B6B", "#7A1F2B", "#1E6B52", "#F2E9DA", "#101010", "#C0D8F0"]
every = [n, b, P.bloomberg(True), e] + [P.adaptive(t) for t in tones]
check("Every palette's headers and body pass WCAG AAA (7:1) and every accent at least 4.5:1, on its own card",
      all(p.checks()["header"] >= 7 and p.checks()["body"] >= 7 and p.checks()["accent"] >= 4.5
          and p.checks()["overlay_accent"] >= 4.5 for p in every),
      str([(p.name, p.bg, p.checks()) for p in every if min(p.checks().values()) < 4.5 or p.checks()["body"] < 7]))
blue = P.adaptive("#2E5E8C")
h_tone = colorsys.rgb_to_hls(*(c / 255 for c in P.rgb("#2E5E8C")))[0]
h_card = colorsys.rgb_to_hls(*(c / 255 for c in P.rgb(blue.bg)))[0]
h_acc = colorsys.rgb_to_hls(*(c / 255 for c in P.rgb(blue.accent)))[0]
check("studio_adaptive: the card wears the studio's own hue, the accent its complementary (a harmonious border)",
      abs(h_tone - h_card) < 0.03 and abs(((h_acc - h_tone) % 1.0) - 0.5) < 0.04, f"{blue.bg} {blue.accent}")
check("...a light set gets a tinted paper card, a dark one a deep card", P.adaptive("#D9C7A8").dark is False
      and P.adaptive("#2E5E8C").dark is True)

# the remap: by role, the accent through its sentinel, black shadows and masks alone
check("The house tokens land on the palette's roles (card, canvas, divider, body, header)",
      [n.color(c) for c in (C.CARD_BG, C.CANVAS, C.RULE, C.SOFT, C.INK)] == [n.bg, n.canvas, n.divider, n.body, n.header]
      and [b.color(c) for c in (C.CARD_BG, C.CANVAS, C.RULE, C.SOFT, C.INK)] == [b.bg, b.canvas, b.divider, b.body, b.header])
check("...the tones in between stay in between (a track sits between the card and a rule, light or dark)",
      all(P.lightness(min(p.bg, p.divider, key=P.lightness)) <= P.lightness(p.color(C.TRACK))
          <= P.lightness(max(p.bg, p.divider, key=P.lightness)) for p in (n, b, e)))
check("...quiet data on the light card is light (the ink accent stands out), gains/losses have no red on nyt",
      P.lightness(n.color(C.LIGHT)) > 55 and n.color(C.NEG) == "#555555" and n.color(C.POS) == "#111111")
snippet = "x{color:#F5F6F8;border:1px solid rgba(255,255,255,.12);box-shadow:0 8px 20px rgba(0,0,0,.5);fill:#FE01FD;g:rgba(254,1,253,0.09);m:#000}"
check("...the accent goes in through its sentinel; black shadows and masks are left alone",
      n.remap(snippet, "#111111") == "x{color:#111111;border:1px solid rgba(17,17,17,.12);box-shadow:0 8px 20px rgba(0,0,0,.5);"
                                     "fill:#111111;g:rgba(17,17,17,0.09);m:#000}", n.remap(snippet, "#111111"))

S = 100.0


def row(label, value="", hl=False, at=None, **kw):
    return CardRow(label, value, highlight=hl, reveal_at=None if at is None else S + at, **kw)


prices = tmp / "prices.csv"
lines, price = ["date,open,high,low,close,volume"], 100.0
for day in range(40):
    change = (1.6 if day % 5 else -2.4) + (day % 3) * 0.3
    lines.append(f"2026-09-{day % 28 + 1:02d},{price:.2f},{price + 2.5:.2f},{price - 2.0:.2f},{price + change:.2f},{1000 + day}")
    price += change
prices.write_text("\n".join(lines))
CARDS = {
    "metric_bars": Card("metric_bars", S, S + 4, headline="Where the money goes",
                        rows=[row("Payroll", "$4.2M", at=0.9), row("Hosting", "$1.1M", at=1.5), row("Ads", "$1.3M", True, 2.0)]),
    "ranked_bars": Card("ranked_bars", S, S + 4, headline="Top markets", rows=[row("US", "$12B", at=1.0), row("Japan", "$6B", True, 1.8)]),
    "diverging_bar": Card("diverging_bar", S, S + 4, headline="Who pays", rows=[row("Buyers", "62%", True, 1.2), row("Sellers", "38%", at=1.8)]),
    "share_split": Card("share_split", S, S + 4, headline="The portfolio",
                        rows=[row("Software", "40%", True, 0.9), row("Fintech", "35%", at=1.4), row("Other", "25%", at=1.9)]),
    "trend_line": Card("trend_line", S, S + 4, headline="ARR", rows=[row("2022", "$5M", at=0.9), row("2024", "$19M", True, 2.1)]),
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
    "candlestick_ema": Card("candlestick_ema", S, S + 3.5, headline="The S&P", source="Sample data", data_path=str(prices)),
    "market_line": Card("market_line", S, S + 5.4, headline="The S&P", source="Yahoo Finance", data_path=str(prices),
                        rows=[CardRow("Sep 1", "$100"), CardRow("Sep 28", "$162"), CardRow("Change", "+62%", True)]),
    "speaker_title": Card("speaker_title", S, S + 3.3, rows=[CardRow("Jonathan Bragdon", detail="Founder")]),
    "stat_pill": Card("stat_pill", S, S + 3, rows=[row("ARR", "40M", at=0.4)]),
    "kinetic_range": Card("kinetic_range", S, S + 3, rows=[row("a year in fees", "$850–$1,700", True, 0.5)]),
    "buckets": Card("buckets", S, S + 3, rows=[row("Expected", at=0.4, dashed=True), row("Actual", at=1.2)]),
    "comparison_card": Card("comparison_card", S, S + 4, headline="Marketing in growth", rows=[
        row("Venture firms", "Before", at=0.8, detail="didn't market themselves"), row("Then we did", "Now", True, 2.0, detail="and it worked")]),
    "step_sequence": Card("step_sequence", S, S + 4, rows=[row("Find the market", at=0.6), row("Hire the team", at=1.4),
                                                          row("Scale the brand", "", True, 2.2, detail="go tell your story")]),
    "geo_map_card": Card("geo_map_card", S, S + 5, headline="Chips leave China", rows=[row("China", at=0.5), row("United States", hl=True, at=0.8)]),
    "prediction_gauge_card": Card("prediction_gauge_card", S, S + 5, headline="A December cut?", rows=[row("Cut", "70%", True, 0.6)]),
    "flow_sankey_card": Card("flow_sankey_card", S, S + 5, headline="Who builds the chips",
                             rows=[row("ASML", at=0.5), row("TSMC", at=0.8), row("NVIDIA", hl=True, at=1.0)]),
    "framework_matrix_card": Card("framework_matrix_card", S, S + 5, headline="The flywheel", context={"layout": "flywheel"},
                                  rows=[row("Lower prices", at=0.4), row("More customers", at=0.6), row("More sellers", at=0.8)]),
    "headline_card": Card("headline_card", S, S + 5, eyebrow="The rule",
                          rows=[row("Never fight the Fed.", "fight the Fed", True, 0.5, detail="Marty Zweig")]),
}
check("A sample of all 23 card types", set(CARDS) == set(TEMPLATES), str(set(CARDS) ^ set(TEMPLATES)))


def kit(palette):
    return BrandKit(palette=palette.to_dict()) if palette else BrandKit()


classic_pages = {t: C.scene(c, BrandKit())[0] for t, c in CARDS.items()}
check("No palette (every older render and test): each page is exactly Round 39's (palette_mode alone changes nothing)",
      all(C.scene(c, BrandKit(palette_mode="nyt_archival"))[0] == classic_pages[t] for t, c in CARDS.items())
      and not any(P.SENTINEL in p for p in classic_pages.values()))
nyt_pages = {t: C.scene(c, kit(n))[0] for t, c in CARDS.items()}
splits = [t for t in CARDS if MODES[t] == "split"]
check("On a palette, all 19 Bento card types are redrawn in it (none keeps the dark card or the sentinel)",
      all(p != classic_pages[t] and P.SENTINEL not in p and C.CARD_BG not in p and n.bg in p
          for t, p in nyt_pages.items() if t in splits), str([t for t in splits if C.CARD_BG in nyt_pages[t]]))


def colours(text):
    found = [m.upper() for m in re.findall(r"#[0-9A-Fa-f]{6}(?![0-9A-Za-z])", text)]
    found += ["#{:02X}{:02X}{:02X}".format(*map(int, m)) for m in re.findall(r"rgba\((\d+),(\d+),(\d+),", text.replace(" ", ""))]
    return found


def reddish(color):
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in P.rgb(color)))
    return (h < 0.045 or h > 0.92) and s > 0.35 and 0.15 < l < 0.9


red = {t: sorted({c for c in colours(nyt_pages[t]) if reddish(c)}) for t in splits}
check("nyt_archival has zero red in any Bento card (losses in slate, told apart by ▲/▼)", not any(red.values()),
      str({t: r for t, r in red.items() if r}))
check("...and a card about one company stays monochrome there; Bloomberg lets it wear its colour, readable",
      n.card_accent("#E50914") == "#111111" and P.contrast(b.card_accent("#7A0010"), b.bg) >= 4.5)
glass = C.scene(CARDS["kinetic_range"], kit(n))[0]
check("The over-the-speaker glass pills keep their dark glass (readable on any footage), with the palette's overlay accent",
      C.INK in glass and n.overlay_accent in glass and n.bg not in glass.replace(n.overlay_accent, ""))
import core.entity_enricher as EE  # noqa: E402

real_cached = EE.cached
EE.cached = lambda key: types.SimpleNamespace(color="#76B900", logo_uri="", logo_kind="") if key == "nvidia" else None
try:
    kept = C.with_entity(Card("metric_bars", S, S + 4, entity="nvidia", rows=CARDS["metric_bars"].rows), kit(b))
    check("A company's colour on a card keeps the clip's palette with it", kept.accent == "#76B900" and kept.palette == b.to_dict())
    page_e = C.scene(Card("metric_bars", S, S + 4, entity="nvidia", rows=CARDS["metric_bars"].rows), kit(b))[0]
    check("...and on Bloomberg the card wears it", "#76B900" in page_e or P.fit_contrast("#76B900", b.bg, 4.5) in page_e)
finally:
    EE.cached = real_cached

# the selector
FIN = "the stock market sold off and bond yields rose while investors priced in inflation and rate cuts by the fed"
AI = "the new ai model runs on nvidia gpus in the cloud and every developer platform ships agents and software"
EXEC = "the ceo told the board that strategy and leadership and culture decide the competition and the hiring"
ESSAY = "my grandmother told me the story of her childhood and the history of the family and the books she loved"
picks = {k: P.select(t).name for k, t in {"fin": FIN, "ai": AI, "exec": EXEC, "essay": ESSAY}.items()}
check("Auto: finance -> Bloomberg obsidian, strategy -> Economist slate, editorial talk -> NYT archival",
      picks == {"fin": "bloomberg_obsidian", "ai": "bloomberg_obsidian", "exec": "economist_slate", "essay": "nyt_archival"}, str(picks))
check("...Bloomberg's amber for markets, cyan for tech and AI", P.select(FIN).accent == "#F59E0B" and P.select(AI).accent == "#38BDF8")
weak = "we talked for a while about it and then we moved on to the next thing"
bright = P.Studio(tone="#EDE8DF", share=0.6, saturation=0.2, brightness=0.72)
coloured = P.Studio(tone="#1E6B8C", share=0.55, saturation=0.65, brightness=0.2)
check("...studio lighting counts: a bright set leans to the light card, a set with its own strong colour gets studio_adaptive",
      P.select(weak, studio=bright).name == "nyt_archival" and P.select(weak, studio=coloured).name == "studio_adaptive"
      and P.select(FIN + " " + FIN, studio=coloured).name == "bloomberg_obsidian")
check("Overrides: every palette_mode by name; classic is Round 39's palette; PALETTE_MODE in .env wins",
      P.resolve("economist_slate", FIN).name == "economist_slate" and P.resolve("classic") is None
      and P.resolve("nyt_archival").name == "nyt_archival" and P.resolve("bloomberg_obsidian", AI).accent == "#38BDF8")
os.environ["PALETTE_MODE"] = "classic"
check("...PALETTE_MODE=classic in .env turns every clip back to Round 39's palette", P.resolve("nyt_archival") is None)
os.environ.pop("PALETTE_MODE")
brand, note = P.for_clip(BrandKit(), words_from(FIN), "Markets this week")
check("The pipeline puts the clip's palette on its own copy of the brand kit, and says which and why",
      brand.palette.get("name") == "bloomberg_obsidian" and note.startswith("Palette: bloomberg_obsidian")
      and BrandKit().palette == {}, note)
check("A brand kit saved before Round 40 still loads (palette_mode auto)", BrandKit(**json.loads(json.dumps(
    {"wordmark": "", "accent": "#F4C430", "logo_path": "", "show_name": ""}))).palette_mode == "auto")

# studio sampling on a real frame: a teal wall, a grey speaker in the middle
studio_src = tmp / "studio.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x1E6B8C:s=640x360:r=30:d=2",
                "-vf", "drawbox=x=200:y=110:w=240:h=250:color=0x808080:t=fill", "-pix_fmt", "yuv420p", str(studio_src)], check=True)
sampled = P.sample_studio(studio_src, 0.0, 2.0)
check("Studio sampling reads the set's dominant background tone (the teal wall, not the speaker)",
      sampled is not None and P.contrast(sampled.tone, "#1E6B8C") < 1.15 and sampled.distinct, str(sampled))
check("...and studio_adaptive builds its card from it", P.resolve("studio_adaptive", source=studio_src, start=0, end=2).bg
      == P.adaptive(sampled.tone).bg if sampled else False)

# drawn for real (Playwright): the light card is parchment, the panel around it the palette's canvas
try:
    import playwright  # noqa: F401
    have_browser = True
except ImportError:
    have_browser = False
if have_browser:
    from PIL import Image

    def card_pixels(palette):
        frames = C.render_card(CARDS["comparison_card"], kit(palette), tmp / "cards")
        image = np.asarray(Image.open(frames.pattern % (frames.count - 8)).convert("RGBA")).astype(int)
        inside = image[C.CARD_Y + 300:C.CARD_Y + 600, C.CARD_X + 40:C.CARD_X + 120, :3].reshape(-1, 3)
        return frames, np.median(inside, axis=0)

    nyt_frames, nyt_px = card_pixels(n)
    classic_frames, classic_px = card_pixels(None)
    parchment = np.array(P.rgb(n.bg))
    check("Rendered: on nyt_archival the card is parchment (the pixels, not just the page)",
          np.abs(nyt_px - parchment).max() < 14 and classic_px.max() < 40, f"{nyt_px} vs {parchment}; classic {classic_px}")
    check("...and the panel around it is the palette's canvas (core.dynamic_layout paints it)",
          nyt_frames.canvas == n.canvas and classic_frames.canvas == C.CANVAS)
else:
    print("(Playwright not installed: the rendered palette checks were skipped)")

# app.py: the override
app = (ROOT / "app.py").read_text()
check("app.py: palette_mode can be set in the brand kit (every palette, auto first); the teaser has its own switch",
      'key="palette_mode"' in app and "palette_mode=palette_mode" in app and P.PALETTE_MODES[0] == "auto"
      and 'key="climax_hook"' in app and "climax_hook=climax_hook" in app)

# ============================================ 6. a real render: teaser, flash, hit, the clip N frames later ============================================
import core.pipeline as PL  # noqa: E402
from config import load_settings  # noqa: E402
from core.hook_selector import ClipResult  # noqa: E402

W_, H_, RATE, SECONDS = 960, 540, 30, 34
BITS = 10


def frame(index):
    img = np.full((H_, W_, 3), 64, np.uint8)
    img[: int(H_ * 0.40)] = 0
    for bit in range(BITS):  # the frame's own number, 10 stripes across the top 40 %
        if index >> bit & 1:
            img[: int(H_ * 0.40), bit * 96:(bit + 1) * 96] = 255
    img[int(H_ * 0.44):int(H_ * 0.56), 240:720] = 255  # a fixed white box: measures the punch-in
    return img


source = tmp / "talk.mp4"
proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W_}x{H_}", "-r", str(RATE),
                         "-i", "-", "-f", "lavfi", "-i", f"sine=f=300:d={SECONDS}:sample_rate=48000", "-shortest",
                         "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p", "-g", "30",
                         "-c:a", "aac", str(source)], stdin=subprocess.PIPE)
for k in range(SECONDS * RATE):
    proc.stdin.write(frame(k).tobytes())
proc.stdin.close()
proc.wait()

settings = dataclasses.replace(load_settings(), temp_dir=tmp / "temp", output_dir=tmp / "out")
(tmp / "temp").mkdir(exist_ok=True)
(tmp / "out").mkdir(exist_ok=True)
clip = ClipResult(start_time=CLIP_START, end_time=clip_end, viral_title="What product market fit feels like", caption="",
                  hashtags=[], virality_score=90)
titles = []
real_title = PL.prepare_title_overlay
PL.prepare_title_overlay = lambda *a, **k: titles.append(k) or real_title(*a, **k)
try:
    with_hook = PL.render_selected_clip(source, clip, story, settings, tmp / "out" / "hook.mp4", layout_mode="blurred_background")
finally:
    PL.prepare_title_overlay = real_title
plain = PL.render_selected_clip(source, clip, story, settings, tmp / "out" / "plain.mp4", layout_mode="blurred_background",
                                climax_hook_on=False)
t = with_hook.teaser
N = t.frames if t else 0


def packets(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                          "stream=nb_read_packets", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return int(out.strip() or 0)


check("Make clips and the worker: the clip opens on its teaser (Settings.enable_climax_hook is on by default)",
      t is not None and load_settings().enable_climax_hook and t.start == teaser.start and t.cut == teaser.cut, str(t))
check("...climax_hook_on=False (or ENABLE_CLIMAX_HOOK=0): the clip renders exactly as before, no teaser",
      plain.teaser is None and packets(plain.path) > 0)
os.environ["ENABLE_CLIMAX_HOOK"] = "0"
check("...ENABLE_CLIMAX_HOOK=0 in .env turns it off everywhere", PL.find_teaser(source, story, CLIP_START, clip_end, True) is None)
os.environ.pop("ENABLE_CLIMAX_HOOK")
total, body = packets(with_hook.path), packets(plain.path)
check("The file is the teaser's N frames, then every frame of the clip", t and total == N + body, f"{total} = {N} + {body}?")


def strip(path, first_frame, count, y0, rows):
    """Grey rows y0..y0+rows of frames first_frame.. (output pixels)."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
                          f"select='between(n\\,{first_frame}\\,{first_frame + count - 1})',crop=1080:{rows}:0:{y0}",
                          "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, rows, 1080)


# the source's top 40 % sits at output y 656-872 (blurred background: 1080 x 608 in the middle)
from core.face_tracker import PULL_IN_CENTER_Y  # noqa: E402

CY = 1920 * PULL_IN_CENTER_Y  # the punch-in zooms around (540, CY)


def decode(rows_img, zoom):
    y = rows_img.shape[0] // 2
    value = 0
    for bit in range(BITS):
        x = 540 + ((bit + 0.5) * 108 - 540) * zoom
        if np.median(rows_img[y - 3:y + 4, int(x) - 6:int(x) + 7]) > 128:
            value |= 1 << bit
    return value


Y_BITS = 740  # a row in the stripes, both at 1.0x and at the punch-in
if t:
    body_rows = strip(with_hook.path, N, 45, Y_BITS - 20, 40)
    plain_rows = strip(plain.path, 0, 45, Y_BITS - 20, 40)
    start_frame = round(CLIP_START * RATE)
    got = [decode(img, 1.0) for img in body_rows[2:45]]
    check("Frame-exact: file frame N+k is the clip's own frame k (its captions, crop and cards with it), past the flash",
          got == list(range(start_frame + 2, start_frame + 45)) and [decode(img, 1.0) for img in plain_rows[2:45]] == got,
          f"{got[:6]}... vs {start_frame + 2}...")
    tail_hook, tail_plain = strip(with_hook.path, total - 30, 30, Y_BITS - 20, 40), strip(plain.path, body - 30, 30, Y_BITS - 20, 40)
    check("...to the very last frame (nothing drifts over the clip)",
          [decode(i, 1.0) for i in tail_hook] == [decode(i, 1.0) for i in tail_plain])
    teaser_rows = strip(with_hook.path, 0, N, Y_BITS - 20, 40)
    first_teaser = decode(teaser_rows[0], H.PUNCH_IN)
    check("The teaser is the climax's own frames, from teaser_start (the 1.05x punch-in), in order",
          abs(first_teaser - t.start * RATE) <= 1.01 and [decode(i, H.PUNCH_IN) for i in teaser_rows]
          == list(range(first_teaser, first_teaser + N)), f"{first_teaser} vs {t.start * RATE:.2f}")
    full = strip(with_hook.path, N - 2, 5, 0, 1920)
    luma = full.reshape(5, -1).mean(axis=1)
    flash_y = 0.2126 * 253 + 0.7152 * 251 + 0.0722 * 247
    base = full[4].astype(float).mean()
    check("The flash: the clip's first frame at 85 % #FDFBF7, its second at 40 %, then the clip as it is",
          abs(luma[2] - (0.85 * flash_y + 0.15 * base)) < 14 and abs(luma[3] - (0.40 * flash_y + 0.60 * base)) < 14
          and abs(luma[1] - luma[0]) < 10, f"{np.round(luma, 1)} (flash {flash_y:.0f}, clip {base:.0f})")
    box_row_out = 656 + int(H_ * 0.50 * 608 / H_)  # the white box's middle row at 1.0x
    widths = []
    for idx, zoom in ((N - 3, H.PUNCH_IN), (N + 5, 1.0)):
        y = int(CY + (box_row_out - CY) * zoom)
        line = strip(with_hook.path, idx, 1, y - y % 2, 2)[0, 0].astype(int)
        widths.append(int((line > 200).sum()))
    check("The teaser is punched in 1.05x on the speaker; the clip after it is back at 1.0x",
          abs(widths[0] / max(1, widths[1]) - 1.05) < 0.015, str(widths))
    calls = [k for k in titles if k.get("pinned") and abs(k.get("clip_start", 0) - t.start) < 1e-6]
    check("The title card banner is up for the whole teaser (from frame 0, no fade)",
          calls and calls[0].get("fade_in") is False and calls[0]["clip_duration"] >= t.offset, str(calls[:1]))
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(with_hook.path), "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    audio = np.frombuffer(pcm, "<i2").astype(float) / 32768
    at = int(round(t.offset * 48000))

    def low(seg):
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), 48000)) ** 2
        return spec[40:91].sum()

    hit_low, before_low = low(audio[at:at + 4800]), low(audio[at - 14400:at - 9600])
    check("The sub-bass hit lands on the clip's first frame (45-80 Hz energy jumps there)", hit_low > 50 * (before_low + 1e-9),
          f"{hit_low:.3g} vs {before_low:.3g}")
    rms = lambda seg: float(np.sqrt(np.mean(seg ** 2)))  # noqa: E731
    check("The teaser's audio fades out over 0.06 s on the cut word (no click)",
          rms(audio[at - 960:at - 120]) < 0.75 * rms(audio[at - 9600:at - 4800]) and rms(audio[at - 240:at]) < 0.3 * rms(audio[at - 9600:at - 4800]))
    check("...and the master stays under its ceiling with the hit in it", np.abs(audio).max() <= 10 ** (-1.0 / 20),
          f"{20 * np.log10(np.abs(audio).max()):.2f} dBFS")
    check("RenderedClip: the offset is T_teaser = N / fps exactly, and output_time maps the clip onto the file",
          abs(t.offset - N / RATE) < 1e-9 and abs(with_hook.output_time(CLIP_START) - N / RATE) < 1e-9
          and abs(with_hook.output_time(CLIP_START + 10) - (10 + N / RATE)) < 1e-9)
run_all = (ROOT / "tests" / "run_all.py").read_text()
check("Older suites run as Round 39 did (ENABLE_CLIMAX_HOOK=0, PALETTE_MODE=classic in tests/run_all.py)",
      "ENABLE_CLIMAX_HOOK" in run_all and "PALETTE_MODE" in run_all)

print("\nAll Round 40 checks passed." if not fails else f"\n{fails} check(s) failed")
sys.exit(1 if fails else 0)
