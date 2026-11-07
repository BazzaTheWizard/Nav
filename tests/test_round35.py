"""Round 35 checks: logos and brand colours (core.entity_enricher), real market and macro
data (core.market_data, with every source mocked), the market_line chart inside the card's
bounds with its source, the 0-1.2 s entrance then 3.8 s still (core.cards' time-warp), the
mesh background, and frame-locked micro sound effects (core.audio_polish) mixed without
moving a frame. No network: logos, Yahoo Finance, FRED and SEC answers are all faked.

Run from the project root:  .venv/bin/python tests/test_round35.py .
"""
import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp(prefix="round35-"))
os.environ.update({"NAV_NO_CLIP_PAGES": "1", "NAV_LOGO_DIR": str(tmp / "logos"), "NAV_MARKET_CACHE": str(tmp / "market"),
                   "NAV_SFX_DIR": str(tmp / "sfx")})
for name in ("BRANDFETCH_CLIENT_ID", "LOGO_DEV_TOKEN", "SEC_USER_AGENT", "NAV_OFFLINE", "MARKET_DATA", "SFX"):
    os.environ.pop(name, None)
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import core.audio_polish as A  # noqa: E402
import core.card_checks as K  # noqa: E402
import core.cards as C  # noqa: E402
import core.entity_enricher as E  # noqa: E402
import core.market_data as M  # noqa: E402
from core.edl import BrandKit, Card, CardRow, ClipEdit  # noqa: E402
from core.transcriber import Word  # noqa: E402


def words_from(text, start=100.0, step=0.3):
    return [Word(t, round(start + k * step, 3), round(start + k * step + 0.25, 3)) for k, t in enumerate(text.split())]


def png(draw):
    image = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    draw(image)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


# ======================= 1. entities, logos and brand colour =======================
check("Entities named in a sentence, in order: companies, $tickers, funds, markets",
      E.detect("Nvidia stock went up while Apple lagged; $TSLA, Sequoia and a16z bet on it, and inflation hit 9%")
      == ["nvidia", "apple", "tesla", "sequoia", "a16z", "inflation"])
check("...an ordinary word isn't a company (\"an apple a day\", \"meta-analysis\", \"conversion rates\")",
      E.detect("an apple a day and a meta-analysis of conversion rates") == [])
check("...and names resolve from a ticker or a name", E.resolve("NVDA").key == "nvidia" and E.resolve("Stripe").key == "stripe"
      and E.resolve("BRK-B").key == "berkshire")
check("A card wears a company's look only when its own text names exactly one",
      E.card_entity("Stripe's payment volume") == "stripe" and E.card_entity("Apple vs Google") == ""
      and E.card_entity("Where inflation went") == "")

red = png(lambda im: [im.putpixel((x, y), (229, 9, 20, 255)) for x in range(20, 108) for y in range(30, 98)])
calls = []
real_download = E._download
E._download = lambda url: calls.append(url) or red
os.environ["BRANDFETCH_CLIENT_ID"] = "test-id"
got = E.enrich("netflix")
again = E.enrich("netflix")
check("A logo is fetched once (Brandfetch, the client id in the request) and cached in the logo folder",
      len(calls) == 1 and "cdn.brandfetch.io/netflix.com/" in calls[0] and "c=test-id" in calls[0]
      and Path(got.logo_path).is_file() and got.logo_kind == "brandfetch" and again == got, f"{calls} {got and got.logo_kind}")
check("...and the renderer reads it from the cache only (a data: URI, no network)",
      E.cached("netflix").logo_uri.startswith("data:image/png;base64,") and len(calls) == 1)
check("The brand colour comes from the logo itself (Pillow quantization): Netflix red, readable on the card",
      got.color.startswith("#") and int(got.color[1:3], 16) > 200 and int(got.color[3:5], 16) < 90
      and E.contrast(got.color, C.CARD_BG) >= 4.5, got.color)
os.environ.pop("BRANDFETCH_CLIENT_ID")
os.environ["LOGO_DEV_TOKEN"] = "pk_test"
E._download = lambda url: calls.append(url) or None
fallback = E.enrich("stripe")
check("No logo from any service (logo.dev tried with its token): a local initials badge in the brand's colour",
      any("img.logo.dev/stripe.com" in u and "token=pk_test" in u for u in calls) and fallback.logo_kind == "initials"
      and Path(fallback.logo_path).is_file() and fallback.color, f"{fallback}")
os.environ.pop("LOGO_DEV_TOKEN")
E._download = real_download
black = png(lambda im: [im.putpixel((x, y), (0, 0, 0, 255)) for x in range(10, 118) for y in range(10, 118)])
navy = png(lambda im: [im.putpixel((x, y), (10, 37, 64, 255)) for x in range(10, 118) for y in range(10, 118)])
check("A black-and-white logo has no brand colour (the card keeps the client's accent)", E.dominant_color(black) is None)
lifted = E.readable(E.dominant_color(navy))
check("A dark brand colour is lifted until it reads on the card, its hue kept",
      E.contrast(lifted, C.CARD_BG) >= 4.5 and int(lifted[5:7], 16) > int(lifted[1:3], 16), lifted)
check("Macro series get a colour but never a logo tile", E.enrich("inflation").logo_uri == "" and E.cached("inflation").color)

# ======================= 2. market and macro data =======================
import pandas as pd  # noqa: E402
import yfinance  # noqa: E402

fetches = []


class FakeTicker:
    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, start, end, interval, auto_adjust):
        fetches.append((self.symbol, start, end, interval))
        days = pd.date_range(start, periods=60, freq="W", tz="America/New_York")
        return pd.DataFrame({"Close": np.linspace(5.87, 187.0, 60)}, index=days)


yfinance.Ticker = FakeTicker
today = date(2026, 11, 7)
said = words_from("And NVIDIA stock has just run since 2020. That changed everything for us.")
found = M.moments(said, 100.0, 120.0, today)
check("A stock named with a market word and no numbers is a moment: NVIDIA's price since 2020",
      [(m.key, m.kind, m.since) for m in found] == [("nvidia", "price", date(2020, 1, 1))], str(found))
check("...but not when the speaker says the numbers (the transcript's own chart draws those)",
      M.moments(words_from("NVIDIA stock went from $5 to $187 a share."), 100.0, 120.0, today) == [])
check("Named eras set the window: the dot-com bubble, 2008, COVID; 'in 2008' is that year with one either side",
      M.window("the dot-com crash", today)[0] == date(1997, 1, 1) and M.window("in 2008 it fell", today)
      == (date(2007, 1, 1), date(2009, 12, 31)) and M.window("since 2015", today) == (date(2015, 1, 1), today))
os.environ["MARKET_DATA"] = "on"
cards = M.market_cards(said, 100.0, 120.0, today)
card, why = cards[0]
check("Yahoo Finance prices (yfinance, monthly closes for a window over 6 years) become a market_line card",
      card.template == "market_line" and fetches and fetches[0][0] == "NVDA" and fetches[0][3] == "1mo"
      and card.source == "Yahoo Finance" and card.entity == "nvidia", f"{fetches} {card}")
check("...formatted for the card: first and last close, the change for the header's pill",
      [(r.label, r.value, r.highlight) for r in card.rows][1:] == [(card.rows[1].label, "$187", False), ("Change", "+3,086%", True)]
      and card.rows[0].value == "$5.87" and card.eyebrow == "NVDA · SHARE PRICE" and card.headline == "NVIDIA since 2020",
      str([(r.label, r.value) for r in card.rows]))
check("...its data saved to its own file (date, value) for the renderer", len(C.read_series(card.data_path)) == 60)
M.market_cards(said, 100.0, 120.0, today)
check("A second clip on the same moment reads the cache (no second fetch)", len(fetches) == 1, str(fetches))
os.environ["NAV_OFFLINE"] = "1"
check("Offline, the cache still answers", M.history("NVDA", date(2020, 1, 1), today)[0][1] == 5.87 and len(fetches) == 1)
os.environ.pop("NAV_OFFLINE")


class FakeAnswer:
    def __init__(self, text="", data=None):
        self.text, self._data, self.status_code = text, data, 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


import requests  # noqa: E402

real_get = requests.get
cpi = "\n".join(["observation_date,CPIAUCSL"] + [f"{2020 + m // 12}-{m % 12 + 1:02d}-01,{100 * 1.004 ** m:.3f}" for m in range(80)])
requests.get = lambda url, **kw: FakeAnswer(cpi)
inflation = M.series(M.Moment("inflation", "macro", 100.0, 103.0, date(2022, 1, 1), today, ""))
check("FRED: CPI becomes inflation, year over year (%), with its source",
      inflation.unit == "%" and abs(inflation.points[0][1] - (1.004 ** 12 - 1) * 100) < 0.01
      and inflation.source.startswith("FRED"), str(inflation.points[:2]))
facts = {"Revenues": [
    {"form": "10-K", "start": f"{y}-01-01", "end": f"{y}-12-31", "val": v} for y, v in ((2021, 10e9), (2022, 12e9), (2023, 15e9))]
    + [{"form": "10-Q", "start": "2024-01-01", "end": "2024-03-31", "val": 4e9},
       {"form": "10-K", "start": "2023-10-01", "end": "2023-12-31", "val": 4e9}]}
check("SEC EDGAR: full fiscal years from 10-K filings only (quarters left out)",
      M.annual_values(facts) == [("2021-12-31", 10e9), ("2022-12-31", 12e9), ("2023-12-31", 15e9)])
try:
    M.cik_for("AAPL")
    sec_ok = False
except M.MarketDataError as exc:
    sec_ok = "SEC_USER_AGENT" in str(exc)
check("SEC requests are refused until SEC_USER_AGENT identifies you (the SEC's rule)", sec_ok)
requests.get = real_get
check("Formatting: $5.87, $187, $1,240, 4.9%, $60.9B, $391B; changes in % or points",
      [M.fmt(5.87, "$"), M.fmt(187, "$"), M.fmt(1240, "$"), M.fmt(4.86, "%"), M.fmt(60.92, "$B"), M.fmt(391.0, "$B")]
      == ["$5.87", "$187", "$1,240", "4.9%", "$60.9B", "$391B"] and M.change(2.0, 5.3, "%") == "+3.3 pts"
      and M.change(200, 100, "$") == "−50%")
res = K.check_card(card, said, 96.0, 120.0, 3.0)
check("A market chart passes the checks with its entity named (no numbers needed: it's sourced data)",
      res.ok and any("real market data" in w for w in res.warnings), str(res.errors))
elsewhere = Card(**{**card.__dict__, "rows": list(card.rows), "start": 112.0, "end": 117.4})
res2 = K.check_card(elsewhere, words_from("We talk about other things here for a while now, " * 3, 100.0, 0.3), 100.0, 125.0, 3.0)
check("...and fails when what it shows isn't named while it's on screen", not res2.ok
      and any("isn't named" in e for e in res2.errors), str(res2.errors))
os.environ.pop("MARKET_DATA")
check("Tests never fetch market data unless they ask (MARKET_DATA unset under the test runner)", not M.enabled())

# ======================= 3. the card: bounds, source, timing, background =======================
from playwright.sync_api import sync_playwright  # noqa: E402

E.enrich("nvidia", offline=True)
page, motion, width, height, top = C.scene(card, BrandKit())
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    view = browser.new_page(viewport={"width": width, "height": height})
    html = tmp / "market.html"
    html.write_text(page)
    view.goto(html.as_uri())
    over = view.evaluate(C._SETUP, motion.js(card.end - card.start))
    view.evaluate(C._SEEK, [int(3.0 * 30), 30])
    boxes = view.evaluate("""() => ['.card', '#lw', '.ftr', '.hrow', '#mtag', '.maxis'].map(s => {
        const r = document.querySelector(s).getBoundingClientRect(); return [s, r.top, r.bottom]; })""")
    footer = view.evaluate("() => document.querySelector('.ftr').textContent")
    brand_var = view.evaluate("() => getComputedStyle(document.documentElement).getPropertyValue('--brand-accent').trim()")
    background = view.evaluate("() => getComputedStyle(document.querySelector('.card')).backgroundImage")
    browser.close()
inside = all(492 <= top + a and top + b <= 1340 for _s, a, b in boxes)
check("The market chart stays inside the card's band, y 492-1340 of the frame (every part of it)",
      not over and inside, str([(s, round(top + a), round(top + b)) for s, a, b in boxes]))
check("...with its source and period in the footer", "Source: Yahoo Finance" in footer and "monthly" in footer, footer)
check("...and NVIDIA's colour as the card's accent (--brand-accent), from its cached logo",
      brand_var.upper() == E.cached("nvidia").color.upper(), brand_var)
check("Mesh background: layered radial gradients (not a flat fill) and a 1px specular top edge",
      background.count("radial-gradient") == 3 and '<div class="spec-top"></div>' in page, background[:80])

S = 100.0
SAMPLES = {
    "metric_bars": Card("metric_bars", S, S + 4, rows=[CardRow("Payroll", "$4.2M"), CardRow("Ads", "$0.9M", True)]),
    "sankey_flow": Card("sankey_flow", S, S + 4, rows=[CardRow("Revenue", "$10M"), CardRow("Costs", "$6M", parent="Revenue"),
                                                        CardRow("Profit", "$4M", True, parent="Revenue")]),
    "step_sequence": Card("step_sequence", S, S + 4, rows=[CardRow("Find the market"), CardRow("Hire the team"),
                                                            CardRow("Scale the brand", highlight=True)]),
    "comparison_card": Card("comparison_card", S, S + 4, rows=[CardRow("Old way", "Before"), CardRow("New way", "Now", True)]),
    "kinetic_range": Card("kinetic_range", S, S + 4, rows=[CardRow("a year", "$850–$1,700", True)]),
    "market_line": card,
}
timing = {}
for name, sample in SAMPLES.items():
    sample.start = S if name != "market_line" else sample.start
    K.pace(sample, 1000.0, [])
    settle = C.settle_time(sample)
    still = sample.end - sample.start - settle - C.EXIT
    timing[name] = (round(settle, 2), round(still, 2), round(sample.end - sample.start, 2))
check("Entrance and morph done by 1.2 s, for every kind of card (Vizzu / Motion Canvas choreography)",
      all(s <= C.ENTRANCE_END + 1e-6 for s, _h, _d in timing.values()), str(timing))
check("...then completely still for 3.8 s or more before it leaves, on screen 4.5-6 s",
      all(h >= K.READ_HOLD - 1e-6 and 4.5 <= d <= 6.0 for _s, h, d in timing.values()), str(timing))
slow = SAMPLES["sankey_flow"]
_page, warped, *_ = C.scene(slow, BrandKit())
a, b, k, delta = warped.warp
check("A chart whose own choreography runs long is played faster, in proportion, to land by 1.2 s (the time-warp)",
      k > 1 and b == C.ENTRANCE_END and abs(warped.real(warped.story(0.9)) - 0.9) < 1e-9 and abs(warped.real(warped.last()) - 1.2) < 1e-6,
      str(warped.warp))
check("Entrances: spring rise (Remotion) and cubic-bezier(0.16, 1, 0.3, 1) growth",
      C.EASE == "cubic-bezier(0.16, 1, 0.3, 1)" and C.SPRING.startswith("linear("))
frames = C.render_card(SAMPLES["metric_bars"], BrandKit(), tmp / "cards")
settled = int(np.ceil(C.settle_time(SAMPLES["metric_bars"]) * C.FPS)) + 2
inode = {os.stat(frames.pattern % i).st_ino for i in range(settled, frames.count)}
check("Rendered frames: after 1.2 s every frame is the same image until the card leaves (truly still)",
      len(inode) == 1, f"{len(inode)} different frames after frame {settled}")

# ======================= 4. micro sound effects =======================
whoosh, tick = A.sound("whoosh"), A.sound("tick")
peak = lambda s: 20 * np.log10(np.max(np.abs(s)))  # noqa: E731
check("Whoosh: 180 ms, -22 dBFS; tick: 40 ms, -26 dBFS (cached as .wav)",
      abs(len(whoosh) / A.RATE - 0.18) < 1e-3 and abs(peak(whoosh) + 22) < 0.1 and abs(len(tick) / A.RATE - 0.04) < 1e-3
      and abs(peak(tick) + 26) < 0.1 and (tmp / "sfx" / "whoosh.wav").is_file(), f"{peak(whoosh):.2f} {peak(tick):.2f}")
spectrum = np.abs(np.fft.rfft(whoosh))
freqs = np.fft.rfftfreq(len(whoosh), 1 / A.RATE)
check("...the whoosh is low-end (over 90% of its energy under 1 kHz)", (spectrum[freqs < 1000] ** 2).sum() > 0.9 * (spectrum ** 2).sum())
steps = SAMPLES["step_sequence"]
cues = C.cues(steps)
check("A step sequence: a whoosh as it comes in, a tick as each step box lights up (warped like its frames)",
      cues[0] == (0.0, "whoosh") and [kind for _t, kind in cues].count("tick") == 3
      and all(t <= C.ENTRANCE_END for t, _k in cues), str(cues))
check("A comparison: one tick, as the side the point lands on lights up", [k for _t, k in C.cues(SAMPLES["comparison_card"])]
      == ["whoosh", "tick"])
check("Name cards make no sound", C.cues(Card("speaker_title", S, S + 3.3, rows=[CardRow("Dana Reyes")])) == [])
track = A.read_wav(A.cue_track([(0.5, "whoosh"), (1.25, "tick"), (2.0, "tick")], 3.0, tmp / "cues.wav"))
exact = all(np.allclose(track[int(t * A.RATE):int(t * A.RATE) + len(s)], s, atol=1 / 32767)
            and np.max(np.abs(track[int(t * A.RATE) - 50:int(t * A.RATE)])) == 0
            for t, s in ((0.5, whoosh), (1.25, tick), (2.0, tick)))
check("The cue track puts each sound on its exact sample (silence up to it, the sound from it)", exact)
edit = ClipEdit(100.0, 112.0, cards=[Card("step_sequence", 104.0, 109.4, rows=[CardRow("A one"), CardRow("B two"),
                                                                                  CardRow("C three", highlight=True)])])
spaced = A.clip_cues(edit, [(4.0, 9.4)])
gaps = [b2 - a2 for (a2, k1), (b2, k2) in zip(spaced, spaced[1:]) if k1 == k2 == "tick"]
check("In a clip: cues sit at the graphic's on-screen start, and ticks are never closer than 0.14 s",
      spaced[0] == (4.0, "whoosh") and all(g >= A.MIN_TICK_GAP - 1e-9 for g in gaps), str(spaced))

# The whole path: a clip with a split chart, its sound mixed in FFmpeg. Silent clip audio, so
# every sound heard is a cue: they must land on the right samples, and not one frame may move.
from core.dynamic_layout import render_with_cards  # noqa: E402

source, base = tmp / "source.mp4", tmp / "base.mp4"
for path, size in ((source, "1920x1080"), (base, "1080x1920")):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=30:d=12", "-f", "lavfi",
                    "-i", "anullsrc=r=48000:cl=mono", "-t", "12", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)
said = words_from("Okay so here is the plan for all of you today. First we find the market and then we hire the team "
                  "and then we scale the brand together and that is how it works every single time for us here", 100.0, 0.3)
steps_edit = ClipEdit(100.0, 112.0, cards=[Card("step_sequence", 104.5, 109.9, rows=[
    CardRow("Find the market"), CardRow("Hire the team"), CardRow("Scale the brand", highlight=True)])])
made, results = render_with_cards(source, steps_edit, said, tmp / "with_sfx.mp4", tmp / "work", lambda kept: base,
                                  hook_seconds=3.0)
check("The clip renders with its graphic", made is not None and all(r.ok for r in results), str([r.errors for r in results]))


def probe(path, entries, stream):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-count_frames", "-show_entries", entries,
                          "-of", "json", str(path)], capture_output=True, text=True).stdout
    return json.loads(out)["streams"][0]


video_in, video_out = probe(base, "stream=nb_read_frames", "v"), probe(made, "stream=nb_read_frames", "v")
check("Not one frame moves: the same number of video frames in and out (-fps_mode passthrough)",
      video_in["nb_read_frames"] == video_out["nb_read_frames"], f"{video_in} {video_out}")
pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(made), "-map", "0:a", "-ac", "1", "-ar", str(A.RATE), "-f", "s16le",
                      "-"], capture_output=True, check=True).stdout
heard = A.onsets(np.frombuffer(pcm, dtype="<i2").astype(float) / 32767, threshold_db=-45.0)
from core.dynamic_layout import windows_of  # noqa: E402

expected = [t for t, _k in A.clip_cues(steps_edit, windows_of(steps_edit))]
check("The sounds land on their cues in the finished clip (within a frame, 33 ms: no drift)",
      len(heard) == len(expected) and all(abs(h - e) < 1 / 30 for h, e in zip(heard, expected)),
      f"heard {heard}, expected {expected}")
os.environ["SFX"] = "off"
quiet, _r = render_with_cards(source, steps_edit, said, tmp / "no_sfx.mp4", tmp / "work", lambda kept: base, hook_seconds=3.0)
pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(quiet), "-map", "0:a", "-f", "s16le", "-"], capture_output=True).stdout
check("SFX=off in .env: the clip's own audio, untouched", np.max(np.abs(np.frombuffer(pcm, dtype="<i2"))) == 0)
os.environ.pop("SFX")
print("\nAll checks passed." if not fails else f"\n{fails} check(s) failed.")
sys.exit(1 if fails else 0)
