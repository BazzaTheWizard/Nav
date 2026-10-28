"""Round 5 checks: outreach email proof + follow-ups, failure handling, downloads,
AI retries, safe cleanup. Uses fakes and a temp folder; touches nothing real.

Run from the project root:  .venv/bin/python tests/test_round5.py
"""
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(root))

import autonomous_worker as W  # noqa: E402
import core.agent_brain as A  # noqa: E402
import core.downloader as D  # noqa: E402
import core.drive_uploader as DU  # noqa: E402
import core.gemini_resilience as G  # noqa: E402
from core import outreach as O  # noqa: E402
from core.pipeline import opening_words  # noqa: E402
from core.storage_manager import purge_storage  # noqa: E402
from core.transcriber import Word  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


DRIVE = "https://drive.google.com/drive/folders/abc"

# --- outreach: proof line, one brand name, subject, follow-up ---------------------------
email = O.build_outreach_email(
    "Coach Dana", "Knee Pain Fixes", "fixing knee pain when squatting", DRIVE,
    opening_line="Most people squat way too deep. And that is the problem", opening_at=762.4,
)
check("Quotes their own words when there's no key point", '—especially the part that starts "Most people squat way too deep".' in email, email.split("\n\n")[1][-200:])
check("One brand name everywhere", "\nNav Studios | " in email and "I run Nav," not in email)
check("Drive link in the first email", DRIVE in email)
check("Subject is the template's", O.subject_for(email) == O.SUBJECT == "3 Shorts from your latest video")
check("Short/empty quotes are skipped", O.clean_quote("Yeah so") == "")
check("Timestamps over an hour", O.format_timestamp(3725) == "1:02:05")
words = [Word(text=t, start=100 + i * 0.4, end=100.3 + i * 0.4) for i, t in enumerate("Most people squat way too deep. And here is why".split())]
check("Opening words come from the clip start", opening_words(words, 100.0).startswith("Most people squat"))
follow = O.build_follow_up("Coach Dana", "", "fixing knee pain", DRIVE)
check("Follow-up is short, has the link and the same question",
      DRIVE in follow and "Open to having me do this for your future uploads?" in follow and follow.count("\n\n") == 3 and follow.endswith(O.site_host()))
check("Older 'Nav' drafts are recognised for an upgrade", O.is_old_template("Hi X,\n\nI run Nav, a short-form post-production"))

# --- worker: follow-ups due ------------------------------------------------------------
tmp = Path(tempfile.mkdtemp(prefix="round5-"))
W.STORAGE_DIR = tmp
W.PENDING_OUTREACH_PATH = tmp / "pending_outreach.json"
W.PROCESSED_CREATORS_PATH = tmp / "processed_creators.json"
W.LEAD_QUEUE_PATH = tmp / "lead_queue.json"
W.SKIPPED_LEADS_PATH = tmp / "skipped_leads.json"
W.SEEN_CHANNELS_PATH = tmp / "seen_channels.json"
W.LOCK_PATH = tmp / ".lock"
now = datetime.now(timezone.utc)
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [
    {"record_id": "old", "channel_title": "Coach Dana", "status": "sent", "sent_at": (now - timedelta(days=5)).isoformat(),
     "drive_link": DRIVE, "full_email_body": email, "subject": O.subject_for(email)},
    {"record_id": "new", "channel_title": "Fresh", "status": "sent", "sent_at": (now - timedelta(days=1)).isoformat(), "drive_link": DRIVE, "full_email_body": email},
    {"record_id": "replied", "channel_title": "Replied", "status": "sent", "sent_at": (now - timedelta(days=9)).isoformat(), "replied_at": now.isoformat(), "full_email_body": email},
])
due = W.get_follow_ups_due()
check("Only pitches sent 4+ days ago without a reply are due", [d["record_id"] for d in due] == ["old"], str([d["record_id"] for d in due]))
check("Follow-up keeps the topic", "fixing knee pain when squatting" in due[0]["follow_up_body"])
check("Follow-up subject is a reply to the first email", due[0]["follow_up_subject"] == "Re: " + O.SUBJECT, due[0]["follow_up_subject"])
W.mark_follow_up("old", "followed_up")
check("Marked follow-ups leave the list", W.get_follow_ups_due() == [])

# --- worker: failures ------------------------------------------------------------------
W._write_json_atomic(W.PROCESSED_CREATORS_PATH, {"processed_channel_ids": ["UC1", "UC2"], "processed_video_ids": ["v1", "v2"],
                                                  "records": [{"channel_id": "UC1", "status": "processing"}, {"channel_id": "UC2", "status": "processing"}]})
W._write_json_atomic(W.LEAD_QUEUE_PATH, [])
lead1 = {"channel_id": "UC1", "channel_title": "A", "latest_video_url": "https://www.youtube.com/watch?v=v1"}
lead2 = {"channel_id": "UC2", "channel_title": "B", "latest_video_url": "https://www.youtube.com/watch?v=v2"}
requeued = W._handle_lead_failure(lead1, RuntimeError("ERROR: [youtube] v1: Join this channel to get access to members-only content"))
state = json.loads(W.PROCESSED_CREATORS_PATH.read_text())
check("Members-only video fails at once (no 3 re-downloads)", requeued is False and state["records"][0]["status"] == "failed")
requeued = W._handle_lead_failure(lead2, DU.DriveAuthError("Google Drive sign-in expired."), count_attempt=False)
queue = json.loads(W.LEAD_QUEUE_PATH.read_text())
check("Drive sign-in problem: lead goes back without using an attempt",
      requeued and queue[0]["channel_id"] == "UC2" and queue[0].get("attempts", 0) == 0)
check("Network blip is still retried", W._handle_lead_failure(lead2, RuntimeError("Connection reset by peer")) is True)

# --- Drive: never opens a browser from the worker --------------------------------------
DU.TOKEN_FILE = tmp / "no_token.json"
try:
    DU.check_drive_access()
    ok = False
except DU.DriveAuthError:
    ok = True
check("Missing Drive sign-in raises a clear error instead of waiting on a browser", ok)

# --- downloads -------------------------------------------------------------------------
settings = SimpleNamespace(raw_dir=tmp / "raw")
settings.raw_dir.mkdir()
(settings.raw_dir / "yt_dQw4w9WgXcQ.mp4").write_bytes(b"video")


class Boom:
    def __init__(self, *a, **k):
        raise AssertionError("should not download again")


import yt_dlp  # noqa: E402

real_ydl = yt_dlp.YoutubeDL
yt_dlp.YoutubeDL = Boom
try:
    path = D._download_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ", settings)
    reused = path.name == "yt_dQw4w9WgXcQ.mp4"
except AssertionError:
    reused = False
finally:
    yt_dlp.YoutubeDL = real_ydl
check("Same video isn't downloaded twice", reused)


def pick(formats):
    ydl = real_ydl({"format": D.VIDEO_FORMAT, "format_sort": D.VIDEO_FORMAT_SORT, "quiet": True, "simulate": True})
    info = {"id": "x", "title": "t", "formats": formats, "extractor": "generic", "extractor_key": "Generic", "webpage_url": "http://x"}
    return ydl.process_ie_result(info, download=False).get("format_id")


def fmt(fid, h, vcodec, ext, tbr):
    return {"format_id": fid, "height": h, "width": h * 16 // 9, "vcodec": vcodec, "acodec": "none", "ext": ext, "url": f"http://x/{fid}", "protocol": "https", "tbr": tbr}


audio = {"format_id": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "url": "http://x/a", "protocol": "https", "tbr": 128, "abr": 128}
choice = pick([fmt("av1-4k", 2160, "av01.0.12M.08", "mp4", 9000), fmt("av1-1080", 1080, "av01.0.08M.08", "mp4", 2000),
               fmt("h264-1080", 1080, "avc1.640028", "mp4", 4000), dict(audio)])
check("Picks 1080p H.264, not 4K or AV1", choice == "h264-1080+m4a", choice)

# --- AI step ---------------------------------------------------------------------------
segments = [{"start": float(i * 5), "end": float(i * 5 + 4.8), "text": f"Sentence {i}."} for i in range(200)]
good = json.dumps({"clips": [{"start_time": s, "end_time": s + 40, "hook_title": "h", "reason": "r"} for s in (100, 300, 600)], "video_topic": "t"})
answers = iter(["not json at all", good])
A.load_dotenv = lambda *a, **k: None
A.os.environ["OPENAI_API_KEY"] = "test"
A.os.environ.pop("GEMINI_API_KEY", None)
A._request_openai = lambda key, prompt: next(answers)
result = A.select_clips_and_draft_pitch(segments, "Chan", "Title")
check("A malformed AI answer is retried instead of failing the lead", len(result["clips"]) == 3)

from google.genai.errors import ClientError  # noqa: E402

G.RATE_LIMIT_DELAYS_SECONDS = (0, 0)
calls = []


def flaky(model, **kwargs):
    calls.append(model)
    if len(calls) < 3:
        raise ClientError(429, {"error": {"code": 429, "message": "RESOURCE_EXHAUSTED", "status": "RESOURCE_EXHAUSTED"}})
    return "ok"


check("Gemini rate limit (429) is waited out", G.generate_content_with_fallback(flaky, model="m") == "ok" and len(calls) == 3)

# --- safe cleanup ----------------------------------------------------------------------
area = tmp / "area"
(area / "worker_abc").mkdir(parents=True)
(area / "worker_abc" / "clip.mp4").write_bytes(b"x")
old_file = area / "old.mp4"
old_file.write_bytes(b"x")
past = time.time() - 3600
import os  # noqa: E402

os.utime(old_file, (past, past))
(area / "fresh.part").write_bytes(b"x")
kept = purge_storage(area, keep_recent_minutes=20)
check("Clear keeps the worker's job folder and in-progress files",
      (area / "worker_abc" / "clip.mp4").exists() and (area / "fresh.part").exists() and not old_file.exists() and kept == 2)

print("\nAll checks passed." if not fails else f"\n{fails} check(s) failed.")
sys.exit(1 if fails else 0)
