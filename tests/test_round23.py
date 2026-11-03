"""Checks for round 23: 🧹 Clear buttons, ⏩ forced creators join the back of the queue,
cartoon and drawn faces count as "on camera", the worker's title card is on screen from
frame 0, and the wider lead search.
Temp folders and fakes only: nothing is downloaded, transcribed, uploaded or sent.
Run all tests: .venv/bin/python tests/run_all.py"""
import dataclasses, json, logging, os, subprocess, sys, tempfile, textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["NAV_NO_CLIP_PAGES"] = "1"
os.environ["LEAD_PROFILE"] = "wide"  # round 25: these checks are about the wide (every niche) defaults
for name in ("LEAD_MIN_SUBSCRIBERS", "LEAD_MAX_SUBSCRIBERS"):
    os.environ.pop(name, None)  # your .env must not change what these tests expect

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

tmp = Path(tempfile.mkdtemp(prefix="round23-"))
import autonomous_worker as W  # noqa: E402
from core.face_tracker import CameraCheck  # noqa: E402
from core.transcriber import Word  # noqa: E402

def use_storage(folder):
    W.STORAGE_DIR = folder; folder.mkdir(parents=True, exist_ok=True)
    for attr, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, attr, folder / file)
    W.init_storage_files()

W.get_worker_status = lambda: {"running": False}
W.fetch_video_info = lambda url: {"title": "Latest video"}

# =========================== 1. 🧹 Clear (Pipeline tab) ===========================
use_storage(tmp / "clear")
earlier = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
state = W._processed_state()
state["processed_channel_ids"] += ["UCw1", "UCw2", "UCfail", "UCy1", "UCsent", "UCout"]
state["records"] += [
    {"channel_id": "UCw1", "video_id": "vidw1000001", "channel_title": "Worker Skip 1", "status": "not a fit",
     "error": "no one on camera (a face in 0% of sampled frames)", "updated_at": earlier},
    {"channel_id": "UCw2", "video_id": "vidw2000001", "channel_title": "Worker Skip 2", "status": "not a fit",
     "error": "weak moments: only 1 clip(s) scored 60+", "updated_at": earlier},
    {"channel_id": "UCfail", "video_id": "vidfail0001", "channel_title": "Failed Pod", "status": "failed",
     "error": "HTTP Error 403", "updated_at": earlier},
    {"channel_id": "UCy1", "video_id": "vidy1000001", "channel_title": "You Passed", "status": "skipped",
     "skip_reason": "user_skipped", "pass_reason": "bad_framing", "updated_at": earlier},
    {"channel_id": "UCsent", "video_id": "vidsent0001", "channel_title": "Sent Pod", "status": "sent"},
    {"channel_id": "UCout", "video_id": "vidout00001", "channel_title": "Outbox Pod", "status": "completed"},
]
W._write_json_atomic(W.PROCESSED_CREATORS_PATH, state)
W._write_json_atomic(W.SKIPPED_LEADS_PATH, [
    {"channel_id": "UCy2", "channel_title": "Skipped From Queue", "status": "skipped",
     "skip_reason": "user_skipped_before_processing", "latest_video_url": "https://www.youtube.com/watch?v=vidy2000001",
     "latest_video_title": "Queue Video", "skipped_at": earlier},
])
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [{"record_id": "o1", "channel_id": "UCout", "status": "pending"}])
W._write_json_atomic(W.LEAD_QUEUE_PATH, [{"channel_id": "UCq", "channel_title": "Queued", "priority": 3,
                                          "latest_video_url": "https://www.youtube.com/watch?v=vidqueue001"}])
before = {path: path.read_bytes() for path in (W.PROCESSED_CREATORS_PATH, W.SKIPPED_LEADS_PATH,
                                               W.PENDING_OUTREACH_PATH, W.LEAD_QUEUE_PATH, W.SEEN_CHANNELS_PATH)}

def groups():
    found = {}
    for row in W.get_forceable_leads():
        found.setdefault(row["group"], []).append((row["channel_id"], row["cleared"]))
    return {group: sorted(rows) for group, rows in found.items()}

check("Before: nothing is cleared", all(not cleared for rows in groups().values() for _, cleared in rows), str(groups()))
check("Clear 'Skipped by the worker': hides both", W.clear_forceable_group("worker") == 2)
now_groups = groups()
check("...they're marked cleared; the other lists are untouched",
      now_groups["worker"] == [("UCw1", True), ("UCw2", True)]
      and now_groups["you"] == [("UCy1", False), ("UCy2", False)] and now_groups["unfinished"] == [("UCfail", False)], str(now_groups))
check("...the queue, the Outbox, sent pitches and done creators are byte-for-byte the same",
      all(path.read_bytes() == data for path, data in before.items()))
check("...they stay 'done', so the harvester never finds them again",
      {"UCw1", "UCw2"} <= set(W._processed_state()["processed_channel_ids"]))
check("Clear 'Passed or skipped by you': hides both (one passed, one skipped from the queue)",
      W.clear_forceable_group("you") == 2 and all(cleared for _, cleared in groups()["you"]))

later = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
state = W._processed_state()
state["processed_channel_ids"].append("UCw3")
state["records"].append({"channel_id": "UCw3", "video_id": "vidw3000001", "channel_title": "Newer Skip", "status": "not a fit",
                         "error": "screen_share_pip: a screen share", "updated_at": later})
W._write_json_atomic(W.PROCESSED_CREATORS_PATH, state)
check("A creator skipped after the clear shows up", ("UCw3", False) in groups()["worker"], str(groups()["worker"]))
state["records"][-1]["updated_at"] = earlier  # (back to a past time for the app check below)
W._write_json_atomic(W.PROCESSED_CREATORS_PATH, state)
try:
    W.clear_forceable_group("unfinished")
    check("'Didn't finish' has no Clear (those still need a look)", False)
except ValueError:
    check("'Didn't finish' has no Clear (those still need a look)", True)
W.restore_forceable_group("worker")
check("↩ Restore brings the cleared ones back", all(not cleared for _, cleared in groups()["worker"]), str(groups()["worker"]))
W.clear_forceable_group("worker")
lead = W.force_lead_to_queue("UCw1")
check("A cleared creator can still be forced", lead["channel_id"] == "UCw1"
      and any(entry["channel_id"] == "UCw1" for entry in W._lead_queue()))
check("Cleared list saved next to the other worker files", (W.STORAGE_DIR / "forceable_cleared.json").is_file())

# --- The app: 🧹 Clear (N) with a confirm step, then ↩ Restore ---
from streamlit.testing.v1 import AppTest  # noqa: E402
wrapper = tmp / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys; sys.path.insert(0, {str(ROOT)!r})
    from pathlib import Path
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(W.STORAGE_DIR)!r})
    for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, name, W.STORAGE_DIR / file)
    W.get_worker_status = lambda: {{"running": False}}
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
W.restore_forceable_group("worker"); W.restore_forceable_group("you")
at = AppTest.from_file(str(wrapper), default_timeout=90).run()
check("App runs", not at.exception, str([e.value for e in at.exception][:1]))
labels = lambda: [e.label for e in at.expander if e.label.startswith(("Skipped by the worker", "Passed or skipped by you"))]
check("Both lists show their creators", labels() == ["Skipped by the worker (2)", "Passed or skipped by you (2)"], str(labels()))
check("Each has a 🧹 Clear button that asks first ('Yes, clear N')",
      at.button(key="clear_go_worker").label == "Yes, clear 2" and at.button(key="clear_go_you").label == "Yes, clear 2")
at.button(key="clear_go_worker").click().run()
check("Yes, clear: the list is emptied", not at.exception and labels()[0] == "Skipped by the worker (0)", str(labels()))
check("...with a ↩ Restore button", at.button(key="clear_undo_worker").label == "↩ Restore 2 cleared")
check("...and nothing else changed", W._lead_queue()[0]["channel_id"] == "UCq"
      and json.loads(W.PENDING_OUTREACH_PATH.read_text())[0]["channel_id"] == "UCout")
at.button(key="clear_undo_worker").click().run()
check("↩ Restore: back in the list", labels()[0] == "Skipped by the worker (2)", str(labels()))
check("System tab shows the lead size range", any("3,000–250,000 subscribers" in str(df.value.to_dict()) for df in at.dataframe)
      or any("3,000–250,000 subscribers" in str(t.value) for t in at.get("table")))

# =========================== 2. ⏩ Force to queue: the back of the line ===========================
use_storage(tmp / "fifo")
def queued(cid, priority, **extra):
    return {"channel_id": cid, "channel_title": cid, "priority": priority,
            "latest_video_url": f"https://www.youtube.com/watch?v={cid.ljust(11, '0')[:11]}", **extra}
state = W._processed_state()
state["processed_channel_ids"].append("UCx"); state["processed_video_ids"].append("vidx0000001")
state["records"].append({"channel_id": "UCx", "video_id": "vidx0000001", "channel_title": "Forced X", "status": "not a fit",
                         "error": "weak moments", "updated_at": earlier,
                         "lead": {"channel_id": "UCx", "channel_title": "Forced X", "priority": 7,
                                  "latest_video_url": "https://www.youtube.com/watch?v=vidx0000001", "latest_video_title": "X video"}})
W._write_json_atomic(W.PROCESSED_CREATORS_PATH, state)
W._write_json_atomic(W.LEAD_QUEUE_PATH, [queued("UCa", 2), queued("UCb", 5)])
lead = W.force_lead_to_queue("UCx")
check("Forced creator goes to the back, waiting for everyone queued now",
      [e["channel_id"] for e in W._lead_queue()] == ["UCa", "UCb", "UCx"] and sorted(lead["waits_for"]) == ["UCa", "UCb"], str(lead.get("waits_for")))
check("The app's queue list shows them last", [e["channel_id"] for e in W.get_lead_queue()] == ["UCb", "UCa", "UCx"])
W._write_json_atomic(W.LEAD_QUEUE_PATH, W._lead_queue() + [queued("UClow", 1), queued("UChigh", 9)])  # found later
order = []
while (chosen := W._pop_fresh_lead()) is not None:
    order.append(chosen["channel_id"])
check("Order: a later lead that outscores the line ranks as usual; everyone queued before goes first; "
      "then the forced one, before a lower-scored lead found after it",
      order == ["UChigh", "UCb", "UCa", "UCx", "UClow"], str(order))

use_storage(tmp / "fifo2")
W._write_json_atomic(W.LEAD_QUEUE_PATH, [queued("UCa", 2), queued("UCb", 5), queued("UCf", 0, forced=True, waits_for=["UCa", "UCb"])])
W.prioritize_queued_lead("UCa")
check("▶ Do next still jumps the line", W._pop_fresh_lead()["channel_id"] == "UCa")
W._write_json_atomic(W.LEAD_QUEUE_PATH, [queued("UCa", 2), queued("UCb", 5), queued("UCf", 0, forced=True, waits_for=["UCa", "UCb"]),
                                         queued("UCg", 0, forced=True, waits_for=["UCa", "UCb", "UCf"])])
check("Two forced creators: the first one forced goes first",
      [e["channel_id"] for e in W.get_lead_queue()] == ["UCb", "UCa", "UCf", "UCg"], str([e["channel_id"] for e in W.get_lead_queue()]))
W._write_json_atomic(W.LEAD_QUEUE_PATH, [queued("UCc", 5), queued("UCy", 3), queued("UCd", 3), queued("UCe", 1)])
again = W.force_lead_to_queue("UCy")
order = [e["channel_id"] for e in W.get_lead_queue()]
check("Forcing a creator already queued keeps their place (checks turned off)",
      again["forced"] and again["waits_for"] == ["UCc"] and order == ["UCc", "UCy", "UCd", "UCe"], f"{again.get('waits_for')} {order}")
W._write_json_atomic(W.LEAD_QUEUE_PATH, [queued("UCc", 5), queued("UCold", 0, forced=True)])
check("A creator forced before this update (no waits_for) still goes first", W.get_lead_queue()[0]["channel_id"] == "UCold")

# =========================== 3. Cartoon and drawn faces ===========================
import cv2  # noqa: E402
import core.character_faces as CF  # noqa: E402
import core.face_tracker as F  # noqa: E402
from core import title_card as T  # noqa: E402

def character(d, cx, cy, r, skin=(250, 210, 170), hair=(90, 50, 20), shirt=(60, 120, 200)):
    """A simple 2D cartoon person: round head, big dot eyes, smile."""
    d.rectangle((cx - r, cy + r * 0.8, cx + r, cy + r * 2.2), fill=shirt)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=skin, outline=(40, 30, 20), width=max(2, r // 25))
    d.pieslice((cx - r, cy - r * 1.05, cx + r, cy + r * 0.6), 180, 360, fill=hair)
    for ex in (cx - r * 0.38, cx + r * 0.38):
        d.ellipse((ex - r * 0.13, cy - r * 0.05, ex + r * 0.13, cy + r * 0.27), fill=(25, 20, 20))
        d.ellipse((ex - r * 0.05, cy + r * 0.0, ex + r * 0.01, cy + r * 0.07), fill="white")
    d.arc((cx - r * 0.35, cy + r * 0.2, cx + r * 0.35, cy + r * 0.65), 20, 160, fill=(120, 30, 30), width=max(2, r // 14))

def bgr(image):
    return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)

font = ImageFont.truetype(str(T.FONT_PATH), 30)
small = ImageFont.truetype(str(T.FONT_PATH), 20)
def cartoon_frame(i, w=640, h=360):
    image = Image.new("RGB", (w, h), [(200, 230, 245), (235, 245, 220), (250, 235, 215)][i % 3]); d = ImageDraw.Draw(image)
    d.rectangle((0, h * 0.78, w, h), fill=(150, 190, 120))
    character(d, 150 + (i * 23) % 120, 150, 40 + (i * 7) % 50, shirt=[(60, 120, 200), (200, 80, 80)][i % 2])
    if i % 2:
        character(d, 470, 140, 55, skin=(255, 220, 190), hair=(30, 30, 30), shirt=(80, 160, 90))
    return image
def slide_frame(i, w=640, h=360):
    image = Image.new("RGB", (w, h), "white"); d = ImageDraw.Draw(image)
    d.text((30, 20), ["Good books to look at", "Q3 results", "Pricing table", "Food for thought"][i % 4], font=font, fill=(20, 20, 60))
    if i % 4 == 1:  # bar chart
        for b in range(6):
            top = 330 - 30 * ((b * 5 + i) % 7 + 1)
            d.rectangle((60 + b * 85, top, 110 + b * 85, 330), fill=(60, 110, 200)); d.ellipse((75 + b * 85, top - 30, 95 + b * 85, top - 10), fill=(30, 30, 30))
        d.rectangle((40, 80, 600, 332), outline=(0, 0, 0), width=2)
    elif i % 4 == 2:  # table
        for row in range(6):
            for col in range(4):
                d.rectangle((40 + col * 140, 80 + row * 44, 180 + col * 140, 124 + row * 44), outline=(0, 0, 0), width=2)
                d.text((50 + col * 140, 90 + row * 44), ["oo", "100", "$9.00", "Good"][col], font=small, fill=(0, 0, 0))
    else:  # bullets
        for line in range(5):
            d.ellipse((40, 92 + line * 50, 56, 108 + line * 50), fill=(20, 20, 20))
            d.text((70, 85 + line * 50), ["Look book cool", "Good food zoo", "Moon soon noon", "Boost root loop", "Too cool school"][line], font=small, fill=(30, 30, 30))
    return image

found = [len(CF.drawn_faces(bgr(cartoon_frame(i)))) for i in range(4)]
people = F._face_detector()
check("Drawn-face check finds 2D cartoon characters (1 or 2 per frame)", found == [1, 2, 1, 2], str(found))
check("...that the people detector misses",
      sum(len(people.detect(bgr(cartoon_frame(i)))) for i in range(4)) == 0)
for scale in (40, 70, 110):
    image = Image.new("RGB", (640, 360), (230, 240, 250)); character(ImageDraw.Draw(image), 320, 160, scale)
    check(f"...at any size (head radius {scale} px)", len(CF.drawn_faces(bgr(image))) == 1)
check("Not faces: text with 'oo', bar charts, tables with '$9.00' in cells, bullet lists",
      [CF.has_character_face(bgr(slide_frame(i))) for i in range(4)] == [False] * 4,
      str([len(CF.drawn_faces(bgr(slide_frame(i)))) for i in range(4)]))
noise = np.random.default_rng(23).integers(0, 255, (360, 640, 3), dtype=np.uint8)
check("Not faces: random noise (busy B-roll)", not CF.drawn_faces(noise))
check("Anime face model is in models/ and loads", CF.ANIME_CASCADE_PATH.is_file() and CF._anime_cascade() is not None)

def video(path, make, frames=24):
    for i in range(frames):
        make(i).save(tmp / f"f{i:03d}.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", "2", "-i", str(tmp / "f%03d.png"), "-c:v", "libx264",
                    "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30", str(path)], check=True)
    return path
cartoon = F.camera_check(video(tmp / "cartoon.mp4", cartoon_frame))
slides = F.camera_check(video(tmp / "slides.mp4", slide_frame))
check("Cartoon video: no people, but characters in most frames",
      cartoon.share is not None and cartoon.share < W.MIN_ON_CAMERA_SHARE and cartoon.character_share >= W.MIN_CHARACTER_SHARE, str(cartoon))
check("Slide deck: no faces of any kind", slides.share == 0 and (slides.character_share or 0) <= 0.1, str(slides))

# --- The worker's rule ---
use_storage(tmp / "worker")
(tmp / "src.mp4").write_bytes(b"x")
W.check_drive_access = lambda: None
W.clip_pages.configured = lambda: False
W._wait_for_pending_slot = lambda: None
W.download_source_video = lambda url: str(tmp / "src.mp4")
W.load_cached_transcript = lambda video_id: None
class Reached(Exception):
    pass
def stop(*a, **k):
    raise Reached()
W.extract_audio_only = stop
def run_worker(camera, channel_id):
    W.camera_check = lambda path: camera
    lead = {"channel_id": channel_id, "channel_title": channel_id, "latest_video_title": "Ep 1",
            "latest_video_url": f"https://www.youtube.com/watch?v={channel_id.ljust(11, '0')[:11]}"}
    try:
        W._process_lead(lead, SimpleNamespace(temp_dir=tmp / "temp", raw_dir=tmp))
    except Reached:
        return "made clips"
    except W.NotAFit as exc:
        return str(exc)
check("Worker: an animated channel (characters in 88% of frames) gets clips",
      run_worker(CameraCheck(0.06, character_share=0.88), "UCtoon") == "made clips")
reason = run_worker(CameraCheck(0.0, character_share=0.1), "UCdeck")
check("Worker: a faceless deck is still skipped, with what was found",
      reason.startswith("no one on camera") and "cartoon or drawn faces in 10%" in reason, reason)
check("Worker: no faces at all keeps the old reason",
      run_worker(CameraCheck(0.0, character_share=0.0), "UCbroll") == "no one on camera (a face in 0% of sampled frames)")
check("Worker: a host on camera is unchanged", run_worker(CameraCheck(0.9, character_share=0.9), "UChost") == "made clips")
check("Worker: screen share with a small webcam still skipped",
      run_worker(CameraCheck(1.0, True, "top-right", character_share=1.0), "UCpip").startswith("screen_share_pip"))

# =========================== 4. Title card: on screen from frame 0 ===========================
card = dict(image_path=tmp / "card.png", x=10, y=200, seconds=4.0, card_top=200, card_height=180, placement="top")
graph = T.overlay_filter("base", 1, T.TitleOverlay(**card, fade_in=False), "titled")
check("Worker clips: fade-out only, no fade-in, no slide-up", "fade=t=in" not in graph and "fade=t=out:st=3.750" in graph
      and "overlay=x=10:y=200:" in graph, graph)
graph = T.overlay_filter("base", 1, T.TitleOverlay(**card), "titled")
check("Make clips (default): still fades in", "fade=t=in:st=0" in graph and "fade=t=out" in graph)
graph = T.overlay_filter("base", 1, T.TitleOverlay(**card, fade_in=False, pinned=True), "titled")
check("Pinned and instant: no fades at all", "fade" not in graph and "eof_action=repeat" in graph, graph)

import core.pipeline as P  # noqa: E402
import core.pitch_files as PF  # noqa: E402
from config import load_settings  # noqa: E402
from core.hook_selector import ClipResult  # noqa: E402
source = tmp / "source.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=1280x720:r=30:d=40",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=40", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)], check=True)
words = [Word(w, 0.2 + i * 0.5, 0.6 + i * 0.5) for i, w in enumerate(("this is a test of the title card " * 9).split())]
settings = dataclasses.replace(load_settings(), hook_title_seconds=4.0, caption_highlight=False, temp_dir=tmp / "work")
overlays = []
real_prepare = P.prepare_title_overlay
P.prepare_title_overlay = lambda *a, **k: overlays.append(real_prepare(*a, **k)) or overlays[-1]
clip = ClipResult(start_time=10.0, end_time=28.0, viral_title="How to Squat Pain-Free With Bad Knees", caption="",
                  hashtags=[], virality_score=80)

def card_brightness(path, seconds, top):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path), "-frames:v", "1", "-vf",
                          f"format=gray,crop=2:2:540:{top + 8}", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                         check=True, capture_output=True).stdout
    return raw[0] if raw else -1

instant = P.render_selected_clip(source, clip, words, settings, tmp / "instant.mp4", layout_mode="blurred_background", title_fade_in=False)
top = overlays[-1].card_top
first, gone = card_brightness(instant.path, 0, top), card_brightness(instant.path, 5.0, top)
check("Rendered worker clip: the card is fully there in frame 0", first > 230 and instant.title_fade_in is False, f"brightness {first}")
check("...and still fades out after the hook seconds", gone < 180, f"brightness {gone}")
faded = P.render_selected_clip(source, clip, words, settings, tmp / "faded.mp4", layout_mode="blurred_background")
check("Rendered Make clips clip: frame 0 still fading in", card_brightness(faded.path, 0, overlays[-1].card_top) < 180)
P.prepare_title_overlay = real_prepare

calls = []
W.render_selected_clip = lambda *a, **k: calls.append(k) or SimpleNamespace(path=Path(a[4]), layout_mode="smart_crop")
W._render_selected_clips(source, [clip], tmp / "job", settings, words)
check("The worker asks for the instant title card", calls and calls[0].get("title_fade_in") is False, str(calls[:1]))

PF.PITCHES_DIR = tmp / "pitches"
PF.save_pitch_files("instant1", source, [instant], words, 40.0)
item = PF.load_pitch("instant1")["clips"][0]
check("Outbox files remember it", item.get("title_fade_in") is False)
trims = []
real_render = P.render_selected_clip
P.render_selected_clip = lambda *a, **k: trims.append(k) or real_render(*a, **k)
PF.rerender_trim("instant1", 1, 11.0, 27.0, settings)
P.render_selected_clip = real_render
check("An Outbox trim keeps the instant card", trims and trims[-1].get("title_fade_in") is False, str(trims[-1:]))

# =========================== 5. Lead search ===========================
import core.lead_harvester as H  # noqa: E402
(tmp / "harvest" / "storage").mkdir(parents=True)
H.PROJECT_ROOT = tmp / "harvest"
H.SEEN_CHANNELS_PATH = tmp / "harvest" / "storage" / "seen_channels.json"
H.CHANNEL_CHECKS_PATH = tmp / "harvest" / "storage" / "channel_checks.json"

check("Default lead size: 3,000–250,000 subscribers", H.subscriber_range() == (3_000, 250_000))
os.environ.update(LEAD_MIN_SUBSCRIBERS="1,000", LEAD_MAX_SUBSCRIBERS="500000")
check(".env LEAD_MIN/MAX_SUBSCRIBERS change it", H.subscriber_range() == (1_000, 500_000))
os.environ.update(LEAD_MIN_SUBSCRIBERS="lots", LEAD_MAX_SUBSCRIBERS="100")
check("...nonsense falls back to the defaults", H.subscriber_range() == (3_000, 250_000))
for name in ("LEAD_MIN_SUBSCRIBERS", "LEAD_MAX_SUBSCRIBERS"):
    os.environ.pop(name)

class Req:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()
asked = []
class FakeYouTube:
    def search(self): return self
    def channels(self): return self
    def list(self, part, **kw):
        asked.append((part, kw))
        if "q" in kw:
            return Req(lambda: {"items": [{"id": {"videoId": "v1"}, "snippet": {"channelId": "UC4k", "channelTitle": "c", "title": "t"}}]})
        return Req(lambda: {"items": [CHANNELS[c] for c in kw["id"].split(",") if c in CHANNELS]})
H._search_candidates(FakeYouTube(), "how to start a podcast", "long")
_part, sent = asked[-1]
after = datetime.fromisoformat(sent["publishedAfter"].replace("Z", "+00:00"))
check("Search asks only for long videos posted in the last 45 days",
      sent["videoDuration"] == "long" and abs((datetime.now(timezone.utc) - after).days - H.SEARCH_RECENT_DAYS) <= 1, str(sent))

CHANNELS = {cid: {"id": cid, "snippet": {"title": cid}, "statistics": {"subscriberCount": str(subs)}, "contentDetails": {}}
            for cid, subs in (("UC1k", 1_000), ("UC4k", 4_000), ("UC200k", 200_000), ("UC300k", 300_000),
                              ("UCnear", 3_500), ("UCfar", 800), ("UCpitched", 3_600))}
old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
H._write_channel_checks({
    "UCnear": {"checked_at": old, "result": "subscribers_out_of_range", "soft": True, "subscribers": 3_500},
    "UCfar": {"checked_at": old, "result": "subscribers_out_of_range", "soft": True, "subscribers": 800},
    "UCpitched": {"checked_at": old, "result": "subscribers_out_of_range", "soft": True, "subscribers": 3_600},
    "UCgone": {"checked_at": old, "result": "subscribers_out_of_range", "soft": True, "subscribers": 5_000},
})
H._write_seen_channel_ids({"UCnear", "UCfar", "UCpitched", "UCgone"})
(tmp / "harvest" / "storage" / "processed_creators.json").write_text(json.dumps({"processed_channel_ids": ["UCpitched"], "records": []}))
durations = []
H._youtube_client = lambda: FakeYouTube()
H._search_candidates = lambda youtube, query, duration="any": durations.append((query, duration)) or [
    {"channel_id": c, "video_id": f"{c}-v", "channel_title": c, "video_title": "t"} for c in ("UC1k", "UC4k", "UC200k", "UC300k")]
messages = []
class Grab(logging.Handler):
    def emit(self, record): messages.append(record.getMessage())
H.logger.addHandler(Grab()); H.logger.setLevel(logging.INFO)
asked.clear()
H.discover_channels(["q1", "q2", "q3"], max_results=5)
checks = H._read_channel_checks()
check("Queries take turns: long (20+ min), medium (4–20 min), long", [d for _, d in sorted(durations)] == ["long", "medium", "long"], str(durations))
check("4,000 and 200,000 subscribers now pass the size check",
      checks["UC4k"]["result"] == "no_uploads_list" and checks["UC200k"]["result"] == "no_uploads_list", str({k: v["result"] for k, v in checks.items()}))
check("1,000 and 300,000 are still turned down", checks["UC1k"]["result"] == checks["UC300k"]["result"] == "subscribers_out_of_range")
looked_up = {c for part, kw in asked if "id" in kw for c in kw["id"].split(",")}
check("A channel turned down before at 3,500 subscribers is looked at again (no search needed)",
      "UCnear" in looked_up and checks["UCnear"]["result"] == "no_uploads_list" and checks["UCnear"]["checked_at"] != old)
check("...but not one still out of range, or a creator already pitched",
      "UCfar" not in looked_up and "UCpitched" not in looked_up and checks["UCfar"]["checked_at"] == old)
check("A deleted channel is recorded, so it isn't looked up every round", checks["UCgone"]["result"] == "channel_not_found")
funnel = next((m for m in messages if m.startswith("Search funnel")), "")
check("Log line shows where channels drop out", "re-checked for the new size range" in funnel
      and "subscribers out of range" in funnel and "no uploads list" in funnel, funnel)

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
