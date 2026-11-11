"""Checks for round 8: worker status in the app, Do next, saved Outbox edits, passed ->
sent, first-name greetings. Run all tests: .venv/bin/python tests/run_all.py"""
import json, os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
import core.outreach as _outreach_r44; _outreach_r44.LINK_IN_FIRST_EMAIL = False  # tests round 25's ask-first email (round 44: tests/test_round44.py)
import os as _os; _os.environ["NAV_NO_CLIP_PAGES"] = "1"  # never touch the real clip-page bucket
os.environ["OPENAI_API_KEY"] = "test"; os.environ.pop("GEMINI_API_KEY", None)

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

import autonomous_worker as W
from core import outreach as O

tmp = Path(tempfile.mkdtemp(prefix="r8-"))
W.STORAGE_DIR = tmp
for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                   ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                   ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
    setattr(W, name, tmp / file)
DRIVE = "https://drive.google.com/drive/folders/abc"
now = datetime.now(timezone.utc)

# ---------- Worker status ----------
check("No status file -> not running", W.get_worker_status()["running"] is False and W.get_worker_status()["state"] == "never_started")
W._set_status("processing", "Rich Ryan")
st_ = W.get_worker_status()
check("Status written while working -> running, with what it's doing", st_["running"] and st_["state"] == "processing" and st_["detail"] == "Rich Ryan")
W._set_status("stopped")
check("Stopped -> not running", W.get_worker_status()["running"] is False)
W._write_json_atomic(tmp / "worker_status.json", {"pid": 2_000_000_000, "state": "processing", "detail": "", "updated_at": ""})
check("Worker closed without saying so (process gone) -> not running", W.get_worker_status()["running"] is False)

# Outbox full -> status says so (stop the loop at its first sleep)
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [{"record_id": str(i), "status": "pending"} for i in range(W.MAX_PENDING_LEADS)])
class Stop(Exception):
    pass
real_sleep = W.time.sleep
W.time.sleep = lambda seconds: (_ for _ in ()).throw(Stop())
W.load_settings = lambda: None
W._prune_leftovers = lambda settings: None
W.init_storage_files = lambda: None
try:
    W.run_continuous_queue()
except Stop:
    pass
W.time.sleep = real_sleep
check("Full Outbox -> status 'outbox_full'", W.get_worker_status()["state"] == "outbox_full" and W.get_worker_status()["running"])

# main() always leaves 'stopped' behind
W._setup_logging = lambda: None
W.run_worker = lambda batch: (_ for _ in ()).throw(KeyboardInterrupt())
sys.argv = ["autonomous_worker.py", "--batch", "1"]
code = W.main()
check("Ctrl+C in batch mode exits cleanly and status becomes 'stopped'", code == 0 and W.get_worker_status()["state"] == "stopped")

# ---------- Queue: Do next ----------
W._write_json_atomic(W.LEAD_QUEUE_PATH, [{"channel_id": c, "channel_title": c.upper()} for c in ("a", "b", "c")])
check("Do next moves a creator to the top", W.prioritize_queued_lead("c") and [l["channel_id"] for l in W._lead_queue()] == ["c", "a", "b"])
check("Unknown creator -> nothing changes", W.prioritize_queued_lead("zzz") is False)

# ---------- Outbox edits are saved; Mark as Sent keeps what you sent ----------
body = O.build_outreach_email("Marcela Collier From HIC Parenting Education", "T", "anger for parents", DRIVE)
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [
    {"record_id": "p1", "channel_id": "UC1", "channel_title": "Marcela Collier From HIC Parenting Education",
     "drive_link": DRIVE, "full_email_body": body, "subject": O.SUBJECT, "status": "pending", "site_url": O.SITE_URL},
])
check("update_pending_draft saves subject, text and name",
      W.update_pending_draft("p1", subject="New subject", body="Hi Marcela,\n\nEdited.", greeting_name="Marcela")
      and json.loads(W.PENDING_OUTREACH_PATH.read_text())[0]["greeting_name"] == "Marcela")
W._write_json_atomic(W.PROCESSED_CREATORS_PATH, {"processed_channel_ids": ["UC1"], "processed_video_ids": [],
                                                 "records": [{"channel_id": "UC1", "status": "completed"}]})
W.mark_outreach_sent("p1", subject="Final subject", body="Hi Marcela,\n\nFinal text.")
saved = json.loads(W.PENDING_OUTREACH_PATH.read_text())[0]
check("Mark as Sent records the text you actually sent", saved["status"] == "sent" and saved["full_email_body"] == "Hi Marcela,\n\nFinal text." and saved["subject"] == "Final subject")
check("Edits to a sent pitch are refused", W.update_pending_draft("p1", body="x") is False)

# ---------- Passed by mistake -> Sent ----------
W._write_json_atomic(W.SKIPPED_LEADS_PATH, [
    {"record_id": "s_old", "channel_id": "UC9", "channel_title": "Old", "drive_link": DRIVE, "skipped_at": (now - timedelta(days=30)).isoformat()},
    {"record_id": "s_recent", "channel_id": "UC2", "channel_title": "Aviva Real Estate", "drive_link": DRIVE,
     "full_email_body": body, "subject": O.SUBJECT, "status": "skipped", "skip_reason": "user_skipped",
     "skipped_at": (now - timedelta(days=5)).isoformat()},
    {"channel_id": "UC3", "channel_title": "Queue skip", "skipped_at": now.isoformat(), "status": "skipped"},
])
passed = W.get_recently_passed()
check("Passed recently: last 14 days, pitches only (not queue skips)", [p["record_id"] for p in passed] == ["s_recent"], str([p.get("record_id") for p in passed]))
W._write_json_atomic(W.PROCESSED_CREATORS_PATH, {"processed_channel_ids": ["UC2"], "processed_video_ids": [],
                                                 "records": [{"channel_id": "UC2", "status": "skipped"}]})
check("I sent this one -> moved", W.restore_passed_as_sent("s_recent"))
records = {r["record_id"]: r for r in json.loads(W.PENDING_OUTREACH_PATH.read_text())}
check("...now a sent pitch, dated when you passed it", records["s_recent"]["status"] == "sent" and "skip_reason" not in records["s_recent"]
      and records["s_recent"]["sent_at"].startswith((now - timedelta(days=5)).isoformat()[:10]))
check("...gone from the passed list", [r["record_id"] for r in json.loads(W.SKIPPED_LEADS_PATH.read_text()) if r.get("record_id")] == ["s_old"])
check("...creator status is 'sent'", json.loads(W.PROCESSED_CREATORS_PATH.read_text())["records"][-1]["status"] == "sent")
due = {w["record_id"]: w for w in W.get_awaiting_reply()}
check("...and its follow-up is due (passed 5 days ago)", due.get("s_recent", {}).get("follow_up_due") is True)

# ---------- First names ----------
check("clean_first_name", (O.clean_first_name("marcela"), O.clean_first_name("Jean-Luc"), O.clean_first_name("Host"),
                           O.clean_first_name("Dr. Carl"), O.clean_first_name("")) == ("Marcela", "Jean-Luc", "", "", ""))
named = O.build_outreach_email("Marcela Collier From HIC Parenting Education", "T", "t", DRIVE, greeting_name="Marcela")
check("Greeting uses the first name when known", named.startswith("Hi Marcela,\n"))
check("set_greeting swaps the name", O.set_greeting(body, "Marcela").startswith("Hi Marcela,\n") and O.greeting_name_of(O.set_greeting(body, "Marcela")) == "Marcela")
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [
    {"record_id": "f1", "channel_title": "Long Channel Name", "greeting_name": "Sam", "drive_link": DRIVE, "full_email_body": named,
     "subject": O.SUBJECT, "status": "sent", "sent_at": (now - timedelta(days=5)).isoformat()},
])
check("Follow-up greets the same first name", W.get_awaiting_reply()[0]["follow_up_body"].startswith("Hi Sam, quick bump"))
keep = O.rewrite_old_draft(O.set_greeting(O.build_outreach_email("C", "T", "t", DRIVE, key_point="the three habits that matter"), "Dana"), "C", DRIVE)
check("Upgrading a draft keeps its greeting", keep.startswith("Hi Dana,\n"), keep[:160])
check("Quote fallback doesn't claim the host said it", "you said" not in O.build_outreach_email("X", "T", "t", DRIVE, opening_line="Marcela, I try to take deep breaths now"))

import core.agent_brain as A
A.load_dotenv = lambda *a, **k: None
segments = [{"start": float(i * 5), "end": float(i * 5 + 4.8), "text": f"Sentence {i}."} for i in range(200)]
clips = [{"start_time": s, "end_time": s + 40, "hook_title": "h", "reason": "r"} for s in (100, 300, 600)]
A._request_openai = lambda key, prompt, system_prompt=None: json.dumps({"clips": clips, "video_topic": "anger for parents", "host_first_name": "marcela"})
result = A.select_clips_and_draft_pitch(segments, "Marcela Collier From HIC Parenting Education", "T")
check("AI host name -> 'Hi Marcela,'", result["outreach_email"].startswith("Hi Marcela,\n") and result["host_first_name"] == "Marcela")
A._request_openai = lambda key, prompt, system_prompt=None: json.dumps({"clips": clips, "host_first_name": "Host"})
check("Unclear name -> 'Hi there' (the channel name is in the next words)",
      A.select_clips_and_draft_pitch(segments, "Chan", "T")["outreach_email"].startswith("Hi there,\n\nI watched your recent video"))
check("The AI is asked for the host's first name", '"host_first_name"' in A._SYSTEM_PROMPT)

# ---------- App ----------
W._write_json_atomic(W.LEAD_QUEUE_PATH, [{"channel_id": c, "channel_title": c.upper()} for c in ("a", "b")])
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [
    {"record_id": "p2", "channel_id": "UC5", "channel_title": "Marcela Collier From HIC Parenting Education",
     "drive_link": DRIVE, "full_email_body": body, "subject": O.SUBJECT, "status": "pending", "site_url": O.SITE_URL},
])
W._write_json_atomic(W.SKIPPED_LEADS_PATH, [
    {"record_id": "s3", "channel_id": "UC6", "channel_title": "Joel Harrison B2B", "drive_link": DRIVE,
     "full_email_body": body, "subject": O.SUBJECT, "status": "skipped", "skipped_at": now.isoformat()},
])
W._set_status("stopped")
from streamlit.testing.v1 import AppTest  # noqa: E402
wrapper = tmp / "run_app.py"
wrapper.write_text(f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
import autonomous_worker as W
from pathlib import Path
W.STORAGE_DIR = Path({str(tmp)!r})
for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                   ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                   ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
    setattr(W, name, W.STORAGE_DIR / file)
import runpy
runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
""")
at = AppTest.from_file(str(wrapper), default_timeout=120).run()
check("App runs", not at.exception, str([e.value for e in at.exception]))
check("Worker not running -> says so, with the start command",
      any("isn't running" in str(w.value) for w in at.warning) and any("autonomous_worker.py --continuous" in str(c.value) for c in at.code))
labels = [b.label for b in at.button]
# Round 11: Pass opens a small menu of reasons (one button per reason).
popovers = [p.proto.popover.label for p in at.get("popover")]
check("Buttons: '✅ Mark as Sent' and '❌ Pass (not pitching)'",
      "✅ Mark as Sent" in labels and "❌ Pass (not pitching)" in popovers
      and all(reason in labels for reason in W.PASS_REASONS.values()) and "❌ Pass / Next Creator" not in labels, str(popovers))
check("Queue has 'Do next' (not on the first one)", sum(1 for b in at.button if (b.key or "").startswith("do_next_")) == 1)
at.button(key="do_next_b_1").click().run()
check("Do next works from the app", [l["channel_id"] for l in json.loads(W.LEAD_QUEUE_PATH.read_text())] == ["b", "a"])

name_box = at.text_input(key="pending_name_p2")
check("Name box starts empty when the email still says 'Hi there'", name_box.value == "", name_box.value)
name_box.set_value("Marcela").run()
check("Typing a first name updates the email and saves it",
      at.text_area(key="pending_pitch_p2").value.startswith("Hi Marcela,\n")
      and json.loads(W.PENDING_OUTREACH_PATH.read_text())[0]["full_email_body"].startswith("Hi Marcela,\n"))
edited = at.text_area(key="pending_pitch_p2").value.replace("Want me to send them over?", "Want them?")
assert edited != at.text_area(key="pending_pitch_p2").value
at.text_area(key="pending_pitch_p2").set_value(edited).run()
check("Editing the email saves it", json.loads(W.PENDING_OUTREACH_PATH.read_text())[0]["full_email_body"] == edited)
at.button(key="mark_sent_p2").click().run()
sent_record = json.loads(W.PENDING_OUTREACH_PATH.read_text())[0]
check("Mark as Sent from the app keeps your edits", sent_record["status"] == "sent" and sent_record["full_email_body"] == edited)
check("...and it shows under 'Sent: waiting for a reply'", any("Sent: waiting for a reply (1)" in str(h.value) for h in at.subheader))
at.button(key="restore_sent_s3").click().run()
check("'I sent this one' moves a passed pitch to Sent", any(r["record_id"] == "s3" and r["status"] == "sent" for r in json.loads(W.PENDING_OUTREACH_PATH.read_text())))

print("\nAll checks passed." if not fails else f"\n{fails} check(s) failed.")
sys.exit(1 if fails else 0)
