"""Checks for round 10: the pitch email opens on the best clip's real start time and
point, and each draft gets its own subject. Run all tests: .venv/bin/python tests/run_all.py"""
import json, os, sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["OPENAI_API_KEY"] = "test"; os.environ.pop("GEMINI_API_KEY", None)

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

import core.agent_brain as A
from core import outreach as O, pipeline as P
from core.hook_selector import ClipResult
from core.transcriber import Word

A.load_dotenv = lambda *a, **k: None
DRIVE = "https://drive.google.com/drive/folders/abc123"

# ---------- A transcript and three AI picks, each with its own point ----------
words, t = [], 0.0
for i in range(300):
    for j in range(10):
        words.append(Word(f"w{i}{'.' if j == 9 else ''}", t, t + 0.3, None, None)); t += 0.32
    t += 0.5
duration = t
picks = [
    {"start_time": 100, "end_time": 140, "hook_title": "Best", "reason": "r",
     "point": "knee pain coming from weak hips", "usual_take": "knee pain"},
    {"start_time": 400, "end_time": 440, "hook_title": "Second", "reason": "r",
     "point": "why rest days matter more than extra sets", "usual_take": ""},
    {"start_time": 700, "end_time": 745, "hook_title": "Third", "reason": "r",
     "point": "the one warm-up that actually helps", "usual_take": "stretching"},
]
answer = {"clips": picks, "short_topic": "knee pain", "format": "podcast", "host_first_name": "dana"}
A._request_openai = lambda key, prompt, system_prompt=A._SYSTEM_PROMPT: json.dumps(answer)
report = {}
clips, email, subject = P.pick_clips_with_agent(words, duration, 3, "Coach Dana", "Knee Pain Fixes", report=report)
best = max(clips, key=lambda clip: clip.virality_score)
check("Three clips, an email and a subject come back", len(clips) == 3 and email and subject)
# Round 18: Barry's template names the channel, not a clip's point or time.
check("Email is the round 18 template for this channel",
      email.startswith("Dana — I cut 3 highlight moments from your recent Coach Dana upload into vertical 9:16 clips"), email[:120])
check("No clip points or timestamps in it", best.viral_title == "Best" and "weak hips" not in email
      and O.format_timestamp(best.start_time) not in email and "rest days" not in email and "warm-up" not in email)
check("Podcast -> marked an episode (the clip page and subject use it)", report["format"] == "episode", str(report))
check("Greets the host by first name", email.startswith("Dana — "))
check("Subject names the topic", subject == "3 shorts from your knee pain episode", subject)
check("Subject length is in the 36–50 target here", 36 <= len(subject) <= 50, str(len(subject)))
check("Drive placeholder until the upload", O.DRIVE_LINK_PLACEHOLDER in email and DRIVE in O.fill_drive_link(email, DRIVE))

# The best clip's point is missing: the email doesn't change (it never names a moment).
answer["clips"] = [dict(picks[0], point=""), *picks[1:]]
_, email2, _ = P.pick_clips_with_agent(words, duration, 3, "Coach Dana", "Knee Pain Fixes")
check("No point for the best clip -> same email", email2 == email)

# Solo upload, no topic: still a clean subject.
answer.update(clips=picks, short_topic="", format="video")
_, email3, subject3 = P.pick_clips_with_agent(words, duration, 3, "Coach Dana", "Knee Pain Fixes")
check("Solo upload -> a plain subject",
      "Worth exploring?" in email3 and subject3 == "3 shorts from your recent video", subject3)

# Matching a final clip back to the AI's pick when the title differs.
moved = ClipResult(start_time=108.0, end_time=140.0, viral_title="Renamed", caption="", hashtags=[], virality_score=90)
check("Pick found by nearest start when the title differs", P._ai_pick_for(moved, picks)["hook_title"] == "Best")
far = ClipResult(start_time=250.0, end_time=290.0, viral_title="Renamed", caption="", hashtags=[], virality_score=90)
check("No pick nearby -> nothing borrowed", P._ai_pick_for(far, picks) == {})

# ---------- Signature and site ----------
check("Signature is Best / Barry Navan / Nav Studios | site",
      O.SIGNATURE.splitlines() == ["Best,", "Barry Navan", f"Nav Studios | {O.site_host()}"], repr(O.SIGNATURE))
check("Site shown without https:// or a trailing slash", "/" not in O.site_host() and O.site_host() in O.SITE_URL)

# ---------- App and worker pass the subject through ----------
app_source = (ROOT / "app.py").read_text()
worker_source = (ROOT / "autonomous_worker.py").read_text()
check("App keeps the AI subject", "clips, outreach_email, outreach_subject = pick_clips_with_agent(" in app_source
      and 'st.session_state["ai_outreach_subject"] = outreach_subject' in app_source)
check("Worker saves the AI subject on the draft", "clips, outreach_email, subject = pick_clips_with_agent(" in worker_source
      and '"subject": subject,' in worker_source)

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
