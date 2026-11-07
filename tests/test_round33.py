"""Round 33 checks: the comparison card renders (a serif headline's glyphs past a tight
line box aren't overflow), text that doesn't fit costs the headline or a line instead of
the visual, the comparison card's new layout (words centred, an arrow for then -> now),
bigger type for short bar charts, timelines and step sequences, numbers kept green in the
captions (years aren't), and the kinetic range's even dash.
Temp files only: no AI, YouTube, R2 or network.

Run from the project root:  .venv/bin/python tests/test_round33.py .
"""
import os
import re
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
print("\nAll checks passed." if not fails else f"\n{fails} check(s) failed.")
sys.exit(1 if fails else 0)
