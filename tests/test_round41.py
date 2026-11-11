"""Round 41 checks: the Nav Studios signature edit, "visual proof, not text summaries".

Pillar 1: the split-screen gate (an explicit figure or a tight A-vs-B contrast, at most
1 per clip; 0 on conversational talk). Pillar 2: the floating proof cards (brand logo
pill, Wikipedia card, a figure rolling up) over the 100 % full-screen 1080x1920 speaker,
in the 1320-1580 band. Pillar 3: the newspaper tear-out and its word-synced highlighter.
Pillar 4: audio-emphasis punch-ins 1.00x -> 1.10x and One-Euro smoothing. Pillar 5: the
palettes on the new cards, and the Round 40 teaser alongside them.

Run from the project root (it takes well under a minute):
    timeout 60 .venv/bin/python tests/test_round41.py .
"""
import colorsys
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
tmp = Path(tempfile.mkdtemp(prefix="round41-"))
os.environ.update({"NAV_NO_CLIP_PAGES": "1", "NAV_LOGO_DIR": str(tmp / "logos"), "NAV_SFX_DIR": str(tmp / "sfx"),
                   "NAV_OFFLINE": "1", "NAV_NICHE_CACHE": str(tmp / "niche")})
for name in ("SIGNATURE_EDIT", "ENABLE_CLIMAX_HOOK", "PALETTE_MODE", "WIKI_PROOF", "SFX"):
    os.environ.pop(name, None)
os.environ.update({"CAMERA_FLOW": "0", "OPENING_HOOK": "0"})  # later rounds' editing off: this suite checks its own round's
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


import core.camera_energy as E  # noqa: E402
import core.cards as C  # noqa: E402
import core.palettes as P  # noqa: E402
import core.proof_cards as PC  # noqa: E402
import core.visual_proof as V  # noqa: E402
from core.edl import MODES, BrandKit, Card, CardRow, ClipEdit  # noqa: E402
from core.transcriber import Word  # noqa: E402


def words_from(text, start=0.0, step=0.36):
    out, t = [], start
    for token in text.split():
        out.append(Word(token, round(t, 3), round(t + step - 0.03, 3)))
        t += step
    return out


try:
    import playwright  # noqa: F401
    BROWSER = True
except ImportError:
    BROWSER = False

# ======================================= Pillar 1: the split-screen gate =======================================
check("max_split_cards_per_clip = 1", V.MAX_SPLIT_CARDS_PER_CLIP == 1 and V.DICHOTOMY_SECONDS == 5.0)
check("Explicit figures qualify: \"300%\", \"$50M\", \"3x\"",
      all(V.quantitative(words_from(t)) for t in ("revenue grew 300% that year", "we raised $50M from them",
                                                  "it was 3x faster than before")))
check("...conversational talk doesn't (no number, or a number said in passing)",
      not V.quantitative(words_from("we talked about culture and how the team felt about the move"))
      and not V.quantitative(words_from("I was there two times last year with my brother")))
check("A tight A-vs-B dichotomy within 5.0 s qualifies (\"marketing versus product\", \"not hype but trust\")",
      V.dichotomy(words_from("it's marketing versus product in the end")) and V.dichotomy(words_from("it is not hype but trust")))
check("...a contrast stretched over more than 5 s doesn't",
      not V.dichotomy(words_from("it is not " + "really something that we ever thought about at all " * 2 + "but trust")))

CONVO = words_from("I think the culture of a team matters a lot and we spent years learning how to hire people "
                   "who care about the work and about each other every single day", 10.0)
comparison = Card("comparison_card", 14.0, 19.0, rows=[CardRow("Before", detail="hire fast", reveal_at=14.2),
                                                        CardRow("Now", detail="hire slow", reveal_at=17.0)])
steps = Card("step_sequence", 20.0, 25.0, rows=[CardRow("Hire", reveal_at=20.1), CardRow("Care", reveal_at=21.0)])
buckets = Card("buckets", 12.0, 16.5, rows=[CardRow("Culture"), CardRow("Hiring")])
kept, notes = V.gate_splits([comparison, steps, buckets], CONVO)
check("Conversational segment: zero split-screen cards, and the text-summary boxes give way to proof cards",
      kept == [] and sum("refused" in n for n in notes) == 2 and any("text summary" in n for n in notes), str(notes))
NUMS = words_from("Payroll was $4 million and hosting was $1 million and ads were $2 million last year. Then Japan "
                  "grew 30% while the US grew 12% and that changed the whole plan for us", 10.0)
bars = Card("metric_bars", 10.0, 15.5, rows=[CardRow("Payroll", "$4M"), CardRow("Hosting", "$1M"), CardRow("Ads", "$2M")])
ranked = Card("ranked_bars", 17.0, 22.5, rows=[CardRow("Japan", "30%"), CardRow("US", "12%")])
kept, notes = V.gate_splits([bars, ranked, comparison], NUMS)
check("Numbers said: they qualify, but at most 1 split-screen card per clip (the first figure)",
      [c.template for c in kept] == ["metric_bars"] and any("at most 1 per clip" in n for n in notes), str(notes))
versus = words_from("so it really comes down to renting versus owning for most people today", 14.0)
check("A tight A-vs-B contrast keeps its comparison card (one), its sides said within 5 s",
      [c.template for c in V.gate_splits([comparison], versus)[0]] == ["comparison_card"])

import core.graphics_director as G  # noqa: E402

TALK = ("So I want to tell you about how we started this whole thing back in the day with almost nothing. "
        "When we launched Oakberry we had one store and a lot of ambition, and nobody believed in us. "
        "Warren Buffett once said that the best investment is in yourself, and I took that literally. "
        "We grew revenue 300% in two years while everyone told us to slow down and play it safe. "
        "The real lesson is that speed always beats perfection, every single time you build something new. "
        "Nike did the same thing in the seventies, and that's why they won the whole market.")
talk = words_from(TALK, 100.0)
end = round(talk[-1].end + 0.5, 3)
WIKI = {"Warren Buffett": {"title": "Warren Buffett", "description": "American investor and philanthropist (born 1930)",
                           "extract": "", "image": ""}}
V_lookup = V.wiki_lookup
V.wiki_lookup = lambda name: WIKI.get(name)
INFO = G.VideoInfo(title="How Oakberry grew", channel="Founders", description="")
chatty = words_from(" ".join(["we talked about the team and the people and how it felt to build it together"] * 4) + ".", 100.0)
conv = G.direct(chatty, 100.0, round(chatty[-1].end + 0.5, 3), info=INFO, hook_seconds=4.0, ask=lambda p, s: None)
check("The director on conversational talk: 0 split-screen cards, the speaker full-screen",
      not any(MODES[c.template] == "split" for c in conv.edit.cards) and any("full-screen 100%" in n for n in conv.notes),
      str(conv.notes[-3:]))
d = G.direct(talk, 100.0, end, info=INFO, hook_seconds=4.0, ask=lambda p, s: None)
kinds = [c.template for c in d.edit.cards]
check("The director on a story with a brand, a person, a figure and a thesis: proof cards, not text boxes",
      {"brand_proof", "wiki_proof", "stat_proof", "newspaper_tearout"} <= set(kinds)
      and not set(kinds) & V.TEXT_OVERLAYS and sum(MODES[k] == "split" for k in kinds) <= 1, str(kinds))
check("...every proof card lands on its own word (brand, name, figure), clear of the title and the payoff",
      all(c.start >= 104.0 - 1e-6 and c.end <= end - 2.0 + 1e-6 for c in d.edit.cards)
      and abs(next(c for c in d.edit.cards if c.template == "brand_proof").start
              - (next(w for w in talk if w.text == "Oakberry").start - V.PILL_LEAD)) < 1e-6, str([(c.template, c.start) for c in d.edit.cards]))
check("...never two at once (1.5 s of plain video between graphics)",
      all(b.start >= a.end + 1.5 - 1e-6 for a, b in zip(d.edit.cards, d.edit.cards[1:])))
os.environ["SIGNATURE_EDIT"] = "0"
old = G.direct(talk, 100.0, end, info=INFO, hook_seconds=4.0, ask=lambda p, s: None)
os.environ.pop("SIGNATURE_EDIT")
check("SIGNATURE_EDIT=0: the Round 40 director, untouched", not any(c.template in PC.PROOF_TEMPLATES for c in old.edit.cards))

# ======================================= Pillar 2: floating proof cards =======================================
brand_card = next(c for c in d.edit.cards if c.template == "brand_proof")
wiki_card = next(c for c in d.edit.cards if c.template == "wiki_proof")
stat_card = next(c for c in d.edit.cards if c.template == "stat_proof")
tear_card = next(c for c in d.edit.cards if c.template == "newspaper_tearout")
check("Brand proof: the company, its industry tag, a one-line badge (its domain without Wikipedia), on its word",
      brand_card.rows[0].label == "Oakberry" and brand_card.entity == "oakberry" and brand_card.eyebrow == "Private company"
      and brand_card.rows[0].detail == "oakberry.com")
check("Wikipedia card: the page's title and its one-line description",
      (wiki_card.rows[0].label, wiki_card.rows[0].detail) == ("Warren Buffett", "American investor and philanthropist (born 1930)"))
check("Stat pill: the figure as said, its own phrase, rolling from the word it's said on",
      (stat_card.rows[0].value, stat_card.rows[0].label) == ("300%", "in two years")
      and abs(stat_card.context["at"] - next(w for w in talk if w.text == "300%").start) < 1e-9)
check("Every pill is up 2.4-3.2 s", all(2.4 <= c.end - c.start <= 3.2 for c in (brand_card, wiki_card, stat_card)))
calls = []
V.wiki_lookup = V_lookup


class FakeAnswer:
    def __init__(self, status, payload=None, content=b""):
        self.status_code, self._payload, self.content = status, payload, content

    def json(self):
        return self._payload


import requests  # noqa: E402

real_get = requests.get


def fake_get(url, **kw):
    calls.append(url)
    if "page/summary/Ada_Lovelace" in url:
        return FakeAnswer(200, {"type": "standard", "title": "Ada Lovelace", "description": "English mathematician (1815–1852)",
                                "extract": "Augusta Ada King was an English mathematician.",
                                "thumbnail": {"source": "https://upload.wikimedia.org/ada.jpg"}})
    if url.endswith("ada.jpg"):
        return FakeAnswer(200, content=b"\xff\xd8\xff fake jpeg")
    return FakeAnswer(404, {})


os.environ.pop("NAV_OFFLINE")
requests.get = fake_get
try:
    found = V.wiki_lookup("Ada Lovelace")
    again = V.wiki_lookup("Ada Lovelace")
    missing = V.wiki_lookup("Nobody Atall")
finally:
    requests.get = real_get
    os.environ["NAV_OFFLINE"] = "1"
check("Wikipedia: the REST summary API by slug, its thumbnail.source saved, cached (one fetch each)",
      found and found["description"] == "English mathematician (1815–1852)" and Path(found["image"]).is_file()
      and calls[0] == "https://en.wikipedia.org/api/rest_v1/page/summary/Ada_Lovelace" and again == found
      and sum("Ada_Lovelace" in c for c in calls) == 1 and missing is None, str(calls))

BG = tmp / "speaker.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=16", "-f", "lavfi", "-i",
                "sine=f=220:d=16", "-shortest", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
                str(BG)], check=True)


def frame_at(path, t, y0=0, rows=1920):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-vf",
                          f"crop=1080:{rows}:0:{y0}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(rows, 1080, 3).astype(int)


if BROWSER:
    for card in (brand_card, wiki_card, stat_card):
        frames = C.render_card(card, BrandKit(), tmp / "cards")
        x, y, w, h, r = frames.blur[0]
        check(f"{card.template}: drawn over the full 1080x1920 frame; 860 wide, 220-280 tall, inside y 1320-1580, 20 px corners",
              (frames.width, frames.height, frames.top) == (1080, 1920, 0) and w == 860 and 220 <= h <= 280
              and y >= 1320 and y + h <= 1580 and r == 20 and x == 110, str(frames.blur[0]))
    motion = C.scene(stat_card, BrandKit())[1]
    roll = [s for s in motion.spans if s[1] - s[0] > 0.4]
    check("The stat pill's figure rolls up in 0.45 s, landing on its word (0% -> 300%)",
          any(abs((s[0] + 0.12) - (stat_card.context["at"] - stat_card.start)) < 0.02 and abs(s[1] - s[0] - 0.45) < 1e-6 for s in roll),
          str(roll))
    entry = [kf for kf in motion.js(2.8) if kf["sel"] == "#pf" and "transform" in kf["kf"][1]]
    check("...and every pill springs up into place (a slide-up entry; the overlay's fade-out exit)",
          entry and "translateY(70px)" in json.dumps(entry) and "linear(" in json.dumps(entry))

    # over a real full-screen clip, through the compositor
    import core.dynamic_layout as D

    WORDS = words_from("When we launched Oakberry we had one store and nobody believed in us at all. "
                       "The real lesson is that speed always beats perfection, every single time you build.", 3.0)
    tear_b = V.tearout_moment(WORDS, 3.0, 14.0).card
    brand_b = V.brand_moments(WORDS, lambda n: None)[0].card
    edit = ClipEdit(start=0.0, end=16.0, cards=[brand_b, tear_b], brand=BrandKit())
    seen = {}
    real_compose = D.compose_cards

    def spy(base, source, keep, frames, calm, output, *a, **k):
        seen["calm"] = calm
        return real_compose(base, source, keep, frames, calm, output, *a, **k)

    D.compose_cards = spy
    try:
        def base(kept):
            seen["bold"] = kept
            return BG

        out, checks = D.render_with_cards(BG, edit, WORDS, tmp / "proof.mp4", tmp / "work", base, hook_seconds=3.0,
                                          layout="smart_crop")
    finally:
        D.compose_cards = real_compose
    check("Composited over the full-screen clip (both cards pass the renderer's checks)", out is not None and all(c.ok for c in checks),
          str([c.errors for c in checks]))
    mid = (brand_b.start + brand_b.end) / 2
    shot, plain = frame_at(out, mid), frame_at(BG, mid)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
    check("The speaker stays 100% full-screen (1080x1920): above the pill the picture is the clip's own",
          probe == "1080,1920" and np.abs(shot[:1250] - plain[:1250]).mean() < 6, f"{probe}, Δ {np.abs(shot[:1250] - plain[:1250]).mean():.2f}")
    check("...and the brand pill is in the lower-third band (1320-1580), not over the face",
          np.abs(shot[1340:1560, 140:940] - plain[1340:1560, 140:940]).mean() > 25)
    t_mid = tear_b.start + 1.2
    torn, under = frame_at(out, t_mid), frame_at(BG, t_mid)
    check("The tear-out dims the speaker behind it (and blurs it), the newsprint over the lower/centre third",
          torn[200:800].mean() < 0.8 * under[200:800].mean()
          and np.abs(np.median(torn[1100:1180, 300:800].reshape(-1, 3), axis=0) - np.array([253, 251, 247])).max() < 12,
          f"{torn[200:800].mean():.0f} vs {under[200:800].mean():.0f}; paper {np.median(torn[1100:1180, 300:800].reshape(-1, 3), axis=0)}")
    check("...and no caption runs under it (its headline is the caption)",
          not any(D._during(w, tear_b) for w in seen.get("calm", [])) and not any(D._during(w, tear_b) for w in seen.get("bold", [])))
else:
    print("(Playwright not installed: the rendered proof-card checks were skipped)")

# ======================================= Pillar 3: the newspaper tear-out =======================================
key = tear_card.context["key"]
check("Tear-out: the thesis line as the headline, its key 4-6 words, 1.8-2.5 s, on newsprint with a masthead",
      tear_card.rows[0].label.startswith("The real lesson is that speed always beats perfection") and 4 <= len(key) <= 6
      and 1.8 <= tear_card.end - tear_card.start <= 2.5 and "//" in tear_card.eyebrow
      and PC.NEWSPRINT == "#FDFBF7" and PC.MARKER == "#FDE047" and PC.MARKER_OPACITY == 0.65 and PC.TEAR_TILT == -1.5,
      f"{tear_card.rows[0].label} / {[k[0] for k in key]}")
said = {w.text.strip(".,"): w for w in talk}
check("...each key word is the Whisper word itself, at its own timestamps",
      all(abs(said[t].start - s) < 1e-9 and abs(said[t].end - e) < 1e-9 for t, s, e in key))
tracks = {kf["sel"]: kf for kf in C.scene(tear_card, BrandKit())[1].js(tear_card.end - tear_card.start)}
total = tear_card.end - tear_card.start
sweeps = []
for j, (_t, s, e) in enumerate(key):
    frames_kf = tracks[f"#mk{j}"]["kf"]
    sweeps.append((frames_kf[1]["offset"] * total, frames_kf[2]["offset"] * total))
check("The marker sweeps left to right, word by word: each word's stroke runs exactly while it's said",
      all(abs(a - (s - tear_card.start)) < 0.002 and abs(b - (e - tear_card.start)) < 0.002 for (a, b), (_t, s, e) in zip(sweeps, key))
      and all(b1 <= a2 + 1e-6 for (_a1, b1), (a2, _b2) in zip(sweeps, sweeps[1:])), str(sweeps))
page = C.scene(tear_card, BrandKit())[0]
check("...as a scaleX stroke from the left edge, #FDE047 at 65 % multiply, over a deckled torn edge and a 1.5° tilt",
      "transform-origin: 0 50%" in page and "mix-blend-mode: multiply" in page and "#FDE047" in page and "opacity: 0.65" in page
      and "mask-image" in page and "rotate(-1.5deg)" in page and "feTurbulence" in page)
if BROWSER:
    tear_frames = C.render_card(tear_card, BrandKit(), tmp / "cards")
    from PIL import Image

    def yellow(index):
        img = np.asarray(Image.open(tear_frames.pattern % min(index, tear_frames.count - 1)).convert("RGBA")).astype(int)
        img = img[PC.TEAR_TOP + 130:PC.TEAR_TOP + PC.TEAR_H]  # the headline, clear of the masthead rule (even as it slides in)
        r, g, b, a = img[..., 0], img[..., 1], img[..., 2], img[..., 3]
        return int(((r > 200) & (g > 180) & (b < 150) & (a > 200)).sum())

    times = [key[0][1] - 0.15] + [(s + e) / 2 for _t, s, e in key] + [key[-1][2] + 0.1]
    amounts = [yellow(round((t - tear_card.start) * 30)) for t in times]
    check("Rendered: no highlight before the first key word, then more with every word said, all of it seen before the exit",
          amounts[0] < 50 and all(b > a for a, b in zip(amounts, amounts[1:])), str(amounts))

# ======================================= Pillar 4: camera energy =======================================
spoken = words_from("This is a plain opening line for the test. Here comes the BIG claim that matters a lot to us. "
                    "Then we go quiet again for a while and talk. And here is ANOTHER loud moment for the clip now. "
                    "Then the clip ends very calmly here today.", 0.0, 0.33)
loud = [(-12.0 if w.text in ("BIG", "ANOTHER") else -30.0) for w in spoken]
duration = spoken[-1].end + 1.0
spans = E.emphasis_spans(spoken, loud, duration)
big = next(w for w in spoken if w.text == "BIG")
check("Emphasis: the loudest words (top 20 %) get a punch-in that lands on the word",
      spans and any(abs(a - (big.start - E.RAMP)) < 1e-3 for a, _b in spans), str(spans))
ends = [w.end for k, w in enumerate(spoken) if E._sentence_end(spoken, k)]
check("...each held 2.0-3.5 s and cut back on a sentence boundary",
      all(2.0 - 1e-6 <= b - a <= 3.5 + 1e-6 for a, b in spans)
      and all(any(abs(b - (e + 0.015)) < 0.02 for e in ends) for a, b in spans), str(spans))
check("...never under a split card, a tear-out or the hook's pull-in (excluded spans), and apart",
      E.emphasis_spans(spoken, loud, duration, exclude=[(big.start - 1, big.start + 1)]) != spans
      and all(b1 + E.MIN_GAP <= a2 for (_a1, b1), (a2, _b2) in zip(spans, spans[1:])))
check("The curve: 1.00x -> 1.10x in 0.22 s (cubic ease-out), held, then straight back to 1.00x",
      E.PUNCH == 1.10 and E.RAMP == 0.22 and abs(E.zoom_at(spans[0][0] + 0.22, spans) - 1.10) < 1e-9
      and 1.0 < E.zoom_at(spans[0][0] + 0.05, spans) < 1.10 and E.zoom_at(spans[0][1], spans) == 1.0
      and E.zoom_at(spans[0][0] + 0.05, spans) - 1.0 > 0.5 * (0.10 * 0.05 / 0.22))
rate = 16000
audio = np.zeros(int(rate * duration))
for w in spoken:
    a, b = int(w.start * rate), int(w.end * rate)
    audio[a:b] = (0.6 if w.text in ("BIG", "ANOTHER") else 0.05) * np.sin(np.arange(b - a) * 2 * np.pi * 180 / rate)
measured = E.loudness_of(audio, spoken)
check("...measured from the audio itself (each word's RMS): the loud words are the top 20 %",
      sorted(range(len(spoken)), key=lambda k: -measured[k])[:2] == sorted(k for k, w in enumerate(spoken) if w.text in ("BIG", "ANOTHER"))
      or set(sorted(range(len(spoken)), key=lambda k: -measured[k])[:2]) == {k for k, w in enumerate(spoken) if w.text in ("BIG", "ANOTHER")})

from core.face_tracker import CropBox, PULL_IN_CENTER_Y  # noqa: E402
from core.video_renderer import render_clip  # noqa: E402

src = tmp / "box.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x303030:s=960x540:r=30:d=7",
                "-f", "lavfi", "-i", "sine=f=200:d=7", "-shortest", "-vf", "drawbox=x=240:y=230:w=480:h=80:color=white:t=fill",
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
ass = tmp / "empty.ass"
ass.write_text("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\nFormat: Layer, Start, End, Style, "
               "Name, MarginL, MarginR, MarginV, Effect, Text\n")
punch = [(2.0, 4.5)]
out = render_clip(src, ass, CropBox(0, 0, 2, 2), 0.0, 7.0, tmp / "punch.mp4", layout_mode="blurred_background", emphasis=punch)
row_y = 656 + int(270 * 608 / 540)  # the box's middle row at 1.0x


def width_at(t):
    zoom = E.zoom_at(t, punch)
    y = int(1920 * PULL_IN_CENTER_Y + (row_y - 1920 * PULL_IN_CENTER_Y) * zoom)
    line = frame_at(out, t, y - y % 2, 2)[0]
    return int((line.mean(axis=1) > 180).sum())


widths = {t: width_at(t) for t in (1.0, 2.1, 3.0, 4.4, 4.6)}
check("Rendered punch-in: 1.00x before, easing in, 1.10x held, and straight back to 1.00x after the boundary",
      abs(widths[1.0] - 540) <= 4 and widths[1.0] < widths[2.1] < widths[3.0] and abs(widths[3.0] / widths[1.0] - 1.10) < 0.015
      and abs(widths[4.4] / widths[1.0] - 1.10) < 0.015 and abs(widths[4.6] - widths[1.0]) <= 4, str(widths))

rng = np.random.default_rng(41)
nods = [(k * 0.5, 960 + float(rng.normal(0, 14))) for k in range(40)]
smooth = E.one_euro(nods)
check("One-Euro: micro head-nods are smoothed hard (2.5x less jitter) ...",
      np.std([v for _t, v in smooth[5:]]) < np.std([v for _t, v in nods[5:]]) / 2.5,
      f"{np.std([v for _t, v in nods[5:]]):.1f} -> {np.std([v for _t, v in smooth[5:]]):.1f} px")
from core import camera_crop  # noqa: E402

walk = [(k * 0.5, 960.0) for k in range(8)] + [(4 + k * 0.5, 1360.0 + float(rng.normal(0, 10))) for k in range(16)]
jitter = [(k * 0.5, 960 + float(rng.normal(0, 60))) for k in range(40)]
check("...so the crop never moves on a nod, and still follows a real move",
      camera_crop.reframes(jitter, 960 - 304, 608, 1920, 0.0) == () and len(camera_crop.reframes(walk, 960 - 304, 608, 1920, 0.0)) == 1,
      str(camera_crop.reframes(jitter, 960 - 304, 608, 1920, 0.0)))
pipeline_src = (ROOT / "core" / "pipeline.py").read_text()
check("Every Make clips and worker render gets its punch-ins (core.pipeline.camera_punches -> render_clip(emphasis=))",
      "emphasis=emphasis" in pipeline_src and "camera_punches(" in pipeline_src)

# ======================================= Pillar 5: palettes and the teaser =======================================


def colours(text):
    found = [m.upper() for m in re.findall(r"#[0-9A-Fa-f]{6}(?![0-9A-Za-z])", text)]
    found += ["#{:02X}{:02X}{:02X}".format(*map(int, m)) for m in re.findall(r"rgba\((\d+),(\d+),(\d+),", text.replace(" ", ""))]
    return found


def reddish(color):
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in P.rgb(color)))
    return (h < 0.045 or h > 0.92) and s > 0.35 and 0.15 < l < 0.9


nyt, bb = BrandKit(palette=P.NYT_ARCHIVAL.to_dict()), BrandKit(palette=P.BLOOMBERG_OBSIDIAN.to_dict())
nyt_pages = {c.template: C.scene(c, nyt)[0] for c in (brand_card, wiki_card, stat_card, tear_card)}
check("nyt_archival proof cards: a parchment card, ink type, slate body, zero red",
      all("rgba(253,251,247,0.95)" in nyt_pages[t] and "#111111" in nyt_pages[t] for t in PC.PILL_TEMPLATES)
      and not any(reddish(c) for p in nyt_pages.values() for c in colours(p)))
bb_stat = C.scene(stat_card, bb)[0]
check("bloomberg_obsidian: the carbon card with its amber figure", "rgba(13,15,18,0.95)" in bb_stat and "#F59E0B" in bb_stat)
check("The tear-out is newsprint on every palette, its masthead rule in the palette's accent (ink on nyt)",
      all("#FDFBF7" in C.scene(tear_card, k)[0] for k in (BrandKit(), nyt, bb))
      and "border-top: 3px solid #111111" in nyt_pages["newspaper_tearout"]
      and "border-top: 3px solid #F59E0B" in C.scene(tear_card, bb)[0])
studio = P.adaptive("#2E5E8C")
check("studio_adaptive: the pill wears the sampled card, its text AAA",
      PC.look(BrandKit(palette=studio.to_dict()))["header"] == studio.header and P.contrast(studio.header, studio.bg) >= 7)

import core.climax_hook as H  # noqa: E402

story = words_from("So we spent the first two years building the wrong product for the wrong people, and we burned most "
                   "of the money. Our investors were patient, revenue grew 40% that year, the market was there, and still nothing "
                   "worked for us. Then one Tuesday a customer called me. And what nobody tells you about product market "
                   "fit is that it feels like being pulled, not pushing, and every metric moves at once. That is when we "
                   "knew we had it.", 2.0, 0.38)
teaser = H.find_climax(story, 2.0, story[-1].end + 0.4, rate=30.0)
proofs = V.plan(story, 2.0, story[-1].end + 0.4, 4.0, [], lambda n: None)
check("The Round 40 teaser still opens the clip (2.5-4.0 s from 15 s in, cut on the hinge), next to the proof cards",
      teaser is not None and 2.5 <= teaser.offset <= 4.0 and teaser.start - 2.0 >= 15 and teaser.hinge.endswith("is that")
      and proofs and all(c.card.start >= 2.0 + 4.0 for c in proofs))
offset = H.shift_edit(ClipEdit(start=2.0, end=story[-1].end + 0.4, cards=[m.card for m in proofs]), teaser)
check("...and every proof card's trigger (and each highlighted word) moves by +T_teaser in the finished file",
      all(abs(c.start - (m.card.start - 2.0 + teaser.offset)) < 1e-6 for c, m in zip(offset.cards, proofs)))
run_all = (ROOT / "tests" / "run_all.py").read_text()
check("Older suites run with the editing they were written for (SIGNATURE_EDIT=0 before round 41)",
      'env["SIGNATURE_EDIT"] = "0"' in run_all)

print("\nAll Round 41 checks passed." if not fails else f"\n{fails} check(s) failed")
sys.exit(1 if fails else 0)
