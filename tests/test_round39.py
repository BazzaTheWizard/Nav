"""Round 39 checks: the split view keeps the speaker big (top 54% of the frame, face-centred)
with a compact, bottom-anchored card right under it (no dead black band); every card, step,
comparison side and quote word lands on its own spoken word (t_word - 0.12 s), never before;
a contrast whose sides are 15+ s apart comes in on its punchline; no 01 / 02 watermark
numerals behind a comparison's sides; bold, readable captions on the seam under a chart;
and app.py free of Streamlit's deprecated use_container_width.

Run from the project root:  .venv/bin/python tests/test_round39.py .
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp(prefix="round39-"))
os.environ.update({"NAV_NO_CLIP_PAGES": "1", "NAV_LOGO_DIR": str(tmp / "logos"), "NAV_SFX_DIR": str(tmp / "sfx")})
for name in ("SFX", "KEYWORD_EMPHASIS", "CAPTION_STYLE"):
    os.environ.pop(name, None)
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


import cv2  # noqa: E402
import numpy as np  # noqa: E402

import core.captions as CAP  # noqa: E402
import core.card_checks as K  # noqa: E402
import core.cards as C  # noqa: E402
import core.dynamic_layout as D  # noqa: E402
from core.edl import BrandKit, Card, CardRow, ClipEdit  # noqa: E402
from core.transcriber import Word  # noqa: E402

LEAD = C.WORD_LEAD
EPS = 1e-6


def say(text, at, step=0.3):
    return [Word(t, round(at + k * step, 3), round(at + k * step + 0.25, 3)) for k, t in enumerate(text.split())]


def when(words, token, after=0.0):
    """The start of the first word ``token`` said after ``after``."""
    return next(float(w.start) for w in words if w.text.strip(".,").lower() == token and float(w.start) > after)


# ======================= 1. proportions: speaker big, card compact and bottom-anchored =======================
H = D.FRAME_H
check("The speaker's viewport is the top 52-58% of the frame, never under half",
      0.52 * H <= C.SPLIT_TOP <= 0.58 * H and C.SPLIT_TOP >= H / 2, f"{C.SPLIT_TOP} px = {C.SPLIT_TOP / H:.1%}")
check("The card is compact: 34-40% of the frame", 0.34 * H <= C.CARD_H <= 0.40 * H, f"{C.CARD_H} px = {C.CARD_H / H:.1%}")
check("...bottom-anchored: 140-160 px left under it for the apps' bars",
      140 <= H - C.CARD_BOTTOM <= 160 and C.BOTTOM_MARGIN == H - C.CARD_BOTTOM, str(H - C.CARD_BOTTOM))
check("...and it meets the speaker's viewport: no dead black band between them (under 24 px)",
      0 <= C.CARD_TOP - C.SPLIT_TOP <= 24 and C.CARD_Y == C.CARD_TOP - C.SPLIT_TOP, str(C.CARD_TOP - C.SPLIT_TOP))
check("The page under the speaker is exactly the panel (1080 x 880) and the card fills it but the margins",
      C.SPLIT_H == H - C.SPLIT_TOP and C.CARD_Y + C.CARD_H + C.BOTTOM_MARGIN == C.SPLIT_H)
check("The captions' Bento zone is the card's band", CAP.BENTO_ZONE == (C.CARD_TOP, C.CARD_BOTTOM), str(CAP.BENTO_ZONE))
check("Tighter card internals: header, footer and padding", C.HEADER_H <= 120 and C.FOOTER_H <= 90 and C.PAD <= 40
      and C.HEADER_H + C.FOOTER_H < 0.3 * C.CARD_H, f"{C.HEADER_H}/{C.FOOTER_H}/{C.PAD}")

# face-centred viewport crop
x, y, w, h = D.band_rect(1920, 1080, 1500.0)
check("16:9 camera: the viewport is a 1080:1040 crop at full height, around the face",
      (w, h) == (1120.0, 1078.0) and abs(w / h - 1080 / 1040) < 0.003 and x <= 1500 - 0.3 * w and x + w >= 1500 + 0.3 * w,
      f"{(x, y, w, h)}")
check("...kept inside the frame at the edge", D.band_rect(1920, 1080, 1900.0)[0] == 1920 - 1120
      and D.band_rect(1920, 1080, 10.0)[0] == 0.0)
check("...the face 1.7x bigger than the old 2.35:1 strip (1080 / 1120 vs 1080 / 1920 of the camera)",
      (1080 / w) / (1080 / 1920) > 1.65, f"{(1080 / w) / (1080 / 1920):.2f}x")
check("A wide shot fills the viewport's width, centred in it", D.wide_fit(1920, 1080) == (0, 216, 1080, 608),
      str(D.wide_fit(1920, 1080)))
cents = D.band_centres(lambda t: (100.0 if t < 2 else 900.0, 0.0, 608.0, 1080.0), 0.0, 4.0)
check("The viewport follows the face across a cut", [c for _t, c in cents] == [400.0, 1200.0]
      and D.centre_at(cents, 1.0) == 400.0 and D.centre_at(cents, 3.0) == 1200.0, str(cents))
expr = D._x_expression(cents, 1920, 1080)
check("...as FFmpeg's per-frame crop x", expr == f"if(lt(t,{cents[1][0]:.3f}),0,{int(D.band_rect(1920, 1080, 1200.0)[0])})",
      expr)

# ======================= 2. strict word sync =======================
CLIP = (0.0, 50.0)
talk = (say("today we talk about creators versus affiliates and why that matters", 4.0)
        + say("the affiliates are more loyal to the money that they make not creators", 11.0)
        + say("and that is fine but it is not where the growth comes from", 15.5)
        + say("we spent years figuring out what actually drives sales", 20.0)
        + say("our core creators are like mini CMOs testing styles every single week", 27.0)
        + say("every day they wake up and think about content", 32.0)
        + say("then the visual look how does this look visually", 36.0)
        + say("and they bridge the gap with what other companies make", 39.5)
        + say("that is the whole game for us honestly", 44.0))

contrast = Card("comparison_card", 11.0, 17.0, rows=[
    CardRow("The Affiliates", detail="Loyal to the money they make"),
    CardRow("Core Creators", detail="Like mini CMOs testing styles", highlight=True)])
res = K.check_card(contrast, talk, *CLIP)
a_said, b_said = when(talk, "affiliates", 10.0), when(talk, "core")
left, right = contrast.rows
check("Contrast: the creators' side lands on 'core creators ... mini CMOs' (0:27), not the stray 'creators' at 0:14",
      res.ok and abs(right.reveal_at - (b_said - LEAD)) < EPS, f"{res.errors} {right.reveal_at} vs {b_said}")
check("...its points are 16 s apart, so the card comes in on the punchline (not at 0:11 with half of it)",
      contrast.start > 25.0 and contrast.start + C._lead(contrast) <= right.reveal_at - 0.5 + EPS,
      f"start {contrast.start}")
check("...the affiliates' side (said at 0:11) lands first, as a recap, a beat before the punchline",
      left.reveal_at < right.reveal_at and abs(left.reveal_at - (contrast.start + C._lead(contrast))) < EPS,
      f"{left.reveal_at} / {right.reveal_at}")
check("...and stays up long enough to read after the punchline", contrast.end - right.reveal_at >= K.READ_HOLD,
      f"{contrast.start}-{contrast.end}")
close = Card("comparison_card", 11.0, 17.0, rows=[
    CardRow("The Affiliates", detail="Loyal to the money they make"),
    CardRow("Growth", detail="Not where the growth comes from", highlight=True)])
res = K.check_card(close, talk, *CLIP)
check("A contrast whose sides are close: in on side A's word, side B on its phrase ('not where the growth...') 6 s later",
      res.ok and abs(close.rows[0].reveal_at - (a_said - LEAD)) < EPS
      and abs(close.rows[1].reveal_at - (when(talk, "where") - LEAD)) < EPS
      and abs(close.start + C._lead(close) - close.rows[0].reveal_at) < EPS,
      f"{res.errors} {[r.reveal_at for r in close.rows]} start {close.start}")

steps = Card("step_sequence", 32.0, 38.0, rows=[
    CardRow("Think about content", detail="Wake up every day"),
    CardRow("Visual look", detail="How does this look visually"),
    CardRow("Bridge the gap", detail="What other companies make")])
res = K.check_card(steps, talk, *CLIP)
said = [when(talk, "wake", 31.0), when(talk, "visual", 35.0), when(talk, "bridge", 39.0)]
reveals = [r.reveal_at for r in steps.rows]
check("Step by step: each step lands on its own phrase (t_word - 0.12 s), not all within 1.5 s",
      res.ok and all(abs(r - (s - LEAD)) < 0.31 and r >= s - LEAD - EPS for r, s in zip(reveals, said))
      and reveals[2] - reveals[0] > 6.0, f"{res.errors} {reveals} vs words {said}")
check("...the card comes in as step 1 is said", abs(steps.start + C._lead(steps) - reveals[0]) < EPS, str(steps.start))
check("...and stays until the last step has been read", steps.end >= reveals[-1] + K.READ_HOLD, str(steps.end))
motion = C.warp(steps, C._build(steps, BrandKit())[3])
check("...never played faster than said (no Round 35 time-warp on a word-synced card)",
      C.word_synced(steps) and motion.warp[2] == 1.0 and abs(motion.real(reveals[2] - steps.start) - (reveals[2] - steps.start)) < EPS)
fades = sorted(t0 for (sel, prop), pts in motion.tracks.items() if re.fullmatch(r"#s\d", sel) and prop == "opacity"
               for t0, _v, _e in pts[:1])
check("...the steps' fade-ins start at their words in the rendered motion",
      all(abs((steps.start + f) - r) < 0.01 for f, r in zip(fades, reveals)), f"{fades}")
again = Card("step_sequence", steps.start, steps.end, rows=[CardRow(r.label, detail=r.detail) for r in steps.rows])
K.check_card(again, talk, *CLIP)
check("Checking twice gives the same timing", [r.reveal_at for r in again.rows] == reveals and again.start == steps.start,
      f"{[r.reveal_at for r in again.rows]} vs {reveals}")

# overlays (a number over the speaker) follow the same rule
money = say("we went from zero to 40% margins in two years", 20.0)
pill = Card("kinetic_range", 19.0, 25.0, rows=[CardRow("margins", "40%")])
res = K.check_card(pill, money, 0.0, 40.0)
check("A number over the speaker lands at its word - 0.12 s", res.ok and abs(pill.rows[0].reveal_at - (when(money, "40%") - LEAD)) < EPS,
      f"{res.errors} {pill.rows[0].reveal_at}")

# the quote drag-down
QUOTE = "If you obsess and you don't stay in your lane, you get distracted and you dilute what you're doing."
spoken = say("so here's the thing", 17.0) + say(QUOTE.replace(",", "").replace(".", ""), 20.0) + say("and that matters a lot", 27.0)
quote = Card("headline_card", 15.5, 22.0, rows=[CardRow(QUOTE, value="stay in your lane")])
res = K.check_card(quote, spoken, 0.0, 40.0)
words_at = [float(w.start) for w in spoken if 20.0 <= float(w.start) < 26.5]
cadence = quote.context.get("cadence") or []
check("Quote: in strictly as the quote begins (its first word - 0.12 s)",
      res.ok and abs(quote.rows[0].reveal_at - (20.0 - LEAD)) < EPS and abs(quote.start + C._lead(quote) - (20.0 - LEAD)) < EPS,
      f"{res.errors} {quote.rows[0].reveal_at} {quote.start}")
check("...every word lit in sync with the spoken cadence", len(cadence) == len(QUOTE.split()) == len(words_at)
      and all(abs(c - (t - LEAD)) < EPS for c, t in zip(cadence, words_at)), f"{cadence[:4]}")
quote_end = max(float(w.end) for w in spoken if float(w.start) < 26.5)
check("...and out 0.35 s after the quote ends (then the 0.4 s exit)",
      abs(quote.end - (quote_end + K.QUOTE_TAIL + K.EXIT_SECONDS)) < EPS, f"{quote.end} vs {quote_end}")
qm = C.warp(quote, C._build(quote, BrandKit())[3])
lit = sorted((pts[0][0], sel) for (sel, prop), pts in qm.tracks.items() if sel.startswith("#qw") and prop == "opacity")
check("...the words light one by one on their own times (never before they're said)",
      len(lit) == len(cadence) and all(quote.start + t >= c - EPS for (t, _s), c in zip(lit, cadence)), f"{lit[:3]}")

# ======================= 3. no watermark numerals on a comparison =======================
page = C.scene(contrast, BrandKit())[0]
check("Comparison sides: no faint 01 / 02 watermark numerals", 'class="ix"' not in page and ".cmp .ix" not in page
      and not re.search(r'<div class="cmp"[^>]*>\s*<div[^>]*>0[12]<', page))
step_page = C.scene(steps, BrandKit())[0]
check("Step by step keeps its small 01 / 02 / 03 badges", all(f'id="d{k}"' in step_page and f">{k + 1:02d}</div>" in step_page
                                                            for k in range(3)))

# ======================= 4. readable captions under a chart =======================
edit = ClipEdit(0.0, 50.0, cards=[steps])
ass = D.calm_captions(talk, edit, tmp / "split.ass").read_text()
style = next(line for line in ass.splitlines() if line.startswith("Style: Split"))
size, bold = int(style.split(",")[2]), style.split(",")[7]
dialogue = [line for line in ass.splitlines() if line.startswith("Dialogue")]
check("Under a chart: one bold line of 38 px or more", size >= 38 and bold == "-1", style)
check("...on the seam: its bottom just above the card, in the speaker's viewport",
      dialogue and all(f"\\an2\\pos(540,{D.SEAM_CAPTION_BOTTOM})" in line and ",Split," in line for line in dialogue)
      and D.SPLIT_CAPTIONS[1] < C.CARD_TOP and D.SPLIT_CAPTIONS[0] > C.SPLIT_TOP * 0.8, f"{D.SPLIT_CAPTIONS} {dialogue[:1]}")
check("...one line each (at most 28 characters at 50 px)",
      all(len(line.split(",", 9)[9].split("}", 1)[1]) <= D.SPLIT_CAPTION_CHARS for line in dialogue))
check("The seam band never touches the Bento card", not CAP.overlaps(CAP.caption_band("split")))

# ======================= 5. Streamlit =======================
app = (ROOT / "app.py").read_text()
check("app.py: no deprecated use_container_width", "use_container_width" not in app and 'width="stretch"' in app)

# ======================= rendered: compact cards inside their band =======================
try:
    import playwright  # noqa: F401
    have_browser = True
except ImportError:
    have_browser = False
if have_browser:
    for card in (contrast, steps, quote):
        made = C.render_card(card, BrandKit(), tmp / "cards")
        last = cv2.imread(made.pattern % (made.count - 1), cv2.IMREAD_UNCHANGED)
        alpha = last[:, :, 3]
        rows = np.where(alpha.max(axis=1) > 0)[0]
        check(f"{card.template}: drawn on an {made.width}x{made.height} page under the speaker, inside the card's band",
              (made.width, made.height, made.top) == (1080, C.SPLIT_H, C.SPLIT_TOP) and rows.min() >= C.CARD_Y - 1
              and rows.max() <= C.CARD_Y + C.CARD_H + 1, f"rows {rows.min()}-{rows.max()}")

    # end to end: a camera with the face on the right; the split view shows it big, card right under it
    still = np.full((1080, 1920, 3), (40, 44, 52), np.uint8)
    cv2.circle(still, (1500, 380), 150, (255, 0, 255), -1)  # the "face", right of centre
    cv2.imwrite(str(tmp / "cam.png"), still)
    source = tmp / "cam.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", "25", "-i", str(tmp / "cam.png"),
                    "-t", "16", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", "-pix_fmt", "yuv420p", str(source)],
                   check=True)
    base = tmp / "base.mp4"  # the full-screen clip: the face's 9:16 crop
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-vf", "crop=608:1080:1196:0,scale=1080:1920",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(base)], check=True)
    card = Card("step_sequence", 4.0, 12.0, rows=steps.rows)
    frames = C.render_card(card, BrandKit(), tmp / "cards")
    out = D.compose_cards(base, source, ClipEdit(0.0, 16.0, cards=[card]), [frames], [], tmp / "split.mp4",
                          crop_at=lambda t: (1196.0, 0.0, 608.0, 1080.0))
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "9", "-i", str(out), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    frame = np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3)
    face = (frame[:, :, 0] > 200) & (frame[:, :, 1] < 80) & (frame[:, :, 2] > 200)
    ys, xs = np.where(face)
    vx = D.band_rect(1920, 1080, 1500.0)[0]  # the face sits right of centre: the crop stops at the frame's edge
    check("End to end: the face is in the speaker's viewport (top 1040 rows), where the crop puts it, and big",
          len(ys) and ys.max() < C.SPLIT_TOP and abs(xs.mean() - (1500 - vx) * 1080 / 1120) < 6 and (xs.max() - xs.min()) > 250,
          f"rows {ys.min() if len(ys) else None}-{ys.max() if len(ys) else None}, x {xs.mean() if len(xs) else None}")
    canvas = np.abs(frame[C.SPLIT_TOP + 2:C.CARD_TOP - 2].astype(int) - [4, 5, 7]).max()
    below = frame[C.CARD_BOTTOM + 4:].astype(int)
    check("...the panel under it is only the card's 16 px gap, then the card, then the 152 px bottom margin",
          canvas < 12 and np.abs(below - [4, 5, 7]).max() < 12, f"{canvas} / {np.abs(below - [4, 5, 7]).max()}")
else:
    print("(Playwright not installed: the rendered checks were skipped)")

print(f"\n{'All Round 39 checks passed.' if not fails else f'{fails} check(s) failed.'}")
sys.exit(1 if fails else 0)
