"""Checks for the Nav Studios outreach email (round 18 template).

Run from the project root:  .venv/bin/python tests/test_outreach_email.py .
Uses a temporary storage folder and fake AI responses; nothing real is touched.
"""
import json
import re
import sys
import tempfile
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(root))
import os as _os; _os.environ["NAV_NO_CLIP_PAGES"] = "1"  # never touch the real clip-page bucket

import autonomous_worker as W  # noqa: E402
import core.agent_brain as A  # noqa: E402
from core import outreach as O  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


DRIVE = "https://drive.google.com/drive/folders/abc123"
SIGN_OFF = "Best,\nBarry Navan\nNav Studios | " + O.site_host()

# --- the template (round 18: Barry's wording) ----------------------------------------------
email = O.build_outreach_email(
    "Coach Dana", "How to Squat Pain-Free", "squatting pain-free with knee arthritis", DRIVE,
    opening_at=754.6, key_point="squatting below parallel being fine for healthy knees",
    usual_take="deep squats", content_format="episode", greeting_name="dana", shorts_status="lapsed",
)
expected = (
    "Dana — I cut 3 highlight moments from your recent Coach Dana upload into vertical 9:16 clips with captions:\n\n"
    f"{DRIVE}\n\n"
    "They’re completely yours to post across Shorts, Reels, or TikTok—unwatermarked, no strings attached.\n\n"
    "If you like how they perform, I’d love to produce your clips for the next week completely free "
    "to help you drive more reach.\n\n"
    "Worth exploring?\n\n"
    + SIGN_OFF
)
check("Email matches the template exactly", email == expected, email)
lower = email.lower()
paragraphs = email.split("\n\n")
check("Link on its own line", paragraphs[1] == DRIVE)
check("Signature: Barry Navan, Nav Studios | site", email.endswith(SIGN_OFF) and "Barry Navan\nNav Studios | " in email)
check("Signature shows the site without https:// or a slash", "https://" not in SIGN_OFF and not SIGN_OFF.endswith("/"))
check("No claims the site doesn't make (retention, marketing)",
      not any(word in lower for word in ("retention", "marketing", "viral", "production standards")))
check("No aspiring / student / beginner / entrepreneur wording",
      not any(word in lower for word in ("aspiring", "student", "beginner", "entrepreneur")))
check("Phone number removed", "201-466" not in email)
check("No first name yet -> 'Hi there —' (type the name in the Outbox)",
      O.build_outreach_email("Coach Dana", drive_link=DRIVE).startswith(
          "Hi there — I cut 3 highlight moments from your recent Coach Dana upload into"))
check("Emoji left out of the channel name",
      "your recent Leighton Grey - Grey Matter upload" in O.build_outreach_email("Leighton Grey - Grey Matter 🇨🇦", drive_link=DRIVE))
check("No channel name -> 'your recent upload'", "from your recent upload into" in O.build_outreach_email("", drive_link=DRIVE))
no_link = O.build_outreach_email("Coach Dana", "How_to_Squat_Pain-Free.mp4", "", "")
check("Before upload a placeholder marks the link", O.DRIVE_LINK_PLACEHOLDER in no_link)
check("The link fills the placeholder", DRIVE in O.fill_drive_link(no_link, DRIVE) and O.DRIVE_LINK_PLACEHOLDER not in O.fill_drive_link(no_link, DRIVE))
one = O.build_outreach_email("X", drive_link=DRIVE, clip_count=1)
check("1 clip reads correctly",
      "I cut 1 highlight moment from your recent X upload into a vertical 9:16 clip with captions:" in one
      and "It’s completely yours to post" in one and "If you like how it performs," in one, one)
two = O.build_outreach_email("X", drive_link=DRIVE, clip_count=2)
check("2 clips reads correctly", "I cut 2 highlight moments" in two and "They’re completely yours" in two and "how they perform" in two)
check("Greeting name read back from the email", O.greeting_name_of(email) == "Dana" and O.greeting_name_of(no_link) == "")
check("Typing a name replaces 'Hi there'", O.set_greeting(no_link, "Marcela").startswith("Marcela — I cut 3 highlight moments"))
check("...and replaces a name", O.set_greeting(email, "Sam").startswith("Sam — I cut") and O.set_greeting(email, " ") == email)
check("Older 'Hi Dana,' drafts still work", O.set_greeting("Hi Dana,\n\nBody", "Sam") == "Hi Sam,\n\nBody"
      and O.greeting_name_of("Hi Dana,\n\nBody") == "Dana")
check("Key point cleanup", O.clean_key_point("Your point about deep squats being safe.") == "deep squats being safe"
      and O.clean_key_point("The point about deep squats being safe.") == "deep squats being safe")
check("Too short/long key points are dropped", O.clean_key_point("squats") == "" and O.clean_key_point(" ".join(["w"] * 25)) == "")
check("Usual take cleanup", O.clean_usual_take("the usual take on homework.") == "homework"
      and O.clean_usual_take("one two three four five") == "")
legacy = "Clips: " + O.DRIVE_LINK_PLACEHOLDER
check("Older drafts: the link still fills the placeholder", O.fill_drive_link(legacy, DRIVE) == "Clips: " + DRIVE)
check("Topic cleanup", O.clean_topic(' "On squat depth." ') == "squat depth")

# --- subject line ------------------------------------------------------------------------
check("Subject names the topic (episode)", O.build_subject("knee arthritis", "episode") == "3 shorts from your knee arthritis episode")
check("Subject names the topic (video)", O.build_subject("Michigan offense", "video") == "3 shorts from your Michigan offense video")
check("Podcast counts as an episode", O.clean_format("Podcast") == "episode" and O.clean_format("") == "video")
check("Topic tidied", O.clean_short_topic(' "Your knee pain episode." ') == "knee pain")
check("4+ word topic -> 'recent'", O.build_subject("the complete guide to squats", "video") == "3 shorts from your recent video")
long_subject = O.build_subject("extraordinarily complicated biomechanics", "episode")
check("Never over 50 characters", long_subject == "3 shorts from your recent episode", long_subject)
check("1 clip subject", O.build_subject("knee pain", "video", 1) == "1 short from your knee pain video")
check("Older drafts get a plain subject", O.subject_for(email) == "3 shorts from your recent video" and O.subject_for("x") == O.SUBJECT)
check("Old subjects move to the new style, yours stay",
      O.modern_subject("3 Shorts from your knee arthritis episode") == "3 shorts from your knee arthritis episode"
      and O.modern_subject("2 Shorts from your latest video") == "2 shorts from your recent video"
      and O.modern_subject("Loved your squat episode") == "Loved your squat episode")

# --- the AI step ---------------------------------------------------------------------------
segments = [{"start": float(i * 5), "end": float(i * 5 + 4.8), "text": f"Sentence {i}."} for i in range(200)]
clips = [{"start_time": s, "end_time": s + 40, "hook_title": f"h{s}", "reason": "r",
          "point": "The point about knee pain coming from weak hips.", "usual_take": "knee pain"} for s in (100, 300, 600)]
A.load_dotenv = lambda *a, **k: None
A.os.environ["OPENAI_API_KEY"] = "test"
A.os.environ.pop("GEMINI_API_KEY", None)
A._request_openai = lambda key, prompt: json.dumps({"clips": clips, "video_topic": "fixing knee pain when squatting",
                                                    "short_topic": "knee pain", "format": "podcast"})
result = A.select_clips_and_draft_pitch(segments, "Coach Sam", "Squat Tips")
check("AI path: subject from the short topic", result["subject"] == "3 shorts from your knee pain episode", result["subject"])
check("AI path: each clip's point and usual take are kept (cleaned)",
      result["clips"][0]["point"] == "knee pain coming from weak hips" and result["clips"][0]["usual_take"] == "knee pain")
check("AI path email is the new template",
      "highlight moments from your recent Coach Sam upload into vertical 9:16 clips with captions:" in result["outreach_email"])
A._request_openai = lambda key, prompt: json.dumps({"clips": [{k: v for k, v in c.items() if k not in ("point", "usual_take")} for c in clips]})
result = A.select_clips_and_draft_pitch(segments, "Coach Sam", "Squat Tips")
check("Missing topic no longer fails the lead", "Worth exploring?" in result["outreach_email"] and result["subject"] == O.SUBJECT)
check("Missing point/usual take -> empty, not an error", result["clips"][0]["point"] == "" and result["clips"][0]["usual_take"] == "")
check("Prompt asks for the phrases, not an email",
      all(f'"{field}"' in A._SYSTEM_PROMPT for field in ("video_topic", "short_topic", "format", "point", "usual_take"))
      and "aspiring" not in A._SYSTEM_PROMPT and '"outreach_email"' not in A._SYSTEM_PROMPT)

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
round7_body = (
    "Hi Dana,\n\nI loved your recent video on knee pain—especially the point about weak hips.\n\n"
    f"I edited 3 ready-to-post (9:16) sample Shorts from that episode, complete with active framing and dynamic captions:\n{DRIVE}\n\n"
    "Best,\nBarry Navan\nNav Studios | " + O.site_host()
)
PAGE = "https://clips.navstudiolab.com/coach-dana-k7m2qx"
round17_body = (  # the template used from round 10 to round 17
    "Hi Dana,\n\nYour point at 12:34 about weak hips stuck with me.\n\n"
    "I cut that moment and one other from the episode into vertical clips with captions:\n\n"
    f"{PAGE}\n\nThey're yours to post on Shorts, Reels, or TikTok. No watermark, no strings.\n\n"
    "If they're useful, would you want me to do 3–5 of these for every new upload?\n\n"
    "Best,\nBarry Navan\nNav Studios | " + O.site_host()
)
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [
    {"record_id": "a", "channel_title": "Coach Dana", "drive_link": DRIVE, "full_email_body": old_body, "status": "pending"},
    {"record_id": "b", "channel_title": "Sent One", "drive_link": DRIVE, "full_email_body": old_body, "status": "sent"},
    {"record_id": "c", "channel_title": "Coach Dana", "drive_link": DRIVE, "full_email_body": round7_body,
     "status": "pending", "subject": "3 Shorts from your latest video", "site_url": O.SITE_URL},
    {"record_id": "d", "channel_title": "Coach Dana", "drive_link": DRIVE, "page_url": PAGE, "full_email_body": round17_body,
     "status": "pending", "subject": "2 Shorts from your knee pain episode", "site_url": O.SITE_URL, "greeting_name": "Dana",
     "clips": [{"start": 754.0, "end": 790.0}, {"start": 900.0, "end": 940.0}]},
])
pending = W.get_pending_outreach()
saved = json.loads(W.PENDING_OUTREACH_PATH.read_text())
check("Very old unsent draft rewritten with the new email",
      pending[0]["full_email_body"].startswith("Hi there — I cut 3 highlight moments from your recent Coach Dana upload")
      and DRIVE in pending[0]["full_email_body"], pending[0]["full_email_body"][:200])
check("…and it is saved, so it happens once", "Worth exploring?" in saved[0]["full_email_body"] and saved[0]["subject"] == O.SUBJECT)
upgraded = next(r for r in saved if r["record_id"] == "d")
check("Round 10-17 draft in the Outbox: the new email, same name, clip count and page link",
      upgraded["full_email_body"].startswith("Dana — I cut 2 highlight moments from your recent Coach Dana upload")
      and PAGE in upgraded["full_email_body"] and DRIVE not in upgraded["full_email_body"], upgraded["full_email_body"][:160])
check("…its subject keeps the topic, in the new style", upgraded["subject"] == "2 shorts from your knee pain episode"
      and upgraded["greeting_name"] == "Dana", upgraded["subject"])
check("Sent emails are left exactly as they were", saved[1]["full_email_body"] == old_body)
check("Round 7 drafts are kept as written (your edits stay)", saved[2]["full_email_body"] == round7_body)

# --- worker: a whole lead, with downloads/AI/upload faked -------------------------------
from types import SimpleNamespace  # noqa: E402

W.download_source_video = lambda url: str(tmp / "src.mp4")
(tmp / "src.mp4").write_bytes(b"x")
W.extract_audio_only = lambda src, dst: dst
W.transcribe_audio = lambda audio, settings: []
W._video_duration = lambda path: 1800.0
W.video_info_and_peaks = lambda url: ({}, [])
W.pick_clips_with_agent = lambda *a, **k: (
    [], O.build_outreach_email("Coach Dana", "Knee Pain Fixes", "fixing knee pain"), "3 shorts from your knee pain video"
)
W._render_selected_clips = lambda *a, **k: []
W.upload_clips_to_drive = lambda *a, **k: DRIVE
W.check_drive_access = lambda: None  # round 5: the worker checks the Drive login first
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [])
settings = SimpleNamespace(temp_dir=tmp / "temp", raw_dir=tmp)
record = W._process_lead(
    {"channel_id": "UC1", "channel_title": "Coach Dana", "latest_video_title": "Knee Pain Fixes",
     "latest_video_url": "https://www.youtube.com/watch?v=abc"},
    settings,
)
check("Worker draft gets the real Drive link", DRIVE in record["full_email_body"] and O.DRIVE_LINK_PLACEHOLDER not in record["full_email_body"])
check("Worker draft is the new email with its own subject and video title saved",
      "Worth exploring?" in record["full_email_body"] and record["subject"] == "3 shorts from your knee pain video"
      and record["video_title"] == "Knee Pain Fixes", record["subject"])

# --- app fallback + Outbox --------------------------------------------------------------
import app  # noqa: E402

pitch = app._generate_outreach_pitch("How to Squat Pain-Free.mp4", DRIVE, 3, "Coach Dana")
check("App fallback email uses the same template",
      "Worth exploring?" in pitch.body and DRIVE in pitch.body and pitch.subject == "3 shorts from your recent video")

from streamlit.testing.v1 import AppTest  # noqa: E402

at = AppTest.from_file(str(root / "app.py"), default_timeout=120).run()
check("App runs", not at.exception, str([e.value for e in at.exception]))
outbox_bodies = [t.value for t in at.text_area if (t.key or "").startswith("pending_pitch_")]
outbox_subjects = [t.value for t in at.text_input if (t.key or "").startswith("pending_subject_")]
check("Outbox shows the new email and its subject line",
      bool(outbox_bodies) and "Worth exploring?" in outbox_bodies[0]
      and outbox_subjects == ["3 shorts from your knee pain video"], str(outbox_subjects))

print("\nAll checks passed." if not fails else f"\n{fails} check(s) failed.")
sys.exit(1 if fails else 0)
