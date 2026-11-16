"""Round 62: the stream picker (core.clip_mode.picker), chat lag, and the Clip mode tool's
plumbing. A fake model stands in for Gemini/OpenAI; no network.

Run: timeout 600 .venv/bin/python -u tests/test_round62.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["CLIP_LENGTH"] = "gaming"
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


from core import clip_length as L  # noqa: E402
from core.clip_mode import picker as P  # noqa: E402
from core.clip_mode import signals as SG  # noqa: E402
from core.clip_mode.chat import Message  # noqa: E402
from core.transcriber import Word  # noqa: E402

check("gaming length in force", L.MIN_SECONDS == 15.0 and L.MAX_SECONDS == 60.0)

# --- a 20-minute "stream": words every 0.5 s with pauses every 10 s, two real moments ----
DUR = 1200.0
words: list[Word] = []
t = 0.0
n = 0
while t < DUR:
    if n % 20 == 19:
        t += 1.2  # a pause
    words.append(Word(text=f"w{n}", start=round(t, 2), end=round(t + 0.35, 2)))
    t += 0.5
    n += 1
chat = [Message(float(s), "hello") for s in range(0, 1200, 3)]
chat += [Message(400.0 + 4 + i * 0.1, "KEKW NO WAY") for i in range(90)]  # reaction to a moment at ~400 s
chat += [Message(900.0 + 5 + i * 0.1, "clip it W") for i in range(60)]  # a second, weaker one at ~900 s
heat = SG.heatmap(SG.chat_values(chat, DUR))

seeds = P.seeds(heat, words, chat, DUR)
check("seeds: two moments, in time order", len(seeds) == 2 and seeds[0].at < seeds[1].at, [(s.at, s.value) for s in seeds])
check("seeds: the chat lag puts the moment at or before the burst", 390 <= seeds[0].at <= 404, seeds[0].at)
check("seeds: words around and chat after", "w" in seeds[0].words and 1 <= len(seeds[0].chat) <= P.CHAT_SAMPLE
      and sum("KEKW" in c for c in seeds[0].chat) >= 0.8 * len(seeds[0].chat), seeds[0].chat[:3])
check("seeds: spacing keeps one moment per burst", all(abs(a.at - b.at) >= P.MIN_SPACING for a, b in zip(seeds, seeds[1:])))

# --- the fake model -------------------------------------------------------------------------
calls: list[str] = []


def fake_ask(prompt: str, system: str) -> str:
    calls.append(system[:20])
    if system.startswith("You pick highlight"):
        data = json.loads(prompt)
        check("prompt: the model sees every seed with words and chat",
              [m["moment"] for m in data["moments"]] == [1, 2] and data["moments"][0]["chat_after_it"], data["moments"][0])
        return json.dumps({"moments": [
            {"moment": 1, "keep": True, "label": "fail", "payoff_time": 401.5, "setup_seconds": 5, "score": 9,
             "hook_title": "He had ONE job", "why": "a crash"},
            {"moment": 2, "keep": True, "label": "silly", "payoff_time": 99999, "setup_seconds": 40, "score": 6,
             "hook_title": "x" * 80, "why": "odd answer"},
        ]})
    return json.dumps({"scores": [{"clip": 1, "hook": 9, "cold": 9, "payoff": 8}, {"clip": 2, "hook": 4, "cold": 5, "payoff": 4}]})


ms = P.moments(seeds, fake_ask)
check("moments: both kept, in seed order", [m.seed.number for m in ms] == [1, 2])
check("moments: the model's payoff and setup are used", ms[0].payoff == 401.5 and ms[0].setup == 5.0, (ms[0].payoff, ms[0].setup))
check("moments: a wild payoff falls back to the spike, setup is clamped, label and title are tidied",
      ms[1].payoff == seeds[1].at and ms[1].setup == 14.0 and ms[1].label == "other" and len(ms[1].title) == 50,
      (ms[1].payoff, ms[1].setup, ms[1].label, len(ms[1].title)))
none = P.moments(seeds, None)
check("moments: no model keeps every spike", len(none) == 2 and all(m.label == "other" for m in none))
dropped = P.moments(seeds, lambda p, s: json.dumps({"moments": [{"moment": 1, "keep": False}]}))
check("moments: keep=false drops it", dropped == [])

# --- boundaries -------------------------------------------------------------------------
start, end = P.bounds(ms[0], words, DUR)
check("bounds: setup before the payoff, reaction after, 15-60 s",
      start <= 401.5 - 2 and end >= 401.5 + P.REACTION_MIN and L.MIN_SECONDS <= end - start <= L.MAX_SECONDS,
      (start, end))
check("bounds: edges sit in pauses, not inside a word",
      all(not (w.start < start < w.end) and not (w.start < end < w.end) for w in words), (start, end))
check("bounds: a 15 s floor is enforced", P.bounds(P.Moment(seeds[0], 401.5, 2.0, "", "x", 5), words, DUR)[1]
      - P.bounds(P.Moment(seeds[0], 401.5, 2.0, "", "x", 5), words, DUR)[0] >= L.MIN_SECONDS)
long_seed = P.Seed(9, 500.0, 500.0, 700.0, 1.0)  # a hot run that goes on for 200 s
s2, e2 = P.bounds(P.Moment(long_seed, 500.0, 14.0, "", "x", 5), words, DUR)
check("bounds: a long run is capped at 60 s", e2 - s2 <= L.MAX_SECONDS and s2 <= 500.0 - 2, (s2, e2))
s3, e3 = P.bounds(P.Moment(P.Seed(1, 2.0, 2.0, 6.0, 1.0), 2.0, 6.0, "", "x", 5), words, DUR)
check("bounds: never before 0", s3 >= 0.0 and e3 - s3 >= L.MIN_SECONDS, (s3, e3))
s4, e4 = P.bounds(P.Moment(P.Seed(1, DUR - 3, DUR - 3, DUR, 1.0), DUR - 3, 6.0, "", "x", 5), words, DUR)
check("bounds: never past the end", e4 <= DUR and e4 - s4 >= L.MIN_SECONDS, (s4, e4))
check("pauses: one per inserted gap", 100 <= len(P.pauses(words)) <= 140 and P.pauses([]) == [], len(P.pauses(words)))

# --- all together -----------------------------------------------------------------------------
report: dict = {}
calls.clear()
clips = P.pick_stream_clips(words, chat, heat, DUR, 2, ask=fake_ask, report=report)
check("pick: two clips in time order", len(clips) == 2 and clips[0].start_time < clips[1].start_time)
check("pick: the judge blended in (clip 1 judged 88 -> above its 90 * 0.5 + 88 * 0.5 = 89)",
      clips[0].virality_score == 89 and clips[1].virality_score == round(0.5 * 44 + 0.5 * 60), [c.virality_score for c in clips])
check("pick: the stream and judge prompts were each called once", calls == ["You pick highlight c", "You review highlight"], calls)
check("pick: peak_retention_score set from the spikes", clips[0].peak_retention_score >= 0.65, clips[0].peak_retention_score)
check("report: every moment with its scores", len(report["moments"]) == 2 and report["moments"][0]["judge"] == 88, report["moments"][0])
only_one = P.pick_stream_clips(words, chat, heat, DUR, 1, ask=fake_ask)
check("pick: count=1 keeps the best one", len(only_one) == 1 and only_one[0].viral_title == "He had ONE job")
dry = P.pick_stream_clips(words, chat, heat, DUR, 5, use_model=False)
check("pick: a dry run with no model still cuts clips", len(dry) == 2 and all(L.MIN_SECONDS <= c.end_time - c.start_time <= L.MAX_SECONDS for c in dry))
bad_judge = P.pick_stream_clips(words, chat, heat, DUR, 2, ask=lambda p, s: (json.dumps({"moments": [
    {"moment": 1, "keep": True, "payoff_time": 401, "score": 7}, {"moment": 2, "keep": True, "payoff_time": 905, "score": 7}]})
    if s.startswith("You pick") else "not json"))
check("pick: an unusable judge answer keeps the first-pass scores", [c.virality_score for c in bad_judge] == [70, 70])
check("pick: nothing hot -> no clips", P.pick_stream_clips(words, [], SG.heatmap(SG.chat_values([], DUR)), DUR, 3, ask=fake_ask) == [])

# --- the tool's plumbing ---------------------------------------------------------------------
helps = subprocess.run([sys.executable, "tools/clip_stream.py", "--help"], cwd=ROOT, capture_output=True, text=True)
check("clip_stream --help", helps.returncode == 0 and "--plan" in helps.stdout and "--campaign" in helps.stdout)
from tools.clip_stream import shifted  # noqa: E402

sh = shifted([Word(text="a", start=10.0, end=10.5), Word(text="b", start=2.0, end=2.4)], 5.0)
check("shifted words: moved to the section's clock, earlier ones dropped", [(w.text, w.start) for w in sh] == [("a", 5.0)])

# --- the stream layout (core.webinar slides="stream") -------------------------------------
from core.face_tracker import CropBox  # noqa: E402
from core.webinar import SLIDE_SHAPES, WebinarLayout, _stream_box  # noqa: E402

cam = CropBox(1424, 86, 404, 302)  # a facecam top-right of a 1920x1080 stream (a face box)
box = _stream_box(cam, "top-right", 1920, 1080, "face")
check("stream box: 4:3, hugging the left edge, clear of the camera (and the window around the face)",
      box.x == 0 and box.x + box.width <= 1424 - 0.5 * 404 and abs(box.width / box.height - 4 / 3) < 0.02, box)
box_l = _stream_box(CropBox(40, 40, 400, 300), "top-left", 1920, 1080, "webcam window")
check("stream box: a camera top-left -> the crop hugs the right edge", box_l.x + box_l.width == 1920 and box_l.x >= 440, box_l)
huge = _stream_box(CropBox(900, 0, 1020, 1080), "top-right", 1920, 1080, "webcam window")
check("stream box: a huge camera is ignored, the crop is centred", huge.x > 0 and huge.width == 1440, huge)
check("stream shape registered", "stream" in SLIDE_SHAPES)
lay = WebinarLayout(1920, 1080, "top-right", "face", cam, box, 1080, 810, slides="stream", divider="shadow")
graph = lay.filter_graph()
check("stream layout: the game sits on a blurred copy of itself, split 4 ways",
      "split=4" in graph and "web_game_back" in graph and "boxblur" in graph and "[base]" in graph)
plain = WebinarLayout(1920, 1080, "top-right", "face", cam, box, 1080, 608).filter_graph()
check("slides layout unchanged: black padding, split 3 ways", "split=3" in plain and "color=black" in plain and "web_game_back" not in plain)

print(f"\n{'ALL PASSED' if not FAILED else f'{len(FAILED)} FAILED: ' + ', '.join(FAILED)}")
sys.exit(1 if FAILED else 0)
