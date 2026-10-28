"""Checks for round 7: new email template, waiting-for-reply list,
website address updates, st.iframe, and full-frame shots inside cropped clips.
Run all tests: .venv/bin/python tests/run_all.py"""
import json, os, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["OPENAI_API_KEY"] = "test"; os.environ.pop("GEMINI_API_KEY", None)

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

from core import outreach as O

DRIVE = "https://drive.google.com/drive/folders/abc123"

# ---------- First email: your template ----------
email = O.build_outreach_email("Coach Dana", "Knee Pain Fixes", "fixing knee pain when squatting", DRIVE,
                               key_point="squatting below parallel being fine for healthy knees", shorts_status="none")
body = email.split("\n\n", 1)[1].rsplit("\n\nBest,", 1)[0]
check("Ends on one interest question", body.endswith("like this?") and body.count("?") == 1)
check("No-strings line", "no strings attached" in body)
check("Drive link is the only link", email.count("http") == 1 and DRIVE in email)
check("Key point from the AI is used", "especially the point about squatting below parallel" in body)
check("The AI is asked for a key point", '"key_point"' in __import__("core.agent_brain", fromlist=["x"])._SYSTEM_PROMPT)
for status in ("none", "tried", "lapsed", "fading", "occasional"):
    if "aren't posting much vertical content" not in O.build_outreach_email("X", "T", "t", DRIVE, shorts_status=status):
        check(f"Low-Shorts line for '{status}'", False)
for status in ("active_weak", "active_unknown", "active_working", "unknown", ""):
    if "aren't posting" in O.build_outreach_email("X", "T", "t", DRIVE, shorts_status=status):
        check(f"No low-Shorts claim for '{status}'", False)
check("'Not posting much' claim only where it's true (checked 10 statuses)", True)

# ---------- Older drafts are upgraded, keeping topic/title and quote ----------
marketing = ('Hi Tristan,\n\nI run Nav Studios, a short-form post-production and marketing agency that turns long-form '
             'podcasts and video backlogs into high-retention vertical clips, and I cut 3 sample Shorts from your recent '
             'video "The Death Of Ivan Ilych". One opens on your line "His life had been most simple." (1:02:05), and all '
             'three are in a private Google Drive folder for you: https://drive.google.com/x\n\nBest,\nBarry Navan\nNav Studios')
softer = ('Hi Coach,\n\nI run Nav Studios, a small editing studio that turns long videos and podcasts into captioned vertical '
          'clips, and I cut 3 sample Shorts from your recent video on squat depth. They\'re in a private Google Drive '
          'folder for you: https://drive.google.com/x\n\nHere\'s what\'s in each clip: https://navstudios.base44.app/#samples')
check("Round 5 'marketing agency' draft is recognised", O.is_old_template(marketing))
check("Round 5 softer draft is recognised", O.is_old_template(softer))
redone = O.rewrite_old_draft(marketing, "Tristan", "https://drive.google.com/x")
check("Rewrite keeps the title and the quote",
      '"The Death Of Ivan Ilych"—especially the part that starts "His life had been most simple"' in redone, redone[:300])
check("Rewrite puts the Drive link in", "https://drive.google.com/x" in redone)
check("Rewrite keeps the topic", "on squat depth" in O.rewrite_old_draft(softer, "Coach", "https://drive.google.com/x"))
check("New email is not seen as old", not O.is_old_template(redone) and not O.is_old_template(email))

# ---------- Website address: one line, unsent drafts follow it ----------
check("Signature shows the site", email.endswith("\nNav Studios | " + O.site_host()))
O.SITE_URL = "https://navstudios.co"
check("Swap old address for new", O.refresh_site_links("see navstudios.base44.app and https://navstudios.base44.app/#samples",
                                                       "https://navstudios.base44.app") == "see navstudios.co and https://navstudios.co/#samples")
check("Same address -> unchanged", O.refresh_site_links("x navstudios.co", "https://navstudios.co") == "x navstudios.co")

import autonomous_worker as W
tmp = Path(tempfile.mkdtemp(prefix="r7-"))
W.STORAGE_DIR = tmp
W.PENDING_OUTREACH_PATH = tmp / "pending.json"
W.LOCK_PATH = tmp / ".lock"
W.SITE_URL = O.SITE_URL
old_email = email  # signed with navstudios.base44.app
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [
    {"record_id": "p", "channel_title": "Coach Dana", "drive_link": DRIVE, "full_email_body": old_email, "status": "pending"},
    {"record_id": "s", "channel_title": "Sent", "drive_link": DRIVE, "full_email_body": old_email, "status": "sent",
     "sent_at": datetime.now(timezone.utc).isoformat()},
])
pending = W.get_pending_outreach()
saved = json.loads(W.PENDING_OUTREACH_PATH.read_text())
check("Unsent draft switches to the new site address", pending[0]["full_email_body"].endswith("| navstudios.co") and saved[0]["site_url"] == "https://navstudios.co")
check("Sent email left as it was", saved[1]["full_email_body"] == old_email)
check("Second load changes nothing", W.get_pending_outreach()[0]["full_email_body"] == pending[0]["full_email_body"])
O.SITE_URL = W.SITE_URL = "https://navstudios.base44.app"

# ---------- Sent, waiting for a reply ----------
now = datetime.now(timezone.utc)
def sent(rid, days, **extra):
    return {"record_id": rid, "channel_title": rid.title(), "drive_link": DRIVE, "full_email_body": email,
            "subject": O.subject_for(email), "status": "sent", "sent_at": (now - timedelta(days=days)).isoformat(), **extra}
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [
    sent("fresh", 1), sent("due", 5), sent("followed", 6, followed_up_at=now.isoformat()),
    sent("replied", 5, replied_at=now.isoformat()), sent("ancient", 40),
    {"record_id": "draft", "channel_title": "Draft", "drive_link": DRIVE, "full_email_body": email, "status": "pending"},
])
waiting = W.get_awaiting_reply()
ids = [w["record_id"] for w in waiting]
check("Waiting list: sent, no reply, last 30 days, newest first", ids == ["fresh", "due", "followed"], str(ids))
by_id = {w["record_id"]: w for w in waiting}
check("Follow-up due only after 4 days and only once", by_id["due"]["follow_up_due"] and not by_id["fresh"]["follow_up_due"] and not by_id["followed"]["follow_up_due"])
check("Due item has a 'Re:' follow-up with the Drive link", by_id["due"]["follow_up_subject"] == "Re: " + O.SUBJECT and DRIVE in by_id["due"]["follow_up_body"])
W.mark_follow_up("fresh", "replied")
check("'They replied' removes it from the list", "fresh" not in [w["record_id"] for w in W.get_awaiting_reply()])

# ---------- App: st.iframe, 'yes' reply in the Outbox, waiting section ----------
app_source = (ROOT / "app.py").read_text()
check("Streamlit warning fixed: st.iframe, no st.components.v1.html", "st.iframe(" in app_source and "components.v1" not in app_source)
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [
    {"record_id": "draft", "channel_title": "Draft Channel", "drive_link": DRIVE, "full_email_body": email,
     "subject": "s", "status": "pending", "site_url": O.SITE_URL},
    sent("due", 5),
])
import app  # noqa: E402,F401  (the app imports cleanly)
from streamlit.testing.v1 import AppTest  # noqa: E402
script = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
import autonomous_worker as W
from pathlib import Path
W.STORAGE_DIR = Path({str(tmp)!r}); W.PENDING_OUTREACH_PATH = W.STORAGE_DIR / "pending.json"; W.LOCK_PATH = W.STORAGE_DIR / ".lock"
import runpy
runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""
wrapper = tmp / "run_app.py"; wrapper.write_text(script)
at = AppTest.from_file(str(wrapper), default_timeout=120).run()
check("App runs", not at.exception, str([e.value for e in at.exception]))
keys = {t.key: t.value for t in at.text_area if t.key}
check("Outbox card shows the email", "I loved your recent video" in keys.get("pending_pitch_draft", ""), str(list(keys)))
check("Waiting section shows the follow-up", "Open to having me do this" in keys.get("follow_body_due", "") and DRIVE in keys.get("follow_body_due", ""))
check("Waiting section heading", any("Sent: waiting for a reply (1)" in str(h.value) for h in at.subheader))

# ---------- Framing: full-frame shots inside a cropped clip ----------
from core.face_tracker import CropPlan, Shot
plan = CropPlan(frame_width=1920, frame_height=1080, crop_width=606, crop_height=1078, y=1, shots=(
    Shot(100.0, 104.0, 600, True, False),   # speaker
    Shot(104.0, 108.0, 600, False, False),  # slide
    Shot(108.0, 110.0, 600, True, True),    # two people far apart
    Shot(110.0, 110.3, 600, False, False),  # flash shot: too short
    Shot(110.3, 114.0, 640, True, False),   # speaker
))
check("Slides and wide shots become one full-frame span", plan.full_frame_ranges(100.0, 114.0) == [(4.0, 10.0)], str(plan.full_frame_ranges(100.0, 114.0)))
check("Spans are clip-relative and clipped to the clip", plan.full_frame_ranges(105.0, 112.0) == [(0.0, 5.0)], str(plan.full_frame_ranges(105.0, 112.0)))
check("All-speaker clip -> no spans", CropPlan(1920, 1080, 606, 1078, 1, (Shot(0, 5, 600, True, False),)).full_frame_ranges(0, 5) == [])

from core.video_renderer import render_clip, FULL_FRAME_BOTTOM_Y
from core.face_tracker import CropBox
import cv2
src = tmp / "src.mp4"
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25", "-f", "lavfi",
                "-i", "sine=frequency=300", "-t", "3", "-pix_fmt", "yuv420p", "-shortest", str(src)], check=True)
ass = tmp / "sub.ass"
ass.write_text("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n"
               "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
               "Style: S,Arial,60,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,680,1\n\n"
               "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
out = render_clip(src, ass, CropBox(x=320, y=0, width=404, height=720), 0.0, 3.0, tmp / "out.mp4",
                  layout_mode="smart_crop", full_frame_ranges=[(1.0, 2.0)])
def detail(t, rows):
    frame = tmp / f"f{t}.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(t), "-i", str(out), "-frames:v", "1", str(frame)], check=True)
    img = cv2.imread(str(frame), cv2.IMREAD_GRAYSCALE)
    return img.shape, cv2.Laplacian(img[rows[0]:rows[1]], cv2.CV_64F).var()
shape, cropped_bottom = detail(0.5, (1300, 1800))
_, full_bottom = detail(1.5, (1300, 1800))
_, full_band = detail(1.5, (FULL_FRAME_BOTTOM_Y - 500, FULL_FRAME_BOTTOM_Y - 20))
_, back_bottom = detail(2.5, (1300, 1800))
check("Output is 1080x1920", shape == (1920, 1080), str(shape))
check("Full-frame span: whole frame above the captions over a blurred background",
      full_band > 5 * full_bottom and cropped_bottom > 5 * full_bottom,
      f"band={full_band:.0f} blurred={full_bottom:.0f} crop={cropped_bottom:.0f}")
check("Back to the crop after the span", back_bottom > 5 * full_bottom, f"{back_bottom:.0f}")

import core.pipeline as P
from core.hook_selector import ClipResult
from core.transcriber import Word
captured = {}
def fake_render(*args, **kwargs):
    captured.update(kwargs); return Path(args[5])
P.render_clip = fake_render
P.analyze_crop_plan = lambda *a, **k: plan
P.prepare_title_overlay = lambda *a, **k: None
settings = SimpleNamespace(temp_dir=tmp, hook_title_seconds=3.0, hook_title_style="light", caption_highlight=True)
words = [Word(f"w{i}", 100 + i * 0.3, 100.25 + i * 0.3) for i in range(46)]
clip = ClipResult(start_time=100.0, end_time=114.0, viral_title="t", caption="", hashtags=[], virality_score=50)
P.render_selected_clip(src, clip, words, settings, tmp / "clip.mp4", layout_mode="smart_crop", work_dir=tmp)
check("Pipeline passes full-frame spans to the renderer", captured.get("full_frame_ranges") == [(4.0, 10.0)], str(captured.get("full_frame_ranges")))
P.render_selected_clip(src, clip, words, settings, tmp / "clip2.mp4", layout_mode="blurred_background", work_dir=tmp)
check("Other layouts don't get spans", captured.get("full_frame_ranges") is None)

print("\nAll checks passed." if not fails else f"\n{fails} check(s) failed.")
sys.exit(1 if fails else 0)
