"""Checks for round 14: regular (not HLS) downloads, the worker keeps running by default,
one worker at a time, Start/Stop from the app, nothing lost when stopped mid-creator.
Uses a temp folder; your real storage files are never touched.
Run all tests: .venv/bin/python tests/run_all.py"""
import json, os, subprocess, sys, tempfile, textwrap, time
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ.pop("SOURCE_MAX_HEIGHT", None)

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

# ---------- Downloads: regular streams, not YouTube's chunked HLS ones ----------
import yt_dlp
import core.downloader as D


def fmt(fid, h, vcodec, ext, tbr, proto="https"):
    return {"format_id": fid, "height": h, "width": h * 16 // 9, "vcodec": vcodec, "acodec": "none", "ext": ext,
            "url": f"http://x/{fid}", "protocol": proto, "tbr": tbr, "dynamic_range": "SDR", "fps": 30}


AUDIO = [{"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "url": "http://x/a", "protocol": "https", "tbr": 129, "abr": 129},
         {"format_id": "251", "vcodec": "none", "acodec": "opus", "ext": "webm", "url": "http://x/o", "protocol": "https", "tbr": 135, "abr": 135}]
# Shaped like YouTube's list for a 1080p podcast upload (616/614/270 are HLS).
YT_1080 = [fmt("137", 1080, "avc1.640028", "mp4", 1200), fmt("248", 1080, "vp9", "webm", 1500),
           fmt("399", 1080, "av01.0.08M.08", "mp4", 1100), fmt("616", 1080, "vp09.00.40.08", "mp4", 5800, "m3u8_native"),
           fmt("614", 1080, "vp09.00.40.08", "mp4", 1700, "m3u8_native"), fmt("270", 1080, "avc1.640028", "mp4", 4500, "m3u8_native")]
YT_1440 = YT_1080 + [fmt("271", 1440, "vp9", "webm", 4200), fmt("400", 1440, "av01.0.12M.08", "mp4", 3800),
                     fmt("620", 1440, "vp09.00.50.08", "mp4", 4400, "m3u8_native")]


def pick(formats):
    ydl = yt_dlp.YoutubeDL({"format": D.VIDEO_FORMAT, "format_sort": D.video_format_sort(), "quiet": True, "simulate": True})
    info = {"id": "x", "title": "t", "formats": [dict(f) for f in formats + AUDIO], "extractor": "generic",
            "extractor_key": "Generic", "webpage_url": "http://x"}
    return ydl.process_ie_result(info, download=False).get("format_id")


check("1080p video: regular VP9 (248), not the 616 HLS stream round 12 picked", pick(YT_1080) == "248+140", pick(YT_1080))
check("1440p video: regular VP9 1440p (271), not 620 HLS", pick(YT_1440) == "271+140", pick(YT_1440))
os.environ["SOURCE_MAX_HEIGHT"] = "1080"
check("SOURCE_MAX_HEIGHT=1080 -> 248", pick(YT_1440) == "248+140")
os.environ.pop("SOURCE_MAX_HEIGHT")
only_hls = [f for f in YT_1080 if f["protocol"] != "https"]
check("Only HLS available -> still downloads (VP9 HLS)", pick(only_hls) in {"616+140", "614+140"}, pick(only_hls))
check("Download log line: format, size, time, speed",
      D.download_summary("248+140", 560_000_000, 140) == "Downloaded format 248+140: 560 MB in 140 s (4.0 MB/s)")

# ---------- Worker setup on a temp folder ----------
import autonomous_worker as W

tmp = Path(tempfile.mkdtemp(prefix="r14-"))
W.STORAGE_DIR = tmp
for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                   ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                   ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
    setattr(W, name, tmp / file)
W.LOG_PATH = tmp / "logs" / "worker.log"
W._setup_logging = lambda: None
W.init_storage_files = lambda: None
W.load_settings = lambda: None
W._prune_leftovers = lambda settings: None

# ---------- Plain start = keeps running ----------
calls = []
real_continuous, real_batch = W.run_continuous_queue, W.run_worker
W.run_continuous_queue = lambda **k: calls.append("continuous")
W.run_worker = lambda batch: calls.append(f"batch {batch}") or 0
for argv in ([], ["--continuous"], ["--batch", "2"]):
    sys.argv = ["autonomous_worker.py", *argv]
    W.main()
check("No flags -> keeps the Outbox filled (round 10's 5:12 PM run made 3 and quit)", calls[0] == "continuous")
check("--continuous still works", calls[1] == "continuous")
check("--batch 2 -> one-off run of 2", calls[2] == "batch 2")
check("Status 'stopped' after it ends", W.get_worker_status()["state"] == "stopped")

# ---------- One worker at a time ----------
calls.clear()
W._set_status("processing", "Rich Ryan")
held = W._claim_single_instance()
sys.argv = ["autonomous_worker.py"]
code = W.main()
check("Second worker doesn't start while one runs", code == 0 and calls == [])
check("...and doesn't mark the running one as stopped", W.get_worker_status()["state"] == "processing")
held.close()
W.main()
check("Starts normally once the first one is gone", calls == ["continuous"])
W.run_continuous_queue, W.run_worker = real_continuous, real_batch

# ---------- Stop from the app (while it waits for room in the Outbox) ----------
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [{"record_id": str(i), "status": "pending"} for i in range(W.MAX_PENDING_LEADS)])
W._write_json_atomic(W.LEAD_QUEUE_PATH, [])
real_sleep = W.time.sleep
naps = []
def nap(seconds):
    naps.append(seconds)
    if len(naps) == 2:
        W._stop_request_path().write_text("now")
    if len(naps) > 10:
        raise RuntimeError("didn't stop")
W.time.sleep = nap
W._stop_request_path().write_text("old")  # left over from before: must not stop a fresh start
try:
    W.run_continuous_queue(poll_interval_sec=5)
    stopped_cleanly = True
except RuntimeError:
    stopped_cleanly = False
W.time.sleep = real_sleep
check("Old stop request is ignored at start", len(naps) >= 2)
check("Stop request -> stops within one check (5 s while the Outbox is full)", stopped_cleanly and len(naps) == 2, f"naps={naps}")
check("Stop request is cleared after use", not W._stop_request_path().exists())

# ---------- Stopped mid-creator -> back in the queue ----------
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [])
lead = {"channel_id": "UC_rr", "channel_title": "Rich Ryan", "latest_video_url": "https://www.youtube.com/watch?v=abc123",
        "latest_video_title": "Ep 1"}
W._write_json_atomic(W.LEAD_QUEUE_PATH, [lead])
real_process = W._process_lead
W._process_lead = lambda lead, settings: (_ for _ in ()).throw(KeyboardInterrupt())
try:
    W.run_continuous_queue()
    interrupted = False
except KeyboardInterrupt:
    interrupted = True
W._process_lead = real_process
queue = json.loads(W.LEAD_QUEUE_PATH.read_text())
check("Ctrl+C mid-creator still stops the worker", interrupted)
check("...and the creator is back in the queue, attempt not counted",
      [q["channel_id"] for q in queue] == ["UC_rr"] and queue[0].get("attempts", 0) == 0, str(queue))
check("...and can be picked again", (W._pop_fresh_lead() or {}).get("channel_id") == "UC_rr")

# ---------- stop_worker(): now while waiting, after the current creator while working ----------
fake = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "autonomous_worker"])
W._write_json_atomic(W._worker_status_path(), {"pid": fake.pid, "state": "outbox_full", "detail": "5/5", "updated_at": ""})
result = W.stop_worker()
try:
    fake.wait(timeout=10)
except subprocess.TimeoutExpired:
    fake.kill()
check("Waiting -> stops right away (same as Ctrl+C)", result == "stopped" and fake.returncode is not None and fake.returncode != 0,
      f"{result}, exit {fake.returncode}")

other = subprocess.Popen(["sleep", "60"])
W._write_json_atomic(W._worker_status_path(), {"pid": other.pid, "state": "outbox_full", "detail": "", "updated_at": ""})
result = W.stop_worker()
time.sleep(0.5)
check("Never signals a process that isn't the worker", other.poll() is None and result == "after_current", result)
other.kill()

fake = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "autonomous_worker"])
W._write_json_atomic(W._worker_status_path(), {"pid": fake.pid, "state": "processing", "detail": "Rich Ryan", "updated_at": ""})
result = W.stop_worker()
time.sleep(0.5)
status = W.get_worker_status()
check("Making clips -> finishes that creator first", result == "after_current" and fake.poll() is None and status["stopping"])
fake.kill(); fake.wait()
W._clear_stop_request()

# ---------- start_worker(): background, keeps the Mac awake, not twice ----------
W._set_status("stopped")
launched = {}
real_popen = W.subprocess.Popen
W.subprocess.Popen = lambda cmd, **k: launched.update(cmd=cmd, **k)
W._stop_request_path().write_text("old")
started = W.start_worker()
W.subprocess.Popen = real_popen
cmd = launched.get("cmd") or []
check("Start: runs autonomous_worker.py --continuous in the background",
      started and cmd[-2].endswith("autonomous_worker.py") and cmd[-1] == "--continuous" and launched.get("start_new_session"), str(cmd))
check("Start: clears an old stop request", not W._stop_request_path().exists())
if sys.platform == "darwin":
    check("Start: keeps the Mac from idle-sleeping (caffeinate)", cmd[:2] == ["caffeinate", "-i"], str(cmd))
W._set_status("processing", "x")
check("Start does nothing if a worker already runs", W.start_worker() is False)

# ---------- App: Start / Stop buttons ----------
from streamlit.testing.v1 import AppTest  # noqa: E402

wrapper = tmp / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys; sys.path.insert(0, {str(ROOT)!r})
    import autonomous_worker as W
    from pathlib import Path
    W.STORAGE_DIR = Path({str(tmp)!r})
    for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, name, W.STORAGE_DIR / file)
    W.start_worker = lambda: (W.STORAGE_DIR / "started.flag").write_text("1") and False
    W.stop_worker = lambda: (W.STORAGE_DIR / "stopped.flag").write_text("1") and "after_current"
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
W._set_status("stopped")
at = AppTest.from_file(str(wrapper), default_timeout=120).run()
check("App runs", not at.exception, str([e.value for e in at.exception]))
start_buttons = [b for b in at.button if b.key == "start_worker_header"]
check("Not running -> '▶ Start the worker' next to the warning", len(start_buttons) == 1 and start_buttons[0].label == "▶ Start the worker")
check("Terminal command still shown", any("autonomous_worker.py --continuous" in str(c.value) for c in at.code))
start_buttons[0].click().run()
check("Clicking Start starts the worker", (tmp / "started.flag").exists())

W._set_status("outbox_full", "5/5")
at = AppTest.from_file(str(wrapper), default_timeout=120).run()
stop = [b for b in at.button if b.key == "stop_worker"]
check("Running -> System tab has '⏹ Stop the worker', header has no Start", len(stop) == 1 and not any(b.key == "start_worker_header" for b in at.button))
check("Outbox-full status says it continues on its own",
      any("makes the next pitch as soon as you send or pass one" in str(s.value) for s in at.info))
stop[0].click().run()
check("Clicking Stop stops the worker", (tmp / "stopped.flag").exists())

W._stop_request_path().write_text("now")
W._set_status("processing", "Rich Ryan")
at = AppTest.from_file(str(wrapper), default_timeout=120).run()
check("Stop requested while working -> header says it's finishing Rich Ryan first",
      any("finishes Rich Ryan, then stops" in str(s.value) for s in at.info))
W._set_status("stopped")

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
