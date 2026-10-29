"""Checks for round 11: two-speaker split view, title never over a face, quality
gate, pass reasons, no silent switch to local transcription, FFmpeg time limits.
Run all tests: .venv/bin/python tests/run_all.py"""
import json, os, subprocess, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["OPENAI_API_KEY"] = "test"; os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("ALLOW_LOCAL_WHISPER", None)

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

import core.agent_brain as A
import core.face_tracker as F
import core.pipeline as P
import core.subtitle_engine as S
import core.title_card as T
import core.transcriber as TR
import core.video_renderer as V
from core.face_tracker import CropPlan, Shot
from core.transcriber import Word

tmp = Path(tempfile.mkdtemp(prefix="r11-"))
A.load_dotenv = lambda *a, **k: None

# ---------- Two-speaker split view: geometry ----------
# A remote call: two webcams side by side, faces at x=389 and x=1634 (1920x1080).
pair = ((389.0, 523.0, 265.0), (1634.0, 420.0, 180.0))
plan = CropPlan(1920, 1080, 606, 1078, 1, (Shot(10, 50, 1312, True, True, pair),))
view = plan.split_view(10, 50)
check("Two people side by side -> split view for the whole shot", view is not None and view.spans == ((0.0, 40.0),), str(view and view.spans))
check("Panel is 9:8 (fills a 1080x960 half)", abs(view.width / view.height - 1080 / 960) < 0.01, f"{view.width}x{view.height}")
mid = (389 + 1634) / 2
(_s, _e, top_x, top_y, bottom_x, bottom_y) = view.windows[0]
check("Top panel stays left of the midpoint (never shows the other webcam)", top_x + view.width <= mid + 1, f"{top_x}+{view.width} vs {mid}")
check("Bottom panel stays right of the midpoint", bottom_x >= mid - 1 and bottom_x + view.width <= 1920, f"{bottom_x}")
check("Windows stay inside the frame", 0 <= top_y and top_y + view.height <= 1080 and 0 <= bottom_y and bottom_y + view.height <= 1080)
check("Split shots aren't also shown full-frame", plan.full_frame_ranges(10, 50) == [])
check("Two-person clip -> face crop layout (with the split), not the blurred strip",
      P.choose_layout("auto", plan) == "smart_crop" and plan.unsplit_group_coverage == 0)

wide = ((394.0, 302.0, 108.0), (1531.0, 343.0, 102.0))  # studio wide shot: small faces
multicam = CropPlan(1920, 1080, 606, 1078, 1, (
    Shot(0, 7, 478, True, False), Shot(7, 16, 1234, True, True, wide), Shot(16, 22, 456, True, False),
))
mview = multicam.split_view(0, 22)
check("Multicam: only the wide two-shot is split", mview is not None and mview.spans == ((7.0, 16.0),), str(mview and mview.spans))
check("Panel sized to the faces (head and shoulders), never upscaled more than 2x",
      F.PANEL_OUTPUT[0] / F.MAX_PANEL_UPSCALE <= mview.width <= 108 * F.PANEL_FACE_WIDTHS + 2, str(mview.width))
check("FFmpeg expression switches panels per shot", "lt(t," not in mview.top_x and mview.top_x.isdigit())
two_shots = CropPlan(1920, 1080, 606, 1078, 1, (
    Shot(0, 10, 0, True, True, wide), Shot(10, 20, 0, True, True, ((300.0, 300.0, 100.0), (1500.0, 320.0, 100.0))),
))
check("Two split shots with different positions -> piecewise expression",
      two_shots.split_view(0, 20).top_x.startswith("if(lt(t,10.000)"), two_shots.split_view(0, 20).top_x)
close = CropPlan(1280, 720, 404, 718, 1, (Shot(0, 10, 0, True, True, ((80.0, 300.0, 60.0), (760.0, 300.0, 60.0))),))
check("No room for a sharp panel on one side -> no split (whole frame instead)",
      close.split_view(0, 10) is None and close.full_frame_ranges(0, 10) == [(0.0, 10.0)])
mixed = CropPlan(1920, 1080, 606, 1078, 1, (
    Shot(0, 10, 0, True, True, wide), Shot(10, 20, 0, True, True, ((100.0, 300.0, 60.0), (700.0, 300.0, 60.0))),
))
check("One unsuitable two-person shot doesn't cancel the split for the others",
      mixed.split_view(0, 20).spans == ((0.0, 10.0),) and mixed.full_frame_ranges(0, 20) == [(10.0, 20.0)])
check("A group shot without a pair still counts against the crop",
      P.choose_layout("auto", CropPlan(1920, 1080, 606, 1078, 1, (Shot(0, 30, 600, True, True),))) == "blurred_background")
clusters2 = [[(400.0, 1.0, 300.0, 100.0)] * 8, [(1500.0, 1.0, 320.0, 90.0)] * 7]
check("Speaker pair: two steady faces -> (left, right)", F._speaker_pair(clusters2, 10) == ((400.0, 300.0, 100.0), (1500.0, 320.0, 90.0)))
check("Three steady faces -> no pair", F._speaker_pair(clusters2 + [[(900.0, 1.0, 300.0, 90.0)] * 6], 10) is None)
check("A face seen only briefly doesn't count", F._speaker_pair([clusters2[0], [(1500.0, 1.0, 1.0, 1.0)] * 2], 10) is None)
check("Older shots without the new field still work", Shot(0, 30, 600, True, False).speakers is None)

# ---------- Renderer: the split view and the time limit ----------
source = tmp / "two_up.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=3",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest", "-pix_fmt", "yuv420p", str(source)], check=True)
ass = S.create_ass_subtitles([Word("hello", 0.1, 0.5), Word("there.", 0.6, 1.2)], tmp / "s.ass", clip_duration=3,
                             seam_spans=[(0.0, 3.0)])
render_plan = CropPlan(1920, 1080, 606, 1080, 0, (Shot(0, 1.5, 600, True, False), Shot(1.5, 3, 600, True, True, pair)))
out = V.render_clip(source, ass, render_plan.crop, 0, 3, tmp / "split.mp4", layout_mode="smart_crop",
                    crop_x_expression=render_plan.x_expression(0, 3), split_view=render_plan.split_view(0, 3),
                    full_frame_ranges=[(0.0, 0.7)])
probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
check("Split view + full-frame span render to 1080x1920", out.is_file() and probe == "1080,1920", probe)
whole_plan = CropPlan(1920, 1080, 606, 1080, 0, (Shot(0, 3, 600, True, True, pair),))
out2 = V.render_clip(source, ass, whole_plan.crop, 0, 3, tmp / "split_whole.mp4", layout_mode="smart_crop",
                     split_view=whole_plan.split_view(0, 3))
probe2 = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", str(out2)], capture_output=True, text=True).stdout.strip()
check("Whole clip as split view renders too (single-face crop skipped)", probe2 == "1080,1920", probe2)
real_run = subprocess.run
def slow(*args, **kwargs):
    raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))
V.subprocess.run = slow
try:
    V.render_clip(source, ass, render_plan.crop, 0, 3, tmp / "hang.mp4", layout_mode="smart_crop")
    check("A stuck FFmpeg is stopped and reported", False)
except RuntimeError as exc:
    check("A stuck FFmpeg is stopped and reported", "longer than" in str(exc), str(exc))
finally:
    V.subprocess.run = real_run
check("Render calls have a time limit", V.RENDER_TIMEOUT_SEC <= 15 * 60)

# ---------- Captions ----------
text = ass.read_text()
check("Captions in the split view sit on the seam", r"{\an5\pos(540,960)}" in text)
ass2 = S.create_ass_subtitles([Word("hello", 0.1, 0.5), Word("later.", 5.0, 5.5)], tmp / "s2.ass", clip_duration=8,
                              seam_spans=[(0.0, 8.0)], seam_title=(4.0, 1050))
lines = [l for l in ass2.read_text().splitlines() if l.startswith("Dialogue")]
check("While the title is on the seam, captions sit just under it", r"\pos(540,1064)" in lines[0] and r"\pos(540,960)" in lines[-1], lines[0][-60:])
plain = S.create_ass_subtitles([Word("hello", 0.1, 0.5)], tmp / "s3.ass", clip_duration=2).read_text()
check("No split view -> captions unchanged", r"\pos" not in plain)
long_words = [Word(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate("WWWWW MMMMM WWWWW MMMMM".split())]
groups = S._make_groups(long_words)
check("Caption chunks stay on one line (21 characters max)",
      all(len(" ".join(w.text for w in g)) <= S.MAX_CHARACTERS_PER_CHUNK for g in groups) and S.MAX_CHARACTERS_PER_CHUNK == 21)

# ---------- Title card: never over a face ----------
face_fills_top = [[(150.0, 1100.0)]] * 12
top, covered = T._best_slot(163, face_fills_top)
check("Close-up: title goes under the captions instead of over the face", covered == 0 and top >= T.CAPTION_ZONE_BOTTOM + T.GAP - 1, f"top={top}")
check("…and stays above the app's bottom bar", top + 163 <= T.LOWEST_TITLE_BOTTOM)
top, covered = T._best_slot(163, [[(150.0, 700.0)]] * 12)
check("Small face up top: title goes between the face and the captions", covered == 0 and 700 <= top and top + 163 <= T.CAPTION_ZONE_TOP)
top, covered = T._best_slot(163, [[]] * 12, [T.SEAM_CAPTION_ZONE])
check("Blocked zones are respected", top == T.SAFE_TOP)
overlay = T.prepare_title_overlay("Why Founders Should Hire Slower", 4.0, layout="smart_crop", plan=plan,
                                  source_video=source, clip_start=10, clip_duration=40, work_dir=tmp, name="seam")
check("Split view: title on the seam between the two speakers",
      overlay is not None and overlay.placement == T.SEAM_PLACEMENT and overlay.card_top + overlay.card_height // 2 == T.SEAM_Y,
      overlay and overlay.placement)
check("Pipeline moves captions under a seam title", P._seam_title(overlay) == (4.0, overlay.card_top + overlay.card_height))

# ---------- Quality gate ----------
words, t = [], 0.0
for i in range(300):
    for j in range(10):
        words.append(Word(f"w{i}{'.' if j == 9 else ''}", t, t + 0.3)); t += 0.32
    t += 0.5
picks = [{"start_time": s, "end_time": s + 40, "hook_title": f"c{k}", "reason": "r", "point": "a clear point about the thing"}
         for k, s in enumerate((100, 300, 500, 700, 900))]
A._request_openai = lambda key, prompt, system_prompt=A._SYSTEM_PROMPT: json.dumps({"clips": picks, "short_topic": "growth", "format": "video"})
def judge_with(scores):
    P.judge_clips = lambda texts: scores[: len(texts)] + [0] * (len(texts) - len(scores))
judge_with([90, 85, 40, 30, 20, 10, 10, 10])
report = {}
clips, email, subject = P.pick_clips_with_agent(words, t, 3, "Chan", "Title", min_judge_score=60, report=report)
check("Weak third clip dropped: 2 clips sent", len(clips) == 2 and report["judge_scores"] == [90, 85], str(report))
check("Email and subject say two", "that moment and one other" in email and subject.startswith("2 Shorts"), subject)
judge_with([90, 40, 30, 20, 10, 10, 10, 10])
try:
    P.pick_clips_with_agent(words, t, 3, "Chan", "Title", min_judge_score=60)
    check("Only one good clip -> lead skipped (WeakMoments)", False)
except P.WeakMoments as exc:
    check("Only one good clip -> lead skipped (WeakMoments)", "only 1 clip" in str(exc), str(exc))
clips, _e, _s = P.pick_clips_with_agent(words, t, 3, "Chan", "Title")
check("App (no gate): still 3 clips", len(clips) == 3)
P.judge_clips = lambda texts: None
clips, _e, _s = P.pick_clips_with_agent(words, t, 3, "Chan", "Title", min_judge_score=60)
check("No judge answer -> nothing dropped", len(clips) == 3)

# ---------- Worker: gate, record data, pass reasons ----------
import autonomous_worker as W
W.STORAGE_DIR = tmp
for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                   ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                   ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
    setattr(W, name, tmp / file)
from types import SimpleNamespace
W.check_drive_access = lambda: None
W.download_source_video = lambda url: str(tmp / "src.mp4")
(tmp / "src.mp4").write_bytes(b"x")
W.on_camera_share = lambda path: 0.9
W.extract_audio_only = lambda src, dst: dst
W.transcribe_audio = lambda audio, settings: words
W._video_duration = lambda path: 1800.0
W.video_info_and_peaks = lambda url: ({}, [])
W.upload_clips_to_drive = lambda *a, **k: "https://drive.google.com/drive/folders/x"
lead = {"channel_id": "UC1", "channel_title": "Coach Dana", "latest_video_title": "T",
        "latest_video_url": "https://www.youtube.com/watch?v=abc", "subscriber_count": 42000}
state = W._processed_state(); state["records"].append({"channel_id": "UC1", "status": "processing"}); W._write_json_atomic(W.PROCESSED_CREATORS_PATH, state)
settings = SimpleNamespace(temp_dir=tmp / "temp", raw_dir=tmp)
def fake_pick(*a, **k):
    k["report"]["judge_scores"] = [88, 72]
    two = [P.ClipResult(start_time=100, end_time=140, viral_title="a", caption="", hashtags=[], virality_score=90),
           P.ClipResult(start_time=300, end_time=340, viral_title="b", caption="", hashtags=[], virality_score=80)]
    assert k["min_judge_score"] == W.MIN_CLIP_SCORE
    return two, O_email, "2 Shorts from your growth video"
from core import outreach as O
O_email = O.build_outreach_email("Coach Dana", clip_count=2)
W.pick_clips_with_agent = fake_pick
W.render_selected_clip = lambda src, clip, words, settings, path, **k: SimpleNamespace(path=path, layout_mode="smart_crop")
record = W._process_lead(lead, settings)
check("Worker sends 2 clips when only 2 pass", len(record["clips"]) == 2)
check("Record keeps judge scores, layout, subscribers and length",
      record["clips"][0] == {"start": 100, "end": 140, "judge": 88, "layout": "smart_crop"}
      and record["subscriber_count"] == 42000 and record["video_seconds"] == 1800, str(record["clips"]))
def weak(*a, **k):
    raise P.WeakMoments("only 1 clip(s) scored 60+ (scores: 90, 40, 30)")
W.pick_clips_with_agent = weak
try:
    W._process_lead({**lead, "channel_id": "UC2"}, settings)
    check("Weak lead -> skipped as not a fit (never reaches your Outbox)", False)
except W.NotAFit as exc:
    W._skip_not_a_fit({**lead, "channel_id": "UC1"}, exc)
    check("Weak lead -> skipped as not a fit (never reaches your Outbox)", str(exc).startswith("weak moments"))
counts = W.pass_summary()
check("Auto-skips are counted", counts.get("auto_weak") == 1, str(counts))
try:
    W.skip_lead(record["record_id"], pass_reason="nope")
    check("Unknown pass reason is refused", False)
except ValueError:
    check("Unknown pass reason is refused", True)
check("Pass with a reason", W.skip_lead(record["record_id"], pass_reason="weak_moments"))
skipped = json.loads(W.SKIPPED_LEADS_PATH.read_text())
check("Reason saved on the passed pitch", skipped[-1]["pass_reason"] == "weak_moments" and skipped[-1]["skip_reason"] == "user_skipped")
check("Reasons are counted for the app", W.pass_summary().get("weak_moments") == 1)
W.restore_passed_as_sent(record["record_id"])
check("'I sent this one' clears the pass reason", "pass_reason" not in json.loads(W.PENDING_OUTREACH_PATH.read_text())[-1])

# ---------- Groq: wait for the limit, never switch to local transcription ----------
class RateLimit(Exception):
    status_code = 429
    def __init__(self, message, headers=None):
        super().__init__(message)
        self.response = SimpleNamespace(headers=headers or {})
check("Wait time from Groq's message", TR._groq_wait_seconds(RateLimit("Please try again in 1h2m3.5s.")) == 3723.5)
check("Wait time from the retry-after header", TR._groq_wait_seconds(RateLimit("x", {"retry-after": "90"})) == 90)
check("No hint -> wait an hour", TR._groq_wait_seconds(RateLimit("x")) == TR.GROQ_LIMIT_DEFAULT_WAIT_SEC)
audio = tmp / "a.wav"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1", str(audio)], check=True)
TR._groq_api_key = lambda: "key"
local_calls = []
TR._get_whisper_model = lambda *a: local_calls.append(1) or (_ for _ in ()).throw(RuntimeError("local model"))
def limit(*a, **k):
    raise TR.TranscriptionLimitReached("Groq's transcription limit is used up", 600)
TR._transcribe_with_groq = limit
settings2 = SimpleNamespace(whisper_language="en", whisper_model="small", whisper_device="cpu", whisper_compute_type="int8")
try:
    TR.transcribe_audio(audio, settings2)
    check("Groq limit -> stops (no local fallback)", False)
except TR.TranscriptionLimitReached as exc:
    check("Groq limit -> stops (no local fallback)", exc.retry_after == 600 and not local_calls)
TR._transcribe_with_groq = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("network down"))
try:
    TR.transcribe_audio(audio, settings2)
    check("Other Groq error -> clear error, still no local fallback", False)
except RuntimeError as exc:
    check("Other Groq error -> clear error, still no local fallback", "Groq transcription failed" in str(exc) and not local_calls, str(exc))
os.environ["ALLOW_LOCAL_WHISPER"] = "1"
TR.load_dotenv = lambda *a, **k: None
try:
    TR.transcribe_audio(audio, settings2)
except RuntimeError:
    pass
check("ALLOW_LOCAL_WHISPER=1 brings the old fallback back", local_calls == [1])
os.environ.pop("ALLOW_LOCAL_WHISPER")
check("Pause text says when it continues", "continues at about" in W._groq_pause_text(TR.TranscriptionLimitReached("x", 600)))
source_code = (ROOT / "autonomous_worker.py").read_text()
check("Both worker loops wait on the Groq limit", source_code.count("except TranscriptionLimitReached") == 2)

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
