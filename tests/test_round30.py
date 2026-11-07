"""Round 30 checks: the move into and out of a chart (one continuous span, no plain frame
in between, the blurred layout's picture glides into the band, captions only once the
panel has landed), more visuals (2-3 per clip, the micro-stat, the "in context" layer
under a chart built only from what was said), Remove clip in Make clips (carried to the
preview page and Drive), embedded charts and screen shares shown whole, name cards never
on the captions, and the worker tuned to B2B, SaaS, VC, consulting and finance.
Temp files and fakes only: no AI, YouTube, R2 or real faces.

Run from the project root:  .venv/bin/python tests/test_round30.py .
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
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


import cv2  # noqa: E402

import core.card_checks as K  # noqa: E402
import core.dynamic_layout as D  # noqa: E402
import core.face_tracker as F  # noqa: E402
import core.graphics_director as G  # noqa: E402
from core.cards import Frames, render_card  # noqa: E402
from core.edl import MODES, TEMPLATES, BrandKit, Card, CardRow, ClipEdit  # noqa: E402
from core.face_tracker import CropBox  # noqa: E402
from core.transcriber import Word  # noqa: E402
from core.video_renderer import render_clip  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="round30-"))


def words(text, start=100.0, step=0.32):
    return [Word(t, start + k * step, start + k * step + 0.28) for k, t in enumerate(text.split())]


# ---------- the move into a chart, on the blurred-background layout, at 24 fps ----------
RATE = 24
# A still camera picture: a soft gradient with one white stripe across it at row 300, so
# the picture's position can be read off every frame.
yy, xx = np.mgrid[0:1080, 0:1920]
picture = np.dstack([(60 + 80 * xx / 1920), (90 + 60 * yy / 1080), (120 + 40 * np.sin(xx / 300))]).astype(np.uint8)
picture[294:306] = 255
cv2.imwrite(str(tmp / "still.png"), cv2.cvtColor(picture, cv2.COLOR_RGB2BGR))
source = tmp / "camera.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", str(RATE), "-i", str(tmp / "still.png"),
                "-f", "lavfi", "-i", "sine=d=40", "-t", "40", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)], check=True)
ass = tmp / "none.ass"
ass.write_text("[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, "
               "PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
               "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
               "Style: Default,Arial,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,"
               "10,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
clip_start = 2.0
base = render_clip(source, ass, CropBox(0, 0, 608, 1080), clip_start, clip_start + 20.0, tmp / "base.mp4",
                   layout_mode="blurred_background")
chart = tmp / "chart"
chart.mkdir()
page = np.zeros((1460, 1080, 4), np.uint8)
page[200:260, 100:900] = (240, 200, 40, 255)  # a bar on the panel
for k in range(240):
    cv2.imwrite(str(chart / f"f_{k:05d}.png"), page)
frames = Frames(str(chart / "f_%05d.png"), 240, 1080, 1460, 460)
a_clip = 8.15  # clip seconds: the move starts between frames, as in your clip 3
card = Card("metric_bars", clip_start + a_clip, clip_start + a_clip + 7.0,
            rows=[CardRow("Nikkei 225", "−5%"), CardRow("SoftBank", "−10%")])
made = D.compose_cards(base, source, ClipEdit(clip_start, clip_start + 20.0, cards=[card]), [frames], [],
                       tmp / "moved.mp4", layout="blurred_background")
count = round(D.MOVE * RATE)
first_in = int(np.ceil(a_clip * RATE - 1e-6))
last_out = int(np.ceil((a_clip + 7.0) * RATE - 1e-6))
first_out = last_out - count


class Frames24:
    """Only the frames around the moves, decoded once (the whole clip would be 3 GB)."""

    def __init__(self, path, ranges):
        self.frames = {}
        for lo, hi in ranges:
            raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select='between(n\\,{lo}\\,{hi})'",
                                  "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                 capture_output=True, check=True).stdout
            for k, frame in enumerate(np.frombuffer(raw, np.uint8).reshape(-1, 1920, 1080, 3)):
                self.frames[lo + k] = frame

    def __getitem__(self, n):
        return self.frames[n]


video = Frames24(made, [(first_in - 3, first_in + count + 4), (first_in + count + 5, first_in + count + 40),
                        (first_out - 3, first_out + count + 2)])


def panel_edge(frame):
    rows = (np.abs(frame.astype(int) - [5, 6, 8]).max(axis=2) < 10).mean(axis=1)
    return next((y for y in range(40, 1920) if rows[y] > 0.98 and rows[min(1919, y + 30)] > 0.98), 1920)


def stripe(frame):
    """The white stripe's row in the sharp picture (columns 300-800, rows above the panel)."""
    rows = frame[:, 300:800].astype(int).min(axis=2).mean(axis=1)
    candidates = [y for y in range(1920) if rows[y] > 235]
    return int(np.median(candidates)) if candidates else None


edges = [panel_edge(video[n]) for n in [*range(first_in, first_in + count + 40), *range(first_out, first_out + count)]]
check("24 fps: from the first move frame on, every frame is the move or the chart (no plain frame)",
      all(edge <= 470 for edge in edges[count:-count]) and min(edges[:count]) <= 470, str(edges[:count + 2]))
check("...the panel rises steadily in and falls steadily out", edges[:count] == sorted(edges[:count], reverse=True)
      and edges[-count:] == sorted(edges[-count:]), f"{edges[:count]} / {edges[-count:]}")
tall = 608
expected_base = 656 + round(300 * tall / 1080)  # the stripe in the clip's own layout
expected_band = round(300 * tall / 1080) - D.strip_crop_top(tall)  # ...and in the chart view's top band
path = [stripe(video[n]) for n in range(first_in - 1, first_in + count + 2)]
check("The picture glides from its place in the clip to the top band, a little each frame",
      path[0] is not None and abs(path[0] - expected_base) <= 2 and all(p is not None for p in path)
      and path == sorted(path, reverse=True), f"{path} (from {expected_base} to {expected_band})")
check("...and lands exactly on the chart view's band: the last move frame equals the steady view (no 1-2 px snap)",
      path[count] == path[count + 1] == path[count + 2] and abs(path[count] - expected_band) <= 1,
      f"{path[count:]} vs {expected_band}")
check("The chart view's band starts at the picture's row 52, as FFmpeg crops it", D.strip_crop_top(608) == 52)
plain, first_move = video[first_in - 1], video[first_in]
backdrop_diff = float(np.abs(cv2.resize(first_move[:600], (135, 75)).astype(int)
                             - cv2.resize(plain[:600], (135, 75)).astype(int)).mean())
check("The move's blurred background is the clip's own (no flash of a different background)", backdrop_diff < 3,
      f"{backdrop_diff:.1f}")
out_path = [stripe(video[n]) for n in range(first_out - 1, first_out + count + 1)]
check("Leaving: the picture glides back and the last move frame is the clip's own picture",
      out_path == sorted(out_path) and abs(out_path[-2] - expected_base) <= 2 and out_path[-1] == out_path[-2],
      str(out_path))
band = D.picture_band("blurred_background", 1920, 1080)
check("The blurred layout's picture band: 1080 x 608 at row 656 (none for other layouts)",
      band == (0.0, 656.0, 1080.0, 608.0) and D.picture_band("smart_crop", 1920, 1080) is None, str(band))

# ---------- captions under a chart only once the panel has landed ----------
talk = words("so the Nikkei 225 was down 5% overnight and SoftBank which is all geared towards AI "
             "that stock was down 10% overnight and people panicked", start=100.0)
split_card = Card("metric_bars", 100.0, 106.5, rows=[CardRow("Nikkei 225", "−5%"), CardRow("SoftBank", "−10%")])
edit = ClipEdit(96.0, 120.0, cards=[split_card])
calm = D.calm_captions(talk, edit, tmp / "calm.ass", None, 24).read_text()
landed, leaving = D.landed(split_card, edit, 24)
times = [line.split(",")[1:3] for line in calm.splitlines() if line.startswith("Dialogue")]


def seconds(stamp):
    h, m, rest = stamp.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


check("Under a chart, the small captions start once the panel is in and end before it leaves",
      times and all(seconds(a) + edit.start >= landed - 0.01 and seconds(b) + edit.start <= leaving + 0.01
                    for a, b in times), f"{times[:2]} landed {landed - edit.start:.2f}")

# ---------- more visuals: 2-3 per clip, the micro-stat, the context layer ----------
check("Up to 3 graphics in a clip (2 under 25 s); 2 charts, 3 over the speaker",
      G.max_graphics(40) == 3 and G.max_graphics(20) == 2 and K.MAX_CARDS == 2 and K.MAX_OVERLAYS == 3
      and K.MAX_GRAPHICS == 3)
check("The micro-stat is in the library, over the speaker", "stat_pill" in TEMPLATES and MODES["stat_pill"] == "overlay")
money = words("and the big thing is we are at 40 million in ARR now after only 18 months of selling it to banks",
              start=100.0)
pill = Card("stat_pill", 103.0, 107.0, rows=[CardRow("in ARR", "40M")])
res = K.check_card(pill, money, 96.0, 130.0, 3.0)
check("A micro-stat of a number said, with the speaker's words", res.ok, str(res.errors))
bad = Card("stat_pill", 103.0, 107.0, rows=[CardRow("in ARR", "50M")])
check("...never a number that wasn't said", not K.check_card(bad, money, 96.0, 130.0, 3.0).ok)
trend = words("revenue was 2 million in 2022 then 5 million in 2023 and 11 million this year in 2024", start=100.0)
series = Card("stat_pill", 100.0, 105.0, rows=[CardRow("2022", "2M"), CardRow("2023", "5M"), CardRow("2024", "11M")])
res = K.check_card(series, trend, 96.0, 130.0, 3.0)
check("A micro-stat trend: 2-4 numbers, each labelled with when it is", res.ok, str(res.errors))
unlabelled = Card("stat_pill", 100.0, 105.0, rows=[CardRow("", "2M"), CardRow("", "5M")])
check("...each point needs its when", not K.check_card(unlabelled, trend, 96.0, 130.0, 3.0).ok)

ratio = Card("metric_bars", 100.0, 110.0, headline="Overnight sell-off",
             rows=[CardRow("Nikkei 225", "−5%"), CardRow("SoftBank", "−10%", highlight=True)],
             context={"kind": "ratio", "of": [2, 1], "text": "SoftBank's drop vs the Nikkei's"})
res = K.check_card(ratio, talk, 96.0, 120.0, 3.0)
check("In context: one number as a multiple of another, worked out from the chart (2×)",
      res.ok and ratio.context.get("value") == "2×", f"{ratio.context} {res.warnings}")
padded = Card("metric_bars", 100.0, 110.0, rows=[CardRow("Nikkei 225", "−5%"), CardRow("SoftBank", "−10%")],
              context={"kind": "ratio", "of": [2, 1], "text": "SoftBank fell twice as far"})
res = K.check_card(padded, talk, 96.0, 120.0, 3.0)
check("...its line can't carry a number of its own (the chart stays, the layer goes)",
      res.ok and not padded.context and any("number of its own" in w for w in res.warnings), str(res.warnings))
share_talk = words("we raised 100 million and we put 40 million of it into one company", start=100.0)
share = Card("metric_bars", 100.0, 110.0, rows=[CardRow("One company", "40M"), CardRow("The fund", "100M")],
             context={"kind": "share", "of": [1, 2], "text": "of the fund in a single company"})
res = K.check_card(share, share_talk, 96.0, 120.0, 3.0)
check("...a part of a whole (40%)", res.ok and share.context.get("value") == "40%", f"{share.context} {res.warnings}")
earlier = Card("metric_bars", 100.0, 110.0, rows=[CardRow("Nikkei 225", "−5%"), CardRow("SoftBank", "−10%")],
               context={"kind": "earlier", "value": "$100B", "text": "the Vision Fund it runs",
                        "said": "SoftBank runs the Vision Fund which is about 100 billion dollars", "at": 312.0})
res = K.check_card(earlier, talk, 96.0, 120.0, 3.0)
check("...a number the speaker said elsewhere in the same video, with that sentence",
      res.ok and earlier.context.get("value") == "$100B", f"{earlier.context} {res.warnings}")
made_up = Card("metric_bars", 100.0, 110.0, rows=[CardRow("Nikkei 225", "−5%"), CardRow("SoftBank", "−10%")],
               context={"kind": "earlier", "value": "$300B", "text": "the Vision Fund it runs",
                        "said": "SoftBank runs the Vision Fund which is about 100 billion dollars", "at": 312.0})
res = K.check_card(made_up, talk, 96.0, 120.0, 3.0)
check("...never a figure that isn't in the sentence it quotes", res.ok and not made_up.context, str(res.warnings))
facts = G.earlier_facts(words("the fund is 100 billion dollars . we hired people . " * 1, start=10.0)
                        + talk + words("and the index is up 30 percent this year .", start=200.0), 100.0, 110.0, talk)
check("The director is offered numbers said elsewhere in the video (never inside the clip)",
      len(facts) == 2 and all("billion" in t or "percent" in t for _at, t in facts), str(facts))
direction_answer = {"graphics": [{"template": "metric_bars", "first_sentence": 1, "last_sentence": 1, "strength": 8,
                                  "items": [{"label": "Nikkei 225", "value": "−5%"}, {"label": "SoftBank", "value": "−10%"}],
                                  "context": {"kind": "earlier", "ref": "E1", "value": "$100B", "text": "x"}}]}
proposed = G.proposals(direction_answer, [(100.0, 106.0, "so the Nikkei...")], [(312.0, "SoftBank runs ... 100 billion")])
check("...and an 'earlier' layer carries the transcript's own sentence and time, not the AI's",
      proposed and proposed[0][1].context.get("said") == "SoftBank runs ... 100 billion"
      and proposed[0][1].context.get("at") == 312.0, str(proposed[0][1].context if proposed else None))

three = ClipEdit(96.0, 140.0, cards=[Card("kinetic_range", 103.0, 106.0, rows=[CardRow("", "1")]),
                                     Card("stat_pill", 110.0, 113.0, rows=[CardRow("", "2")]),
                                     Card("buckets", 117.0, 121.0, rows=[CardRow("A"), CardRow("B")]),
                                     Card("stat_pill", 125.0, 128.0, rows=[CardRow("", "3")])])
results = K.check_edit(three, [], 3.0)
check("At most 3 data graphics in a clip", any("At most 3 data graphics" in e for e in results[3].errors)
      or any("At most 3 number" in e for e in results[3].errors), str(results[3].errors))
two_charts = ClipEdit(96.0, 126.0, cards=[Card("metric_bars", 100.0, 110.0, rows=[CardRow("a", "1"), CardRow("b", "2")]),
                                          Card("metric_bars", 112.0, 121.0, rows=[CardRow("a", "1"), CardRow("b", "2")])])
results = K.check_edit(two_charts, [], 3.0)
check("Two charts may not cover over 55% of the clip (the speaker stays the star)",
      any("55%" in e for e in results[1].errors), str(results[1].errors))

ctx_card = Card("metric_bars", 100.0, 110.0, headline="Overnight sell-off",
                rows=[CardRow("Nikkei 225", "−5%"), CardRow("SoftBank", "−10%", highlight=True)],
                context={"kind": "ratio", "of": [2, 1], "text": "SoftBank's drop vs the Nikkei's"})
K.check_card(ctx_card, talk, 96.0, 120.0, 3.0)
drawn = render_card(ctx_card, BrandKit(), tmp / "cards")
last = cv2.imread(drawn.pattern % (drawn.count - 30), cv2.IMREAD_UNCHANGED)
from core import cards as C31  # noqa: E402
footer = last[C31.CARD_Y + C31.CARD_H - C31.FOOTER_H + 2:C31.CARD_Y + C31.CARD_H - 2, C31.CARD_X + 20:C31.CARD_X + C31.CARD_W - 20]
lit = (footer[:, :, :3].max(axis=2) > 150) & (footer[:, :, 3] > 200)
check("The context layer is drawn in the card's footer (Round 31: the 'In context' strip)", int(lit.sum()) > 400, str(int(lit.sum())))
pill_frames = render_card(pill, BrandKit(), tmp / "cards")
check("The micro-stat is drawn small, on frosted glass, top left", pill_frames.blur and pill_frames.blur[0][1] < 400
      and pill_frames.blur[0][2] < 640, str(pill_frames.blur))

# ---------- name cards and micro-stats never on the captions ----------
clip_edit = ClipEdit(100.0, 140.0)
low_card = Card("speaker_title", 104.2, 107.5, rows=[CardRow("Jonathan Bragdon", detail="Founder")],
                place="540,1200,1.00,center")
moved = D.clear_of_captions(low_card, clip_edit)
y = float(moved.split(",")[1])
check("A name card that would touch the small captions is moved just clear of them",
      moved != low_card.place and y + 166 + D.CAPTION_CLEARANCE <= D.CALM_CAPTIONS[0], moved)
fine = Card("speaker_title", 104.2, 107.5, rows=[CardRow("Jonathan Bragdon")], place="540,150,1.00,center")
check("...a card clear of them stays where the placer put it", D.clear_of_captions(fine, clip_edit) == fine.place)
check("A micro-stat in its corner keeps the bold captions (no caption switch for it)",
      D.keeps_bold_captions(Card("stat_pill", 104.0, 107.0, rows=[CardRow("", "1")], place="56,190,1.00,left"), clip_edit))

# ---------- embedded charts and screen shares are shown whole ----------
slide = np.full((360, 640, 3), 245, np.uint8)
for row in range(60, 300, 28):  # lines of text on a white page
    cv2.putText(slide, "Revenue grew 40% to $12M in Q3 2025", (120, row), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (20, 20, 20), 1)
tiles = [(30 * 3, 0.0, 60 * 3, 30 * 3), (610 * 3, 0.0, 60 * 3, 30 * 3)]  # two webcam tiles' faces, 30 px wide
check("A page of text with small webcam tiles beside it is content (shown whole, not cropped or split)",
      F._content_like(slide, tiles, 3.0, 1920))
camera = cv2.GaussianBlur((np.random.default_rng(3).random((360, 640, 3)) * 255).astype(np.uint8), (0, 0), 2)
check("...a talking head (a face 20% of the width) never is",
      not F._content_like(camera, [(960.0, 0.0, 540.0, 384.0)], 3.0, 1920)
      and not F._content_like(slide, [(960.0, 0.0, 540.0, 384.0)], 3.0, 1920))
black = np.zeros((360, 640, 3), np.uint8)
check("...nor a blank screen (no text or lines)", not F._content_like(black, [], 3.0, 1920))
call = np.full((360, 640, 3), 26, np.uint8)  # a call layout: two webcam tiles beside a shared post
call[100:255, 8:130] = (120, 90, 70)
call[100:255, 510:632] = (110, 100, 80)
call[10:350, 222:420] = 250
for row in range(40, 330, 24):
    cv2.putText(call, "margins and outcomes", (230, row), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1)
tile_faces = [(69 * 3, 0.0, 170 * 3, 40 * 3), (571 * 3, 0.0, 170 * 3, 40 * 3)]
box = F._content_box(call, tile_faces, 3.0)
check("A shared post between two webcam tiles is found: its centre and width (the tiles are left out)",
      box is not None and abs(box[0] - 321 * 3) < 12 and abs(box[1] - 198 * 3) < 20, str(box))
check("...it fits the 9:16 crop (608 of 1920 px), so it's framed on and shown big, full height",
      box is not None and box[1] <= 608 * F.CONTENT_CROP_FIT)
check("A chart filling the frame (no call background around it) is shown as the whole frame",
      F._content_box(slide, [], 3.0) is None)

# ---------- the worker's niche ----------
import core.lead_harvester as L  # noqa: E402
import core.agent_brain as A  # noqa: E402

check("Core-market channels rank higher (SaaS, VC, finance terms)",
      L.niche_fit("Weekly interviews with SaaS founders and venture investors on ARR, pricing and fundraising",
                  ["How we hit $10M ARR", "VC fund math explained"]) >= 6)
check("Self-help channels with no business terms are off niche",
      L.off_niche("Daily motivation and mindset for your best life", ["Morning routine for success", "Manifest it"])
      and not L.off_niche("Mindset for SaaS founders", ["Pricing your SaaS"]))
check("The B2B search aims at SaaS, VC, consulting and finance shows",
      "venture capital" in L._CATEGORY_GUIDANCE["B2B"] and "unit economics" in L.B2B_QUERY_HINT)
check("Clip picking prefers unit economics, market structure, GTM and valuation over self-help and banter",
      "unit economics" in A._SYSTEM_PROMPT and "banter" in A._SYSTEM_PROMPT)

# ---------- Remove clip in Make clips: off the preview page, Drive and this Mac ----------
store = tmp / "store"
store.mkdir()
videos = []
for k in range(3):
    clip = tmp / f"clip_0{k + 1}.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=108x192:d=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    videos.append(str(clip))
BASE = "https://clips.example.com"
wrapper = tmp / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys; sys.path.insert(0, {str(ROOT)!r})
    from pathlib import Path
    from types import SimpleNamespace
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(store)!r})
    for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, name, W.STORAGE_DIR / file)
    W.get_worker_status = lambda: {{"running": False}}
    from core import clip_pages as C
    C.configured = lambda: True
    C.page_settings = lambda: SimpleNamespace(base_url={BASE!r})
    C.MANUAL_PAGES_PATH = Path({str(store / "manual_pages.json")!r})
    C.page_activity = lambda: {{}}
    calls = Path({str(store / "calls.json")!r})
    def log(*entry):
        old = __import__("json").loads(calls.read_text()) if calls.exists() else []
        calls.write_text(__import__("json").dumps(old + [list(entry)]))
    def fake_publish(channel, title, clips, kind="video", slug=""):
        log("publish", len(clips))
        return "dana-growth-k7m2qx", {BASE!r} + "/dana-growth-k7m2qx"
    C.publish_page = fake_publish
    C.remove_clip = lambda name, index, count: log("page", name, index, count) or count - 1
    import core.drive_uploader as DU
    DU.remove_named_clip_in_drive = lambda link, name, count: log("drive", name, count) or True
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
from streamlit.testing.v1 import AppTest  # noqa: E402
from core.hook_selector import ClipResult  # noqa: E402

at = AppTest.from_file(str(wrapper), default_timeout=180)
at.session_state["generated_clips"] = [
    (path, ClipResult(start_time=10.0 + 40 * k, end_time=45.0 + 40 * k, viral_title=f"Moment {k + 1}", caption="",
                      hashtags=[], virality_score=90)) for k, path in enumerate(videos)]
at.session_state["agent_channel_name"] = "Dana Growth Co"
at.session_state["agent_video_title"] = "Ep 41: pricing retainers"
at.session_state["drive_folder_link"] = "https://drive.google.com/drive/folders/abc123"
at.run()
check("App runs", not at.exception, str([e.value for e in at.exception][:1]))
publish = next((b for b in at.button if b.label == "🌐 Publish to Client Preview Page"), None)
publish.click().run()
remove = next((b for b in at.button if b.label == "Yes, remove clip 2"), None)
check("Each clip has 🗑 Remove clip (confirm in a pop-over)", remove is not None, str([b.label for b in at.button][:12]))
remove.click().run()
calls = json.loads((store / "calls.json").read_text())
check("Removing clip 2 takes it off the published preview page (clip 2 of 3)",
      ["page", "dana-growth-k7m2qx", 2, 3] in calls, str(calls))
check("...and out of the Google Drive folder", any(c[0] == "drive" and c[1] == "clip_02.mp4" for c in calls), str(calls))
left = at.session_state["generated_clips"]
check("...and out of Make clips: the other two stay, in order",
      [Path(p).name for p, _c in left] == ["clip_01.mp4", "clip_03.mp4"], str([Path(p).name for p, _c in left]))
check("...the page record says 2 clips, and a later Remove uses the new order",
      json.loads((store / "manual_pages.json").read_text())[0]["clips"] == 2
      and [Path(p).name for p in at.session_state["published_page"]["paths"]] == ["clip_01.mp4", "clip_03.mp4"])
check("No error shown", not at.error, str([e.value for e in at.error]))

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
