"""Checks for round 15: clip previews and quick trims in the Outbox.
Uses temp folders; your real storage and Drive are never touched.
Run all tests: .venv/bin/python tests/run_all.py"""
import json, subprocess, sys, tempfile, textwrap
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
import os as _os; _os.environ["NAV_NO_CLIP_PAGES"] = "1"  # never touch the real clip-page bucket

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

import numpy as np
import core.pitch_files as P
from core.hook_selector import ClipResult
from core.transcriber import Word

tmp = Path(tempfile.mkdtemp(prefix="r15-"))
P.PITCHES_DIR = tmp / "pitches"


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def duration(path):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                                capture_output=True, text=True).stdout)


def gray_frame(path, t):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.4f}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).astype(int)


# A 2-minute source with a keyframe every 2 s, and a stand-in rendered clip.
src = tmp / "src.mp4"
ff("-f", "lavfi", "-i", "testsrc2=s=320x180:r=24:d=120", "-f", "lavfi", "-i", "anoisesrc=d=120:c=pink:r=48000:a=0.3",
   "-c:v", "libx264", "-preset", "veryfast", "-g", "48", "-keyint_min", "48", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
   "-c:a", "aac", "-shortest", str(src))
rendered_clip = tmp / "clip_01.mp4"
ff("-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=4", "-f", "lavfi", "-i", "sine=d=4", "-c:v", "libx264", "-preset",
   "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(rendered_clip))

# ---------- Lossless slice of the source ----------
check("Keyframe at or before 13.3 s is 12 s", P.keyframe_at_or_before(src, 13.3) == 12.0)
origin = P.cut_source_slice(src, 37.7, 95.0, tmp / "slice.mp4")
check("Slice starts at the keyframe before the margin (36 s, minus the sound's head start)", 35.8 < origin <= 36.0, f"{origin:.3f}")
worst = max(abs(gray_frame(src, s) - gray_frame(tmp / "slice.mp4", s - origin)).mean() for s in (36.43, 56.84, 92.26))
check("Slice frames match the source exactly at the recorded offset", worst == 0, f"diff {worst}")

# ---------- Saving a pitch's files ----------
words = [Word(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(240)]
clip = ClipResult(start_time=50.0, end_time=85.0, viral_title="A title", caption="c", hashtags=["x"], virality_score=80)
item = SimpleNamespace(path=rendered_clip, clip=clip, layout_mode="smart_crop")
P.save_pitch_files("pitchA", src, [item], words, 120.0)
pitch = P.load_pitch("pitchA")
c = pitch["clips"][0]
names = sorted(f.name for f in P.pitch_dir("pitchA").iterdir())
check("Saved: clip, preview, source slice, pitch.json",
      names == ["clip_01.mp4", "clip_01.preview.mp4", "clip_01.source.mp4", "pitch.json"], str(names))
check("Preview is 720 px wide", subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width",
                                                "-of", "csv=p=0", str(c["preview_path"])], capture_output=True, text=True).stdout.strip() == "720")
check("Slice covers 30 s either side of the clip", c["slice_start"] <= 20.0 and c["slice_end"] >= 114.9, f"{c['slice_start']}–{c['slice_end']}")
check("Words saved relative to the slice", abs(c["words"][0][1] - (20.0 - c["slice_start"])) < 0.6 and len(c["words"]) > 150)
check("Layout and title kept for re-rendering", c["layout"] == "smart_crop" and c["clip"]["viral_title"] == "A title")
check("Trim limits = the slice", P.trim_limits(c) == (c["slice_start"], c["slice_end"] - 0.05))
check("Unknown pitch -> no preview (not an error)", P.load_pitch("nothere") is None and P.load_pitch("../x") is None)
try:
    P.pitch_dir("../../etc")
    check("Pitch ids can't point outside storage/pitches", False)
except ValueError:
    check("Pitch ids can't point outside storage/pitches", True)

# ---------- Re-render a trim (render faked: checks what it's asked to do) ----------
asked = {}
def fake_render(source, clip, words, settings, output, *, layout_mode, work_dir, graphics=None):
    asked.update(source=source, start=clip.start_time, end=clip.end_time, title=clip.viral_title, layout=layout_mode,
                 first_word=words[0], n=len(words))
    output.write_bytes(rendered_clip.read_bytes())
    return SimpleNamespace(path=output)
import core.pipeline
real_render = core.pipeline.render_selected_clip
core.pipeline.render_selected_clip = fake_render
settings = SimpleNamespace(temp_dir=tmp / "temp")
P.rerender_trim("pitchA", 1, 47.5, 83.0, settings)
offset = c["slice_start"]
check("Trim renders from the slice with times shifted to it",
      asked["source"].name == "clip_01.source.mp4" and abs(asked["start"] - (47.5 - offset)) < 1e-6 and abs(asked["end"] - (83.0 - offset)) < 1e-6)
check("...same layout and title as the original", asked["layout"] == "smart_crop" and asked["title"] == "A title")
check("...with the saved words", asked["n"] == len(c["words"]) and asked["first_word"].text == c["words"][0][0])
pitch = P.load_pitch("pitchA")
check("Trimmed version waits (original untouched)", pitch["clips"][0]["trim_waiting"] and pitch["clips"][0]["start"] == 50.0
      and pitch["clips"][0]["trim"] == {"start": 47.5, "end": 83.0})
check("Temp render folder cleaned up", not any((tmp / "temp").glob("trim_*")))
for bad, why in (((50.0, 60.0), "too short"), ((10.0, 60.0), "beyond the 30 s margin"), ((50.0, 150.0), "past the slice")):
    try:
        P.rerender_trim("pitchA", 1, *bad, settings)
        check(f"Refuses a trim that's {why}", False)
    except ValueError as exc:
        check(f"Refuses a trim that's {why}", True, str(exc)[:70])

# ---------- Use / keep ----------
def failing_remote(path, name):
    raise RuntimeError("Drive is down")
try:
    P.accept_trim("pitchA", 1, failing_remote)
except RuntimeError:
    pass
pitch = P.load_pitch("pitchA")
check("Drive failure -> nothing changes locally (the new cut still waits)",
      pitch["clips"][0]["start"] == 50.0 and pitch["clips"][0]["trim_waiting"])
remote = []
times = P.accept_trim("pitchA", 1, lambda path, name: remote.append((path.name, name)))
pitch = P.load_pitch("pitchA")
check("Use this cut: Drive file replaced first, under the original name", remote == [("clip_01.trim.mp4", "clip_01.mp4")])
check("...then the local clip, and the new times are saved", times == {"start": 47.5, "end": 83.0}
      and pitch["clips"][0]["start"] == 47.5 and not pitch["clips"][0]["trim_waiting"] and not list(P.pitch_dir("pitchA").glob("*trim*")))
check("...the AI's original cut is remembered", pitch["clips"][0]["ai_start"] == 50.0)
P.rerender_trim("pitchA", 1, 48.0, 80.0, settings)
P.discard_trim("pitchA", 1)
pitch = P.load_pitch("pitchA")
check("Keep the old cut: trim files deleted, times unchanged",
      not list(P.pitch_dir("pitchA").glob("*trim*")) and pitch["clips"][0]["start"] == 47.5 and "trim" not in pitch["clips"][0])
core.pipeline.render_selected_clip = real_render

# ---------- Drive: replace in place ----------
import core.drive_uploader as DU
calls = {}
class FakeRequest:
    def __init__(self, result): self.result = result
    def execute(self, num_retries=0): return self.result
class FakeFiles:
    def list(self, q, fields, pageSize): calls["q"] = q; return FakeRequest({"files": [{"id": "FILE1", "name": "clip_02.mp4"}]})
    def update(self, fileId, media_body, fields): calls["update"] = fileId; return FakeRequest({"id": fileId})
real_build, real_creds = DU.build, DU._get_user_credentials
DU.build = lambda *a, **k: SimpleNamespace(files=lambda: FakeFiles())
DU._get_user_credentials = lambda *a, **k: None
DU.replace_clip_in_drive("https://drive.google.com/drive/folders/1AbC_d-9?usp=sharing", "clip_02.mp4", rendered_clip)
check("Drive: finds the clip by name in the pitch's folder",
      calls.get("q") == "'1AbC_d-9' in parents and name = 'clip_02.mp4' and trashed = false", calls.get("q"))
check("Drive: updates that same file (link unchanged)", calls.get("update") == "FILE1")
for link, name in (("https://example.com/x", "clip_01.mp4"), ("https://drive.google.com/drive/folders/abc", "x' or name != '")):
    try:
        DU.replace_clip_in_drive(link, name, rendered_clip)
        check(f"Drive: refuses {name if 'or' in name else link}", False)
    except ValueError:
        check(f"Drive: refuses {'a strange file name' if 'or' in name else 'a non-Drive link'}", True)
DU.build, DU._get_user_credentials = real_build, real_creds

# ---------- Worker: files made with the pitch, gone when sent/passed ----------
import autonomous_worker as W
from core.face_tracker import CameraCheck  # noqa: E402
W.STORAGE_DIR = tmp / "storage"
W.STORAGE_DIR.mkdir()
for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                   ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                   ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
    setattr(W, name, W.STORAGE_DIR / file)
W.init_storage_files()
P.PITCHES_DIR = W.STORAGE_DIR / "pitches"

from core import outreach as O
W.check_drive_access = lambda: None
W.download_source_video = lambda url: str(src)
W.camera_check = lambda path: CameraCheck(1.0)  # round 19: one check for camera and screen share
W.extract_audio_only = lambda s, d: d
W.transcribe_audio = lambda audio, settings: words
W._video_duration = lambda path: 120.0
W.video_info_and_peaks = lambda url: ({}, [])
W.pick_clips_with_agent = lambda *a, **k: ([clip], O.build_outreach_email("Dana", "Ep", "knees"), "3 Shorts from your knee video")
def fake_render_clips(source, clips, job_dir, settings, words, layouts_out=None, rendered_out=None, graphics=None):
    out = job_dir / "clip_01.mp4"; out.write_bytes(rendered_clip.read_bytes())
    layouts_out.append("smart_crop"); rendered_out.append(SimpleNamespace(path=out, clip=clip, layout_mode="smart_crop"))
    return [out]
W._render_selected_clips = fake_render_clips
W.upload_clips_to_drive = lambda *a, **k: "https://drive.google.com/drive/folders/abc"
raw = tmp / "raw"; raw.mkdir()
src_copy = raw / "src.mp4"; src_copy.write_bytes(src.read_bytes())
W.download_source_video = lambda url: str(src_copy)
lead = {"channel_id": "UC1", "channel_title": "Coach Dana", "latest_video_title": "Ep", "latest_video_url": "https://www.youtube.com/watch?v=abc"}
record = W._process_lead(dict(lead), SimpleNamespace(temp_dir=tmp / "temp", raw_dir=raw))
check("Worker: each new pitch gets its preview/trim files", P.load_pitch(record["record_id"]) is not None)
check("...and the full source download is still deleted", not src_copy.exists())

src_copy.write_bytes(src.read_bytes())
W.upload_clips_to_drive = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("upload failed"))
before = set(p.name for p in P.PITCHES_DIR.iterdir())
try:
    W._process_lead(dict(lead, channel_id="UC2"), SimpleNamespace(temp_dir=tmp / "temp", raw_dir=raw))
except RuntimeError:
    pass
check("Worker: a pitch that fails later leaves no files behind", set(p.name for p in P.PITCHES_DIR.iterdir()) == before)

check("Trim saved on the pitch record, AI's times kept",
      W.record_clip_trim(record["record_id"], 1, 47.5, 83.0)
      and (lambda c: c["start"] == 47.5 and c["ai_start"] == 50.0 and "trimmed_at" in c)(
          next(r for r in json.loads(W.PENDING_OUTREACH_PATH.read_text()) if r["record_id"] == record["record_id"])["clips"][0]))
check("Mark as Sent deletes the pitch's files", W.mark_outreach_sent(record["record_id"]) and P.load_pitch(record["record_id"]) is None)

src_copy.write_bytes(src.read_bytes())
W.upload_clips_to_drive = lambda *a, **k: "https://drive.google.com/drive/folders/abc"
second = W._process_lead(dict(lead, channel_id="UC3"), SimpleNamespace(temp_dir=tmp / "temp", raw_dir=raw))
check("Pass deletes the pitch's files", W.skip_lead("UC3", pass_reason=next(iter(W.PASS_REASONS))) and P.load_pitch(second["record_id"]) is None)

(P.PITCHES_DIR / "deadbeef").mkdir(parents=True)
(P.PITCHES_DIR / "not a pitch").mkdir()
src_copy.write_bytes(src.read_bytes())
third = W._process_lead(dict(lead, channel_id="UC4"), SimpleNamespace(temp_dir=tmp / "temp", raw_dir=raw))
W._prune_leftovers(SimpleNamespace(raw_dir=raw, temp_dir=tmp / "temp"))
left = sorted(p.name for p in P.PITCHES_DIR.iterdir())
check("Startup cleanup: removes leftover pitch folders, keeps the Outbox's", left == sorted([third["record_id"], "not a pitch"]), str(left))

# ---------- App: previews and trim on the pitch card ----------
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
    import core.pitch_files as P, core.drive_uploader as DU, shutil, json
    P.PITCHES_DIR = W.STORAGE_DIR / "pitches"
    log = W.STORAGE_DIR / "calls.log"
    def fake_rerender(record_id, index, start, end, settings):
        d = P.pitch_dir(record_id)
        shutil.copy(d / "clip_01.mp4", d / "clip_01.trim.mp4"); shutil.copy(d / "clip_01.preview.mp4", d / "clip_01.trim.preview.mp4")
        m = json.loads((d / "pitch.json").read_text()); m["clips"][0]["trim"] = {{"start": start, "end": end}}
        (d / "pitch.json").write_text(json.dumps(m))
        with log.open("a") as f: f.write(f"rerender {{index}} {{start}} {{end}}\\n")
    P.rerender_trim = fake_rerender
    def fake_replace(link, name, path):
        with log.open("a") as f: f.write(f"drive {{link}} {{name}}\\n")
    DU.replace_clip_in_drive = fake_replace
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
pending = json.loads(W.PENDING_OUTREACH_PATH.read_text())
pending.append({"record_id": "oldpitch", "channel_id": "UC9", "channel_title": "Old Pitch", "status": "pending",
                "drive_link": "https://drive.google.com/drive/folders/old", "full_email_body": "Hi,\n\nx", "subject": "s"})
W._write_json_atomic(W.PENDING_OUTREACH_PATH, pending)
rid = third["record_id"]
at = AppTest.from_file(str(wrapper), default_timeout=120).run()
check("App runs", not at.exception, str([e.value for e in at.exception]))
check("Pitch card plays the clip (720p preview)", len(at.get("video")) == 1)
check("Clip line: number, times, length, AI score",
      any(m.value.startswith("**Clip 1** · 0:50–1:25 · 35 s") for m in at.markdown), str([m.value for m in at.markdown][:8]))
check("Older pitch without files -> says to use Drive", any("No preview on this Mac" in c.value for c in at.caption))
start_input = next(n for n in at.number_input if n.key == f"trim_start_{rid}_1")
end_input = next(n for n in at.number_input if n.key == f"trim_end_{rid}_1")
go = next(b for b in at.button if b.key == f"trim_go_{rid}_1")
check("Trim: Re-render is off until something changes", go.disabled)
check("Trim: start can move up to 30 s earlier", start_input.min <= -29.9, str(start_input.min))
start_input.set_value(-2.5).run()
end_input = next(n for n in at.number_input if n.key == f"trim_end_{rid}_1")
end_input.set_value(-2.0).run()
check("Trim: shows the new cut", any("New cut: 0:47–1:23 (35.5 s)" in c.value for c in at.caption))
next(b for b in at.button if b.key == f"trim_go_{rid}_1").click().run()
calls_log = (W.STORAGE_DIR / "calls.log").read_text()
check("Re-render asked for 47.5–83.0 s", "rerender 1 47.5 83.0" in calls_log, calls_log)
check("New cut waits with Use / Keep buttons",
      any("New cut, not used yet" in m.value for m in at.markdown)
      and any(b.key == f"trim_use_{rid}_1" for b in at.button) and any(b.key == f"trim_keep_{rid}_1" for b in at.button))
next(b for b in at.button if b.key == f"trim_use_{rid}_1").click().run()
calls_log = (W.STORAGE_DIR / "calls.log").read_text()
check("Use this cut: replaces clip_01.mp4 in the pitch's Drive folder",
      "drive https://drive.google.com/drive/folders/abc clip_01.mp4" in calls_log, calls_log)
saved = next(r for r in json.loads(W.PENDING_OUTREACH_PATH.read_text()) if r["record_id"] == rid)["clips"][0]
check("...saves the new times on the pitch", saved["start"] == 47.5 and saved["end"] == 83.0 and saved["ai_start"] == 50.0)
check("...says the link didn't change (a toast, so the page doesn't move), and the inputs reset to 0",
      any("link in the email stays the same" in t.value for t in at.toast)
      and next(n for n in at.number_input if n.key == f"trim_start_{rid}_1").value == 0.0)
check("...and the clip line shows the new times", any(m.value.startswith("**Clip 1** · 0:47–1:23") for m in at.markdown))

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
