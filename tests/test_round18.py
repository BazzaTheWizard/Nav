"""Checks for round 18: Barry's new outreach email.
Temp folders only: nothing is sent or uploaded.
Run all tests: .venv/bin/python tests/run_all.py"""
import json, os, sys, tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
import core.outreach as _outreach_r44; _outreach_r44.LINK_IN_FIRST_EMAIL = False  # tests round 25's ask-first email (round 44: tests/test_round44.py)
os.environ["NAV_NO_CLIP_PAGES"] = "1"

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

from core import outreach as O  # noqa: E402
import autonomous_worker as W  # noqa: E402

PAGE = "https://clips.navstudiolab.com/coach-dana-k7m2qx"

# ---------- The email, word for word ----------
email = O.build_outreach_email("Coach Dana", drive_link=PAGE, greeting_name="Dana")
check("Round 25 template (no moment saved yet), word for word", email == (
    "Hi Dana,\n\n"
    "I watched your recent video and cut 3 vertical clips from it for Shorts and Reels, captioned and ready to post. "
    "Free, and yours to keep either way.\n\n"
    "Want me to send them over?\n\n"
    "Best,\nBarry Navan\nNav Studios | navstudiolab.com"), email)
check("Subject: '3 shorts from your ___ video'", O.build_subject("knee pain", "video") == "3 shorts from your knee pain video")
check("Fallback subject: '3 shorts from your recent video'", O.SUBJECT == "3 shorts from your recent video")
check("The count matches the clips sent", "cut 2 vertical clips" in O.build_outreach_email("C", drive_link=PAGE, clip_count=2)
      and O.build_subject("knee pain", "video", 2) == "2 shorts from your knee pain video")
check("A name the AI isn't sure of isn't used ('Hi there' instead)",
      O.build_outreach_email("C", drive_link=PAGE, greeting_name="Host").startswith("Hi there,\n"))
check("Older drafts with this text are recognised as old", O.is_old_template(
    "If they're useful, would you want me to do 3–5 of these for every new upload?") and not O.is_old_template(email))
check("Round 18 drafts are old too (round 25)", O.is_old_template(
    "I’d love to produce your clips for the next week completely free to help you drive more reach."))

# ---------- Clip page: episode or video ----------
check("Page wording from the AI's format", W._content_kind({"format": "episode"}, "3 shorts", email) == "episode"
      and W._content_kind({"format": "video"}, "3 shorts from your knee pain episode", email) == "video")
check("No format reported -> read from the subject", W._content_kind({}, "3 shorts from your knee pain episode", email) == "episode"
      and W._content_kind({}, "3 shorts from your knee pain video", email) == "video")

# ---------- Outbox drafts ----------
tmp = Path(tempfile.mkdtemp())
W.STORAGE_DIR = tmp
for attr, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                   ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                   ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
    setattr(W, attr, tmp / file)
old = ("Hi Coach Dana,\n\nYour recent episode on knee pain stuck with me.\n\nI cut three moments from the episode into "
       f"vertical clips with captions:\n\n{PAGE}\n\nThey're yours to post on Shorts, Reels, or TikTok. No watermark, no strings.\n\n"
       "If they're useful, would you want me to do 3–5 of these for every new upload?\n\n" + O.SIGNATURE)
edited = old.replace("would you want me to do 3–5 of these for every new upload?", "want me to keep going?")
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [
    {"record_id": "a", "channel_title": "Coach Dana", "page_url": PAGE, "full_email_body": old, "status": "pending",
     "subject": "Clips for you, Dana", "site_url": O.SITE_URL, "clips": [{}, {}, {}]},
    {"record_id": "b", "channel_title": "Coach Dana", "page_url": PAGE, "full_email_body": edited, "status": "pending",
     "subject": "3 Shorts from your knee pain episode", "site_url": O.SITE_URL},
    {"record_id": "c", "channel_title": "Coach Dana", "page_url": PAGE, "full_email_body": old, "status": "sent",
     "subject": "3 Shorts from your knee pain episode", "site_url": O.SITE_URL},
])
W.get_pending_outreach()
saved = {r["record_id"]: r for r in json.loads(W.PENDING_OUTREACH_PATH.read_text())}
check("An unsent draft in the old template gets the new email",
      saved["a"]["full_email_body"].startswith("Hi there,\n\nI watched your recent video and cut 3 vertical clips")
      and PAGE not in saved["a"]["full_email_body"], saved["a"]["full_email_body"][:120])
check("...greeting the channel name becomes 'Hi there', so you add the first name", saved["a"]["greeting_name"] == "")
check("...a subject you wrote yourself stays", saved["a"]["subject"] == "Clips for you, Dana")
check("A draft you already rewrote (no old ask in it) is left alone", saved["b"]["full_email_body"] == edited
      and saved["b"]["subject"] == "3 Shorts from your knee pain episode")
check("Sent emails are never changed", saved["c"]["full_email_body"] == old)
check("Follow-ups for pitches that had the link keep their wording",
      "Quick follow-up in case this got buried" in O.build_follow_up("Coach Dana", "Knee Pain Fixes", "", PAGE)
      and O.build_follow_up("Coach Dana", "Knee Pain Fixes", "", PAGE).startswith("Hi there,"))

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
