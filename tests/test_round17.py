"""Checks for round 17: the quality gate can't be skipped, damaged state files stop the
worker, saved transcripts, clip page first (Drive only a backup), pauses for outside
services, encoder keyframes, quieter logs, and the Outbox (About button, trim panel).
Temp folders and fakes only: nothing is downloaded, uploaded or sent.
Run all tests: .venv/bin/python tests/run_all.py"""
import json, logging, os, subprocess, sys, tempfile, textwrap, time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["NAV_NO_CLIP_PAGES"] = "1"
os.environ["GEMINI_API_KEY"] = "test-key"  # set, so your .env can't fill in a real one
os.environ["DRIVE_BACKUP"] = "on"

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

tmp = Path(tempfile.mkdtemp())
from core.transcriber import Word  # noqa: E402
from core.service_pause import ServicePause  # noqa: E402

# ---------- Quality gate: never sends unscored filler (the Prime Venture Partners case) ----------
import core.pipeline as P  # noqa: E402
words, t = [], 0.0
for i in range(3000):
    words.append(Word(text=f"word{i}" + ("." if i % 10 == 9 else ""), start=t, end=t + 0.35)); t += 0.4

def ai_picks(starts):
    def select(**kwargs):
        return {"clips": [{"start_time": s, "end_time": s + 40, "hook_title": f"Moment {s:.0f}", "reason": "r",
                           "point": "", "usual_take": ""} for s in starts],
                "outreach_email": "x", "subject": "s", "video_topic": "", "key_point": "",
                "host_first_name": "", "short_topic": "", "format": "video"}
    return select

judge_calls = []
P.select_clips_and_draft_pitch = ai_picks([400.0])
P.judge_clips = lambda texts: judge_calls.append(len(texts)) or [90] * len(texts)
try:
    P.pick_clips_with_agent(words, t, 3, "Chan", "Title", [], min_judge_score=60)
    check("One real moment -> skipped (WeakMoments), not padded with filler", False)
except P.WeakMoments as exc:
    check("One real moment -> skipped (WeakMoments), not padded with filler", "only 1 clip" in str(exc), str(exc))
check("...and the judge ran even with nothing to choose between", judge_calls == [1], str(judge_calls))

P.select_clips_and_draft_pitch = ai_picks([100.0, 400.0, 800.0, 1000.0])
P.judge_clips = lambda texts: None
try:
    P.pick_clips_with_agent(words, t, 3, "Chan", "Title", [], min_judge_score=60)
    check("Judge down -> JudgeUnavailable (a 10-min pause), never unscored clips", False)
except P.JudgeUnavailable as exc:
    check("Judge down -> JudgeUnavailable (a 10-min pause), never unscored clips",
          isinstance(exc, ServicePause) and exc.service == "judge" and exc.retry_after == 600)

P.judge_clips = lambda texts: [80, 70, 65, 40][: len(texts)]
report = {}
clips, _email, _subject = P.pick_clips_with_agent(words, t, 3, "Chan", "Title", [], min_judge_score=60, report=report)
check("Four good candidates -> the best three, all scored",
      [round(c.start_time) for c in clips] == [100, 400, 800] and report["judge_scores"] == [80, 70, 65], str(report))
P.select_clips_and_draft_pitch = ai_picks([400.0])
P.judge_clips = lambda texts: None
clips, _e, _s = P.pick_clips_with_agent(words, t, 3, "Chan", "Title", [])
check("App (no gate) unchanged: still fills to 3 clips", len(clips) == 3)

# ---------- The judge: reads whole clips, one retry, passes a Gemini pause on ----------
import core.agent_brain as A  # noqa: E402
real_request_gemini = A._request_gemini
prompts = []
answers = iter(["not json", json.dumps({"scores": [{"clip": 1, "hook": 8, "standalone": 8, "payoff": 8}]})])
A._request_gemini = lambda key, prompt, system_prompt=None: prompts.append(prompt) or next(answers)
long_clip = " ".join(f"w{i}" for i in range(280))
scores = A.judge_clips([long_clip])
check("Judge retries an unusable answer once", scores == [80] and len(prompts) == 2, str(scores))
check("Judge reads up to 300 words per clip (the ending counts for 'payoff')", "w279" in prompts[0])
A._request_gemini = lambda *a, **k: "still not json"
check("Two unusable answers -> None", A.judge_clips(["a b c"]) is None)
def limit(*a, **k):
    raise ServicePause("Gemini's request limit is used up", 1800, service="gemini")
A._request_gemini = limit
try:
    A.judge_clips(["a b c"])
    check("A used-up Gemini limit reaches the worker (it pauses)", False)
except ServicePause:
    check("A used-up Gemini limit reaches the worker (it pauses)", True)
A._request_gemini = real_request_gemini

try:
    import google.genai as genai
    from google.genai.errors import ClientError
except ImportError:
    genai = None
if genai is not None:
    seen = {}
    class FakeModels:
        def generate_content(self, model, contents, config):
            seen["config"] = config
            return SimpleNamespace(text='{"ok": 1}')
    class FakeClient:
        def __init__(self, api_key, http_options=None):
            seen["http_options"] = http_options; self.models = FakeModels()
    real_client, genai.Client = genai.Client, FakeClient
    A._request_gemini("key", "prompt")
    genai.Client = real_client
    check("Gemini requests time out after 2 minutes (no more endless hang)", seen["http_options"].timeout == 120_000)
    check("Automatic function calling is off", seen["config"].automatic_function_calling.disable is True)

    import core.gemini_resilience as G
    real_sleep = time.sleep
    time.sleep = lambda seconds: None
    quota = ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED",
                                        "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "5400s"}]}})
    models_tried = []
    def always_limited(model, **kwargs):
        models_tried.append(model); raise quota
    try:
        G.generate_content_with_fallback(always_limited, model="gemini-3.8-flash")
        check("Gemini limit used up -> ServicePause with Gemini's own wait", False)
    except ServicePause as exc:
        check("Gemini limit used up -> ServicePause with Gemini's own wait",
              exc.service == "gemini" and exc.retry_after == 5400 and models_tried[-1] == G.FALLBACK_MODEL, str(models_tried))
    time.sleep = real_sleep

# ---------- Encoder: a keyframe every 2 s ----------
import core.video_renderer as V  # noqa: E402
real_run = V.subprocess.run
V.subprocess.run = lambda *a, **k: SimpleNamespace(stdout=" V..... h264_videotoolbox VideoToolbox H.264 Encoder")
mac = V._video_encoder_args("ffmpeg-r17-mac")
V.subprocess.run = real_run
soft = V._video_encoder_args("ffmpeg-r17-missing")
check("VideoToolbox: -g 60 (was FFmpeg's default of 12)", mac[mac.index("-g") + 1] == "60" and mac[:4] == ("-c:v", "h264_videotoolbox", "-b:v", "10M"), str(mac))
check("libx264 fallback: -g 60 too", soft[soft.index("-g") + 1] == "60")

# ---------- Downloads: quiet, YouTube blocks, heatmap reuse, audio language ----------
import core.downloader as D  # noqa: E402
check("Bot check -> pause (not a used attempt)",
      isinstance(D._download_error(Exception("ERROR: Sign in to confirm you’re not a bot"), "x"), ServicePause))
check("HTTP 429 -> pause", isinstance(D._download_error(Exception("HTTP Error 429: Too Many Requests"), "x"), ServicePause))
check("HTTP 403 -> DownloadForbidden", isinstance(D._download_error(Exception("HTTP Error 403: Forbidden"), "x"), D.DownloadForbidden))
other = D._download_error(Exception("Private video"), "x")
check("Other errors unchanged (RuntimeError)", type(other) is RuntimeError and "Private video" in str(other))

captured = {}
class FakeYDL:
    def __init__(self, options): captured.update(options)
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def extract_info(self, url, download=True):
        Path(captured["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"video")
        return {"format_id": "137+140-1", "heatmap": [{"start_time": 0, "end_time": 5, "value": 1.0}],
                "requested_formats": [{"acodec": "none"}, {"acodec": "mp4a.40.2", "language": "en"}]}
fake_module = SimpleNamespace(YoutubeDL=FakeYDL, DownloadError=type("DownloadError", (Exception,), {}))
real_ytdlp = sys.modules.get("yt_dlp"); sys.modules["yt_dlp"] = fake_module
lines = []
handler = logging.Handler(); handler.emit = lambda record: lines.append(record.getMessage())
D.logger.addHandler(handler); D.logger.setLevel(logging.INFO)
(tmp / "raw").mkdir()
url = "https://example.com/video.mp4"
D._download_url(url, SimpleNamespace(raw_dir=tmp / "raw"))
if real_ytdlp is not None:
    sys.modules["yt_dlp"] = real_ytdlp
check("yt-dlp progress bars off (they filled worker_console.log)", captured.get("noprogress") is True and captured.get("quiet") is True)
info = D.downloaded_video_info(url)
check("The download's heatmap is kept for clip picking", info and info["heatmap"][0]["value"] == 1.0, str(info))
check("...handed over once", D.downloaded_video_info(url) is None)
check("Download log line names the audio language", any("audio language en" in line for line in lines), str(lines))
check("yt-dlp version is readable for the startup log", isinstance(D.yt_dlp_version(), str))

result = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import autonomous_worker; "
                         "print('faster_whisper' in sys.modules, 'av' in sys.modules)", str(ROOT)],
                        capture_output=True, text=True, cwd=ROOT)
check("Worker starts without loading faster-whisper/PyAV (no objc duplicate-class warning)",
      result.stdout.strip().endswith("False False"), result.stdout + result.stderr[-300:])

# ---------- Worker state: a damaged file stops it instead of starting over empty ----------
import autonomous_worker as W  # noqa: E402
from core.face_tracker import CameraCheck  # noqa: E402
def use_storage(folder):
    W.STORAGE_DIR = folder; folder.mkdir(parents=True, exist_ok=True)
    for attr, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, attr, folder / file)
use_storage(tmp / "state")
W.PROCESSED_CREATORS_PATH.write_text('{"processed_channel_ids": ["UC1", ')
try:
    W._processed_state()
    check("Damaged processed_creators.json -> StateFileError", False)
except W.StateFileError as exc:
    check("Damaged processed_creators.json -> StateFileError", "can't be read" in str(exc), str(exc))
check("...the file is left exactly as it was", W.PROCESSED_CREATORS_PATH.read_text() == '{"processed_channel_ids": ["UC1", ')
try:
    W._processed_state()
except W.StateFileError:
    pass
check("...and one copy is saved next to it (not one per read)", len(list((tmp / "state").glob("processed.json.damaged-*"))) == 1)
W.PENDING_OUTREACH_PATH.write_text('{"not": "a list"}')
try:
    W._pending_records()
    check("Outbox file in the wrong format -> StateFileError", False)
except W.StateFileError:
    check("Outbox file in the wrong format -> StateFileError", W.PENDING_OUTREACH_PATH.read_text() == '{"not": "a list"}')
W.LEAD_QUEUE_PATH.write_text("")
try:
    W._lead_queue()
    check("Empty lead queue file -> StateFileError (atomic writes never leave one)", False)
except W.StateFileError:
    check("Empty lead queue file -> StateFileError (atomic writes never leave one)", True)
use_storage(tmp / "fresh")
check("Missing files on a first run are still created", W._processed_state()["records"] == [] and W._pending_records() == [])

# ---------- Worker: saved transcripts, clip page first, Drive only a backup ----------
from core.hook_selector import ClipResult  # noqa: E402
from core import outreach as O  # noqa: E402
use_storage(tmp / "worker")
W.init_storage_files()
calls = []
W.download_source_video = lambda url: calls.append("download") or str(tmp / "src.mp4")
W.camera_check = lambda path: CameraCheck(1.0)  # round 19: one check for camera and screen share
W.extract_audio_only = lambda src, dst: calls.append("audio") or dst
W.transcribe_audio = lambda audio, settings: calls.append("groq") or [Word("Hello there.", 1.0, 1.6, 1.0, 1.6)]
W._video_duration = lambda path: 1800.0
W.video_info_and_peaks = lambda url: calls.append("metadata") or ({}, [])
picked = [ClipResult(start_time=100 + i * 60, end_time=140 + i * 60, viral_title="t", caption="c", hashtags=[], virality_score=80) for i in range(3)]
email = O.build_outreach_email("Coach Dana", "Knee Pain Fixes", "fixing knee pain")
pick_args = {}
def good_pick(*a, **k):
    pick_args["peaks"] = a[5] if len(a) > 5 else k.get("peaks"); return picked, email, "3 Shorts"
def render(source, clips_, job_dir, settings, words_, layouts_out=None, rendered_out=None, graphics=None):
    paths = [job_dir / f"clip_{i:02d}.mp4" for i in range(1, len(clips_) + 1)]
    for path in paths:
        path.write_bytes(b"clip")
    return paths
W._render_selected_clips = render
W.clip_pages.configured = lambda: True
PAGE = "https://clips.navstudiolab.com/coach-dana-abc123"
page_state = {"fail": False}
def publish(*a, **k):
    if page_state["fail"]:
        raise RuntimeError("R2 down")
    return "coach-dana-abc123", PAGE
W.clip_pages.publish_page = publish
W.clip_pages.delete_page = lambda name: 0
drive = {"fail": True}
def upload(*a, **k):
    calls.append("drive")
    if drive["fail"]:
        raise RuntimeError("Drive down")
    return "https://drive.google.com/drive/folders/abc"
W.upload_clips_to_drive = upload
W.check_drive_access = lambda: calls.append("drive check")
settings = SimpleNamespace(temp_dir=tmp / "temp", raw_dir=tmp)
lead = {"channel_id": "UC1", "channel_title": "Coach Dana", "latest_video_title": "Knee Pain Fixes",
        "latest_video_url": "https://www.youtube.com/watch?v=abcdef12345"}
transcript_file = W._transcripts_dir() / "abcdef12345.json"

def hiccup(*a, **k):
    raise RuntimeError("Gemini hiccup")
W.pick_clips_with_agent = hiccup
try:
    W._process_lead(dict(lead), settings)
except RuntimeError:
    pass
check("A failed creator keeps its transcript", transcript_file.is_file() and calls.count("groq") == 1, str(calls))
calls.clear()
W.pick_clips_with_agent = good_pick
record = W._process_lead(dict(lead), settings)
check("The retry reuses it: no audio extraction, no Groq call", "groq" not in calls and "audio" not in calls, str(calls))
check("...and drops it once the pitch is made", not transcript_file.exists())
check("Clip page first; a Drive failure doesn't block the pitch",
      record["page_url"] == PAGE and W.clip_link(record) == PAGE and record["drive_link"] == "")
check("With pages set up, Drive isn't checked before the download", "drive check" not in calls, str(calls))

W._write_json_atomic(W.PENDING_OUTREACH_PATH, [])
calls.clear(); drive["fail"] = False; page_state["fail"] = True
record = W._process_lead(dict(lead, channel_id="UC2"), settings)
check("Page fails -> the Drive link is used", record["page_url"] is None and "drive.google.com" in W.clip_link(record))
drive["fail"] = True
try:
    W._process_lead(dict(lead, channel_id="UC3"), settings)
    check("Page and Drive both fail -> no pitch (retried later)", False)
except RuntimeError as exc:
    check("Page and Drive both fail -> no pitch (retried later)", "Drive down" in str(exc))
W.clip_pages.configured = lambda: False
drive["fail"] = False; calls.clear()
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [])
W._process_lead(dict(lead, channel_id="UC4"), settings)
check("No clip pages: Drive is checked first, before any download",
      calls[:2] == ["drive check", "download"], str(calls))

W.clip_pages.configured = lambda: True; page_state["fail"] = False
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [])
W.downloaded_video_info = lambda url: {"heatmap": [{"start_time": 60, "end_time": 70, "value": 1.0}] + [
    {"start_time": s, "end_time": s + 10, "value": 0.1} for s in range(0, 1800, 10) if s != 60]}
calls.clear()
W._process_lead(dict(lead, channel_id="UC5"), settings)
check("The heatmap comes from the download: no second YouTube request",
      "metadata" not in calls and pick_args["peaks"], f"{calls} {pick_args}")

W._write_json_atomic(W.PENDING_OUTREACH_PATH, [])
W.pick_clips_with_agent = hiccup
try:
    W._process_lead(dict(lead, channel_id="UC6"), settings)
except RuntimeError:
    pass
W.camera_check = lambda path: CameraCheck(0.05)  # round 19: one check for camera and screen share
try:
    W._process_lead(dict(lead, channel_id="UC6"), settings)
except W.NotAFit:
    pass
check("A creator skipped as not a fit drops its transcript", not transcript_file.exists())

# ---------- Pauses for outside services ----------
use_storage(tmp / "pauses")
W.init_storage_files()
creator = {"channel_id": "UC7", "channel_title": "Paused Creator", "latest_video_url": "https://www.youtube.com/watch?v=zzzzzz1"}
W._pause_streak.clear()
gemini = ServicePause("Gemini's request limit is used up", 1800, service="gemini", hint="It resets on its own.")
first = W._pause_for_service(dict(creator), gemini)
queue = W._lead_queue()
status = json.loads((W.STORAGE_DIR / "worker_status.json").read_text())
check("Pause: the creator goes back in the queue with no attempt used",
      len(queue) == 1 and queue[0]["channel_id"] == "UC7" and queue[0].get("attempts", 0) == 0, str(queue))
check("Pause: the app shows why and when it resumes",
      status["state"] == "quota_pause" and "trying again at about" in status["detail"] and "resets on its own" in status["detail"], str(status))
second = W._pause_for_service(dict(creator), gemini)
check("Pausing again doubles the wait (30 min -> 1 h)", (first, second) == (1800, 3600))
for _ in range(6):
    longest = W._pause_for_service(dict(creator), gemini)
check("...up to 4 hours", longest == W.MAX_SERVICE_PAUSE_SEC)

class StopLoop(BaseException):
    pass
use_storage(tmp / "loop")
W.init_storage_files()
W._pause_streak.clear()
real_prune_leftovers, real_sleep = W._prune_leftovers, time.sleep
W.load_settings = lambda: settings
W._prune_leftovers = lambda s: None
leads = [dict(creator, channel_id=f"UC{n}", channel_title=f"Creator {n}") for n in (8, 9)]
W._pop_fresh_lead = lambda: leads.pop(0) if leads else None
def forbidden(lead, settings):
    raise W.DownloadForbidden("yt-dlp could not download the input URL: HTTP Error 403: Forbidden")
W._process_lead = forbidden
sleeps = []
def fake_sleep(seconds):
    sleeps.append(seconds)
    if seconds >= 1800:
        raise StopLoop
W.time.sleep = fake_sleep
try:
    W.run_continuous_queue()
except StopLoop:
    pass
queue = {lead["channel_id"]: lead.get("attempts", 0) for lead in W._lead_queue()}
status = json.loads((W.STORAGE_DIR / "worker_status.json").read_text())
check("One 403: normal retry (attempt used); two in a row: 30-min pause",
      sleeps == [10, 1800] and queue == {"UC8": 1, "UC9": 0}, f"{sleeps} {queue}")
check("...and the pause says to update yt-dlp if it keeps happening", "pip install -U yt-dlp" in status["detail"], status["detail"])

leads[:] = [dict(creator, channel_id="UC10")]
def damaged(lead, settings):
    raise W.StateFileError("storage/pending.json can't be read")
W._process_lead = damaged
try:
    W.run_continuous_queue()
    check("A damaged state file stops the worker", False)
except W.StateFileError:
    check("A damaged state file stops the worker", True)
time.sleep = real_sleep

# ---------- Cleanup and logs ----------
use_storage(tmp / "cleanup")
old, recent = time.time() - 9 * 86400, time.time() - 2 * 86400
temp_dir = tmp / "cleanup_temp"; temp_dir.mkdir()
for name in ("sub_1_title_62.png", "sub_1.ass"):
    (temp_dir / name).write_bytes(b"x"); os.utime(temp_dir / name, (old, old))
W._transcripts_dir().mkdir(parents=True)
for name, when in (("oldvideo01.json", old), ("newvideo01.json", recent)):
    (W._transcripts_dir() / name).write_text("[]"); os.utime(W._transcripts_dir() / name, (when, when))
W.clip_pages.configured = lambda: False
(tmp / "cleanup_raw").mkdir()
real_prune_leftovers(SimpleNamespace(raw_dir=tmp / "cleanup_raw", temp_dir=temp_dir))
check("Startup cleanup removes old title images and subtitle files from temp", not any(temp_dir.iterdir()),
      str(list(temp_dir.iterdir())))
check("...and saved transcripts older than a week (a recent one stays)",
      sorted(p.name for p in W._transcripts_dir().iterdir()) == ["newvideo01.json"])

W.LOG_PATH = tmp / "logs" / "worker.log"; W.LOG_PATH.parent.mkdir()
console = W.LOG_PATH.parent / "worker_console.log"
console.write_bytes(b"x" * (W.CONSOLE_LOG_MAX_BYTES + 10))
started = []
real_status, real_popen = W.get_worker_status, W.subprocess.Popen
W.get_worker_status = lambda: {"running": False}
W.subprocess.Popen = lambda *a, **k: started.append(a)
W.start_worker()
W.subprocess.Popen, W.get_worker_status = real_popen, real_status
check("Console log over 1 MB is set aside on start (it grew forever)",
      started and (W.LOG_PATH.parent / "worker_console.old.log").stat().st_size > W.CONSOLE_LOG_MAX_BYTES
      and console.stat().st_size == 0)

# ---------- App: About button, small page/Drive buttons, trim panel ----------
from streamlit.testing.v1 import AppTest  # noqa: E402
use_storage(tmp / "app")
W.init_storage_files()
import core.pitch_files as PF  # noqa: E402
rid = "pitch17"
pitch = tmp / "app" / "pitches" / rid; pitch.mkdir(parents=True)
for suffix in ("mp4", "preview.mp4", "source.mp4"):
    (pitch / f"clip_01.{suffix}").write_bytes(b"\x00\x00\x00\x18ftypmp42")
(pitch / "pitch.json").write_text(json.dumps({"record_id": rid, "created_at": "2026-10-30T00:00:00+00:00", "clips": [{
    "index": 1, "file": "clip_01.mp4", "preview": "clip_01.preview.mp4", "source": "clip_01.source.mp4",
    "start": 100.0, "end": 140.0, "ai_start": 100.0, "ai_end": 140.0, "slice_start": 70.0, "slice_end": 170.0,
    "layout": "smart_crop", "clip": {"start_time": 30, "end_time": 70, "viral_title": "Hook", "caption": "c",
    "hashtags": [], "virality_score": 80}, "words": [["Hello", 30.0, 30.4]]}]}))
channel_id = "UCabcdefghijklmnopqrstuv"
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [{
    "record_id": rid, "channel_id": channel_id, "channel_title": "Coach Dana", "contact_email": "dana@example.com",
    "drive_link": "https://drive.google.com/drive/folders/abc", "page_name": "coach-dana-abc123", "page_url": PAGE,
    "subject": "3 Shorts", "full_email_body": "Hi Dana,\n\nBody", "status": "pending",
    "clips": [{"start": 100.0, "end": 140.0, "judge": 80, "layout": "smart_crop"}]}])
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
    import core.pitch_files as P, core.clip_pages as C, core.drive_uploader as DU, shutil, json
    P.PITCHES_DIR = W.STORAGE_DIR / "pitches"
    def fake_rerender(record_id, index, start, end, settings):
        d = P.pitch_dir(record_id)
        shutil.copy(d / "clip_01.mp4", d / "clip_01.trim.mp4"); shutil.copy(d / "clip_01.preview.mp4", d / "clip_01.trim.preview.mp4")
        m = json.loads((d / "pitch.json").read_text()); m["clips"][0]["trim"] = {{"start": start, "end": end}}
        (d / "pitch.json").write_text(json.dumps(m))
    P.rerender_trim = fake_rerender
    C.replace_clip = lambda *a, **k: None
    DU.replace_clip_in_drive = lambda *a, **k: None
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
at = AppTest.from_file(str(wrapper), default_timeout=120).run()
check("App runs", not at.exception, str([e.value for e in at.exception]))
buttons = {getattr(b.proto, "label", ""): getattr(b.proto, "url", "") for b in at.get("link_button")}
check("Big About page button, built from the channel id",
      buttons.get("📇 Open their About page") == f"https://www.youtube.com/channel/{channel_id}/about", str(buttons))
check("Their clip page and Drive backup are the small pair", buttons.get("🌐 Their clip page") == PAGE and "📁 Drive backup" in buttons)
check("Found email still shown", any("dana@example.com" in m.value for m in at.markdown))
open_key = f"trim_open_{rid}_1"
check("Trim panel starts closed", not at.session_state[open_key] if open_key in at.session_state else True)
end_input = next(n for n in at.number_input if n.key == f"trim_end_{rid}_1")
end_input.set_value(1.5).run()
next(b for b in at.button if b.key == f"trim_go_{rid}_1").click().run()
check("Re-render: the new cut waits, inside the trim panel",
      any("New cut, not used yet" in m.value for m in at.markdown) and any(b.key == f"trim_use_{rid}_1" for b in at.button))
check("Re-render: the inputs keep your numbers", next(n for n in at.number_input if n.key == f"trim_end_{rid}_1").value == 1.5)
next(b for b in at.button if b.key == f"trim_use_{rid}_1").click().run()
check("Use this cut: a toast (floats, so nothing moves) and the inputs reset",
      any("link in the email stays the same" in toast.value for toast in at.toast)
      and next(n for n in at.number_input if n.key == f"trim_end_{rid}_1").value == 0.0)
check("No success banner pushed in above the clips", not any("stays the same" in s.value for s in at.success))
at2 = AppTest.from_file(str(wrapper), default_timeout=120)
PF_dir = tmp / "app" / "pitches" / rid
(PF_dir / "clip_01.trim.mp4").write_bytes(b"x"); (PF_dir / "clip_01.trim.preview.mp4").write_bytes(b"x")
m = json.loads((PF_dir / "pitch.json").read_text()); m["clips"][0]["trim"] = {"start": 99.0, "end": 141.0}
(PF_dir / "pitch.json").write_text(json.dumps(m))
at2.run()
check("A new cut waiting when the app opens: its trim panel opens by itself", at2.session_state[open_key] is True)

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
