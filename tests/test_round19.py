"""Checks for round 19: removing a clip from an Outbox pitch (page, Drive, files, email),
and skipping screen shares with a corner webcam ("screen_share_pip").
Fakes and temp folders only: nothing is uploaded, downloaded or sent.
Run all tests: .venv/bin/python tests/run_all.py"""
import io, json, os, subprocess, sys, tempfile, textwrap
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["NAV_NO_CLIP_PAGES"] = "1"
os.environ["DRIVE_BACKUP"] = "on"

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

tmp = Path(tempfile.mkdtemp())
from core import outreach as O  # noqa: E402

# ---------- The email and subject follow the clip count ----------
email = O.build_outreach_email("Coach Dana", drive_link="https://clips.navstudiolab.com/coach-dana-k7m2qx", greeting_name="Dana")
two, found = O.set_clip_count(email, 2)
check("Email: 'cut 2 vertical clips', the rest unchanged",
      found and two == O.build_outreach_email("Coach Dana", drive_link="https://clips.navstudiolab.com/coach-dana-k7m2qx",
                                               greeting_name="Dana", clip_count=2))
one, _ = O.set_clip_count(email, 1)
check("...and singular wording for 1", one == O.build_outreach_email(
    "Coach Dana", drive_link="https://clips.navstudiolab.com/coach-dana-k7m2qx", greeting_name="Dana", clip_count=1))
custom = "Dana, here are the clips I made for you."
check("An email you rewrote is left alone (and the app says to check the number)", O.set_clip_count(custom, 2) == (custom, False))
check("Subject: '3 shorts from' -> '2 shorts from'", O.set_subject_count("3 shorts from your knee pain video", 2) == "2 shorts from your knee pain video")
check("...older capital 'Shorts' too", O.set_subject_count("3 Shorts from your latest episode", 2) == "2 Shorts from your latest episode")
check("A subject without the number stays", O.set_subject_count("Clips for you, Dana", 2) == "Clips for you, Dana")

# ---------- Their clip page (R2): renumber, update page.json, delete the leftovers ----------
import core.clip_pages as C  # noqa: E402

class FakeR2:
    def __init__(self):
        self.objects, self.fail_page_once, self.fail_after_copies = {}, False, None
    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key][0])}
    def put_object(self, Bucket, Key, Body, **meta):
        if Key.endswith("page.json") and self.fail_page_once:
            self.fail_page_once = False
            raise RuntimeError("network drop")
        self.objects[Key] = (Body, meta)
    def copy_object(self, Bucket, Key, CopySource):
        if self.fail_after_copies is not None:
            if self.fail_after_copies == 0:
                self.fail_after_copies = None
                raise RuntimeError("network drop")
            self.fail_after_copies -= 1
        self.objects[Key] = self.objects[CopySource["Key"]]
    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)
    def list_objects_v2(self, Bucket, Prefix=""):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}

r2 = FakeR2()
settings = SimpleNamespace(bucket="nav-clips", base_url="https://clips.navstudiolab.com")
C._require = lambda: (settings, r2)
NAME = "coach-dana-k7m2qx"
def fresh_page():
    r2.objects.clear()
    for i in (1, 2, 3):
        r2.objects[f"{NAME}/clip_{i:02d}.mp4"] = (f"video{i}".encode(), {"ContentType": "video/mp4"})
        r2.objects[f"{NAME}/poster_{i:02d}.jpg"] = (f"poster{i}".encode(), {"ContentType": "image/jpeg"})
    r2.objects[f"{NAME}/page.json"] = (json.dumps({"v": 1, "clips": [{"start": 10}, {"start": 20}, {"start": 30}]}).encode(), {})
def page_state():
    clips = {k.split("/")[1]: v[0] for k, v in r2.objects.items() if not k.endswith("page.json")}
    return clips, json.loads(r2.objects[f"{NAME}/page.json"][0])["clips"]

fresh_page()
check("Page: 3 -> 2", C.remove_clip(NAME, 2, 3) == 2)
files, listed = page_state()
check("Page: clip 3 becomes clip_02 (poster too); clip_03 and poster_03 are gone",
      files == {"clip_01.mp4": b"video1", "clip_02.mp4": b"video3", "poster_01.jpg": b"poster1", "poster_02.jpg": b"poster3"}, str(files))
check("Page: page.json lists the 2 clips left, in order (the page says 'Your 2 Shorts')", listed == [{"start": 10}, {"start": 30}])
fresh_page()
C.remove_clip(NAME, 3, 3)
files, listed = page_state()
check("Page: removing the last clip needs no renumbering", set(files) == {"clip_01.mp4", "clip_02.mp4", "poster_01.jpg", "poster_02.jpg"}
      and files["clip_02.mp4"] == b"video2" and listed == [{"start": 10}, {"start": 20}])
# A network drop at every possible step, then a retry: always the same, right result.
outcomes = set()
for copies_before_drop in range(0, 12):
    for page_drop in (False, True):
        fresh_page()
        r2.fail_after_copies, r2.fail_page_once = copies_before_drop, page_drop
        for _attempt in range(4):
            try:
                C.remove_clip(NAME, 1, 3)
                break
            except RuntimeError:
                continue
        C.remove_clip(NAME, 1, 3)  # once more: already done, nothing more removed
        files, listed = page_state()
        outcomes.add((tuple(sorted(files.items())), json.dumps(listed)))
check("Page: a network drop at any step, then a retry -> clips 2 and 3 as clip_01 and clip_02, never a second removal",
      outcomes == {(tuple(sorted({"clip_01.mp4": b"video2", "clip_02.mp4": b"video3",
                                  "poster_01.jpg": b"poster2", "poster_02.jpg": b"poster3"}.items())),
                    json.dumps([{"start": 20}, {"start": 30}]))}, str(outcomes)[:300])
try:
    C.remove_clip(NAME, 1, 5)
    check("Page: a count that doesn't match the page -> error, nothing changed", False)
except RuntimeError:
    check("Page: a count that doesn't match the page -> error, nothing changed", page_state()[1] == [{"start": 20}, {"start": 30}])

# ---------- Drive backup: trash the clip, renumber, rename the folder ----------
import core.drive_uploader as DU  # noqa: E402

class FakeDrive:
    def __init__(self, names):
        self.files_by_id = {f"id{i}": name for i, name in enumerate(names)}
        self.trashed, self.folder_name = [], "Nav Studios – 3 sample Shorts for Coach Dana"
    def files(self):
        return self
    def list(self, q, fields, pageSize):
        return SimpleNamespace(execute=lambda num_retries=0: {"files": [{"id": i, "name": n} for i, n in self.files_by_id.items()]})
    def update(self, fileId, body):
        def run(num_retries=0):
            if fileId == "folder":
                self.folder_name = body["name"]
            elif body.get("trashed"):
                self.trashed.append(self.files_by_id.pop(fileId))
            else:
                self.files_by_id[fileId] = body["name"]
            return {}
        return SimpleNamespace(execute=run)
    def get(self, fileId, fields):
        return SimpleNamespace(execute=lambda num_retries=0: {"name": self.folder_name})

FOLDER = "https://drive.google.com/drive/folders/folder"
DU._get_user_credentials = lambda *a, **k: None
drive = FakeDrive(["clip_01.mp4", "clip_02.mp4", "clip_03.mp4"])
DU.build = lambda *a, **k: drive
DU.remove_clip_in_drive(FOLDER, 2, 3)
check("Drive: clip_02 to the trash (recoverable), clip_03 renamed clip_02",
      drive.trashed == ["clip_02.mp4"] and sorted(drive.files_by_id.items()) == [("id0", "clip_01.mp4"), ("id2", "clip_02.mp4")])
check("Drive: the folder name says 2", drive.folder_name == "Nav Studios – 2 sample Shorts for Coach Dana", drive.folder_name)
DU.remove_clip_in_drive(FOLDER, 2, 3)
check("Drive: running it again changes nothing", drive.trashed == ["clip_02.mp4"] and len(drive.files_by_id) == 2)
drive = FakeDrive(["clip_01.mp4", "clip_03.mp4"])  # stopped right after the trash step
DU.build = lambda *a, **k: drive
DU.remove_clip_in_drive(FOLDER, 2, 3)
check("Drive: a retry after a half-finished run only renames", drive.trashed == []
      and sorted(drive.files_by_id.values()) == ["clip_01.mp4", "clip_02.mp4"])

# ---------- This Mac's pitch files ----------
import core.pitch_files as PF  # noqa: E402
pitches = tmp / "pitches"
def make_pitch(record_id, trim_on=None):
    d = pitches / record_id
    d.mkdir(parents=True)
    clips = []
    for i in (1, 2, 3):
        for suffix in ("mp4", "preview.mp4", "source.mp4"):
            (d / f"clip_{i:02d}.{suffix}").write_text(f"{suffix} {i}")
        clips.append({"index": i, "file": f"clip_{i:02d}.mp4", "preview": f"clip_{i:02d}.preview.mp4",
                      "source": f"clip_{i:02d}.source.mp4", "start": i * 100.0, "end": i * 100.0 + 40,
                      "slice_start": i * 100.0 - 30, "slice_end": i * 100.0 + 70, "layout": "smart_crop",
                      "clip": {"start_time": 30, "end_time": 70, "viral_title": "Hook", "caption": "c",
                               "hashtags": [], "virality_score": 80}, "words": [["Hello", 30.0, 30.4]]})
        if i == trim_on:
            (d / f"clip_{i:02d}.trim.mp4").write_text(f"trim {i}")
            (d / f"clip_{i:02d}.trim.preview.mp4").write_text(f"trim preview {i}")
            clips[-1]["trim"] = {"start": i * 100.0 - 2, "end": i * 100.0 + 40}
    (d / "pitch.json").write_text(json.dumps({"record_id": record_id, "clips": clips}))
    return d
d = make_pitch("p1", trim_on=3)
check("Files: 3 -> 2", PF.remove_clip_files("p1", 2, pitches) == 2)
names = sorted(p.name for p in d.iterdir())
manifest = json.loads((d / "pitch.json").read_text())
check("Files: clip 2's files are gone; clip 3's are now clip_02 (its waiting trim too)",
      names == ["clip_01.mp4", "clip_01.preview.mp4", "clip_01.source.mp4", "clip_02.mp4", "clip_02.preview.mp4",
                "clip_02.source.mp4", "clip_02.trim.mp4", "clip_02.trim.preview.mp4", "pitch.json"]
      and (d / "clip_02.mp4").read_text() == "mp4 3" and (d / "clip_02.trim.mp4").read_text() == "trim 3", str(names))
check("Files: the manifest is renumbered and keeps clip 3's times and trim",
      [(c["index"], c["file"], c["start"]) for c in manifest["clips"]] == [(1, "clip_01.mp4", 100.0), (2, "clip_02.mp4", 300.0)]
      and manifest["clips"][1]["trim"]["start"] == 298.0)

# ---------- The whole removal, from the Outbox ----------
import autonomous_worker as W  # noqa: E402
from core.face_tracker import CameraCheck  # noqa: E402
def use_storage(folder):
    W.STORAGE_DIR = folder; folder.mkdir(parents=True, exist_ok=True)
    for attr, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, attr, folder / file)
use_storage(tmp / "outbox")
pitches = W._pitches_dir()
PAGE = "https://clips.navstudiolab.com/coach-dana-k7m2qx"
def outbox_record(record_id, **extra):
    make_pitch(record_id)
    return {"record_id": record_id, "channel_id": "UC1", "channel_title": "Coach Dana", "status": "pending",
            "page_name": NAME, "page_url": PAGE, "drive_link": FOLDER,
            "full_email_body": O.build_outreach_email("Coach Dana", drive_link=PAGE, greeting_name="Dana"),
            "subject": "3 shorts from your knee pain video", "greeting_name": "Dana",
            "clips": [{"start": 100.0, "end": 140.0, "judge": 81}, {"start": 200.0, "end": 240.0, "judge": 64},
                      {"start": 300.0, "end": 340.0, "judge": 77}], **extra}
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [outbox_record("a")])
calls = []
W.clip_pages.remove_clip = lambda name, index, count: calls.append(("page", name, index, count)) or count - 1
W.remove_clip_in_drive = lambda link, index, count: calls.append(("drive", index, count))
result = W.remove_pitch_clip("a", 2)
record = json.loads(W.PENDING_OUTREACH_PATH.read_text())[0]
check("Outbox: their page and Drive are told 'clip 2 of 3'", calls == [("page", NAME, 2, 3), ("drive", 2, 3)], str(calls))
check("Outbox: 2 clips left, and the email and subject say 2",
      result == {"count": 2, "email_updated": True, "drive_failed": False, "named_clip_removed": False}
      and "cut 2 vertical clips" in record["full_email_body"] and record["subject"] == "2 shorts from your knee pain video")
check("Outbox: the record keeps clips 1 and 3, and notes the removed one (for tuning)",
      [c["start"] for c in record["clips"]] == [100.0, 300.0] and record["removed_clips"][0]["judge"] == 64)
check("Outbox: this Mac's files match", [c["start"] for c in PF.load_pitch.__wrapped__("a")["clips"]] == [100.0, 300.0]
      if hasattr(PF.load_pitch, "__wrapped__") else
      [c["start"] for c in json.loads((pitches / "a" / "pitch.json").read_text())["clips"]] == [100.0, 300.0])
try:
    W.remove_pitch_clip("a", 1)
    check("Outbox: never below 2 clips (Pass the creator instead)", False)
except ValueError as exc:
    check("Outbox: never below 2 clips (Pass the creator instead)", "at least 2" in str(exc) and len(calls) == 2)

W._write_json_atomic(W.PENDING_OUTREACH_PATH, [outbox_record("b")])
def page_down(*a):
    raise RuntimeError("R2 down")
W.clip_pages.remove_clip = page_down
try:
    W.remove_pitch_clip("b", 1)
    check("Outbox: the page can't be changed -> nothing on this Mac changes (try again)", False)
except RuntimeError:
    rec = json.loads(W.PENDING_OUTREACH_PATH.read_text())[0]
    check("Outbox: the page can't be changed -> nothing on this Mac changes (try again)",
          len(rec["clips"]) == 3 and "cut 3 vertical clips" in rec["full_email_body"]
          and len(json.loads((pitches / "b" / "pitch.json").read_text())["clips"]) == 3)
W.clip_pages.remove_clip = lambda name, index, count: count - 1
def drive_down(*a):
    raise RuntimeError("Drive down")
W.remove_clip_in_drive = drive_down
result = W.remove_pitch_clip("b", 1)
check("Outbox: Drive failing doesn't block it when their page is the link (you're told)",
      result["count"] == 2 and result["drive_failed"] is True)
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [outbox_record("c", page_name=None, page_url=None)])
try:
    W.remove_pitch_clip("c", 1)
    check("Outbox: ...but it does when Drive is the link (nothing changes)", False)
except RuntimeError:
    check("Outbox: ...but it does when Drive is the link (nothing changes)",
          len(json.loads(W.PENDING_OUTREACH_PATH.read_text())[0]["clips"]) == 3)

# ---------- Screen share with a corner webcam ----------
import core.face_tracker as F  # noqa: E402
# Real measurements from podcast clips (close-ups 3-4% of the frame, wide shots
# 0.3-0.5% at mid-height on both sides): never a screen share.
closeup = [[(0.43, 0.30, 0.033)]] * 12
wide = [[(0.197, 0.43, 0.0048), (0.831, 0.431, 0.003)]] * 4
check("Real close-ups and wide two-person shots are not a screen share", not F.classify_camera(closeup + wide, 16).screen_share_pip)
for corner, (x, y) in {"top-right": (0.87, 0.09), "top-left": (0.12, 0.10), "bottom-right": (0.88, 0.88), "bottom-left": (0.13, 0.86)}.items():
    result = F.classify_camera([[(x, y, 0.006)]] * 14, 16)
    check(f"Small steady face in the {corner} corner -> screen_share_pip", result.screen_share_pip and result.pip_corner == corner, str(result))
check("Half screen share, half on camera -> not skipped (the screen-share shots show the whole frame)",
      not F.classify_camera([[(0.87, 0.09, 0.006)]] * 8 + closeup[:8], 16).screen_share_pip)
check("A big face in a corner isn't a webcam overlay", not F.classify_camera([[(0.85, 0.2, 0.05)]] * 14, 16).screen_share_pip)
moving = [[(0.76 + (i % 4) * 0.08, 0.04 + (i % 3) * 0.12, 0.006)] for i in range(14)]  # someone moving around
check("A small face that moves around isn't an overlay", not F.classify_camera(moving, 16).screen_share_pip)
check("Faces in two different corners aren't one webcam", not F.classify_camera([[(0.1, 0.1, 0.006), (0.9, 0.9, 0.006)]] * 14, 16).screen_share_pip)
check("No faces -> not a screen share (the 'nobody on camera' check handles it)",
      F.classify_camera([[]] * 16, 16) == CameraCheck(0.0))

video = tmp / "screen.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:d=12:r=10", "-pix_fmt", "yuv420p", str(video)], check=True)
class FakeDetector:
    available = True
    def __init__(self, faces):
        self.faces = faces
    def detect(self, frame):
        h, w = frame.shape[:2]
        return [(cx * w, cy * h, fw * w, fh * h, 0.9) for cx, cy, fw, fh in self.faces]
real_detector = F._face_detector
F._face_detector = lambda: FakeDetector([(0.88, 0.10, 0.045, 0.08)])  # webcam face, top-right
found = F.camera_check(video)
check("camera_check: one pass finds the corner webcam", (found.share, found.screen_share_pip, found.pip_corner) == (1.0, True, "top-right"), str(found))
check("on_camera_share still works the same", F.on_camera_share(video) == 1.0)
plan = F.analyze_crop_plan(video, 1, 11)
check("Crop: a corner webcam face never steers the crop (whole frame shown instead)",
      not any(shot.has_face for shot in plan.shots) and plan.full_frame_ranges(1, 11) == [(0.0, 10)], str(plan.shots))
F._face_detector = lambda: FakeDetector([(0.5, 0.35, 0.12, 0.21)])  # a host on camera
found = F.camera_check(video)
check("camera_check: a host on camera is not a screen share", (found.share, found.screen_share_pip, found.pip_corner) == (1.0, False, ""), str(found))
check("Crop: ...and is still followed", all(shot.has_face for shot in F.analyze_crop_plan(video, 1, 11).shots))
F._face_detector = real_detector

use_storage(tmp / "worker")
W.init_storage_files()
steps = []
W.download_source_video = lambda url: steps.append("download") or str(tmp / "src.mp4")
(tmp / "src.mp4").write_bytes(b"x")
W.camera_check = lambda path: CameraCheck(1.0, True, "top-right")
W.extract_audio_only = lambda *a: steps.append("audio")
W.transcribe_audio = lambda *a: steps.append("groq")
W.pick_clips_with_agent = lambda *a, **k: steps.append("gemini")
lead = {"channel_id": "UCscreen", "channel_title": "Slide Guy", "latest_video_title": "Tutorial",
        "latest_video_url": "https://www.youtube.com/watch?v=abcdefg1234"}
state = W._processed_state(); state["records"].append({"channel_id": "UCscreen", "status": "processing"})
W._write_json_atomic(W.PROCESSED_CREATORS_PATH, state)
try:
    W._process_lead(dict(lead), SimpleNamespace(temp_dir=tmp / "temp", raw_dir=tmp))
    check("Worker: a screen share is skipped as screen_share_pip", False)
except W.NotAFit as exc:
    W._skip_not_a_fit(dict(lead), exc)
    check("Worker: a screen share is skipped as screen_share_pip", str(exc).startswith("screen_share_pip"), str(exc))
check("...right after the download: no transcribing or AI calls", steps == ["download"], str(steps))
saved = next(r for r in W._processed_state()["records"] if r["channel_id"] == "UCscreen")
check("...the creator is marked 'not a fit' with the reason, and never queued again",
      saved["status"] == "not a fit" and saved["error"].startswith("screen_share_pip") and "UCscreen" in W._seen_channels())
check("...and counted in the app's 'Skipped by the worker' line", W.pass_summary().get("auto_screen_share") == 1, str(W.pass_summary()))

# ---------- App: the Remove clip button ----------
from streamlit.testing.v1 import AppTest  # noqa: E402
use_storage(tmp / "app")
W.init_storage_files()
pitches = W._pitches_dir()
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [outbox_record("p9")])
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
    import core.pitch_files as P, core.clip_pages as C
    P.PITCHES_DIR = W.STORAGE_DIR / "pitches"
    C.remove_clip = lambda name, index, count: count - 1
    W.remove_clip_in_drive = lambda *a: None
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
at = AppTest.from_file(str(wrapper), default_timeout=120).run()
check("App runs", not at.exception, str([e.value for e in at.exception]))
check("Each clip has a Remove clip confirm button", all(any(b.key == f"remove_clip_p9_{i}" for b in at.button) for i in (1, 2, 3)))
next(b for b in at.button if b.key == "remove_clip_p9_2").click().run()
check("After removing: 2 clips, and the email and subject boxes say 2",
      next(t for t in at.text_input if t.key == "pending_subject_p9").value == "2 shorts from your knee pain video"
      and "cut 2 vertical clips" in next(t for t in at.text_area if t.key == "pending_pitch_p9").value
      and not any(b.key == "remove_clip_p9_3" for b in at.button))
check("...a pop-up says so", any("Clip 2 removed" in t.value for t in at.toast))
check("...and with 2 left, Remove is off (Pass the creator to send fewer)",
      all(b.disabled for b in at.button if (b.key or "").startswith("remove_off_p9_")) and
      sum((b.key or "").startswith("remove_off_p9_") for b in at.button) == 2)

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
