"""Checks for the new Nav Studios outreach email.

Run from the project root:  .venv/bin/python claude_review/outreach_email/tests/test_outreach_email.py .
Uses a temporary storage folder and fake AI responses; nothing real is touched.
"""
import json
import re
import sys
import tempfile
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(root))

import autonomous_worker as W  # noqa: E402
import core.agent_brain as A  # noqa: E402
from core import outreach as O  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def sentences(email):
    """Sentences between the greeting and the sign-off."""
    body = email.split("\n\n", 1)[1].rsplit("\n\nBest,", 1)[0]
    count = 0
    for paragraph in body.split("\n\n"):
        count += len([part for part in re.split(r"(?<=[.!?])\s+", paragraph.strip()) if part])
    return count


DRIVE = "https://drive.google.com/drive/folders/abc123"
email = O.build_outreach_email("Coach Dana", "How to Squat Pain-Free", "squatting pain-free with knee arthritis", DRIVE)
lower = email.lower()

check("Portfolio link is in the email", "https://navstudios.base44.app/#samples" in email)
check("Positioned as running Nav, a post-production and marketing agency",
      "I run Nav, a short-form post-production and marketing agency" in email)
check("Mentions podcasts and video backlogs", "long-form podcasts and video backlogs" in email)
check("No aspiring / student / beginner / entrepreneur wording",
      not any(word in lower for word in ("aspiring", "student", "beginner", "entrepreneur")))
check("References the video topic", "your recent video on squatting pain-free with knee arthritis" in email)
check("Mentions the 3 sample cuts in their private Drive folder, with the link",
      "3 sample Shorts" in email and "private Google Drive folder" in email and DRIVE in email)
check("No passwords or API access; raw MP4 only",
      "No channel passwords or API access needed" in email and "raw MP4 files only" in email)
check("3-4 sentences", 3 <= sentences(email) <= 4, f"{sentences(email)} sentences")
check("Clean sign-off", email.endswith("Best,\nBarry Navan\nNav Studios"))
check("Phone number removed", "201-466" not in email)

no_topic = O.build_outreach_email("Coach Dana", "How_to_Squat_Pain-Free.mp4", "", "")
check("No topic from the AI -> uses the video title", 'your recent video "How to Squat Pain-Free"' in no_topic, no_topic.split("\n\n")[1][:200])
check("Before upload a placeholder marks the Drive link", O.DRIVE_LINK_PLACEHOLDER in no_topic)
check("Drive link fills the placeholder", DRIVE in O.fill_drive_link(no_topic, DRIVE) and O.DRIVE_LINK_PLACEHOLDER not in O.fill_drive_link(no_topic, DRIVE))
check("Topic cleanup", O.clean_topic(' "On squat depth." ') == "squat depth")
check("1 clip reads correctly", "1 sample Short from" in O.build_outreach_email("X", "T", "t", DRIVE, clip_count=1))

# --- the AI step: only a topic comes from the model ---------------------------------
segments = [{"start": float(i * 5), "end": float(i * 5 + 4.8), "text": f"Sentence {i}."} for i in range(200)]
clips = [{"start_time": s, "end_time": s + 40, "hook_title": "h", "reason": "r"} for s in (100, 300, 600)]
A.load_dotenv = lambda *a, **k: None
A.os.environ["OPENAI_API_KEY"] = "test"
A.os.environ.pop("GEMINI_API_KEY", None)
A._request_openai = lambda key, prompt: json.dumps({"clips": clips, "video_topic": "fixing knee pain when squatting"})
result = A.select_clips_and_draft_pitch(segments, "Coach Sam", "Squat Tips")
check("AI path builds the new email with the topic", "your recent video on fixing knee pain when squatting" in result["outreach_email"])
check("AI path email has the portfolio link", O.PORTFOLIO_URL in result["outreach_email"])
A._request_openai = lambda key, prompt: json.dumps({"clips": clips})
result = A.select_clips_and_draft_pitch(segments, "Coach Sam", "Squat Tips")
check("Missing topic no longer fails the lead", '"Squat Tips"' in result["outreach_email"])
check("Prompt asks for the topic, not an email",
      '"video_topic"' in A._SYSTEM_PROMPT and "aspiring" not in A._SYSTEM_PROMPT and '"outreach_email"' not in A._SYSTEM_PROMPT)

# --- unsent drafts from the old template are upgraded once -----------------------------
tmp = Path(tempfile.mkdtemp(prefix="outreach-"))
W.STORAGE_DIR = tmp
W.PENDING_OUTREACH_PATH = tmp / "pending_outreach.json"
W.LOCK_PATH = tmp / ".lock"
old_body = (
    "Hello Coach Dana,\n\nMy name is Barry Navan and I am an aspiring entrepreneur building a short-form "
    'content agency...\n\nI loved the "Knee Pain Fixes" and thought...\n\nAccess the 3 edited clips here:\n'
    f"{DRIVE}\n\nBest,\nBarry Navan\n201-466-4807"
)
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [
    {"record_id": "a", "channel_title": "Coach Dana", "drive_link": DRIVE, "full_email_body": old_body, "status": "pending"},
    {"record_id": "b", "channel_title": "Sent One", "drive_link": DRIVE, "full_email_body": old_body, "status": "sent"},
])
pending = W.get_pending_outreach()
saved = json.loads(W.PENDING_OUTREACH_PATH.read_text())
check("Old unsent draft rewritten with the new email", "I run Nav" in pending[0]["full_email_body"] and DRIVE in pending[0]["full_email_body"])
check("…keeping its video title", '"Knee Pain Fixes"' in pending[0]["full_email_body"])
check("…and it is saved, so it happens once", "I run Nav" in saved[0]["full_email_body"] and saved[0]["subject"] == O.SUBJECT)
check("Sent emails are left exactly as they were", saved[1]["full_email_body"] == old_body)

# --- worker: a whole lead, with downloads/AI/upload faked -------------------------------
from types import SimpleNamespace  # noqa: E402

W.download_source_video = lambda url: str(tmp / "src.mp4")
(tmp / "src.mp4").write_bytes(b"x")
W.extract_audio_only = lambda src, dst: dst
W.transcribe_audio = lambda audio, settings: []
W._video_duration = lambda path: 1800.0
W.video_info_and_peaks = lambda url: ({}, [])
W.pick_clips_with_agent = lambda *a, **k: ([], O.build_outreach_email("Coach Dana", "Knee Pain Fixes", "fixing knee pain"))
W._render_selected_clips = lambda *a, **k: []
W.upload_clips_to_drive = lambda *a, **k: DRIVE
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [])
settings = SimpleNamespace(temp_dir=tmp / "temp", raw_dir=tmp)
record = W._process_lead(
    {"channel_id": "UC1", "channel_title": "Coach Dana", "latest_video_title": "Knee Pain Fixes",
     "latest_video_url": "https://www.youtube.com/watch?v=abc"},
    settings,
)
check("Worker draft gets the real Drive link", DRIVE in record["full_email_body"] and O.DRIVE_LINK_PLACEHOLDER not in record["full_email_body"])
check("Worker draft is the new email with subject and video title saved",
      "I run Nav" in record["full_email_body"] and record["subject"] == O.SUBJECT and record["video_title"] == "Knee Pain Fixes")

# --- app fallback + Outbox --------------------------------------------------------------
import app  # noqa: E402

pitch = app._generate_outreach_pitch("How to Squat Pain-Free.mp4", DRIVE, 3, "Coach Dana")
check("App fallback email uses the same template", "I run Nav" in pitch.body and O.PORTFOLIO_URL in pitch.body and pitch.subject == O.SUBJECT)

from streamlit.testing.v1 import AppTest  # noqa: E402

at = AppTest.from_file(str(root / "app.py"), default_timeout=120).run()
check("App runs", not at.exception, str([e.value for e in at.exception]))
outbox_bodies = [t.value for t in at.text_area if (t.key or "").startswith("pending_pitch_")]
outbox_subjects = [t.value for t in at.text_input if (t.key or "").startswith("pending_subject_")]
check("Outbox shows the new email and a subject line",
      bool(outbox_bodies) and "I run Nav" in outbox_bodies[0] and outbox_subjects == [O.SUBJECT], str(outbox_subjects))

print("\nAll checks passed." if not fails else f"\n{fails} check(s) failed.")
sys.exit(1 if fails else 0)
