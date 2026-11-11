"""Checks for round 2. Run all tests: .venv/bin/python tests/run_all.py"""
import json, os, re, sys, tempfile, time
from pathlib import Path
import numpy as np

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
import core.outreach as _outreach_r44; _outreach_r44.LINK_IN_FIRST_EMAIL = False  # tests round 25's ask-first email (round 44: tests/test_round44.py)
os.environ["OPENAI_API_KEY"] = "test"; os.environ.pop("GEMINI_API_KEY", None)

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

from core.transcriber import Word
from core import hook_selector as H, subtitle_engine as S, face_tracker as F, pipeline as P

# ---------- Most replayed peaks ----------
heat = [{"start_time": i * 10.0, "end_time": i * 10.0 + 10, "value": v}
        for i, v in enumerate([1.0, .2, .3, .9, .95, .2, .1, .6, .2, .3])]
peaks = H.replay_peaks(heat)
check("Replay peaks skip the opening and merge neighbours", peaks[0][:2] == (30.0, 50.0) and all(p[0] >= 20 for p in peaks), str(peaks))
check("No heatmap → no peaks", H.replay_peaks(None) == [])

# ---------- Transcript for clip picking ----------
words, t = [], 0.0
for i in range(300):
    for j in range(10):
        words.append(Word(f"w{i}{'.' if j == 9 else ''}", t, t + 0.3, None, None)); t += 0.32
    t += 0.5
duration = t

# ---------- AI candidates: ranked, snapped, overlaps dropped, bad ones skipped ----------
import core.agent_brain as A
A.load_dotenv = lambda *a, **k: None
cands = [
    {"start_time": 100, "end_time": 140, "hook_title": "Best", "reason": "r"},
    {"start_time": 110, "end_time": 150, "hook_title": "Overlaps best", "reason": "r"},
    {"start_time": 400, "end_time": 428, "hook_title": "Second", "reason": "r"},   # 28 s: gets snapped
    {"start_time": "bad", "end_time": 1, "hook_title": "Broken", "reason": "r"},
    {"start_time": 700, "end_time": 745, "hook_title": "Third", "reason": "r"},
]
A._request_openai = lambda key, prompt: json.dumps({"clips": cands, "channel_content_summary": "s", "specific_moment_reference": "m"})
clips, email, _subject = P.pick_clips_with_agent(words, duration, 3, "Chan", "Title", [(410.0, 420.0, 0.9)])
titles = [c.viral_title for c in clips]
check("Best non-overlapping picks kept in rank order", titles == ["Best", "Second", "Third"], str(titles))
check("Every pick is 30–60 s", all(30 <= c.end_time - c.start_time <= 60 for c in clips),
      str([round(c.end_time - c.start_time, 1) for c in clips]))
check("Outreach email still produced", "Want me to send them over?" in email)
sent = {}
A._request_openai = lambda key, prompt: (sent.update(json.loads(prompt)), json.dumps({"clips": cands, "channel_content_summary": "s", "specific_moment_reference": "m"}))[1]
P.pick_clips_with_agent(words, duration, 3, "Chan", "Title", [(410.0, 420.0, 0.9)])
check("Picker asks for 3 + spare candidates and gets replay peaks", sent.get("candidate_count") == 3 + P.EXTRA_CANDIDATES and sent.get("most_replayed"))

# ---------- Layout choice ----------
def plan(width=1920, height=1080, shots=None):
    cw = round(height * 9 / 16)
    return F.CropPlan(width, height, cw, height, 0, tuple(shots or [F.Shot(0, 30, 600, True, False)]))
check("Auto → face crop for clear faces at 1080p", P.choose_layout("auto", plan()) == "smart_crop")
check("Auto → blurred for 720p (crop would be blurry)", P.choose_layout("auto", plan(1280, 720)) == "blurred_background")
check("Auto → blurred for wide group shots", P.choose_layout("auto", plan(shots=[F.Shot(0, 30, 600, True, True)])) == "blurred_background")
check("Auto → blurred when faces are rare", P.choose_layout("auto", plan(shots=[F.Shot(0, 10, 600, True, False), F.Shot(10, 30, 600, False, False)])) == "blurred_background")
check("Explicit layouts are respected", P.choose_layout("stacked", None) == "stacked")

# ---------- Crop expression ----------
p2 = plan(shots=[F.Shot(10, 14, 552, True, False), F.Shot(14, 17, 748, True, False), F.Shot(17, 20, 750, True, False), F.Shot(20, 40, 552, True, False)])
expr = p2.x_expression(10, 30)
check("Crop switches exactly at cuts (clip-relative), equal positions merged", expr == "if(lt(t,4.000),552,if(lt(t,10.000),748,552))", expr)
check("Single shot → plain number", plan().x_expression(0, 30) == "600")

# ---------- Cut detection catches same-colour angle changes ----------
rng = np.random.default_rng(0)
base = rng.integers(0, 255, (54, 96), dtype=np.uint8)
frames = [base] * 30 + [np.fliplr(base)] * 30 + [base] * 30   # flip: identical histogram
noisy = [np.clip(f.astype(int) + rng.integers(-3, 4, f.shape), 0, 255).astype(np.uint8) for f in frames]
cuts = F._cut_times(np.stack(noisy))
check("Cuts found between same-colour camera angles", cuts == [1.0, 2.0], str(cuts))

# ---------- Subtitles: position, highlight, hook ----------
ws = [Word("So", 0, .3), Word("the", .3, .6), Word("real", .6, .9), Word("reason.", .9, 1.4)]
with tempfile.TemporaryDirectory() as d:
    text = S.create_ass_subtitles(ws, Path(d) / "a.ass", clip_duration=5, hook_title="Why people quit", hook_seconds=4).read_text()
    plain = S.create_ass_subtitles(ws, Path(d) / "b.ass", clip_duration=5, highlight_active_word=False, hook_title="Key Highlight", hook_seconds=4).read_text()
style = [l for l in text.splitlines() if l.startswith("Style: ShortForm")][0]
check("Captions sit above the bottom UI zone (MarginV 680)", style.endswith(",680,1"), style[-20:])
dialogue = [l for l in text.splitlines() if ",ShortForm," in l]
check("One event per spoken word, active word in yellow", len(dialogue) == 4 and all("\\c&H00FFFF&" in l for l in dialogue))
check("Hook title shown for 4 s", any(",Hook," in l and "0:00:04.00" in l and "Why people quit" in l for l in text.splitlines()))
check("Generic fallback titles are not shown", ",Hook," not in plain and "\\c&H00FFFF&" not in plain)

# ---------- Renderer: faster blur, stacked keeps proportions ----------
import core.video_renderer as R
captured = {}
def fake_run(cmd, **kwargs):
    if "-filter_complex" in cmd:
        captured["cmd"] = cmd
    return type("Result", (), {"stdout": ""})()
R.subprocess.run = fake_run
with tempfile.TemporaryDirectory() as d:
    ass = Path(d) / "s.ass"; ass.write_text("x"); src = Path(d) / "v.mp4"; src.write_bytes(b"x")
    graphs = {}
    for layout in ("blurred_background", "stacked"):
        captured.clear()
        R.render_clip(src, ass, F.CropBox(500, 0, 608, 1080), 0, 10, Path(d) / f"{layout}.mp4", layout_mode=layout, crop_x_expression="if(lt(t,2),500,700)")
        graph = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
        graphs[layout] = graph
check("Background blurred at quarter size", "scale=270:480" in graphs["blurred_background"] and "boxblur=20:5" not in graphs["blurred_background"])
check("Stacked bottom crop is 9:8 around the face (no stretching)", "*9/8" in graphs["stacked"] and "if(lt(t,2),500,700)" in graphs["stacked"])

# ---------- Gemini thinking level ----------
import google.genai.types as T
from core.gemini_resilience import gemini_thinking_config
check("Gemini thinking level defaults to low", str(gemini_thinking_config(T).thinking_level).endswith("LOW"))
os.environ["GEMINI_THINKING_LEVEL"] = ""
check("Thinking level can be left to the model", gemini_thinking_config(T) is None)
os.environ.pop("GEMINI_THINKING_LEVEL")

# ---------- Worker retries ----------
import autonomous_worker as W
tmp = Path(tempfile.mkdtemp())
for name in ("STORAGE_DIR",): setattr(W, name, tmp)
W.LOCK_PATH = tmp / ".lock"
for name in ("LEAD_QUEUE_PATH", "PROCESSED_CREATORS_PATH", "PENDING_OUTREACH_PATH", "QUOTA_STATUS_PATH", "SKIPPED_LEADS_PATH", "SEEN_CHANNELS_PATH"):
    if hasattr(W, name): setattr(W, name, tmp / f"{name.lower()}.json")
lead = {"channel_id": "UC1", "channel_title": "C", "latest_video_url": "https://www.youtube.com/watch?v=v1", "latest_video_title": "t"}
W._enqueue_leads([lead])
results = []
for attempt in range(3):
    popped = W._pop_fresh_lead()
    results.append((popped is not None, W._handle_lead_failure(popped, RuntimeError("network blip")) if popped else None))
state = json.loads(W.PROCESSED_CREATORS_PATH.read_text())
check("Failed lead retried twice, then marked failed", results == [(True, True), (True, True), (True, False)] and state["records"][-1]["status"] == "failed", str(results))
check("Nothing left in the queue after the last attempt", W._pop_fresh_lead() is None)

print(f"\n{fails} failure(s)"); sys.exit(1 if fails else 0)
