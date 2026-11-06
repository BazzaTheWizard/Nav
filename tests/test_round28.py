"""Round 28 checks: the camera (crop switches on the exact frame of each cut, quick shots
framed, no opening sliver, wide two-person shots, the dead zone), clean clip edges, the
graphics director (restraint, checks, name card) wired into every render, and the app
cleanup. Temp files and fakes only: no AI, YouTube, R2 or real faces.

Run from the project root:  .venv/bin/python tests/test_round28.py .
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


import core.face_tracker as F  # noqa: E402
from core.transcriber import Word, clip_words_from_transcript  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="round28-"))
FPS = 30000 / 1001

# ---------- a 29.97 fps test video: shot A (person on the left), a quick shot B (person on the right), A ----------
W_, H_ = 1920, 1080
SHOTS = [(0, 90, "A"), (90, 112, "B"), (112, 180, "A")]  # frame ranges: B is 22 frames (0.73 s)
GREEN, MAGENTA, DARK = (40, 220, 60), (230, 40, 220), (30, 30, 30)


def frame_for(kind):
    img = np.full((H_, W_, 3), DARK, np.uint8)
    if kind == "A":
        img[:, :640] = GREEN
    else:
        img[:, 1280:] = MAGENTA
    return img


source = tmp / "multicam.mp4"
proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W_}x{H_}",
                         "-r", "30000/1001", "-i", "-", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-shortest",
                         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-g", "15",
                         str(source)], stdin=subprocess.PIPE)
for a, b, kind in SHOTS:
    for _ in range(a, b):
        proc.stdin.write(frame_for(kind).tobytes())
proc.stdin.close()
proc.wait()


class FakeDetector:
    """A "face" where the coloured block is: green on the left, magenta on the right."""
    available = True

    def detect(self, frame):
        h, w = frame.shape[:2]
        found = []
        if frame[:, : w // 3, 1].mean() > 150:  # green
            found.append((w * 0.17, h * 0.4, w * 0.06, h * 0.1, 0.9))
        if frame[:, 2 * w // 3:, 0].mean() > 150 and frame[:, 2 * w // 3:, 2].mean() > 150:  # magenta (BGR)
            found.append((w * 0.83, h * 0.4, w * 0.06, h * 0.1, 0.9))
        return found


F._face_detector = lambda: FakeDetector()
start, end = 0.517, 5.5  # a clip that doesn't start on a frame
plan = F.analyze_crop_plan(source, start, end)
cut1, cut2 = 90 / FPS, 112 / FPS
check("Cuts found at the source's own frames (no 30 fps resampling)", len(plan.cuts) == 2, str(plan.cuts))
check("Each switch sits between the last frame of one shot and the first of the next",
      len(plan.cuts) == 2 and 89 / FPS < plan.cuts[0] < cut1 and 111 / FPS < plan.cuts[1] < cut2,
      f"{plan.cuts} vs frames at {cut1:.4f}, {cut2:.4f}")
xs = [shot.x for shot in plan.shots]
check("The quick 0.73 s shot is framed on its own person (was: the shot before's framing)",
      len(plan.shots) == 3 and xs[1] > 1000 and xs[0] < 400 and xs[2] < 400, str(plan.shots))

from core.video_renderer import render_clip  # noqa: E402

ass = tmp / "empty.ass"
ass.write_text("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n"
               "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
               "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
               "MarginR, MarginV, Encoding\nStyle: Default,Arial,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,"
               "100,100,0,0,1,1,0,2,10,10,10,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
               "MarginV, Effect, Text\n")


def colours(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=108:192", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, 192, 108, 3)
    return ["G" if f[96, 54, 1] > 150 and f[96, 54, 0] < 120 else "M" if f[96, 54, 0] > 150 and f[96, 54, 2] > 150 else "?"
            for f in frames]


out = render_clip(source, ass, plan.crop, start, end, tmp / "framed.mp4", layout_mode="smart_crop",
                  crop_x_expression=plan.x_expression(start, end), holds=plan.holds(start, end))
seen = colours(out)
first = int(np.floor(start * FPS + 1e-9))  # the first frame: the one on screen at the clip's start
expected = ["M" if 90 <= first + n < 112 else "G" for n in range(len(seen))]
wrong = [n for n, (a, b) in enumerate(zip(seen, expected)) if a != b]
check("Rendered: every frame shows its own shot's person, even right at the cuts (no one-frame flash)",
      not wrong and len(seen) >= 140, f"wrong frames {wrong[:10]} of {len(seen)}")

# ---------- a clip that opens on a 0.3 s sliver of the other camera ----------
sliver_start = cut1 - 0.3
held = F.analyze_crop_plan(source, sliver_start, 5.5)
head, tail = held.holds(sliver_start, 5.5)
check("A cut 0.3 s into the clip is a sliver: the opening is held", abs(head - 0.3) < 0.02 and tail == 0, str((head, tail)))
out = render_clip(source, ass, held.crop, sliver_start, 5.5, tmp / "held.mp4", layout_mode="smart_crop",
                  crop_x_expression=held.x_expression(sliver_start, 5.5), holds=(head, tail))
seen = colours(out)
check("...so the clip opens on the main shot, framed, never on a flash of the other camera",
      seen[:9] == ["M"] * 9 and "G" not in seen[:20], "".join(seen[:30]))
check("...and keeps its length", abs(float(subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
    capture_output=True, text=True).stdout) - (5.5 - sliver_start)) < 0.1)
both_start, both_end = cut1 - 0.3, cut2 + 0.3  # a sliver of the other camera at each end
both = F.analyze_crop_plan(source, both_start, both_end)
out = render_clip(source, ass, both.crop, both_start, both_end, tmp / "both.mp4", layout_mode="smart_crop",
                  crop_x_expression=both.x_expression(both_start, both_end), holds=both.holds(both_start, both_end))
seen = colours(out)
check("A sliver at both ends: held at both ends, the quick shot fills the clip, no frame re-timed",
      seen == ["M"] * len(seen) and abs(len(seen) - (both_end - both_start) * FPS) <= 2, "".join(seen))


# ---------- a wide two-person shot framed high near the edges ----------
class WideDetector:
    available = True

    def detect(self, frame):
        h, w = frame.shape[:2]  # two people high up near the edges (a podcast's wide shot)
        return [(w * 0.165, h * 0.21, w * 0.078, h * 0.13, 0.9), (w * 0.845, h * 0.29, w * 0.057, h * 0.1, 0.85)]


class WebcamDetector:
    available = True

    def detect(self, frame):
        h, w = frame.shape[:2]
        return [(w * 0.88, h * 0.10, w * 0.045, h * 0.08, 0.9)]


F._face_detector = lambda: WideDetector()
wide = F.analyze_crop_plan(source, 0.5, 2.8)
check("A wide shot with two people high up near the edges is two people (was: two 'webcams', a tiny full frame)",
      wide.shots[0].group and wide.shots[0].speakers is not None and wide.split_view(0.5, 2.8) is not None
      and not wide.full_frame_ranges(0.5, 2.8), str(wide.shots[0]))
F._face_detector = lambda: WebcamDetector()
pip = F.analyze_crop_plan(source, 0.5, 2.8)
check("...a lone small face in a corner is still a screen-share webcam (whole frame)", not pip.shots[0].has_face)

# ---------- the dead zone ----------
crop_w, frame_w = 810, 2560
still = [(t / 2, 600 + 405 + (40 if t % 2 else -40)) for t in range(20)]
check("Head movement inside the dead zone never moves the crop", F._reframes(still, 600, crop_w, frame_w, 100.0) == ())
lean = [(t / 2, 1005 if t < 6 else 1005 + 330) for t in range(20)]
moves = F._reframes(lean, 600, crop_w, frame_w, 100.0)
check("A real move (outside the dead zone for 2 s) re-frames once, from when it started",
      len(moves) == 1 and abs(moves[0][0] - 103.0) < 1e-6 and moves[0][1] > 800, str(moves))
blip = [(t / 2, 1005 + (330 if 6 <= t < 8 else 0)) for t in range(20)]
check("A brief lean (1 s) doesn't", F._reframes(blip, 600, crop_w, frame_w, 100.0) == ())
moving = F.CropPlan(2560, 1440, 810, 1440, 0, (F.Shot(100.0, 110.0, 600, True, False, None, ((103.0, 930),)),))
expression = moving.x_expression(100.0, 110.0)
check("The re-frame is an eased pan, not a jump", "between(t,3.000,3.800)" in expression and "(3-2*" in expression, expression)
check("No re-frames: the same expressions as before", F.CropPlan(2560, 1440, 810, 1440, 0, (
    F.Shot(100.0, 104.0, 600, True, False), F.Shot(104.0, 110.0, 900, True, False))).x_expression(100.0, 110.0)
    == "if(lt(t,4.000),600,900)")

# ---------- clean clip edges ----------
words = [Word("lectures.", 9.6, 10.15), Word("Someone", 10.2, 10.6), Word("said", 10.6, 11.0), Word("it.", 11.0, 11.4),
         Word("What", 11.45, 11.7), Word("if", 11.7, 11.9)]
inside = clip_words_from_transcript(words, 10.05, 11.52)
check("No caption flash for the tail of the sentence before or a word mostly cut off at the end",
      [w.text for w in inside] == ["Someone", "said", "it."], str([w.text for w in inside]))
from core import hook_selector as HS  # noqa: E402

spoken = [Word("before.", 0.0, 2.05)] + [Word(f"w{i}." if i % 8 == 7 else f"w{i}", 2.0 + i * 0.4, 2.38 + i * 0.4) for i in range(120)]
sentences = HS._sentence_spans(spoken) if hasattr(HS, "_sentence_spans") else None
ordered = sorted(spoken, key=lambda w: (w.start, w.end))
spans = HS._collect_spans(ordered, use_pauses=False)
bounds = HS._clip_bounds(spans, 1, 10, ordered, 200.0)
opening = clip_words_from_transcript(spoken, *bounds) if bounds else None
check("A clip never opens on the end of the sentence before (no caption of it, and no earlier start)",
      bounds is not None and bounds[0] >= 2.0 - 1e-9 and opening and opening[0].text == "w0", str(bounds))

# ---------- the graphics director ----------
from core import graphics_director as G  # noqa: E402
from core import card_checks as K  # noqa: E402
from core.edl import Card, CardRow  # noqa: E402


def words_from(text, start=0.0, step=0.3):
    out, t = [], start
    for token in text.split():
        out.append(Word(token, round(t, 2), round(t + step - 0.02, 2)))
        t += step
    return out


TALK = ("Thanks for having me on the show today, it is really great to be here with all of you this morning. "
        "We grew revenue from $2 million to $8 million in one year, which nobody expected at all. "
        "And honestly it was about five trillion dollars of market value that moved that week in the end. "
        "I have trouble with six times seven to this day, and I always have, ever since I was a small kid. "
        "The real lesson is that patience wins over the long run, every single time, for every single one of us.")
talk = words_from(TALK, start=100.0, step=0.36)  # a 37 s clip: room for 2 graphics
clip_end = talk[-1].end + 0.3
spans = G.sentences(talk)
INFO = G.VideoInfo(title="Founders Podcast: Dana Reyes on growth", channel="Founders Podcast",
                   description="Our guest Dana Reyes is the CEO of Northwind Labs.")


def answer(*graphics, speaker=None):
    return lambda prompt, system: json.dumps({"graphics": list(graphics), "speaker": speaker})


def idea(template, first, last, strength, items, headline=""):
    return {"template": template, "first_sentence": first, "last_sentence": last, "strength": strength,
            "headline": headline, "items": items, "reason": "test"}


change = idea("kinetic_range", 2, 2, 9, [{"value": "$2M → $8M", "label": ""}])
hedged = idea("kinetic_range", 3, 3, 8, [{"value": "$5T", "label": ""}])
fluff = idea("kinetic_range", 4, 4, 5, [{"value": "6 × 7", "label": ""}])
wrong = idea("kinetic_range", 5, 5, 9, [{"value": "$9M"}])
direction = G.direct(talk, 100.0, clip_end, info=INFO, hook_seconds=4.0, ask=answer(change, hedged, fluff, wrong))
kinds = [(c.template, c.rows[0].value) for c in direction.edit.cards]
check("Director: real data moments get a graphic, each checked", ("kinetic_range", "$2M → $8M") in kinds, str(direction.notes))
check("...the speaker's hedge is added, not dropped (\"about five trillion\" -> ~$5T)", ("kinetic_range", "~$5T") in kinds,
      str(direction.notes))
check("...a casual number scored low is skipped", any("not strong enough" in n for n in direction.notes))
check("...a number that wasn't said never gets on screen", not any(c.rows[0].value == "$9M" for c in direction.edit.cards)
      and any("Dropped" in n and "$9M" in n for n in direction.notes), str(direction.notes))
check("...and every graphic passes the renderer's own check", all(r.ok for r in K.check_edit(direction.edit, talk, 4.0)))
short_end = talk[0].start + 25.0
short = G.direct(talk, 100.0, short_end, info=INFO, hook_seconds=4.0, ask=answer(change, hedged))
check("Density (Round 30): at most two graphics in a clip under 25 s, three otherwise",
      len(short.edit.cards) <= G.max_graphics(short_end - 100.0) and G.max_graphics(24.0) == 2
      and G.max_graphics(45.0) == 3, str(short.notes))
check("The prompt states the limit and when the title is up", "At most 3 graphics" in G.build_prompt(
    spans, 100.0, 45.0, 4.0, INFO) and "No graphic before 4.0 s" in G.build_prompt(spans, 100.0, 45.0, 4.0, INFO))
named = G.direct(talk, 100.0, clip_end, info=INFO, hook_seconds=4.0,
                 ask=answer(speaker={"name": "Dana Reyes", "role": "CEO, Northwind Labs"}))
card = next((c for c in named.edit.cards if c.template == "speaker_title"), None)
check("Name card from the video's own title and description, right after the title",
      card is not None and card.rows[0].label == "Dana Reyes" and card.rows[0].detail == "CEO, Northwind Labs"
      and abs(card.start - 104.2) < 1e-6 and abs(card.end - card.start - 3.3) < 1e-6, str(named.notes))
G._GUESTS.clear()  # Round 29: a video's guest is remembered for its other clips; start fresh here
made_up = G.direct(talk, 100.0, clip_end, info=INFO, hook_seconds=4.0, ask=answer(speaker={"name": "Sam Altman", "role": "CEO"}))
check("...never a name that isn't in the title or description", not made_up.edit.cards)
check("...never the channel's own host", G.grounded_name({"name": "Founders Podcast", "role": ""}, INFO) is None)
check("...a role it can't find there is left off", G.grounded_name({"name": "Dana Reyes", "role": "Nobel laureate"}, INFO)
      == ("Dana Reyes", ""))
both = G.direct(talk, 100.0, clip_end, info=INFO, hook_seconds=4.0,
                ask=answer(change, speaker={"name": "Dana Reyes", "role": "CEO, Northwind Labs"}))
check("The name card makes room for the data graphic (no overlap, 1.5 s apart)",
      len(both.edit.cards) == 2 and all(r.ok for r in K.check_edit(both.edit, talk, 4.0)), str(both.notes))
none = G.direct(talk, 100.0, clip_end, info=INFO, hook_seconds=4.0, ask=lambda p, s: None)
check("No AI key: no graphics, and it says why", not none.edit.cards and "no AI key" in none.notes[0])
junk = G.direct(talk, 100.0, clip_end, info=INFO, hook_seconds=4.0, ask=lambda p, s: "not json")
check("An unusable AI answer: no graphics, the clip still renders", not junk.edit.cards and "unusable" in junk.notes[0])
from core.service_pause import ServicePause  # noqa: E402


def limit(prompt, system):
    raise ServicePause("Gemini limit", 60, service="gemini")


paused = G.direct(talk, 100.0, clip_end, info=INFO, hook_seconds=4.0, ask=limit)
check("A used-up AI limit doesn't stop the clip (no graphics this time)", not paused.edit.cards and "limit" in paused.notes[0])

# changes and spoken numbers
grade = words_from("I went from being a C student in math to an A student overnight, which was amazing really.", 100.0)
card = Card("kinetic_range", 100.0, grade[-1].end + 0.15, rows=[CardRow("", "C → A")])
result = K.check_card(card, grade, 95.0, 120.0)
check("A change: both ends said, in order (\"C → A\")", result.ok and card.rows[0].reveal_at < card.rows[0].reveal_to,
      str(result.errors))
card = Card("kinetic_range", 100.0, grade[-1].end + 0.15, rows=[CardRow("", "B → A")])
check("...an end that wasn't said is stopped", not K.check_card(card, grade, 95.0, 120.0).ok)
article = words_from("I went to a school in a town that had a lot of a things to do on the weekends.", 100.0)
card = Card("kinetic_range", 100.0, article[-1].end, rows=[CardRow("", "C → A")])
check("...the word 'a' never counts as the grade 'A'", not K.check_card(card, article, 95.0, 120.0).ok)
hook_line = words_from("I came from being a C student in math to an A student overnight. Because it was more "
                       "analytical and less focused on numbers, and that changed everything for me.", 100.0)
recap = Card("kinetic_range", 100.0, hook_line[-1].end, rows=[CardRow("overnight", "C → A")])
first_pass = K.check_card(recap, hook_line, 100.0, 130.0, hook_seconds=4.0)
check("Said under the title: the change comes in right after the title as a recap (not refused)",
      first_pass.ok and abs(recap.start - 104.2) < 1e-6 and recap.rows[0].reveal_at < recap.rows[0].reveal_to,
      str(first_pass.errors))
check("...and stays only a few seconds (it covers the speaker)", recap.end - recap.start <= K.MAX_OVERLAY_SECONDS + 1e-6,
      f"{recap.start:.2f}-{recap.end:.2f}")
again = K.check_card(recap, hook_line, 100.0, 130.0, hook_seconds=4.0)
check("Checking a graphic twice gives the same answer (the renderer checks again)", again.ok
      and abs(recap.start - 104.2) < 1e-6, str(again.errors))
crash = words_from("So right now we can do the what if and say what if it turns out the stock market crashes by 20%? "
                   "Given my age and the history, when the markets go down, eventually they go back up again.", 100.0)
spans_c = G.sentences(crash)
number = Card("kinetic_range", spans_c[0][0] + 4.0, spans_c[1][1], rows=[CardRow("What if the stock market crashes", "20%")])
first_pass = K.check_card(number, crash, 95.0, 140.0)
second_pass = K.check_card(number, crash, 95.0, 140.0)
check("A number's line may quote the words that led up to it, on every check", first_pass.ok and second_pass.ok
      and abs(number.start - (number.rows[0].reveal_at - 0.12)) < 1e-6, str(first_pass.errors + second_pass.errors))
check("A number graphic leaves a few seconds after its number (not at the end of a long sentence)",
      abs(number.end - (number.rows[0].reveal_at + K.OVERLAY_HOLD)) < 1e-6, f"{number.start:.2f}-{number.end:.2f}")
check("\"the last hundred years\" is 100", [(n.value, n.scale) for n in K.spoken_numbers(
    words_from("based on the last hundred years of history"))] == [(100.0, None)])

# ---------- every render runs the director (pipeline) ----------
import dataclasses  # noqa: E402

import core.pipeline as P  # noqa: E402
from config import load_settings  # noqa: E402
from core.hook_selector import ClipResult  # noqa: E402

settings = dataclasses.replace(load_settings(), temp_dir=tmp / "temp", output_dir=tmp / "out")
(tmp / "temp").mkdir(exist_ok=True)
plain_source = tmp / "talk.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x406080:s=1280x720:r=30:d=40",
                "-f", "lavfi", "-i", "sine=f=300:d=40", "-shortest", "-pix_fmt", "yuv420p", str(plain_source)], check=True)
spoken_talk = words_from(TALK, start=2.0)
clip = ClipResult(start_time=2.0, end_time=spoken_talk[-1].end + 0.3, viral_title="Growth in one year", caption="",
                  hashtags=[], virality_score=90)
G._GUESTS.clear()  # no guest remembered from the checks above
asked = []
G.ask_ai = lambda prompt, system: asked.append(prompt) or json.dumps({"graphics": [change], "speaker": None})
rendered = P.render_selected_clip(plain_source, clip, spoken_talk, settings, tmp / "out" / "auto.mp4",
                                  layout_mode="blurred_background", graphics=INFO)
check("Make clips and the worker: the graphics are decided and rendered in automatically",
      rendered.graphics is not None and [c.template for c in rendered.graphics.cards] == ["kinetic_range"]
      and rendered.path.is_file() and any("Added kinetic_range" in n for n in rendered.graphics_notes), str(rendered.graphics_notes))
check("...the director sees the clip's own sentences, with clip times", asked and "S2 [" in asked[-1] and "$2 million" in asked[-1])
asked.clear()
off = P.render_selected_clip(plain_source, clip, spoken_talk, dataclasses.replace(settings, auto_graphics=False),
                             tmp / "out" / "off.mp4", layout_mode="blurred_background", graphics=INFO)
check("AUTO_GRAPHICS=0 turns it off", off.graphics is None and not asked)
G.ask_ai = lambda prompt, system: "not json"
fallback = P.render_selected_clip(plain_source, clip, spoken_talk, settings, tmp / "out" / "plain.mp4",
                                  layout_mode="blurred_background", graphics=INFO)
check("No usable graphics: the clip renders exactly as before", fallback.graphics is None and fallback.path.is_file())

# ---------- the app ----------
from streamlit.testing.v1 import AppTest  # noqa: E402

source_text = (ROOT / "app.py").read_text()
check("'Finish a long video' and the quote grabber are gone (UI and backend)",
      "Finish a long video" not in source_text and "Quote grabber" not in source_text
      and not (ROOT / "core" / "quotes.py").exists()
      and "suggest_chapters_and_titles" not in (ROOT / "core" / "agent_brain.py").read_text()
      and "def download_section" not in (ROOT / "core" / "downloader.py").read_text())
store = tmp / "app"
store.mkdir()
calls = tmp / "calls.json"
wrapper = tmp / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys, json; sys.path.insert(0, {str(ROOT)!r})
    from pathlib import Path
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(store)!r})
    for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, name, W.STORAGE_DIR / file)
    W.get_worker_status = lambda: {{"running": False}}
    import core.clip_pages as CP
    CP.configured = lambda: True
    from types import SimpleNamespace
    CP.page_settings = lambda: SimpleNamespace(base_url="https://clips.example")
    CP.manual_pages = lambda: [{{"page_name": "p1", "page_url": "https://clips.example/p1", "channel_title": "Founders",
                                "created_at": "2026-11-05"}}]
    import core.clipper as C
    def rerender(**kw):
        Path({str(calls)!r}).write_text(json.dumps({{"pin": kw.get("pin_title"), "start": kw.get("new_start")}}))
        Path(kw["output_path"]).write_bytes(b"plain"); return Path(kw["output_path"])
    C.rerender_clip = rerender
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
clip_file = tmp / "clip_01.mp4"
clip_file.write_bytes(b"clip with graphics")
key = str(clip_file.resolve())
app = AppTest.from_file(str(wrapper), default_timeout=120)
app.session_state["generated_clips"] = [(clip_file, ClipResult(start_time=100.0, end_time=clip_end,
                                                               viral_title="Growth", caption="", hashtags=[], virality_score=88))]
app.session_state["clip_render_contexts"] = {key: {
    "source_path": str(plain_source), "words": talk, "layout_mode": "blurred_background", "start_time": 100.0,
    "end_time": clip_end, "pin_title": True, "graphics_notes": ["Added kinetic_range ($2M → $8M) at 0:05.1: test"]}}
from dataclasses import asdict  # noqa: E402

app.session_state["card_drafts"] = {key: [asdict(c) for c in both.edit.cards]}
app.run()
check("App runs", not app.exception, str([e.value for e in app.exception][:1]))
check("Make clips: Highlights and Series only", list(app.radio(key="clip_mode").options)
      == ["🎯 Highlights", "🎞️ Series: the whole video in order"])
check("The automatic graphics are listed for a last look, with why",
      sum("Remove" == b.label for b in app.button) == 2 and any(e.label == "Why these graphics" for e in app.expander))
check("Nothing to render until something changes", not any(b.label.startswith("🎬 Render this clip again") for b in app.button))
pipeline_tab = app.tabs[2]
check("Published preview pages: at the bottom of 🧭 Pipeline, not in Make clips",
      any("Published preview pages" in e.label for e in pipeline_tab.expander)
      and not any("Published preview pages" in e.label for e in app.tabs[3].expander))
next(b for b in app.button if b.label == "Remove").click().run()
next(b for b in app.button if b.label == "Remove").click().run()
check("Remove both: render again without graphics", any(b.label == "🎬 Render this clip again without graphics" for b in app.button))
next(b for b in app.button if b.label == "🎬 Render this clip again without graphics").click().run()
sent = json.loads(calls.read_text()) if calls.is_file() else None
new_path = Path(app.session_state["generated_clips"][0][0])
check("...the clip is replaced in place (the title pinned again, as you set it)",
      sent == {"pin": True, "start": 100.0} and new_path.is_file() and new_path.read_bytes() == b"plain"
      and not clip_file.exists(), str(sent))

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
