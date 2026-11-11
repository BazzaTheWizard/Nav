"""Checks for round 6: skip faceless videos early, better moment picks.
Run all tests: .venv/bin/python tests/run_all.py"""
import json, os, subprocess, sys, tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
import core.outreach as _outreach_r44; _outreach_r44.LINK_IN_FIRST_EMAIL = False  # tests round 25's ask-first email (round 44: tests/test_round44.py)
import os as _os; _os.environ["NAV_NO_CLIP_PAGES"] = "1"  # never touch the real clip-page bucket
os.environ["OPENAI_API_KEY"] = "test"; os.environ.pop("GEMINI_API_KEY", None)

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

from core.transcriber import Word
from core import hook_selector as H, face_tracker as F, pipeline as P
import core.agent_brain as A
A.load_dotenv = lambda *a, **k: None

# ---------- On camera? (fake detector: a "face" in bright frames) ----------
tmp = Path(tempfile.mkdtemp())
video = tmp / "half.mp4"
# 40 s: bright for the first 10 s, dark for the last 30 s.
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=white:s=320x180:r=10:d=10",
                "-f", "lavfi", "-i", "color=c=black:s=320x180:r=10:d=30", "-filter_complex", "[0][1]concat=n=2:v=1",
                "-pix_fmt", "yuv420p", str(video)], check=True)

class FakeDetector:
    available = True
    def detect(self, frame):
        return [(10.0, 10.0, 40.0, 40.0, 0.9)] if frame.mean() > 128 else []

real_detector = F._face_detector
F._face_detector = lambda: FakeDetector()
share = F.on_camera_share(video)
check("Face share = share of sampled frames with a face", share is not None and 0.15 <= share <= 0.3, str(share))

class TinyFace(FakeDetector):
    def detect(self, frame):
        return [(10.0, 10.0, 3.0, 3.0, 0.9)]  # 1% of the frame width: a logo, not a person
F._face_detector = lambda: TinyFace()
check("Tiny 'faces' don't count", F.on_camera_share(video) == 0.0)

class NoDetector:
    available = False
F._face_detector = lambda: NoDetector()
check("No detector -> can't tell (None), never a skip", F.on_camera_share(video) is None)
F._face_detector = real_detector
check("Unreadable video -> None", F.on_camera_share(tmp / "missing.mp4") is None)

# ---------- Worker: faceless creator is skipped before any expensive step ----------
import autonomous_worker as W
from core.face_tracker import CameraCheck  # noqa: E402
W.STORAGE_DIR = tmp
for name, value in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                    ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                    ("SKIPPED_LEADS_PATH", "skipped.json")):
    setattr(W, name, tmp / value)
W._write_json_atomic(W.PROCESSED_CREATORS_PATH, {"processed_channel_ids": ["UCx"], "processed_video_ids": [],
                                                 "records": [{"channel_id": "UCx", "status": "processing"}]})
W._write_json_atomic(W.SEEN_CHANNELS_PATH, [])
W._write_json_atomic(W.LEAD_QUEUE_PATH, [])
W._wait_for_pending_slot = lambda: None
W.check_drive_access = lambda: None
src = tmp / "raw"; src.mkdir()
calls = []
def fake_download(url):
    path = src / "yt_abc.mp4"; path.write_bytes(b"x"); calls.append("download"); return str(path)
W.download_source_video = fake_download
W.extract_audio_only = lambda *a, **k: calls.append("audio") or tmp / "a.wav"
W.transcribe_audio = lambda *a, **k: calls.append("transcribe") or []
lead = {"channel_id": "UCx", "channel_title": "Slides Channel", "latest_video_title": "T",
        "latest_video_url": "https://www.youtube.com/watch?v=abc"}
settings = SimpleNamespace(temp_dir=tmp / "temp", raw_dir=src)
(tmp / "temp").mkdir()

W.camera_check = lambda path: CameraCheck(0.1)  # round 19: one check for camera and screen share
try:
    W._process_lead(lead, settings)
    raised = None
except W.NotAFit as exc:
    raised = exc
check("Faceless video raises NotAFit", raised is not None and "no one on camera" in str(raised), str(raised))
check("...before transcribing (only the download ran)", calls == ["download"], str(calls))
check("...and the download is cleaned up", not (src / "yt_abc.mp4").exists())

W._skip_not_a_fit(lead, raised)
state = json.loads(W.PROCESSED_CREATORS_PATH.read_text())
check("Creator marked 'not a fit' with the reason", state["records"][-1]["status"] == "not a fit" and "camera" in state["records"][-1]["error"])
check("Creator is never queued again (seen list)", "UCx" in json.loads(W.SEEN_CHANNELS_PATH.read_text()))
check("Not requeued for a retry", json.loads(W.LEAD_QUEUE_PATH.read_text()) == [])

calls.clear()
W.camera_check = lambda path: CameraCheck(None)  # round 19: one check for camera and screen share
try:
    W._process_lead(lead, settings)
except W.NotAFit:
    pass
except Exception:
    pass  # later steps are faked badly on purpose; we only care that it went past the check
check("Check unsure (None) -> carries on to transcribing", "transcribe" in calls, str(calls))
calls.clear()
W.camera_check = lambda path: CameraCheck(0.8)  # round 19: one check for camera and screen share
try:
    W._process_lead(lead, settings)
except W.NotAFit:
    pass
except Exception:
    pass
check("Person on camera -> carries on", "transcribe" in calls, str(calls))

# run_worker: a NotAFit lead is skipped, not counted as a failure, and the run continues
W.load_settings = lambda: settings
W.init_storage_files = lambda: None
W._prune_leftovers = lambda s: None
leads = [dict(lead, channel_id="UC1"), dict(lead, channel_id="UC2")]
W._pop_fresh_lead = lambda: leads.pop(0) if leads else None
W._discover_leads = lambda n: []
W._enqueue_leads = lambda found: 0
processed = []
def fake_process(lead_, settings_):
    if lead_["channel_id"] == "UC1":
        raise W.NotAFit("no one on camera (a face in 0% of sampled frames)")
    processed.append(lead_["channel_id"])
    return {"channel_title": "ok", "drive_link": "d"}
W._process_lead = fake_process
skipped = []
W._skip_not_a_fit = lambda l, e: skipped.append(l["channel_id"])
W._handle_lead_failure = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not retry"))
W.run_worker(batch=1)
check("Batch run skips the faceless lead and processes the next", skipped == ["UC1"] and processed == ["UC2"], f"{skipped} {processed}")

# ---------- Moment quality ----------
mp = H.moment_problem
check("'And ...' opens mid-thought", mp("And that's when it clicked for me.") == "opens mid-thought")
check("'Yeah, exactly.' opens mid-thought", mp("Yeah, exactly. The second thing is") == "opens mid-thought")
check("Quote marks and 'uh' don't hide it", mp('"Uh, but then we'))
check("'That's why' opens mid-thought", mp("That's why I stopped doing it."))
check("'So here's the thing' is fine", mp("So here's the thing nobody tells you.") is None)
check("'This is the biggest mistake' is fine", mp("This is the biggest mistake beginners make.") is None)
check("'Andrew' isn't 'And'", mp("Andrew told me something wild.") is None)
check("Sponsor read is filler", mp("This episode is brought to you by our friends at") == "sponsor read, intro or outro")
check("Intro is filler", mp("Hey everyone, welcome back to the podcast. Today") == "sponsor read, intro or outro")
check("Outro is filler", mp("That's it for today, thanks so much for watching") == "sponsor read, intro or outro")
check("'Subscribe to newsletters' is not filler", mp("Most people subscribe to newsletters they never read.") is None)


def make_words(sentences, gap=0.5):
    out, t = [], 0.0
    for sentence in sentences:
        tokens = sentence.split()
        for tok in tokens:
            out.append(Word(tok, t, t + 0.3, None, None)); t += 0.32
        t += gap
    return out, t

# 40 sentences of 12 words (~4.3 s each). Sentence 10 starts with "And", 20 holds an ad.
body = [f"Point number {i} is a clear idea that stands on its own here." for i in range(40)]
body[10] = "And this continues the thought from before in a way that needs context."
body[20] = "This video is brought to you by a sponsor with a promo code today ok."
words, duration = make_words(body)
starts = {}
t = 0.0
for i, s in enumerate(body):
    starts[i] = next(w.start for w in words if w.start >= t - 1e-6)
    t = starts[i] + len(s.split()) * 0.32 + 0.5

def proposal(i, length=40, score=90, title="t"):
    return H.ClipResult(start_time=starts[i], end_time=starts[i] + length, viral_title=title,
                        caption="r", hashtags=[], virality_score=score)

prepared = H.prepare_candidates([proposal(10, title="dangling"), proposal(19, title="has ad"),
                                 proposal(2, title="clean")], words, duration)
by_title = {c.viral_title: c for c in prepared}
check("Clean moment kept", "clean" in by_title)
check("Clip containing an ad is dropped", "has ad" not in by_title)
check("Mid-thought start moved forward one sentence",
      "dangling" in by_title and abs(by_title["dangling"].start_time - (starts[11] - H.PRE_ROLL_SECONDS)) < 0.05,
      str(by_title.get("dangling")))
check("...and still 30-60 s", all(30 <= c.end_time - c.start_time <= 60 for c in prepared))
check("...and no kept clip has a moment problem",
      all(H.moment_problem(H.clip_text(words, c.start_time, c.end_time)) is None for c in prepared))

fallback = H._fallback_clips(3, duration, words)
check("Spaced fallback picks skip weak moments too",
      all(H.moment_problem(H.clip_text(words, c.start_time, c.end_time)) is None for c in fallback) and len(fallback) == 3)

# Backup picks must fit around the clips already chosen (a 3-minute video where
# the old code ended with 2 clips, which fails the whole lead).
short_words, t = [], 0.0
for i in range(45):
    for j in range(12):
        short_words.append(Word(f"w{i}{'.' if j == 11 else ''}", t, t + 0.3, None, None)); t += 0.32
    t += 0.5
two = [H.ClipResult(start_time=a, end_time=b, viral_title="ai", caption="", hashtags=[], virality_score=90 - k)
       for k, (a, b) in enumerate([(12.7, 45.35), (113.08, 155.03)])]
filled = H.finalize_candidates(two, short_words, t, 3)
check("Backup picks fill around chosen clips (3 clips on a short video)", len(filled) == 3, str([(round(c.start_time), round(c.end_time)) for c in filled]))

# ---------- Judge ----------
check("Judge scores: hook and standalone count double",
      A._judge_scores({"scores": [{"clip": 1, "hook": 10, "standalone": 10, "payoff": 10},
                                  {"clip": 2, "hook": 1, "standalone": 1, "payoff": 10}]}, 2) == [100, 28])
try:
    A._judge_scores({"scores": [{"clip": 1, "hook": 5, "standalone": 5, "payoff": 5}]}, 2)
    ok = False
except ValueError:
    ok = True
check("Judge must score every clip", ok)

seen_prompts = []
def fake_openai(key, prompt, system_prompt=A._SYSTEM_PROMPT):
    seen_prompts.append(system_prompt)
    return json.dumps({"scores": [{"clip": i, "hook": 9 - i, "standalone": 8, "payoff": 7} for i in (1, 2)]})
A._request_openai = fake_openai
check("judge_clips returns one score per clip", A.judge_clips(["a b c", "d e f"]) == [round((2 * 8 + 16 + 7) / 50 * 100), round((2 * 7 + 16 + 7) / 50 * 100)])
check("...using the judge instructions, not the picker's", seen_prompts[-1] == A._JUDGE_PROMPT)
A._request_openai = lambda key, prompt, system_prompt=None: "not json"
check("Unusable judge answer -> None (keep first-pass order)", A.judge_clips(["a", "b"]) is None)
saved = os.environ.pop("OPENAI_API_KEY")
check("No AI key -> None", A.judge_clips(["a", "b"]) is None)
os.environ["OPENAI_API_KEY"] = saved

# ---------- Rescoring ----------
four = [H.ClipResult(start_time=i * 100.0, end_time=i * 100.0 + 40, viral_title=f"c{i}", caption="", hashtags=[],
                     virality_score=100 - 10 * i) for i in range(4)]
P.judge_clips = lambda texts: [10, 10, 10, 100]
check("Judge can lift a lower-ranked clip to the top", P.rescore_with_judge(four, words, 3)[0].viral_title == "c3")
P.judge_clips = lambda texts: None
check("No judge answer -> order unchanged", [c.viral_title for c in P.rescore_with_judge(four, words, 3)] == ["c0", "c1", "c2", "c3"])
P.judge_clips = lambda texts: (_ for _ in ()).throw(AssertionError("should not be called"))
check("Only 3 candidates -> judge not called", len(P.rescore_with_judge(four[:3], words, 3)) == 3)
P.judge_clips = A.judge_clips

# ---------- Whole picker: judge reorders, weak moments never reach the final 3 ----------
cands = [
    {"start_time": starts[10], "end_time": starts[10] + 40, "hook_title": "Dangling", "reason": "r"},
    {"start_time": starts[19], "end_time": starts[19] + 40, "hook_title": "Ad", "reason": "r"},
    {"start_time": starts[1], "end_time": starts[1] + 40, "hook_title": "First", "reason": "r"},
    {"start_time": starts[26], "end_time": starts[26] + 40, "hook_title": "Second", "reason": "r"},
    {"start_time": starts[33], "end_time": starts[33] + 35, "hook_title": "Third", "reason": "r"},
]
def fake_ai(key, prompt, system_prompt=A._SYSTEM_PROMPT):
    if system_prompt == A._JUDGE_PROMPT:
        n = prompt.count("Clip ")
        # The judge loves the last candidate it is shown and dislikes the first.
        return json.dumps({"scores": [{"clip": i, "hook": 1 if i == 1 else (10 if i == n else 6), "standalone": 7, "payoff": 7}
                                      for i in range(1, n + 1)]})
    return json.dumps({"clips": cands, "video_topic": "clear ideas"})
A._request_openai = fake_ai
clips, email, _subject = P.pick_clips_with_agent(words, duration, 3, "Chan", "Title")
titles = [c.viral_title for c in clips]
check("Final 3 never include the ad", "Ad" not in titles, str(titles))
check("Final clips have no moment problems",
      all(H.moment_problem(H.clip_text(words, c.start_time, c.end_time)) is None for c in clips))
check("Picker still returns 3 clips and an email", len(clips) == 3 and "Want me to send them over?" in email)

# Judge failing must not change anything versus no judge
A._request_openai = lambda key, prompt, system_prompt=A._SYSTEM_PROMPT: (
    json.dumps({"clips": cands}) if system_prompt != A._JUDGE_PROMPT else "garbage")
clips2, _, _ = P.pick_clips_with_agent(words, duration, 3, "Chan", "Title")
check("Broken judge -> still 3 clean clips", len(clips2) == 3 and all(
    H.moment_problem(H.clip_text(words, c.start_time, c.end_time)) is None for c in clips2))

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
