"""Checks for round 20: ⏩ Force to queue (Pipeline tab) and 🎞️ Series mode (Make clips).
Temp folders and fakes only: nothing is downloaded, transcribed, uploaded or sent.
Run all tests: .venv/bin/python tests/run_all.py"""
import json, os, random, sys, tempfile, textwrap
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["NAV_NO_CLIP_PAGES"] = "1"

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

tmp = Path(tempfile.mkdtemp())
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

# =========================== Feature 1: ⏩ Force to queue ===========================
use_storage(tmp / "force")
W.get_worker_status = lambda: {"running": False}
lookups = []
W.fetch_video_info = lambda url: lookups.append(url) or {"title": "Why Most Startups Fail"}
state = W._processed_state()
state["processed_channel_ids"] += ["UCweak", "UCfail", "UCpassed", "UCsent", "UCoutbox", "UCbusy"]
state["processed_video_ids"] += ["vidweak0001", "vidfail0001", "vidpass0001", "vidsent0001"]
state["records"] += [
    # An older record (before round 20): no saved lead, so the title is looked up.
    {"channel_id": "UCweak", "video_id": "vidweak0001", "channel_title": "Weak Moments Pod", "status": "not a fit",
     "error": "weak moments: only 1 clip(s) scored 60+", "priority": 4, "duration_seconds": 1800,
     "shorts": {"status": "none"}, "updated_at": "2026-10-30T10:00:00+00:00"},
    {"channel_id": "UCfail", "video_id": "vidfail0001", "channel_title": "Failed Pod", "status": "failed",
     "error": "HTTP Error 403: Forbidden", "updated_at": "2026-10-30T11:00:00+00:00",
     "lead": {"channel_id": "UCfail", "channel_title": "Failed Pod", "latest_video_url": "https://www.youtube.com/watch?v=vidfail0001",
              "latest_video_title": "Saved Title", "contact_email": "host@failedpod.com", "subscriber_count": 52000, "priority": 2}},
    {"channel_id": "UCpassed", "video_id": "vidpass0001", "channel_title": "Passed Pod", "status": "skipped",
     "skip_reason": "user_skipped", "pass_reason": "bad_framing", "updated_at": "2026-10-30T09:00:00+00:00"},
    {"channel_id": "UCsent", "video_id": "vidsent0001", "channel_title": "Sent Pod", "status": "sent"},
    {"channel_id": "UCoutbox", "video_id": "vidout00001", "channel_title": "Outbox Pod", "status": "completed"},
    {"channel_id": "UCbusy", "video_id": "vidbusy0001", "channel_title": "Busy Pod", "status": "processing"},
]
W._write_json_atomic(W.PROCESSED_CREATORS_PATH, state)
W._write_json_atomic(W.SKIPPED_LEADS_PATH, [
    {"record_id": "r-pass", "channel_id": "UCpassed", "channel_title": "Passed Pod", "status": "skipped", "pass_reason": "bad_framing",
     "video_title": "Pitch Video", "target_video_id": "vidpass0001", "contact_email": "https://www.youtube.com/channel/UCpassed/about",
     "subscriber_count": 9100, "skipped_at": "2026-10-30T09:00:00+00:00"},
    {"channel_id": "UCqskip", "channel_title": "Queue Skip Pod", "status": "skipped", "skip_reason": "user_skipped_before_processing",
     "latest_video_url": "https://www.youtube.com/watch?v=vidqskp0001", "latest_video_title": "Queue Video",
     "contact_email": "hi@qskip.com", "priority": 1, "skipped_at": "2026-10-29T09:00:00+00:00"},
])
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [{"record_id": "o1", "channel_id": "UCoutbox", "status": "pending"}])
W._write_json_atomic(W.LEAD_QUEUE_PATH, [{"channel_id": "UCq", "channel_title": "Queued", "priority": 9,
                                          "latest_video_url": "https://www.youtube.com/watch?v=vidqueue001"}])

rows = {row["channel_id"]: row for row in W.get_forceable_leads()}
check("Listed: didn't finish, skipped by the worker, passed or skipped by you",
      {k: rows[k]["group"] for k in rows} == {"UCweak": "worker", "UCfail": "unfinished", "UCpassed": "you", "UCqskip": "you", "UCbusy": "unfinished"},
      str({k: rows[k]["group"] for k in rows}))
check("...each with why", rows["UCweak"]["reason"].startswith("weak moments") and rows["UCpassed"]["reason"] == W.PASS_REASONS["bad_framing"]
      and rows["UCfail"]["reason"].startswith("HTTP Error 403"))
check("Not listed: sent, in the Outbox, already queued", not {"UCsent", "UCoutbox", "UCq"} & set(rows))
W.get_worker_status = lambda: {"running": True, "state": "processing", "detail": "Busy Pod"}
check("Not listed: the creator the worker is making clips for right now", "UCbusy" not in {r["channel_id"] for r in W.get_forceable_leads()})
W.get_worker_status = lambda: {"running": False}

lead = W.force_lead_to_queue("UCweak")
queue = W._lead_queue()
state = W._processed_state()
record = next(r for r in reversed(state["records"]) if r["channel_id"] == "UCweak")
check("Forced: at the top of the queue, marked forced, no attempts used",
      queue[0]["channel_id"] == "UCweak" and queue[0]["forced"] is True and queue[0]["attempts"] == 0)
check("...rebuilt from an older record: video link, title looked up once, channel link",
      lead["latest_video_url"] == "https://www.youtube.com/watch?v=vidweak0001" and lead["latest_video_title"] == "Why Most Startups Fail"
      and lead["channel_url"] == "https://www.youtube.com/channel/UCweak" and lead["priority"] == 4 and len(lookups) == 1)
check("...no longer marked as done, so the worker takes them",
      "UCweak" not in state["processed_channel_ids"] and "vidweak0001" not in state["processed_video_ids"])
check("...old status and error cleared (kept under forced_from)",
      record["status"] == W.FORCED_STATUS and "error" not in record and record["forced_from"]["status"] == "not a fit")

lead = W.force_lead_to_queue("UCfail")
check("A record with its saved lead: everything comes back, no lookup",
      lead["latest_video_title"] == "Saved Title" and lead["contact_email"] == "host@failedpod.com"
      and lead["subscriber_count"] == 52000 and len(lookups) == 1)
lead = W.force_lead_to_queue("UCpassed")
skipped = W._skipped_records()
check("A pitch you passed: rebuilt from the passed pitch (the About link isn't kept as an email)",
      lead["latest_video_url"].endswith("vidpass0001") and lead["latest_video_title"] == "Pitch Video" and "contact_email" not in lead)
check("...and it leaves 'Passed recently'", not any(r["channel_id"] == "UCpassed" for r in skipped)
      and "pass_reason" not in next(r for r in reversed(W._processed_state()["records"]) if r["channel_id"] == "UCpassed"))
lead = W.force_lead_to_queue("UCqskip")
check("One you skipped from the queue: back with its email and video", lead["contact_email"] == "hi@qskip.com"
      and lead["latest_video_title"] == "Queue Video")
for blocked, why in (("UCsent", "already pitched"), ("UCoutbox", "Outbox"), ("UCnobody", "no saved details")):
    try:
        W.force_lead_to_queue(blocked)
        check(f"Refused: {why}", False)
    except ValueError:
        check(f"Refused: {why}", True)
W.force_lead_to_queue("UCq")
check("Already queued: just moved to the top and marked forced",
      W._lead_queue()[0]["channel_id"] == "UCq" and W._lead_queue()[0]["forced"] and sum(l["channel_id"] == "UCq" for l in W._lead_queue()) == 1)
check("Forced creators no longer appear in the skipped list", not {"UCweak", "UCfail", "UCpassed", "UCqskip"} & {r["channel_id"] for r in W.get_forceable_leads()})

# The worker takes forced (and "Do next") creators first, whatever their score.
use_storage(tmp / "order")
W._write_json_atomic(W.LEAD_QUEUE_PATH, [
    {"channel_id": "UChigh", "channel_title": "High", "priority": 9, "latest_video_url": "https://www.youtube.com/watch?v=vidhigh0001"},
    {"channel_id": "UCforced", "channel_title": "Forced", "priority": 0, "forced": True, "latest_video_url": "https://www.youtube.com/watch?v=vidforc0001"},
    {"channel_id": "UClow", "channel_title": "Low", "priority": 1, "latest_video_url": "https://www.youtube.com/watch?v=vidlow00001"},
])
check("Forced goes first even with a low score", W._pop_fresh_lead()["channel_id"] == "UCforced")
W.prioritize_queued_lead("UClow")
check("'Do next' now really goes next (it only moved the row before)", W._pop_fresh_lead()["channel_id"] == "UClow")
saved = next(r for r in W._processed_state()["records"] if r["channel_id"] == "UCforced")
check("Starting a lead saves it on its record (so it can be forced again later)",
      saved["lead"]["latest_video_url"].endswith("vidforc0001") and saved["forced"] is True)

# A forced creator skips the automatic checks and the score bar, but every clip is still scored.
use_storage(tmp / "process")
calls = {}
W.download_source_video = lambda url: str(tmp / "src.mp4")
(tmp / "src.mp4").write_bytes(b"x")
W.camera_check = lambda path: calls.setdefault("camera", True) and CameraCheck(0.0)
W.load_cached_transcript = lambda video_id: [Word("Hi.", 1.0, 1.5)]
W._video_duration = lambda path: 1800.0
W.downloaded_video_info = lambda url: {"heatmap": None}
from core.hook_selector import ClipResult  # noqa: E402
from core import outreach as O  # noqa: E402
picked = [ClipResult(start_time=100 + 60 * i, end_time=140 + 60 * i, viral_title="t", caption="c", hashtags=[], virality_score=50) for i in range(3)]
def pick(*a, **k):
    calls["min_score"] = k["min_judge_score"]
    k["report"]["judge_scores"] = [41, 35, 52]
    return picked, O.build_outreach_email("Weak Moments Pod", drive_link=""), "3 shorts from your recent video"
W.pick_clips_with_agent = pick
W._render_selected_clips = lambda source, clips_, job_dir, *a, **k: [job_dir / f"clip_{i}.mp4" for i in (1, 2, 3)]
W.clip_pages.configured = lambda: False
W.check_drive_access = lambda: None
W.upload_clips_to_drive = lambda *a, **k: "https://drive.google.com/drive/folders/x"
record = W._process_lead({"channel_id": "UCweak", "channel_title": "Weak Moments Pod", "latest_video_title": "T",
                          "latest_video_url": "https://www.youtube.com/watch?v=vidweak0001", "forced": True}, SimpleNamespace(temp_dir=tmp / "t", raw_dir=tmp))
check("Forced: no camera / screen-share check", "camera" not in calls)
check("Forced: clips kept whatever their AI score (still scored)", calls["min_score"] == W.FORCED_MIN_CLIP_SCORE == 0
      and [c["judge"] for c in record["clips"]] == [41, 35, 52])
check("Forced: the pitch is marked (the Outbox shows '⏩ forced through')", record["forced"] is True)
(tmp / "src.mp4").write_bytes(b"x")
calls.clear()
try:
    W._process_lead({"channel_id": "UCnormal", "channel_title": "Normal", "latest_video_title": "T",
                     "latest_video_url": "https://www.youtube.com/watch?v=vidnorm0001"}, SimpleNamespace(temp_dir=tmp / "t", raw_dir=tmp))
    check("Not forced: the checks still apply", False)
except W.NotAFit:
    check("Not forced: the checks still apply", calls.get("camera") is True)

# =========================== Feature 2: 🎞️ Series mode ===========================
from core import series as S  # noqa: E402

def talk(seconds, seed=4, punctuated=True):
    random.seed(seed)
    words, t = [], 1.0
    while t < seconds - 2:
        n = random.randint(4, 30)
        for j in range(n):
            d = random.uniform(0.18, 0.45)
            text = f"w{len(words)}" + ("." if punctuated and j == n - 1 else "")
            words.append(Word(text, t, t + d)); t += d + random.uniform(0.02, 0.12)
        t += random.choice([0.2, 0.3, 0.5, 0.9, 2.5])
    return words, t + 2

words, duration = talk(1200)
parts = S.plan_parts(words, duration, 60, 90)
check("Plan: every part 60–90 s (only the last can be shorter)", all(60 <= b - a <= 90 for a, b in parts[:-1]) and parts[-1][1] - parts[-1][0] <= 90,
      str([round(b - a) for a, b in parts]))
inside = [sum(1 for a, b in parts if a <= w.start and w.end <= b) for w in words]
check("Plan: every word is in exactly one part (nothing said is lost or repeated)", all(c == 1 for c in inside))
check("Plan: parts follow each other (only silence over 0.8 s is trimmed between them)",
      all(0 <= parts[i + 1][0] - parts[i][1] <= 2.5 for i in range(len(parts) - 1)) and parts[0][0] == 0 and parts[-1][1] == round(duration, 3))
endings = [max((w for w in words if w.end <= b + 1e-3), key=lambda w: w.end).text for _a, b in parts[:-1]]
check("Plan: each part ends on a finished sentence", all(text.endswith(".") for text in endings))
check("Plan: the same transcript always gives the same parts (batches line up)", S.plan_parts(words, duration, 60, 90) == parts)
long_parts = S.plan_parts(words, duration, 120, 180)
check("Plan: longer parts (120–180 s) work too", all(120 <= b - a <= 180 for a, b in long_parts[:-1]))
bare, bare_duration = talk(600, punctuated=False)
bare_parts = S.plan_parts(bare, bare_duration, 60, 90)
check("No punctuation: cuts in the longest pause instead, never inside a word",
      all(60 <= b - a <= 90 for a, b in bare_parts[:-1]) and all(not (w.start < b < w.end) for _a, b in bare_parts for w in bare))
check("No speech at all (music): plain 90 s parts", S.plan_parts([], 400.0, 60, 90) == [(0.0, 90.0), (90.0, 180.0), (180.0, 270.0), (270.0, 360.0), (360.0, 400.0)])
check("A tiny last bit joins the part before", S.plan_parts([], 190.0, 60, 90) == [(0.0, 90.0), (90.0, 190.0)])

check("Batches: 1 = Parts 1–3, 2 = Parts 4–6", [n for n, *_r in S.batch_parts(parts, 1)] == [1, 2, 3]
      and [n for n, *_r in S.batch_parts(parts, 2)] == [4, 5, 6] and S.batch_parts(parts, 2)[0][1] == parts[3][0])
try:
    S.batch_parts(parts, S.batch_count(parts) + 1)
    check("A batch past the end is refused", False)
except ValueError:
    check("A batch past the end is refused", True)
check("Title card text: 'Part N: title', shortened",
      S.part_title(4, "The British Are Coming | History Podcast Ep 12 (Full Episode)") == "Part 4: The British Are Coming"
      and len(S.clean_series_title("word " * 30)) <= S.MAX_TITLE_CHARS and S.part_title(2, "") == "Part 2")
check("Series key: YouTube id, or a fingerprint of an uploaded file",
      S.series_key(url="https://www.youtube.com/watch?v=abcdefghijk") == "yt-abcdefghijk"
      and S.series_key(file_name="a.mp4", file_head=b"x" * 10, file_size=10) == S.series_key(file_name="a.mp4", file_head=b"x" * 10, file_size=10)
      and S.series_key(file_name="a.mp4", file_head=b"x", file_size=10).startswith("file-"))

S.SERIES_DIR = tmp / "series"
S.save_series("yt-abcdefghijk", {"words": S.words_to_rows(words), "duration": duration, "shortest": 60, "longest": 90, "made": [1, 2, 3]})
check("Next batch after Parts 1–3 is batch 2", S.next_batch(S.load_series("yt-abcdefghijk"), 60, 90) == 2)
check("...but with other part lengths it starts over at 1", S.next_batch(S.load_series("yt-abcdefghijk"), 90, 120) == 1)
check("The transcript is saved with the series (round trip)", S.rows_to_words(S.load_series("yt-abcdefghijk")["words"])[5] == words[5])

# ---------- The Make clips tab, series mode ----------
from streamlit.testing.v1 import AppTest  # noqa: E402
use_storage(tmp / "app")
log = tmp / "calls.log"
out = tmp / "Desktop"
talk_words, talk_seconds = talk(300, seed=7)
(tmp / "talk.json").write_text(json.dumps([[w.text, w.start, w.end] for w in talk_words]))
wrapper = tmp / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys, json; sys.path.insert(0, {str(ROOT)!r})
    from pathlib import Path
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(W.STORAGE_DIR)!r})
    for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, name, W.STORAGE_DIR / file)
    import core.series as S, core.downloader as D, core.transcriber as T, core.pipeline as P, core.pitch_files as PF
    import core.storage_manager as SM, main
    from core.transcriber import Word
    S.SERIES_DIR = Path({str(tmp / "app_series")!r})
    LOG = Path({str(log)!r})
    def note(text):
        with LOG.open("a") as f: f.write(text + "\\n")
    def audio(url, output_dir="", on_progress=None):
        note("audio"); Path(output_dir).write_bytes(b"wav"); return str(output_dir)
    D.download_audio_only = audio
    D.download_source_video = lambda url, output_path=None, on_progress=None: note("video") or {str(tmp / "src.mp4")!r}
    D.fetch_video_info = lambda url: {{"title": "The British Are Coming | Ep 12"}}
    main._audio_duration = lambda path: {talk_seconds!r}
    T.transcribe_audio = lambda path, settings, on_progress=None: note("groq") or [Word(*row) for row in json.load(open({str(tmp / "talk.json")!r}))]
    def render(source, clip, words, settings, output, **kwargs):
        note(f"render {{clip.viral_title}} {{clip.start_time:.1f}}-{{clip.end_time:.1f}}")
        Path(output).write_bytes(b"part " + clip.viral_title.encode())
    P.render_selected_clip = render
    PF.make_preview = lambda clip, output: Path(output).write_bytes(Path(clip).read_bytes())
    SM.render_output_dir = lambda: Path({str(out)!r})
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
at = AppTest.from_file(str(wrapper), default_timeout=180).run()
check("App runs", not at.exception, str([e.value for e in at.exception]))
check("Make clips has the two modes", [o for o in at.radio(key="clip_mode").options] == ["🎯 3 highlights", "🎞️ Series: the whole video in order"])
at.radio(key="clip_mode").set_value("🎞️ Series: the whole video in order").run()
at.text_input(key="series_url").set_value("https://www.youtube.com/watch?v=abcdefghijk").run()
check("Series controls: title, part length 60–90 s, batch 1", at.slider(key="series_lengths").value == (60, 90)
      and at.number_input(key="series_batch").value == 1 and any("Make parts 1–3" == b.label for b in at.button))
next(b for b in at.button if b.label == "Make parts 1–3").click().run()
check("Batch 1 runs without errors", not at.exception, str([e.value for e in at.exception]))
def part_no(line):
    return int(line.split("Part ", 1)[1].split(":", 1)[0])
renders = sorted((line for line in log.read_text().splitlines() if line.startswith("render")), key=part_no)  # 2 at a time, any order
check("Batch 1: Parts 1–3 with 'Part N: title' on the title card",
      [line.split(" ", 1)[1].rsplit(" ", 1)[0] for line in renders] == [f"Part {n}: The British Are Coming" for n in (1, 2, 3)], str(renders))
folder = next((out / "Series").iterdir())
check("Saved on this Mac (no Drive, no clip page)", sorted(p.name for p in folder.iterdir()) == ["part_01.mp4", "part_02.mp4", "part_03.mp4"])
downloads = [b.label for b in at.get("download_button")]
check("A Download button per part (round 21: no zip)", downloads == ["⬇ Download Part 1", "⬇ Download Part 2", "⬇ Download Part 3"], str(downloads))
check("After batch 1: the next batch and the found title are filled in",
      at.number_input(key="series_batch").value == 2 and at.text_input(key="series_title").value == "The British Are Coming")
next(b for b in at.button if b.label.startswith("Make parts 4")).click().run()
lines = log.read_text().splitlines()
check("Batch 2: Parts 4–5 (this 5-minute video has 5), numbered after batch 1",
      [line.split(" ", 2)[2].split(":")[0] for line in lines if line.startswith("render Part 4") or line.startswith("render Part 5")] and
      sum(line.startswith("render") for line in lines) == 5, str(lines))
check("Batch 2 didn't download the audio or call Groq again", lines.count("groq") == 1 and lines.count("audio") == 1, str(lines))
ordered = sorted((line for line in lines if line.startswith("render")), key=part_no)
starts = [float(line.rsplit(" ", 1)[1].split("-")[0]) for line in ordered]
ends = [float(line.rsplit(" ", 1)[1].split("-")[1]) for line in ordered]
check("Parts 1–5 follow on from each other across the two batches", all(abs(starts[i + 1] - ends[i]) <= 2.5 for i in range(4)), f"{starts} {ends}")

import app as A  # noqa: E402
check("Made-so-far text", A._part_ranges([1, 2, 3, 4, 5, 6, 10, 11, 12]) == "Parts 1–6, 10–12")

W._write_json_atomic(W.LEAD_QUEUE_PATH, [{"channel_id": "UCf", "channel_title": "Forced Pod", "forced": True, "priority": 0,
                                          "latest_video_url": "https://www.youtube.com/watch?v=vidforc0001"}])
pipeline = AppTest.from_file(str(wrapper), default_timeout=60).run()
texts = [m.value for m in pipeline.markdown] + [c.value for c in pipeline.caption]
check("Up next labels a forced creator (and doesn't say 'queued before this update')",
      any("Forced Pod" in t and "forced: automatic checks off" in t for t in texts)
      and not any("queued before this update" in t for t in texts))

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
