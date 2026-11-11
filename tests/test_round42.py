"""Round 42 checks: the flowing camera (smooth push-ins and slow releases, never a hard
zoom cut, few moves and each for a reason, rested side-to-side re-frames) and the
first seconds (no filler or dead air before word one, the opening's pull measured, and a
cold open from the clip's most gripping line when there's no cliffhanger).

The bug it guards against, measured on two real clips: every zoom-out was a single-frame
0.907x drop (1.10x -> 1.00x), about every 7-8 s.

Run from the project root (well under a minute):
    timeout 60 .venv/bin/python tests/test_round42.py .
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp(prefix="round42-"))
os.environ.update({"NAV_NO_CLIP_PAGES": "1", "NAV_OFFLINE": "1", "NAV_SFX_DIR": str(tmp / "sfx")})
for name in ("SIGNATURE_EDIT", "CAMERA_FLOW", "OPENING_HOOK", "ENABLE_CLIMAX_HOOK"):
    os.environ.pop(name, None)
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


import core.camera_path as CP  # noqa: E402
import core.climax_hook as H  # noqa: E402
import core.opening as O  # noqa: E402
from core.transcriber import Word  # noqa: E402

RATE = 30.0


def words_from(text, start=0.0, step=0.33):
    out, t = [], start
    for token in text.split():
        out.append(Word(token, round(t, 3), round(t + step - 0.03, 3)))
        t += step
    return out


# ======================================= the curve =======================================
emph = CP.Move(10.0, CP.PUSH, 13.5, CP.RELEASE, CP.AMOUNT)
hook = CP.Move(0.0, CP.HOOK_PUSH, CP.HOOK_HOLD_UNTIL, CP.HOOK_RELEASE, CP.HOOK_AMOUNT, "hook", 0.008, ease_out_only=True)
moves = [hook, emph]
frames = [CP.zoom_at(n / RATE, moves) for n in range(int(20 * RATE))]
steps = np.abs(np.diff(frames))
check("No hard zoom cut: every frame-to-frame change is under 0.5 % (the bug: a 9.3 % one-frame drop)",
      steps.max() < 0.005, f"largest {steps.max() * 100:.2f} %")
a, top = emph.start, emph.start + emph.push
check("A push eases in AND out (smootherstep): it starts and lands at rest, no jerk at either end",
      CP.zoom_at(a + 1 / RATE, moves) - 1 < 0.0005 and abs(CP.zoom_at(top - 1 / RATE, moves) - CP.zoom_at(top, moves)) < 0.0004
      and 1.0 + emph.amount * 0.45 < CP.zoom_at(a + emph.push / 2, moves) < 1.0 + emph.amount * 0.55)
check("...holds while the line lands, still creeping in (the frame never sits dead)",
      CP.zoom_at(13.4, moves) > CP.zoom_at(top + 0.1, moves) + 0.008)
rel = [CP.zoom_at(emph.hold_end + k / RATE, moves) for k in range(int(emph.release * RATE) + 2)]
check("...and releases slowly (2.3 s), easing out to exactly 1.00x, never a drop",
      emph.release >= 2.0 and all(x >= y - 1e-12 for x, y in zip(rel, rel[1:])) and abs(rel[-1] - 1.0) < 1e-9
      and max(np.abs(np.diff(rel))) < 0.003, f"{rel[0]:.4f} -> {rel[-1]:.4f}")
check("The hook is already moving on frame 1 (eased out from frame 0): motion from the first frame",
      CP.zoom_at(1 / RATE, moves) > 1.0005 and CP.zoom_at(0, moves) == 1.0)
cut = CP.Move(10.0, CP.PUSH, 13.5, CP.RELEASE, CP.AMOUNT, cut=12.6)
check("A camera cut during the hold ends the move inside the cut (the only hard change, hidden by the cut)",
      CP.zoom_at(12.6 - 1 / RATE, [cut]) > 1.05 and CP.zoom_at(12.6, [cut]) == 1.0)
teaser = CP.teaser_move(3.2)
check("The cold open drifts in from 1.04x to 1.10x (it never sits still at a fixed punch-in)",
      abs(CP.zoom_at(0, [teaser]) - 1.04) < 1e-9 and abs(CP.zoom_at(3.2 - 1e-6, [teaser]) - 1.10) < 1e-3)

expr = "(" + "+".join(f"({CP._expr(m, RATE)})" for m in moves + [cut]) + ")"
env = {"between": lambda x, a, b: float(a <= x <= b), "gt": lambda a, b: float(a > b), "lt": lambda a, b: float(a < b),
       "gte": lambda a, b: float(a >= b), "min": min, "max": max, "pow": pow}
py = expr.replace("\\,", ",")
worst = max(abs(1 + eval(re.sub(r"\bin\b", str(n), py), env) - CP.zoom_at(n / RATE, moves + [cut])) for n in range(0, 600, 3))
check("FFmpeg draws exactly this curve (its per-frame expression vs the curve)", worst < 1e-5, f"{worst:.2e}")

# ======================================= the plan: few moves, each for a reason =======================================
TALK = ("we started with nothing and we built it slowly over the years with a small team that cared a lot. " * 9).split()
talk = [Word(t, round(k * 0.33, 3), round(k * 0.33 + 0.3, 3)) for k, t in enumerate(TALK)]
duration = talk[-1].end + 0.5
loud = [-30.0] * len(talk)
for k in (40, 60, 80, 100, 130, 160, 190, 220, 250):
    if k < len(loud):
        loud[k] = -12.0
plan = CP.plan(talk, loud, duration, cuts=[], blocked=[])
emphasis = [m for m in plan if m.kind != "hook"]
check(f"About one move per 15 s, not every 5-8 s ({duration:.0f} s clip: {len(emphasis)} emphasis moves + the hook)",
      1 <= len(emphasis) <= max(1, int(duration // CP.SECONDS_PER_MOVE)), str([(m.kind, round(m.start, 1)) for m in plan]))
check("At least 6 s of a still frame between moves",
      all(b.start - a.end >= CP.NEUTRAL_GAP - 1e-6 for a, b in zip(plan, plan[1:])))
check("The camera moves at most 45 % of the clip", sum(m.end - m.start for m in plan) <= CP.MAX_MOVING_SHARE * duration)
paid = CP.plan(talk, loud, duration, payoff_at=30.0, thesis_at=50.0)
check("Meaning first: the cold open's payoff gets the strongest push, the thesis next, then emphasis",
      any(m.kind == "payoff" and m.amount == CP.PAYOFF_AMOUNT for m in paid) and any(m.kind == "thesis" for m in paid),
      str([(m.kind, round(m.start, 1)) for m in paid]))
blocked = CP.plan(talk, loud, duration, blocked=[(0.0, duration)], hook=False)
check("Never under a split-screen card, a tear-out or a two-person split (blocked spans)", blocked == [])
with_cuts = CP.plan(talk, loud, duration, cuts=[k * 4.0 for k in range(1, int(duration // 4))])
check("A push never crosses a camera cut; a cut after it ends the move inside the cut",
      all(not any(m.start - 0.2 < c < m.start + m.push + CP.CUT_GUARD for c in [k * 4.0 for k in range(1, 40)]) for m in with_cuts
          if m.kind != "hook"))

# ======================================= rendered: the zoom on every frame =======================================
from core.face_tracker import CropBox, PULL_IN_CENTER_Y  # noqa: E402
from core.video_renderer import render_clip  # noqa: E402

src = tmp / "still.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x303030:s=960x540:r=30:d=11", "-f", "lavfi", "-i",
                "sine=f=200:d=11", "-shortest", "-vf", "drawbox=x=240:y=90:w=480:h=90:color=white:t=fill", "-pix_fmt", "yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
ass = tmp / "empty.ass"
ass.write_text("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\nFormat: Layer, Start, End, Style, "
               "Name, MarginL, MarginR, MarginV, Effect, Text\n")
render_moves = [CP.Move(0.0, CP.HOOK_PUSH, 3.6, CP.HOOK_RELEASE, CP.HOOK_AMOUNT, "hook", 0.008, ease_out_only=True),
                CP.Move(6.0, CP.PUSH, 7.8, CP.RELEASE, CP.PAYOFF_AMOUNT, "payoff")]
out = render_clip(src, ass, CropBox(0, 0, 2, 2), 0.0, 11.0, tmp / "flow.mp4", layout_mode="blurred_background", camera=render_moves)
row = int(round(1920 * PULL_IN_CENTER_Y))  # the zoom's centre row: the box crosses it at every zoom
raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-vf", f"crop=1080:2:0:{row - row % 2}", "-f", "rawvideo",
                      "-pix_fmt", "gray", "-"], capture_output=True).stdout
lines = np.frombuffer(raw, np.uint8).reshape(-1, 2, 1080)[:, 0, :].astype(int)
widths = np.array([(line > 150).sum() for line in lines], float)
expected = np.array([480 * 1080 / 960 * CP.zoom_at(n / RATE, render_moves) for n in range(len(widths))])
measured_steps = np.abs(np.diff(widths)) / widths[:-1]
check("Rendered: the zoom on every frame follows the planned curve (within 3 px of 540 px x zoom)",
      np.abs(widths - expected).max() <= 3.0, f"worst {np.abs(widths - expected).max():.1f} px")
check("...with no frame-to-frame jump over 0.6 % anywhere in the clip (the old punch-out was 9.3 %)",
      measured_steps.max() < 0.006, f"largest {measured_steps.max() * 100:.2f} %")
check("...in, held, out: 1.00x at rest, ~1.09x at the payoff's peak, back to 1.00x",
      abs(widths[int(5.5 * RATE)] - 540) <= 2 and abs(widths[int(7.6 * RATE)] / 540 - 1.0 - CP.PAYOFF_AMOUNT) < 0.012
      and abs(widths[-1] - 540) <= 2, f"{widths[int(5.5 * RATE)]:.0f} / {widths[int(7.6 * RATE)]:.0f} / {widths[-1]:.0f}")

# ======================================= side-to-side pans: rested, not forced =======================================
from core import camera_crop  # noqa: E402

wander = [(k * 0.5, 960.0) for k in range(4)] + [(2 + k * 0.5, 1350.0) for k in range(5)] + \
         [(4.5 + k * 0.5, 600.0) for k in range(20)]
flowing = camera_crop.reframes(wander, 960 - 304, 608, 1920, 0.0)
os.environ["CAMERA_FLOW"] = "0"
before = camera_crop.reframes(wander, 960 - 304, 608, 1920, 0.0)
os.environ.pop("CAMERA_FLOW")
rest = camera_crop.MOVE_SECONDS + camera_crop.REFRAME_COOLDOWN
check("Side-to-side re-frames rest 4 s after each move: a speaker shifting again 2.5 s later isn't chased at once",
      len(before) == 2 and before[1][0] - before[0][0] < rest and len(flowing) == 2 and flowing[1][0] - flowing[0][0] >= rest,
      f"before {before}, now {flowing}")
check("...and the camera still follows them once it has rested (the same framing within 20 px, just calmer)",
      abs(flowing[1][1] - before[1][1]) <= 20)

# ======================================= the first seconds =======================================
spoken = words_from("Um, so you know, the biggest mistake founders make is hiring too fast, and it costs them everything "
                    + "we learned that the hard way over many years of building the company together. " * 3, 10.4)
new_start, note = O.tighten(10.0, spoken[-1].end + 0.3, spoken)
check("Word one on frame one: leading \"Um, so you know\" and the silence before it are cut",
      abs(new_start - (spoken[4].start - 0.06)) < 1e-6 and "Um, so you know" in note, f"{new_start} {note}")
plain = words_from("The biggest mistake founders make is hiring too fast. " * 6, 10.05)
check("...a clean opening is left alone", O.tighten(10.0, plain[-1].end + 0.3, plain) == (10.0, ""))
long_filler = words_from("um " * 12 + "the point is that we grew. " * 10, 10.0)
check("...at most 2 s is ever cut (then only the silence)", O.tighten(10.0, long_filler[-1].end, long_filler)[0] - 10.0 <= O.MAX_TRIM)
check("The opening's pull: a question to the viewer scores high, a context-dependent fragment low",
      O.strength(words_from("Why would you let a stranger decide your worth?"), 0, 9) >= O.STRONG_OPENING
      > O.strength(words_from("One is we're living in a time where everybody has an opinion."), 0, 9))

TEXT = ("one is we're living in a time where everybody has an opinion about everything you do online. "
        "And we read the opinions that people have of us every single day, and honestly it gets to you after a while. "
        "Back before social media, the aunties in the kitchen were saying the exact same things about all of us. "
        "Why would you let a stranger on the internet decide how you feel about your own life? "
        "So take that power away, because nothing has really changed except how much of it we can see. "
        "Protect your sanity by understanding that this comes with the job, and that's the whole secret.")
clip = words_from(TEXT, 3.6)
end = clip[-1].end + 0.4
cold = O.cold_open(clip, 3.6, end, rate=RATE)
why = next(w for w in clip if w.text == "Why")
check("No cliffhanger, a weak opening: the clip opens cold on its most gripping line (the open question)",
      H.find_climax(clip, 3.6, end, RATE) is None and cold is not None and cold.line.startswith("Why would you let a stranger"),
      cold and cold.line)
check("...cut while the question is still open, on a strong word (never \"the\" or \"about\")",
      cold and not cold.line.endswith("?") and cold.line.split()[-1].lower() not in {"the", "about", "how", "a", "on"}
      and abs(cold.start - (why.start - H.PRE_ROLL)) < 1 / RATE + 1e-6)
check("...2.2-3.6 s, a whole number of frames, from the clip's back 65 % (8 s in or later), under 59 s in all",
      cold and O.COLD_SECONDS[0] - 1e-6 <= cold.offset <= O.COLD_SECONDS[1] + 1e-6 and cold.frames == round(cold.offset * RATE)
      and cold.start - 3.6 >= max(O.MIN_COLD_AT, 0.35 * (end - 3.6)) - H.PRE_ROLL - 1e-6 and (end - 3.6) + cold.offset <= 59)
strong = words_from("Why would you ever let a stranger decide your worth? " + TEXT, 3.6)
check("...never forced: an opening that already pulls keeps its own first line",
      O.cold_open(strong, 3.6, strong[-1].end + 0.4, rate=RATE) is None)
short = words_from(" ".join(TEXT.split()[:50]), 3.6)
check("...and never on a clip under 20 s", O.cold_open(short, 3.6, short[-1].end, rate=RATE) is None)
cliff = words_from("So we spent the first two years building the wrong product for the wrong people, and we burned most "
                   "of the money. Our investors were patient, the team was great, the market was there, and still nothing "
                   "worked for us. Then one Tuesday a customer called me. And what nobody tells you about product market "
                   "fit is that it feels like being pulled, not pushing, and every metric moves at once. That is when we "
                   "knew we had it.", 2.0, 0.38)
import core.pipeline as PL  # noqa: E402

found = PL.find_teaser(Path("/nonexistent.mp4"), cliff, 2.0, cliff[-1].end + 0.4, True)
check("A real cliffhanger still wins (Round 40's teaser first, the cold open only without one)",
      found is not None and found.hinge.endswith("is that"), found and found.reason)
os.environ["OPENING_HOOK"] = "0"
check("OPENING_HOOK=0: no tightening and no cold open",
      O.enabled() is False and PL.find_teaser(Path("/nonexistent.mp4"), clip, 3.6, end, True) is None)
os.environ.pop("OPENING_HOOK")
os.environ["CAMERA_FLOW"] = "0"
check("CAMERA_FLOW=0: Round 41's camera", CP.enabled() is False)
os.environ.pop("CAMERA_FLOW")
src_pipeline = (ROOT / "core" / "pipeline.py").read_text()
check("Every Make clips and worker render: the opening tightened, one flowing camera path, the cold open drifting in",
      "opening.tighten(" in src_pipeline and "camera=moves" in src_pipeline and "camera_moves(" in src_pipeline
      and "teaser_camera(" in src_pipeline and "pull_until = 0.0" in src_pipeline)
run_all = (ROOT / "tests" / "run_all.py").read_text()
check("Older suites keep the camera and opening they were written for (CAMERA_FLOW=0, OPENING_HOOK=0 before round 42)",
      '"CAMERA_FLOW"' in run_all and '"OPENING_HOOK"' in run_all)

print("\nAll Round 42 checks passed." if not fails else f"\n{fails} check(s) failed")
sys.exit(1 if fails else 0)
