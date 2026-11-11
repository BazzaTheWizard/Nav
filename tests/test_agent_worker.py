# Updated copy of claude_review/round2/tests/test_agent_worker.py for the new email (video_topic).
import json, os, sys, tempfile, time
from pathlib import Path
root = Path(sys.argv[1]).resolve(); sys.path.insert(0, str(root))
import os as _os; _os.environ["NAV_NO_CLIP_PAGES"] = "1"  # never touch the real clip-page bucket
os.environ["OPENAI_API_KEY"] = "test"; os.environ.pop("GEMINI_API_KEY", None)
import core.agent_brain as A
fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

segs = [{"start": float(i * 5), "end": float(i * 5 + 4.8), "text": f"Sentence {i}."} for i in range(200)]
def fake(clips, extra=None):
    payload = {"clips": clips, "video_topic": "breaking down the squat"}
    payload.update(extra or {})
    return lambda key, prompt: json.dumps(payload)
A.load_dotenv = lambda *a, **k: None
good = [{"start_time": 100, "end_time": 140, "hook_title": "a", "reason": "r"},
        {"start_time": 300, "end_time": 340, "hook_title": "b", "reason": "r"},
        {"start_time": 600, "end_time": 640, "hook_title": "c", "reason": "r"}]
A._request_openai = fake(good)
r = A.select_clips_and_draft_pitch(segs, "Coach Sam", "Squat Tips")
check("Valid response works without the model writing the email", len(r["clips"]) == 3 and "breaking down the squat" in r["outreach_email"])
# Round 44 onward the first email shows the clips (the clip page link, filled in once the page is
# published) instead of asking first; the model still never writes the email.
check("Email still filled from the template (round 44: the clip page link in the first email)",
      r["outreach_email"].startswith("Hi there,\n\nI cut 3 vertical clips from your recent video on breaking down the squat:")
      and "[Your clip page link goes here once the page is published]" in r["outreach_email"]
      and "Want me to do the same for your next video?" in r["outreach_email"]
      and "Free, and yours to keep either way." in r["outreach_email"], r["outreach_email"][:200])
bad_one = good + [{"start_time": "oops", "end_time": 5, "hook_title": "x", "reason": "r"}]
A._request_openai = fake(bad_one)
r = A.select_clips_and_draft_pitch(segs, "Coach Sam", "Squat Tips")
check("One malformed candidate no longer fails the whole pick", len(r["clips"]) == 3)
check("Prompt no longer asks for the email", '"outreach_email"' not in A._SYSTEM_PROMPT)

# ---- worker lead queue ----
import autonomous_worker as W
tmp = Path(tempfile.mkdtemp())
W.STORAGE_DIR = tmp; W.LOCK_PATH = tmp / ".lock"; W.LEAD_QUEUE_PATH = tmp / "lead_queue.json"
W.PROCESSED_CREATORS_PATH = tmp / "processed.json"
for name in ("PENDING_OUTREACH_PATH", "QUOTA_STATUS_PATH", "SKIPPED_LEADS_PATH", "SEEN_CHANNELS_PATH"):
    if hasattr(W, name):
        setattr(W, name, tmp / f"{name.lower()}.json")
leads = [{"channel_id": f"UC{i}", "channel_title": f"C{i}", "latest_video_url": f"https://www.youtube.com/watch?v=v{i}",
          "latest_video_title": "t"} for i in range(4)]
check("All discovered leads are kept", W._enqueue_leads(leads) == 4 and W._enqueue_leads(leads) == 0)
first = W._pop_fresh_lead()
state = json.loads(W.PROCESSED_CREATORS_PATH.read_text())
check("Pop reserves the lead", first["channel_id"] == "UC0" and "UC0" in state["processed_channel_ids"])
check("Remaining leads stay queued", len(json.loads(W.LEAD_QUEUE_PATH.read_text())) == 3)
# already-processed leads are skipped
state["processed_channel_ids"].append("UC1"); W.PROCESSED_CREATORS_PATH.write_text(json.dumps(state))
check("Processed leads skipped", W._pop_fresh_lead()["channel_id"] == "UC2")

# ---- continuous queue: no hammering discovery when nothing qualifies ----
calls = {"discover": 0}; sleeps = []
W._wait_for_pending_slot = lambda *a, **k: None
W._pending_records = lambda: []
W._discover_leads = lambda max_results: (calls.__setitem__("discover", calls["discover"] + 1), [])[1]
W.LEAD_QUEUE_PATH.write_text("[]")
class Stop(Exception): pass
def fake_sleep(sec):
    sleeps.append(sec)
    if len(sleeps) >= 3: raise Stop
W.time.sleep = fake_sleep
W.load_settings = lambda: None
try: W.run_continuous_queue()
except Stop: pass
check("Empty discovery backs off (5, 10, 20 min) instead of retrying every 5 s",
      sleeps == [300, 600, 1200], f"sleeps={sleeps}, discovery runs={calls['discover']}")

# ---- storage pruning ----
from core.storage_manager import prune_stale_files
d = Path(tempfile.mkdtemp()); old = d / "old.mp4"; new = d / "new.mp4"; job = d / "worker_x"; job.mkdir()
old.write_bytes(b"x"); new.write_bytes(b"x"); os.utime(old, (time.time() - 3 * 86400,) * 2)
check("Old downloads pruned, fresh ones and folders kept", prune_stale_files(d) == 1 and new.exists() and job.exists())

# ---- renderer picks a working encoder ----
from core.video_renderer import _video_encoder_args
check("Encoder fallback", _video_encoder_args("ffmpeg")[1] in ("h264_videotoolbox", "libx264"), str(_video_encoder_args("ffmpeg")))
print(f"\n{fails} failure(s)"); sys.exit(1 if fails else 0)
