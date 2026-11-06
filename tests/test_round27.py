"""Round 27 checks: the graphics library (accuracy checks, every template drawn, the
dynamic layout with name card, number and chart) and the 📊 Graphics section in Make clips.

Run from the project root:  .venv/bin/python tests/test_round27.py .
Needs Playwright (.venv/bin/pip install playwright && .venv/bin/playwright install chromium).
Uses temporary files and test videos. No AI, YouTube or R2.
"""
import json
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import asdict
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


from core.transcriber import Word  # noqa: E402
from core import card_checks as K  # noqa: E402
from core.edl import MODES, TEMPLATES, BrandKit, Card, CardRow, ClipEdit, sentences  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="round27-"))


def words_from(text, start=0.0, step=0.3):
    out, t = [], start
    for token in text.split():
        out.append(Word(token, round(t, 2), round(t + step - 0.02, 2)))
        t += step
    return out


# ---------- spoken numbers ----------
SAID = ("Where's all this money going to come from? Yeah, yeah. So, punchline again, $17 trillion of S&P "
        "top-line revenue, right? And they've got around about 11 or 12 trillion dollars of expenses. And so, "
        "that means that the net profit approximately of the S&P is somewhere between five and five and a half "
        "trillion dollars, right? So, it's like, all right, we can make some bold claims about the money.")
WORDS = words_from(SAID, start=100.0)
CLIP_START, CLIP_END = 100.0, WORDS[-1].end + 0.2
SPANS = sentences(WORDS)


def at(token, words=WORDS):
    return next(w.start for w in words if w.text.strip(",.?") == token)


values = [(n.value, n.scale) for n in K.spoken_numbers(WORDS)]
check("Hears $17 trillion, 11 or 12 trillion, five and five and a half trillion",
      values == [(17.0, "T"), (11.0, "T"), (12.0, "T"), (5.0, "T"), (5.5, "T")], str(values))
more = [(n.value, n.scale) for n in K.spoken_numbers(words_from("within maximum ninety six hours, 47% growth, "
                                                                 "1,200 dollars, thirty percent, 2.5 billion"))]
check("Hears ninety six (no % borrowed from the next number), 47%, 1,200, thirty percent, 2.5 billion",
      more == [(96.0, None), (47.0, "%"), (1200.0, None), (30.0, "%"), (2.5, "B")], str(more))
check("Card values: '~$11–12T' is a hedged range in trillions",
      (lambda v: (v.numbers, v.scale, v.hedged))(K.parse_value("~$11–12T")) == ([11.0, 12.0], "T", True))
check("Signed values for waterfalls (+$3.2M, −$0.4M)",
      K.signed_value("+$3.2M") == 3.2 and K.signed_value("−$0.4M") == -0.4)
check("Sentences split on . ? !", SPANS[0][2] == "Where's all this money going to come from?" and len(SPANS) == 6,
      str(len(SPANS)))
check("The library: 13 graphics, 3 over the speaker and 10 charts",
      len(TEMPLATES) == 13 and sum(MODES[t] == "overlay" for t in TEMPLATES) == 3)

# ---------- the checks: a breakdown chart (the S&P moment) ----------
card_start, card_end = SPANS[2][0], SPANS[4][1] + 0.15


def sp_card(**over):
    rows = over.pop("rows", [CardRow("Top-line revenue", "$17T"), CardRow("Expenses", "~$11–12T"),
                             CardRow("Net profit", "~$5–5.5T", highlight=True)])
    return Card("metric_bars", start=over.pop("start", card_start), end=over.pop("end", card_end),
                headline="Revenue vs. expenses", rows=rows, **over)


good = sp_card()
result = K.check_card(good, WORDS, CLIP_START, CLIP_END)
check("The S&P chart passes", result.ok, str(result.errors))
check("Each number appears on the word it's said",
      [r.reveal_at for r in good.rows] == [at("$17"), at("11"), at("five")], str([r.reveal_at for r in good.rows]))


def errors_of(card, words=WORDS, start=CLIP_START, end=CLIP_END, **kw):
    return K.check_card(card, words, start, end, **kw).errors


bad = errors_of(sp_card(rows=[CardRow("Revenue", "$17B"), CardRow("Expenses", "~$11–12T")]))
check("Wrong scale ($17B for 17 trillion) is stopped", any("said in billion" in e for e in bad), str(bad))
bad = errors_of(sp_card(rows=[CardRow("Revenue", "$18T"), CardRow("Expenses", "~$11–12T")]))
check("A number that wasn't said is stopped", any("$18T" in e for e in bad), str(bad))
bad = errors_of(sp_card(rows=[CardRow("Revenue", "$17T"), CardRow("Net profit", "$5.5T")]))
check("A dropped hedge is stopped ('somewhere between' -> ~ or a range)", any("hedged" in e for e in bad), str(bad))
bad = errors_of(sp_card(start=CLIP_START + 1))
check("Nothing in the first 3 s (the hook)", any("first 3 s" in e for e in bad), str(bad))
bad = errors_of(sp_card(start=CLIP_START + 3.5), hook_seconds=4.0)
check("Nothing while the 4 s title card is up", any("first 4 s" in e for e in bad), str(bad))
bad = errors_of(sp_card(end=CLIP_END - 0.5))
check("Nothing in the last 2 s (the payoff)", any("last 2 s" in e for e in bad), str(bad))
bad = errors_of(sp_card(end=card_start + 1.5))
check("Charts stay at least 2.5 s", any("at least 2.5 s" in e for e in bad), str(bad))
bad = errors_of(sp_card(rows=[CardRow("A label that is far too long to fit", "$17T"), CardRow("E", "~$11–12T")]))
check("Character limits", any("label is" in e for e in bad), str(bad))
bad = errors_of(sp_card(rows=[CardRow("Revenue", "$17T"), CardRow("Expenses", "~$11–12T"),
                              CardRow("Margin", "47%")]))
check("One unit per chart (no mixing $T and %)", bool(bad), str(bad))

# ---------- the checks: every other graphic ----------
LEAD = "Okay so here is the thing that matters most to me today."  # 12 words: past the 3 s hook
TAIL = "That is the whole story right there for you."


def moment(text, template, rows, start=100.0, **over):
    """A clip of LEAD + text + TAIL; the card covers ``text``. Returns (card, check, words)."""
    words = words_from(f"{LEAD} {text} {TAIL}", start=start)
    lead, tail = len(LEAD.split()), len(TAIL.split())
    inside = words[lead:len(words) - tail]
    card = Card(template, start=inside[0].start, end=inside[-1].end + 0.15, rows=rows, **over)
    return card, K.check_card(card, words, start, words[-1].end + 0.2), words


ok = {}
card, res, _ = moment("We shipped it in ninety six hours flat, from the very first line of code to the final release.",
                      "kinetic_range", [CardRow("from the very first line of code", "96 hrs")])
ok["kinetic_range"] = card
check("A: number over the speaker passes, and starts as the number is said",
      res.ok and abs(card.start - (card.rows[0].reveal_at - 0.12)) < 1e-6, str(res.errors))
card, res, _ = moment("We shipped it in ninety six hours flat, from the very first line of code to the final release.",
                      "kinetic_range", [CardRow("from our pizza party", "96 hrs")])
check("A: the line under the number must be the speaker's words", any("speaker's words" in e for e in res.errors))
card, res, words = moment("There are three kinds of founders here: builders, then sellers, and finally operators.",
                          "buckets", [CardRow("Builders"), CardRow("Sellers", dashed=True),
                                      CardRow("Operators", highlight=True)])
ok["buckets"] = card
check("B: boxes appear in order, each on its word", res.ok and [r.reveal_at for r in card.rows]
      == [at("builders", words), at("sellers", words), at("operators", words)], str(res.errors))
_, res, _ = moment("There are three kinds of founders here: builders, then sellers, and finally operators.",
                   "buckets", [CardRow("Operators"), CardRow("Builders")])
check("B: boxes out of spoken order are stopped", not res.ok, str(res.errors))
TL = ("Twenty years ago they drew up the bombing scenario. Then he gave the orders, ninety six hours to execute "
      "the plans. So how does this end, five or six months into the war?")
card, res, _ = moment(TL, "timeline", [CardRow("", "20 yrs ago", detail="The bombing scenario"),
                                       CardRow("", "96 hrs", detail="Orders to execute the plans"),
                                       CardRow("", "Months 5–6", detail="How does this end?", highlight=True)])
ok["timeline"] = card
check("C: timeline points appear on their words, in order", res.ok and card.rows[0].reveal_at
      < card.rows[1].reveal_at < card.rows[2].reveal_at, str(res.errors))
words = words_from(f"{LEAD} {TL} {TAIL}", start=100.0)
late = next(w for w in words if w.text == "Then").start
recap = Card("timeline", start=late, end=words[-len(TAIL.split()) - 1].end + 0.15,
             rows=[CardRow("", "20 yrs ago", detail="The bombing scenario"), CardRow("", "96 hrs", detail="The orders")])
res = K.check_card(recap, words, 100.0, words[-1].end + 0.2)
check("C: a point said earlier in the clip is drawn in as a recap", res.ok
      and abs(recap.rows[0].reveal_at - (late + 0.2)) < 1e-6, str(res.errors))
_, res, _ = moment(TL, "timeline", [CardRow("", "30 yrs ago", detail="The bombing scenario"),
                                    CardRow("", "96 hrs", detail="Orders to execute the plans")])
check("C: a point that wasn't said is stopped", any("30 yrs ago" in e for e in res.errors), str(res.errors))
_, res, _ = moment(TL, "timeline", [CardRow("", "20 yrs ago", detail="Pizza and fireworks"),
                                    CardRow("", "96 hrs", detail="Orders to execute the plans")])
check("C: descriptions must be the speaker's words", any("speaker's words" in e for e in res.errors), str(res.errors))

ARR = ("We started the year at $10 million in ARR. New customers added $4 million, expansion added $1 million, "
       "churn took away $2 million, and we ended the year at $13 million.")
bridge = [CardRow("Start", "$10M"), CardRow("New", "+$4M"), CardRow("Expansion", "+$1M"), CardRow("Churn", "−$2M"),
          CardRow("End", "$13M", highlight=True)]
card, res, _ = moment(ARR, "arr_bridge", bridge, headline="ARR this year")
ok["arr_bridge"] = card
check("ARR bridge that adds up passes", res.ok, str(res.errors))
_, res, _ = moment(ARR, "arr_bridge", bridge[:3] + [CardRow("Churn", "+$2M")] + bridge[4:])
check("ARR bridge that doesn't add up is stopped (wrong sign)", any("doesn't add up" in e for e in res.errors), str(res.errors))
_, res, _ = moment(ARR, "arr_bridge", bridge[:3] + [CardRow("Churn", "$2M")] + bridge[4:])
check("ARR bridge steps need + or −", any("Mark each step" in e for e in res.errors), str(res.errors))

FLOW = ("Revenue was $100 million. Costs were $70 million and profit was $30 million. The costs were $40 million "
        "on people and $30 million on compute.")
flow = [CardRow("Revenue", "$100M"), CardRow("Costs", "$70M", parent="Revenue"),
        CardRow("Profit", "$30M", parent="Revenue", highlight=True), CardRow("People", "$40M", parent="Costs"),
        CardRow("Compute", "$30M", parent="Costs")]
card, res, _ = moment(FLOW, "sankey_flow", flow, headline="Where the money goes")
ok["sankey_flow"] = card
check("Sankey whose splits add up passes", res.ok, str(res.errors))
_, res, _ = moment(FLOW, "sankey_flow", flow[:4])
check("Sankey with a missing flow is stopped", any("add up to 40" in e for e in res.errors), str(res.errors))
_, res, _ = moment(FLOW, "sankey_flow", flow[:2] + [CardRow("Profit", "$30M")] + flow[3:])
check("Sankey needs exactly one starting row", any("exactly one starting row" in e for e in res.errors), str(res.errors))

card, res, _ = moment("Of every dollar, 50% goes to staff, 30% goes to rent and 20% goes to marketing.", "share_split",
                      [CardRow("Staff", "50%", highlight=True), CardRow("Rent", "30%"), CardRow("Marketing", "20%")])
ok["share_split"] = card
check("Shares of a whole that add to 100% pass", res.ok, str(res.errors))
_, res, _ = moment("Of every dollar, 50% goes to staff, 30% goes to rent and 10% goes to marketing.", "share_split",
                   [CardRow("Staff", "50%"), CardRow("Rent", "30%"), CardRow("Marketing", "10%")])
check("Shares that add to 90% are stopped", any("add up to 90%" in e for e in res.errors), str(res.errors))
card, res, _ = moment("North made $400 billion, East made $300 billion and South made $250 billion last year.",
                      "ranked_bars", [CardRow("North", "$400B", highlight=True), CardRow("East", "$300B"),
                                      CardRow("South", "$250B")])
ok["ranked_bars"] = card
check("Ranked bars pass", res.ok, str(res.errors))
card, res, _ = moment("In the survey 62% said yes and 38% said no to the new plan.", "diverging_bar",
                      [CardRow("Yes", "62%", highlight=True), CardRow("No", "38%")])
ok["diverging_bar"] = card
check("Two-way split passes", res.ok, str(res.errors))
card, res, _ = moment("Sales were $2 million in 2022, then $3 million in 2023 and $5 million in 2024.", "trend_line",
                      [CardRow("2022", "$2M"), CardRow("2023", "$3M"), CardRow("2024", "$5M", highlight=True)])
ok["trend_line"] = card
check("Trend line passes", res.ok, str(res.errors))
card, res, _ = moment("It costs us $1,200 to win a customer and about twelve months to earn it back.", "cac_payback",
                      [CardRow("Cost to win a customer", "$1,200"), CardRow("Payback", "~12 months")])
ok["cac_payback"] = card
check("CAC payback passes (hedge kept)", res.ok, str(res.errors))
_, res, _ = moment("It costs us $1,200 to win a customer and about twelve months to earn it back.", "cac_payback",
                   [CardRow("Cost to win a customer", "$1,200"), CardRow("Payback", "~12")])
check("CAC payback needs the months", any("months" in e for e in res.errors), str(res.errors))

name = Card("speaker_title", start=104.0, end=107.3, rows=[CardRow("Sam Rivera", detail="Founder, Acme Robotics")])
ok["speaker_title"] = name
check("Name card passes", K.check_card(name, WORDS, CLIP_START, CLIP_END).ok)
check("Name card needs a name and 2.5–4.5 s", not K.check_card(Card("speaker_title", 104.0, 110.0, rows=[CardRow("")]),
                                                                WORDS, CLIP_START, CLIP_END).ok)

prices = tmp / "prices.csv"
lines = ["date,open,high,low,close,volume"]
price = 100.0
for day in range(40):
    change = (1.6 if day % 5 else -2.4) + (day % 3) * 0.3
    lines.append(f"2026-09-{day % 28 + 1:02d},{price:.2f},{price + 2.5:.2f},{price - 2.0:.2f},{price + change:.2f},{1000 + 37 * day}")
    price += change
prices.write_text("\n".join(lines))
candle = Card("candlestick_ema", start=card_start, end=card_end, headline="The S&P", data_path=str(prices))
res = K.check_card(candle, WORDS, CLIP_START, CLIP_END)
check("Price chart needs its source", any("Source" in e for e in res.errors), str(res.errors))
candle.source = "Sample data"
res = K.check_card(candle, WORDS, CLIP_START, CLIP_END)
ok["candlestick_ema"] = candle
check("Price chart with a file and a source passes (flagged as drawn from the file)", res.ok and res.warnings, str(res.errors))
ok["metric_bars"] = good
check("Every graphic has a passing example", set(ok) == set(TEMPLATES), str(set(TEMPLATES) - set(ok)))

# ---------- restraint: clip-wide caps and gaps ----------
results = K.check_edit(ClipEdit(CLIP_START, CLIP_END, cards=[sp_card(), sp_card(start=card_start + 1)]), WORDS)
check("At most 1 chart per clip", any("At most 1 chart" in e for e in results[1].errors), str(results[1].errors))
check("Graphics need room between them", any("after the graphic before it" in e for e in results[1].errors))
results = K.check_edit(ClipEdit(CLIP_START, CLIP_END, cards=[name, Card("speaker_title", 110.0, 113.3,
                                                                         rows=[CardRow("Sam")])]), WORDS)
check("At most 1 name card per clip", any("At most 1 name card" in e for e in results[1].errors))
round_trip = ClipEdit.from_json(ClipEdit(CLIP_START, CLIP_END, cards=[sp_card()], brand=BrandKit(accent="#5EC8F2")).to_json())
check("The edit list saves and loads", round_trip.cards[0].rows[2].highlight and round_trip.brand.accent == "#5EC8F2")

# ---------- captions: no bold line under a graphic ----------
from core.dynamic_layout import calm_captions, words_outside  # noqa: E402

straddle = Card("metric_bars", start=at("$17") - 0.1, end=card_end, rows=[])
kept = words_outside(WORDS, ClipEdit(CLIP_START, CLIP_END, cards=[straddle]))
check("Bold captions drop every word that overlaps a graphic (even partly)",
      all(w.end <= straddle.start or w.start >= straddle.end for w in kept) and len(kept) < len(WORDS))
ass = calm_captions(WORDS, ClipEdit(CLIP_START, CLIP_END, cards=[straddle]), tmp / "calm.ass").read_text()
check("Calm captions: small, sentence case, during the graphic", "Style: Calm,Inter,40" in ass and "$17" in ass)

# ---------- drawing every graphic ----------
from PIL import Image  # noqa: E402

from core import cards as C  # noqa: E402

brand = BrandKit()
drawn = {}
for template, card in ok.items():
    try:
        drawn[template] = C.render_card(card, brand, tmp / "cards")
    except Exception as exc:  # noqa: BLE001
        drawn[template] = exc
broken = {t: str(f) for t, f in drawn.items() if isinstance(f, Exception)}
check("All 13 graphics draw", not broken, str(broken))
sizes = {t: (f.width, f.height, f.top) for t, f in drawn.items() if not isinstance(f, Exception)}
check("Over-the-speaker graphics are full frame; charts are the lower 76% (Round 28: 24/76 split)",
      all(sizes[t] == ((1080, 1920, 0) if MODES[t] == "overlay" else (1080, 1460, 460)) for t in sizes), str(sizes))
frames = drawn["metric_bars"]
first, middle, last = (Image.open(frames.pattern % k) for k in (0, frames.count // 2, frames.count - 1))
check("Chart frames: clear at the start, drawn mid-way (the canvas is laid under them)",
      first.getchannel("A").getextrema()[1] < 40 and middle.getchannel("A").getextrema()[1] == 255)
lt = drawn["speaker_title"]
mid = Image.open(lt.pattern % (lt.count // 2))
check("Name card: transparent frame with a frosted pill in the lower middle",
      mid.getpixel((20, 20))[3] == 0 and mid.getpixel((540, 1200))[3] > 0 and lt.blur and lt.blur[0][4] > 0,
      f"{mid.getpixel((540, 1200))} {lt.blur}")
end = Image.open(lt.pattern % (lt.count - 1))
lasts = {t: Image.open(f.pattern % (f.count - 1)).getchannel("A").getextrema()[1] for t, f in drawn.items()
         if not isinstance(f, Exception)}
check("Every graphic leaves smoothly: fully faded on its last frame (no pop)", max(lasts.values()) < 8, str(lasts))
again = C.render_card(good, brand, tmp / "cards")
check("Unchanged graphics are reused, not drawn again", again.pattern == frames.pattern)
try:
    wide = sp_card()
    wide.headline = "W" * 60
    C.render_card(wide, brand, tmp / "cards")
    check("Text that doesn't fit is refused", False)
except C.CardRenderError as exc:
    check("Text that doesn't fit is refused", "shorten" in str(exc), str(exc))
try:
    C.render_card(good, BrandKit(accent="#202020"), tmp / "cards")
    check("An accent too dark to read is refused", False)
except C.CardRenderError:
    check("An accent too dark to read is refused", True)
check("Contrast math (white on black = 21)", round(C.contrast("#FFFFFF", "#000000")) == 21)

# ---------- the dynamic layout: name card, number, chart ----------
from core.dynamic_layout import render_with_cards  # noqa: E402

OPEN = ("Thanks for having me on the show today, it's great to be here with you and I have been looking forward "
        "to this conversation for a long time now.")
FAST = "We shipped it in ninety six hours flat, from the very first line of code to the final release."
SHOW = words_from(f"{OPEN} {FAST} {SAID}", start=100.0)
SHOW_END = SHOW[-1].end + 0.2
fast = [w for w in SHOW if w.start >= next(x.start for x in SHOW if x.text == "We")]
fast_end = next(w.end for w in fast if w.text == "release.")
show_spans = sentences(SHOW)
chart_start = next(s for s in show_spans if "$17" in s[2])[0]
chart_end = next(s for s in show_spans if "trillion dollars, right?" in s[2])[1] + 0.15
show_cards = [Card("speaker_title", start=103.2, end=106.5, rows=[CardRow("Sam Rivera", detail="Founder, Acme Robotics")]),
              Card("kinetic_range", start=fast[0].start, end=fast_end + 0.3,
                   rows=[CardRow("from the very first line of code", "96 hrs")]),
              sp_card(start=chart_start, end=chart_end)]
source = tmp / "source.mp4"
base_clip = tmp / "base.mp4"
duration = SHOW_END - 100.0
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x2060A0:s=1920x1080:r=30:d={SHOW_END + 1}",
                "-f", "lavfi", "-i", f"sine=f=220:d={SHOW_END + 1}", "-pix_fmt", "yuv420p", "-shortest", str(source)],
               check=True)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0xC04040:s=1080x1920:r=30:d={duration}",
                "-f", "lavfi", "-i", f"sine=f=440:d={duration}", "-pix_fmt", "yuv420p", "-shortest", str(base_clip)],
               check=True)
given = {}


def render_base(kept_words):
    given["words"] = kept_words
    return base_clip


edit = ClipEdit(100.0, SHOW_END, cards=show_cards, brand=BrandKit())
made, checks = render_with_cards(source, edit, SHOW, tmp / "with_graphics.mp4", tmp / "work", render_base, hook_seconds=3.0)
check("Clip with a name card, a number and a chart made", made is not None and made.is_file() and all(c.ok for c in checks),
      str([c.errors for c in checks]))
check("The clip is rendered without bold captions under the graphics",
      not any(c.start < w.end and w.start < c.end for w in given.get("words", []) for c in show_cards)
      and len(given.get("words", [])) > 20)


def pixel(path, t, xy):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    x, y = xy
    i = (y * 1080 + x) * 3
    return tuple(raw[i:i + 3])


def near(color, target, tol=24):
    return all(abs(a - b) <= tol for a, b in zip(color, target))


RED, BLUE, CANVAS = (0xC0, 0x40, 0x40), (0x20, 0x60, 0xA0), (5, 6, 8)
during_chart = chart_start - 100.0 + 2.0
check("Before any graphic: the clip as rendered", near(pixel(made, 1.0, (540, 300)), RED))
glass = pixel(made, 104.8 - 100.0, (790, 1300))
check("Name card: frosted glass over the speaker (darker, not opaque)", glass[0] < RED[0] - 25 and glass[0] > 30, str(glass))
check("Name card gone after it leaves", near(pixel(made, 106.6 - 100.0, (790, 1300)), RED))
check("Chart: the speaker on top (the whole frame)", near(pixel(made, during_chart, (540, 300)), BLUE))
check("Chart: the obsidian canvas below", near(pixel(made, during_chart, (1040, 1800)), CANVAS))
check("After the chart: back to full screen", near(pixel(made, chart_end - 100.0 + 0.4, (540, 300)), RED))
mix = pixel(made, chart_end - 100.0 - 0.1, (540, 300))
check("The chart dissolves back to full screen (no hard cut)", not near(mix, RED, 12) and not near(mix, BLUE, 12), str(mix))
probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(made)],
                       capture_output=True, text=True).stdout
check("Same length, audio kept", abs(float(probe) - duration) < 0.15 and b"audio" in subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(made)], capture_output=True).stdout)

# ---------- the app ----------
store = tmp / "app"
store.mkdir()
calls = store / "calls.json"
wrapper = tmp / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys; sys.path.insert(0, {str(ROOT)!r})
    from pathlib import Path
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(store)!r})
    for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, name, W.STORAGE_DIR / file)
    W.get_worker_status = lambda: {{"running": False}}
    import core.edl as E
    E.BRAND_KIT_PATH = Path({str(store / "brand_kit.json")!r})
    import core.dynamic_layout as D
    def fake(source, edit, words, output, work, render_base, hook_seconds=3.0):
        Path({str(calls)!r}).write_text(__import__("json").dumps(
            [[c.template for c in edit.cards], edit.brand.accent, hook_seconds, callable(render_base)]))
        Path(output).write_bytes(Path({str(base_clip)!r}).read_bytes())
        return Path(output), []
    D.render_with_cards = fake
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
from streamlit.testing.v1 import AppTest  # noqa: E402

from core.hook_selector import ClipResult  # noqa: E402

clip_file = tmp / "clip_01.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(base_clip), "-t", "1", "-c", "copy", str(clip_file)], check=True)
key = str(clip_file.resolve())
app = AppTest.from_file(str(wrapper), default_timeout=120)
app.session_state["generated_clips"] = [(clip_file, ClipResult(start_time=100.0, end_time=SHOW_END,
                                                               viral_title="Shipped in 96 hours", caption="",
                                                               hashtags=[], virality_score=88))]
app.session_state["clip_render_contexts"] = {key: {"source_path": str(source), "words": SHOW, "layout_mode": "smart_crop",
                                                   "start_time": 100.0, "end_time": SHOW_END}}
app.run()
check("App runs with the Graphics section", not app.exception and any("Graphics" in str(h.value) for h in app.subheader),
      str([e.value for e in app.exception][:1]))
app.selectbox(key=f"card_template_{key}").set_value("speaker_title").run()
app.text_input(key=f"name_{key}").set_value("Sam Rivera")
app.text_input(key=f"role_{key}").set_value("Founder, Acme Robotics")
next(b for b in app.button if b.label == "✔ Check and add").click().run()
drafts = app.session_state["card_drafts"][key]
check("Name card added, just after the 4 s title, for 3.3 s",
      len(drafts) == 1 and abs(drafts[0]["start"] - 104.2) < 1e-6 and abs(drafts[0]["end"] - drafts[0]["start"] - 3.3) < 1e-6,
      str(drafts))
app.selectbox(key=f"card_template_{key}").set_value("metric_bars").run()
next(b for b in app.button if b.label == "✔ Check and add").click().run()
check("An empty chart is not added, with a plain reason", any("takes 2 to 4 items" in str(e.value) for e in app.error)
      and len(app.session_state["card_drafts"][key]) == 1, str([e.value for e in app.error]))
app.session_state["card_drafts"][key].append(asdict(sp_card(start=chart_start, end=chart_end)))
picker = lambda: next(c for c in app.color_picker if c.label.startswith("The one highlight color"))  # noqa: E731
picker().set_value("#202020")
next(b for b in app.button if b.label == "Save" and "brand_kit_form" in str(b.form_id)).click().run()
check("A too-dark accent is refused", any("too dark" in str(e.value) for e in app.error))
picker().set_value("#5EC8F2")
next(b for b in app.button if b.label == "Save" and "brand_kit_form" in str(b.form_id)).click().run()
next(b for b in app.button if b.label.startswith("🎬 Render this clip")).click().run()
sent = json.loads(calls.read_text()) if calls.is_file() else None
check("Rendering sends the graphics, the saved accent, the title length and the clip renderer",
      sent == [["speaker_title", "metric_bars"], "#5EC8F2", 4.0, True], str(sent))
new_path = Path(app.session_state["generated_clips"][0][0])
check("The clip is replaced by the version with graphics (Round 28: no separate copy)",
      new_path != clip_file and new_path.is_file() and not clip_file.exists(), str(new_path))
check("No leftover watermark or footnote fields in the app",
      not any(t.label.startswith(("Footnote", "Eyebrow", "Name in the corner")) for t in app.text_input))

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
