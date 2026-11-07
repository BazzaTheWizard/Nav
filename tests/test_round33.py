"""Round 33 checks: the comparison card renders (a serif headline's glyphs past a tight
line box aren't overflow), text that doesn't fit costs the headline or a line instead of
the visual, the comparison card's new layout (words centred, an arrow for then -> now),
bigger type for short bar charts, timelines and step sequences, numbers kept green in the
captions (years aren't), and the kinetic range's even dash.
Temp files only: no AI, YouTube, R2 or network.

Run from the project root:  .venv/bin/python tests/test_round33.py .
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["NAV_NO_CLIP_PAGES"] = "1"
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


import core.cards as C  # noqa: E402
import core.dynamic_layout as D  # noqa: E402
from core.edl import BrandKit, Card, CardRow  # noqa: E402
from core.subtitle_engine import NUMBER_COLOUR, create_ass_subtitles, is_figure  # noqa: E402
from core.transcriber import Word  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="round33-"))
S = 100.0


def row(label, value="", hl=False, at=None, **kw):
    return CardRow(label, value, highlight=hl, reveal_at=None if at is None else S + at, **kw)


# ---------- 1. the comparison card renders ----------
then_now = Card("comparison_card", S, S + 4, headline="Marketing in growth", rows=[
    row("Venture firms", "Before", at=0.8, detail="didn't market themselves"),
    row("Then we did", "Now", True, 2.0, detail="and it worked")])
them_us = Card("comparison_card", S, S + 4, rows=[
    row("Human driver", "", at=0.8, detail="we're still debating whether it's safe"),
    row("Autonomous cars", "14x safer", True, 2.0, detail="the data already says it")])
try:
    drawn = [C.render_card(card, BrandKit(), tmp / "cards") for card in (then_now, them_us)]
    check("The comparison card renders (Round 32's 'Too long to fit: Venture firms' is gone)", all(f.count > 0 for f in drawn))
except Exception as exc:  # noqa: BLE001
    check("The comparison card renders (Round 32's 'Too long to fit: Venture firms' is gone)", False, str(exc)[:200])
check("...a real overflow (a whole line too many) is still caught", "slack" in C._SETUP and "tall(el)" in C._SETUP)
page_then, page_vs = C.scene(then_now, BrandKit())[0], C.scene(them_us, BrandKit())[0]
check("Then -> now gets an arrow between the sides; them vs us keeps VS",
      '<div class="vs" id="vs">→</div>' in page_then and '<div class="vs" id="vs">VS</div>' in page_vs)
check("...each side's words sit in its middle (no empty lower half)", "justify-content: center" in page_then.split(".cmp .tag")[0])

# ---------- 2. text that doesn't fit costs a line, not the visual ----------
short = C.compact(then_now)
check("compact(): the headline and the secondary lines go; the labels and numbers the checks passed stay",
      short is not None and short.headline == "" and all(r.detail == "" for r in short.rows)
      and [r.label for r in short.rows] == [r.label for r in then_now.rows]
      and [r.value for r in short.rows] == [r.value for r in then_now.rows])
check("...nothing to drop, or a graphic over the speaker: None",
      C.compact(Card("metric_bars", S, S + 4, rows=[row("a", "1"), row("b", "2")])) is None
      and C.compact(Card("kinetic_range", S, S + 3, rows=[row("", "$5T")])) is None)
check("The renderer retries a chart that doesn't fit with compact() before giving it up",
      "compact(card)" in (ROOT / "core" / "dynamic_layout.py").read_text())

# ---------- 3. bigger type where there's room ----------
bars2 = C.scene(Card("metric_bars", S, S + 4, rows=[row("Human driver", "1x", at=0.9), row("Autonomous", "14x", True, 1.6)]),
                BrandKit())[0]
bars5 = C.scene(Card("ranked_bars", S, S + 4, rows=[row(f"R{k}", f"{9 - k}", at=0.5 + k * 0.3) for k in range(5)]),
                BrandKit())[0]
check("Two or three bars: thicker bars (34 px) and bigger numbers (54 px); five keep the compact size",
      '"h": 34' in bars2 and "font-size:54px" in bars2 and '"h": 22' in bars5 and "font-size:46px" in bars5)
tl = C.scene(Card("timeline", S, S + 4, rows=[row("", "2014", at=0.9, detail="Founded"),
                                               row("", "2023", at=1.9, detail="Listed")]), BrandKit())[0]
check("A two-point timeline sets its dates at 76 px", "font-size: 76px" in tl)
steps = C.scene(Card("step_sequence", S, S + 4, rows=[row("Find the market", at=0.6), row("Hire the team", at=1.4)]),
                BrandKit())[0]
check("Two or three steps at 60 px", "font-size: 60px" in steps)
kin = C.scene(Card("kinetic_range", S, S + 3, rows=[row("a year in fees", "$850–$1,700", True, 0.5)]), BrandKit())[0]
check("The kinetic range's dash has the same gap on both sides", 'class="rest" style="display:inline-flex;gap:22px' in kin)

# ---------- 4. numbers stay green in the captions ----------
check("Figures, not years or names: $5T, 40%, 14X, 3 are figures; 2023, 1990S, Q3, GPT4 aren't",
      all(is_figure(t) for t in ("$5T", "40%", "14X", "3", "1,200")) and not any(is_figure(t) for t in ("2023.", "1990S", "IN", "W3.", "Q3", "GPT4")))
spoken = [Word("It", 0.0, 0.3), Word("grew", 0.3, 0.6), Word("40%", 0.6, 1.0), Word("in", 1.0, 1.2)]
ass = create_ass_subtitles(spoken, tmp / "c.ass").read_text()
lines = [line for line in ass.splitlines() if line.startswith("Dialogue")]
check("A figure is green while another word is spoken, yellow while it's spoken",
      f"{{\\c{NUMBER_COLOUR}}}40%" in lines[0] and "{\\c&H00FFFF&" in lines[2] and f"{NUMBER_COLOUR}}}40%" not in lines[2])
os.environ["NUMBER_EMPHASIS"] = "off"
plain = create_ass_subtitles(spoken, tmp / "d.ass").read_text()
os.environ.pop("NUMBER_EMPHASIS")
check("NUMBER_EMPHASIS=off turns it off", NUMBER_COLOUR not in plain)

# ---------- 5. tidy ----------
check("Dead code gone (unused easing, the old number helpers, the BUILDERS map)",
      not any(hasattr(C, name) for name in ("EASE_IN", "LAND", "_nice_step", "_fmt", "BUILDERS")))
check("The calm captions' line builder binds its group (no loop-variable closure)",
      "def flush(group" in (ROOT / "core" / "dynamic_layout.py").read_text() and callable(D.calm_captions))

# ======================= Part 2: name cards only on the right face, while they talk =======================
from types import SimpleNamespace  # noqa: E402

import numpy as np  # noqa: E402

import core.card_checks as K  # noqa: E402
import core.graphics_director as G  # noqa: E402
import core.name_cards as N  # noqa: E402
import core.speaker_id as SP  # noqa: E402

os.environ["NAV_PEOPLE_DIR"] = str(tmp / "people")
EISMAN, AIDAN, BRANDON, MICHAEL = (1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0)


def talk(start, end, step=0.3):
    """Continuous speech: a word every 0.3 s."""
    return [Word("word", round(t, 2), round(t + 0.27, 2)) for t in np.arange(start, end, step)]


def solo(face, start, end, talking=True):
    option = N.Option(start, end, "solo", (540, 850, 240), N.FULL_REGION, 1.0, 1150, source_face=(960.0, 400.0, 200.0))
    option._talking = talking  # what the mock mouth meter sees: True, False, or a time where talking starts
    option._face = face
    return option


def run(speakers, shots, people, words_=None, intros=(), channel="", recurring=(), clip=(100.0, 140.0), hook=3.3):
    """plan_name_cards with the video's faces, introductions, channel memory and shots mocked."""
    N._IDENTIFIED.clear()
    real = (N.options, SP.video_duration, SP.solo_people, SP.introductions, SP.face_while, SP.recurring_faces,
            SP.mouth_activity)
    by_start = {}

    def mouth(source, start, end, faces):
        option = by_start[round(start, 3)]
        frames = max(4, int((end - start) * SP.MOUTH_FPS))
        times = start + np.arange(frames) / SP.MOUTH_FPS
        on = option._talking if isinstance(option._talking, bool) else None
        moving = np.array([on if on is not None else t >= option._talking for t in times])
        return np.stack([np.where(moving, 9.0, 1.0), np.full(frames, 2.0)], axis=1)

    N.options = lambda source, plan, a, b, face, fallback_x=None: [
        o for o in shots if SP.similarity(face, o._face) >= SP.SAME_PERSON]
    SP.video_duration = lambda source: 3600.0
    SP.solo_people = lambda source, duration, width: list(people)
    SP.introductions = lambda words, names: [(name, at) for name, at, _face in intros]
    SP.face_while = lambda source, at, width: next((f for _n, t, f in intros if t == at), None)
    SP.recurring_faces = lambda ch, key, ppl: list(recurring)
    SP.mouth_activity = mouth
    for option in shots:
        by_start[round(option.start, 3)] = option
    plan = SimpleNamespace(frame_width=1920)
    try:
        return N.plan_name_cards("x.mp4", plan, clip[0], clip[1], hook, [], speakers,
                                 words=words_ if words_ is not None else talk(*clip), channel=channel)
    finally:
        (N.options, SP.video_duration, SP.solo_people, SP.introductions, SP.face_while, SP.recurring_faces,
         SP.mouth_activity) = real


def names_at(cards):
    return {c.rows[0].label: round(c.start, 2) for c in cards}


EISMAN_SHOW = [G.Speaker("Aidan McCaig", "Lemonade Stand Podcast"), G.Speaker("Brandon Ewink", "Lemonade Stand Podcast"),
               G.Speaker("Steve Eisman", "Host", "host")]
shots = [solo(EISMAN, 100.0, 116.0), solo(BRANDON, 116.0, 117.5), solo(EISMAN, 117.5, 140.0)]
people = [(EISMAN, 20), (BRANDON, 6), (AIDAN, 5)]
cards, notes = run(EISMAN_SHOW, shots, people)
check("The S&P 500 clip: two guests and a host nobody can tell apart -> no name card at all (Round 32 put three "
      "names on Steve Eisman's face, back to back after the title)", cards == [], str(notes))
check("...and the note says why (better none than the wrong name)", sum("can't tell which face" in n for n in notes) == 3)
cards, notes = run(EISMAN_SHOW, shots, people, intros=[("Steve Eisman", 30.0, EISMAN)])
check("...a speaker who introduces themself on camera (\"I'm Steve Eisman\") gets theirs, on his own shot",
      names_at(cards) == {"Steve Eisman": 103.5} and all(c.place for c in cards), f"{names_at(cards)} {notes}")
cards, notes = run(EISMAN_SHOW, shots, people, channel="The Real Eisman Playbook", recurring=[EISMAN])
check("...as does the host whose face comes back video after video on the channel",
      names_at(cards) == {"Steve Eisman": 103.5} and any("comes back on the channel" in n for n in notes), str(notes))

BITCOIN = [G.Speaker("Michael Tanguma", "CEO & Co-Founder, Onramp"), G.Speaker("Maurizio Pedrazzoli Grazioli", "Host", "host")]
shots = [solo(MICHAEL, 102.0, 109.5), solo(MICHAEL, 112.5, 128.0)]
cards, notes = run(BITCOIN, shots, [(MICHAEL, 24)])
check("The Bitcoin ETF clip: the guest, alone on screen most by far, gets his card on his own shot after 3.5 s",
      names_at(cards) == {"Michael Tanguma": 103.5}, f"{names_at(cards)} {notes}")
check("...the host, never alone on screen, gets none (Round 32 put his name on the guest's face at 4.5 s)",
      "Maurizio Pedrazzoli Grazioli" not in names_at(cards)
      and any("Maurizio" in n and "can't tell" in n for n in notes), str(notes))
shots = [solo(MICHAEL, 100.0, 110.0, talking=False), solo(MICHAEL, 112.0, 128.0, talking=115.0)]
cards, notes = run(BITCOIN, shots, [(MICHAEL, 24)])
check("A listener's still mouth on his solo shot: no card until he talks (115 s here), never over a silent listener",
      114.75 <= names_at(cards).get("Michael Tanguma", 0) <= 115.3, f"{names_at(cards)} {notes}")
speech = talk(100.0, 104.0) + talk(108.0, 140.0)
cards, notes = run(BITCOIN, [solo(MICHAEL, 100.0, 128.0)], [(MICHAEL, 24)], words_=speech)
check("...and a pause in the talking waits for a verified run of words (1.75 s, 1.5 s of it said)",
      names_at(cards) == {"Michael Tanguma": 108.0}, f"{names_at(cards)} {notes}")
cards, notes = run(BITCOIN, [solo(MICHAEL, 100.0, 140.0)], [(MICHAEL, 24)], hook=1.0)
check("Never before 3.5 s into the clip, even with a short title", names_at(cards) == {"Michael Tanguma": 103.5},
      str(names_at(cards)))
twins = [G.Speaker("Michael Tanguma", "CEO"), G.Speaker("Mike T", "Host", "host")]
cards, notes = run(twins, [solo(MICHAEL, 100.0, 140.0)], [(MICHAEL, 24)],
                   intros=[("Michael Tanguma", 10.0, MICHAEL), ("Mike T", 20.0, MICHAEL)])
check("One face, one name card: two names that land on the same face never both show",
      len(cards) == 1, f"{names_at(cards)} {notes}")
check("Name cards stay 3.0-3.5 s", all(3.0 <= c.end - c.start <= 3.5 for c in cards), str([c.end - c.start for c in cards]))
check("No 'right after the title, whoever is on screen' fallback left", not hasattr(N, "sequential_cards"))
said_words = [Word(t, 30.0 + k * 0.3, 30.25 + k * 0.3) for k, t in enumerate("Hey everyone, I'm Steve Eisman and".split())]
check("Self-introductions read from the transcript (\"I'm Steve Eisman\")",
      [n for n, _t in SP.introductions(said_words, ["Steve Eisman", "Aidan McCaig"])] == ["Steve Eisman"])
SP.remember_people("Some Channel", "video1", [(EISMAN, 12), (AIDAN, 4)])
check("The channel's face memory: a face seen in another of its videos comes back as recurring (the host)",
      SP.recurring_faces("Some Channel", "video2", [(EISMAN, 9), (BRANDON, 5)]) == [EISMAN]
      and SP.recurring_faces("Some Channel", "video1", [(EISMAN, 12)]) == [])
check("Speaking is told from the mouth moving against the forehead (head and camera motion move both)",
      SP.SPEAKING_RATIO > 1 and "cy - 0.78 * w" in (ROOT / "core" / "speaker_id.py").read_text())

# ======================= Part 3: Bento pacing: built in ~1 s, then held still to read =======================
from core.card_checks import BUILD_SECONDS, READ_HOLD  # noqa: E402

share_words = [Word(t, 116.5 + k * 0.32, 116.8 + k * 0.32) for k, t in enumerate(
    "but they move communication to services or consumer discretionary or whatever you add up all those companies "
    "types of you're at 47% of it okay".split())]
share = Card("share_split", 116.5, 124.6, headline="S&P 500 Tech Exposure",
             rows=[CardRow("Tech + Google/Amazon", "47%", True), CardRow("Rest of the S&P 500", "53%")])
res = K.check_card(share, share_words, 100.0, 153.0, 3.3)
said_at = next(w.start for w in share_words if w.text == "47%")
check("The S&P 500 clip's card: it comes in as its number is said, not 5 s early and empty",
      res.ok and share.start >= said_at - 0.5 and share.rows[0].reveal_at - share.start <= 0.5,
      f"{res.errors} start {share.start:.2f}, said {said_at:.2f}")
held = share.end - share.start - C.settle_time(share) - 0.4
check("...it's on screen 4.5-6 s and holds completely still at least 3.5 s before it leaves",
      4.5 <= share.end - share.start <= 6.7 and held >= READ_HOLD - 1e-6, f"{share.end - share.start:.2f} s, still {held:.2f} s")
slow = Card("metric_bars", 104.0, 114.0, rows=[CardRow("Payroll", "$4.2M"), CardRow("Hosting", "$1.1M"),
                                                 CardRow("Ads", "$0.9M", True)])
slow_words = [Word(t, 104.0 + k * 0.45, 104.4 + k * 0.45) for k, t in enumerate(
    "payroll is $4.2M then hosting which honestly is a big line for us at $1.1M and ads were $0.9M".split())]
res = K.check_card(slow, slow_words, 100.0, 140.0, 3.3)
reveals = [r.reveal_at for r in slow.rows]
check("Bars said over 7 s are all drawn within 1 s of the first (in the order said), not one every few seconds",
      res.ok and reveals == sorted(reveals) and reveals[-1] - reveals[0] <= BUILD_SECONDS + 1e-6, f"{res.errors} {reveals}")
page, motion, *_ = C.scene(slow, BrandKit())
check("...after which nothing in the card moves until it leaves (frames are reused, not redrawn)",
      not any(motion.moving(t) for t in np.arange(C.settle_time(slow) + 0.1, slow.end - slow.start - 0.4, 0.1)))
kin_words = [Word(t, 104.0 + k * 0.3, 104.25 + k * 0.3) for k, t in enumerate("fees run $850 to $1,700 a year".split())]
kinetic = Card("kinetic_range", 104.0, 106.0, rows=[CardRow("a year", "$850–$1,700", True)])
res = K.check_card(kinetic, kin_words, 100.0, 140.0, 3.3)
check("A number over the speaker stays 4.5-6 s (it was 2-5 s)", res.ok and 4.5 <= kinetic.end - kinetic.start <= 6.0,
      f"{res.errors} {kinetic.end - kinetic.start:.2f}")
tight = Card("metric_bars", 134.0, 136.5, rows=[CardRow("Payroll", "$4.2M"), CardRow("Ads", "$0.9M", True)])
end_words = [Word(t, 134.0 + k * 0.3, 134.25 + k * 0.3) for k, t in enumerate("payroll is $4.2M and ads $0.9M".split())]
res = K.check_card(tight, end_words, 100.0, 140.0, 3.3)
check("A chart with no room left to be read before the ending is refused, not flashed",
      not res.ok and "Needs" in res.errors[0], str(res.errors))

# ======================= Part 4: the visual engine (Remotion, NumberFlow, Magic UI, TradingView) =======================
check("Remotion-style spring entrances: a solved spring sampled into a CSS linear() easing",
      C.SPRING.startswith("linear(0.0000") and C.SPRING.endswith("1.0000)") and C.SPRING in str(motion.js(6.0)))
mid = C.scene(Card("metric_bars", S, S + 6, rows=[row("Payroll", "$4.2M", at=0.5), row("Ads", "$0.9M", True, 0.8)]),
              BrandKit())
from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    view = browser.new_page(viewport={"width": mid[2], "height": mid[3]})
    html_file = tmp / "odometer.html"
    html_file.write_text(mid[0])
    view.goto(html_file.as_uri())
    view.evaluate(C._SETUP, mid[1].js(6.0))
    view.evaluate(C._SEEK, [int(0.75 * 30), 30])
    rolling = view.evaluate("() => [...document.querySelectorAll('.od')].map(d => +getComputedStyle(d).getPropertyValue('--y'))")
    text_mid = view.evaluate("() => [...document.querySelectorAll('[data-roll]')].map(e => e.textContent)")
    view.evaluate(C._SEEK, [int(3.0 * 30), 30])
    settled = view.evaluate("() => [...document.querySelectorAll('.od')].map(d => [+d.dataset.d, +getComputedStyle(d).getPropertyValue('--y')])")
    browser.close()
check("NumberFlow-style odometer: each digit rolls in its own column, mid-roll between 0 and its value",
      any(0 < y < 9 for y in rolling) and text_mid == ["$4.2M", "$0.9M"], f"{rolling} {text_mid}")
check("...and lands exactly on its value (the text never changes width)", settled and all(abs(d - y) < 1e-6 for d, y in settled),
      str(settled))
trend = C.scene(Card("trend_line", S, S + 6, rows=[row("2022", "$5M", at=0.5), row("2024", "$19M", True, 1.0)]), BrandKit())[0]
check("TradingView-style trend: faint dashed gridlines and a price-tag callout on the point that matters",
      "strokeDasharray: '3,7'" in trend and 'class="bdg"' in trend)
check("Magic UI / Aceternity card: a top-left spotlight and one light beam along the top edge as it lands",
      '<div class="spot"></div>' in trend and 'id="beam"' in trend)

print("\nAll checks passed." if not fails else f"\n{fails} check(s) failed.")
sys.exit(1 if fails else 0)
