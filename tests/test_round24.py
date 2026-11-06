"""Checks for round 24: the explainer layout and 3-7 highlights with a 59 s cap.
(Round 28 removed "Finish a long video" and the quote grabber; their checks went with them.)
Temp folders and fakes only: nothing is downloaded, transcribed, uploaded or sent.
Needs FFmpeg. Run all tests: .venv/bin/python tests/run_all.py"""
import dataclasses, json, os, shutil, subprocess, sys, tempfile, textwrap
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["NAV_NO_CLIP_PAGES"] = "1"

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

tmp = Path(tempfile.mkdtemp(prefix="round24-"))
FFMPEG = shutil.which("ffmpeg")
from core import explainer as E  # noqa: E402
from core import long_video as L  # noqa: E402
import core.downloader as D  # noqa: E402
import core.pipeline as P  # noqa: E402
from config import load_settings  # noqa: E402
from core.face_tracker import CropBox  # noqa: E402
from core.hook_selector import ClipResult, cap_clip_length  # noqa: E402
from core.transcriber import Word  # noqa: E402

L.TRANSCRIPTS_DIR = tmp / "transcripts"

def ffprobe(path, entries, stream="v:0"):
    return subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", entries,
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()

def frame_at(path, seconds):
    raw = subprocess.run([FFMPEG, "-v", "error", "-ss", str(seconds), "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], check=True, capture_output=True).stdout
    width, height = map(int, ffprobe(path, "stream=width,height").split(","))
    return np.frombuffer(raw, np.uint8).reshape(height, width, 3)

# A designed 16:9 slide video: navy, a big yellow block in the center 4:3 box, a small
# logo in the left margin (what Keynote exports look like), and a quiet sine voice.
def slide_png(path, size=(1920, 1080)):
    image = Image.new("RGB", size, (14, 20, 38)); d = ImageDraw.Draw(image)
    w, h = size; box = E.safe_box(w, h)
    d.rectangle((box.x + box.width * 0.07, h * 0.28, box.x + box.width * 0.93, h * 0.74), fill=(255, 196, 0))
    d.rectangle((w * 0.02, h * 0.04, w * 0.06, h * 0.1), fill=(240, 240, 240))
    image.save(path)
slide_png(tmp / "slide.png")
slides = tmp / "slides.mp4"
subprocess.run([FFMPEG, "-v", "error", "-y", "-loop", "1", "-framerate", "30", "-t", "35", "-i", str(tmp / "slide.png"),
                "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=35", "-c:v", "libx264", "-preset",
                "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(slides)], check=True)
busy = tmp / "busy.mp4"  # content right to the edges: a wide podcast shot, people near the sides
image = Image.new("RGB", (1920, 1080), (60, 50, 45)); d = ImageDraw.Draw(image)
for x in range(0, 1920, 80):
    d.line((x, 0, x, 1080), fill=(80, 70, 60), width=3)
for cx in (250, 1670):
    d.ellipse((cx - 110, 250, cx + 110, 500), fill=(220, 180, 150)); d.rectangle((cx - 200, 500, cx + 200, 1080), fill=(30, 60, 120))
image.save(tmp / "busy.png")
subprocess.run([FFMPEG, "-v", "error", "-y", "-loop", "1", "-framerate", "30", "-t", "6", "-i", str(tmp / "busy.png"),
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(busy)], check=True)
four_three = tmp / "four_three.mp4"
slide_png(tmp / "slide43.png", (1440, 1080))
subprocess.run([FFMPEG, "-v", "error", "-y", "-loop", "1", "-framerate", "30", "-t", "4", "-i", str(tmp / "slide43.png"),
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(four_three)], check=True)

# =========================== 1. Explainer layout ===========================
check("Center 4:3 box of a 1920x1080 slide: 1440x1080 at x=240", E.safe_box(1920, 1080) == CropBox(240, 0, 1440, 1080))
check("...shown at the full phone width: 1080x810, from y=440 to 1250", E.slide_size(E.safe_box(1920, 1080)) == (1080, 810)
      and E.SLIDE_TOP + 810 == E.SLIDE_BOTTOM_MAX)
check("...captions start under it (y=1280)", E.caption_top(E.safe_box(1920, 1080)) == 1280)
check("720p works the same: 960x720 box", E.safe_box(1280, 720) == CropBox(160, 0, 960, 720))
check("Auto spots a designed slide video (quiet margins)", E.explainer_fit(slides, 0, 18))
check("...but not a wide shot with people near the edges", not E.explainer_fit(busy, 0, 5))
check("...and not a 4:3 video (it has no margins to drop)", not E.explainer_fit(four_three, 0, 3))

words = [Word(f"word{i}." if i % 8 == 7 else f"word{i}", 0.2 + i * 0.4, 0.55 + i * 0.4) for i in range(85)]
settings = dataclasses.replace(load_settings(), hook_title_seconds=4.0, caption_highlight=False, temp_dir=tmp / "work")
clip = ClipResult(start_time=1.0, end_time=17.0, viral_title="Why Costco Never Raises the Hot Dog Price", caption="",
                  hashtags=[], virality_score=90)
made = P.render_selected_clip(slides, clip, words, settings, tmp / "explainer.mp4", layout_mode="auto",
                              allow_explainer=True, title_fade_in=False)
check("Make clips' Auto picks the explainer layout for it", made.layout_mode == "explainer", made.layout_mode)
first = frame_at(made.path, 0)
yellow = lambda px: px[0] > 200 and px[1] > 150 and px[2] < 90
check("The slide fills the width: its yellow block spans x=75-1005 at y=850",
      yellow(first[850, 90]) and yellow(first[850, 990]) and not yellow(first[850, 40]), str(first[850, [40, 90, 990]].tolist()))
check("The margin logo is cropped away", first[440:520, 0:80].max() < 120)
check("Title card above the slide, fully there in frame 0", first[396, 540].min() > 225, str(first[396, 540].tolist()))
during = frame_at(made.path, 2.0)
check("Captions under the slide (y 1280-1400), not over it", during[1280:1400].max() > 200 and not yellow(during[1300, 540]))
plain = P.render_selected_clip(slides, clip, words, settings, tmp / "worker.mp4", layout_mode="auto")
check("The worker's Auto (no allow_explainer) is unchanged: full frame", plain.layout_mode == "blurred_background")

import core.clipper as C  # noqa: E402
trimmed = C.rerender_clip(slides, 2.0, 18.0, words, tmp / "explainer_trim.mp4", layout_mode="explainer",
                          hook_title=clip.viral_title, hook_seconds=4.0, highlight_active_word=False, title_fade_in=False)
first = frame_at(trimmed, 0)
check("A trim keeps the explainer layout and the instant title", yellow(first[850, 540]) and first[396, 540].min() > 225)

# =========================== 2. 59 s max ===========================
spoken = [Word(f"w{i}." if i % 10 == 9 else f"w{i}", 100 + i * 0.5, 100.45 + i * 0.5) for i in range(200)]
long_clip = ClipResult(start_time=100.0, end_time=162.0, viral_title="t", caption="", hashtags=[], virality_score=9)
short_clip = ClipResult(start_time=110.0, end_time=150.0, viral_title="t", caption="", hashtags=[], virality_score=9)
capped = cap_clip_length([long_clip, short_clip], spoken, 59.0)
length = capped[0].end_time - capped[0].start_time
ending = max((w for w in spoken if w.end <= capped[0].end_time), key=lambda w: w.end)
check("A 62 s clip is cut to 59 s or less", length <= 59.0, f"{length:.2f} s")
check("...on a finished sentence", ending.text.endswith("."), ending.text)
check("...and a 40 s clip is left alone", capped[1] == short_clip)
no_stops = [Word(f"w{i}", 100 + i * 0.5, 100.45 + i * 0.5) for i in range(200)]
length = cap_clip_length([long_clip], no_stops, 59.0)[0]
check("No sentence end in reach: cut after the last whole word", length.end_time - length.start_time <= 59.0
      and any(abs(w.end + 0.4 - length.end_time) < 1e-6 for w in no_stops))

# =========================== 3. Transcripts of files ===========================
check("Same file, same fingerprint; another file, another", L.file_fingerprint(slides) == L.file_fingerprint(slides)
      and L.file_fingerprint(slides) != L.file_fingerprint(busy))

# =========================== 5. The app ===========================
from streamlit.testing.v1 import AppTest  # noqa: E402
log = tmp / "calls.log"
out = tmp / "Desktop"
wrapper = tmp / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys, json, shutil, dataclasses; sys.path.insert(0, {str(ROOT)!r})
    from pathlib import Path
    import config
    real_settings = config.load_settings
    config.load_settings = lambda: dataclasses.replace(real_settings(), raw_dir=Path({str(tmp / "raw")!r}), temp_dir=Path({str(tmp / "temp")!r}))
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(tmp / "storage")!r}); W.STORAGE_DIR.mkdir(exist_ok=True)
    for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, name, W.STORAGE_DIR / file)
    import core.downloader as D, core.transcriber as T, core.pipeline as P, core.clipper as C, core.storage_manager as SM, main
    import core.long_video as L, core.agent_brain as A
    from core.hook_selector import ClipResult
    from core.transcriber import Word
    from core.face_tracker import CropBox
    L.TRANSCRIPTS_DIR = Path({str(tmp / "app_transcripts")!r})
    SM.render_output_dir = lambda: Path({str(out)!r})
    LOG = Path({str(log)!r})
    def note(kind, **kw):
        with LOG.open("a") as f: f.write(json.dumps({{"kind": kind, **kw}}) + chr(10))
    D.download_audio_only = lambda url, output_dir="", on_progress=None: Path(output_dir).write_bytes(b"wav") or str(output_dir)
    D.download_source_video = lambda url, output_path=None, on_progress=None: {str(slides)!r}
    D.extract_audio_only = lambda video, output: Path(output).write_bytes(b"wav") or Path(output)
    main._audio_duration = lambda path: 900.0
    LONG = [Word(f"w{{i}}." if i % 10 == 9 else f"w{{i}}", i * 0.5, i * 0.5 + 0.45) for i in range(1800)]
    QUOTE = [Word("noise", 1.0, 1.4), Word("and", 10.1, 10.3), Word("the", 10.3, 10.5), Word("reason", 10.5, 11.0),
             Word("is", 11.0, 11.2), Word("fees.", 11.2, 11.8), Word("later", 25.0, 25.5)]
    def transcribe(path, settings, on_progress=None):
        note("groq", file=Path(path).name)
        return QUOTE if Path(path).name.startswith("quote_") else LONG
    T.transcribe_audio = transcribe
    P.video_info_and_peaks = lambda url: ({{"title": "Costco", "channel": "Nav"}}, [])
    P.pick_clips_with_agent = lambda words, duration, count, *a, **k: ([ClipResult(
        start_time=100.0 * i, end_time=100.0 * i + (62.0 if i == 1 else 40.0), viral_title=f"Clip {{i}}", caption="why",
        hashtags=[], virality_score=90) for i in range(1, count + 1)], "email", "subject")
    def render(source, clip, words, settings, output, **kw):
        note("render", start=clip.start_time, end=clip.end_time, source=str(source), **{{k: v for k, v in kw.items() if k not in ("webinar_options", "graphics")}})
        Path(output).parent.mkdir(parents=True, exist_ok=True); Path(output).write_bytes(b"clip")
        return P.RenderedClip(path=Path(output), clip=clip, layout_mode="explainer", crop=CropBox(0, 0, 2, 2), crop_plan=None)
    P.render_selected_clip = render
    def rerender(**kw):
        note("rerender", title_fade_in=kw.get("title_fade_in"), layout=kw.get("layout_mode"))
        Path(kw["output_path"]).write_bytes(b"trimmed"); return Path(kw["output_path"])
    C.rerender_clip = rerender
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
(tmp / "temp").mkdir(); (tmp / "raw").mkdir()
calls = lambda kind: [r for r in map(json.loads, log.read_text().splitlines()) if r["kind"] == kind] if log.exists() else []
at = AppTest.from_file(str(wrapper), default_timeout=180).run()
check("App runs", not at.exception, str([e.value for e in at.exception][:1]))
check("Make clips modes: Highlights and Series (Round 28 removed the other two)",
      list(at.radio(key="clip_mode").options) == ["🎯 Highlights", "🎞️ Series: the whole video in order"])
check("Highlights: 3 to 7 clips (3 by default), 59 s max and the instant title on by default",
      list(at.select_slider(key="clip_count").options) == ["3", "4", "5", "6", "7"] and at.select_slider(key="clip_count").value == 3
      and at.checkbox(key="cap_59").value and at.checkbox(key="instant_title").value)
check("The layout list has the explainer", any("Explainer" in o for o in at.selectbox(key="crop_mode").options))
at.select_slider(key="clip_count").set_value(5).run()
check("The button follows the count", any(b.label == "Make 5 vertical clips" for b in at.button))
at.text_input(key="video_url_input").set_value("https://www.youtube.com/watch?v=abcdefghijk")
next(b for b in at.button if b.label == "Make 5 vertical clips").click().run()
renders = calls("render")
check("5 clips made", not at.exception and len(renders) == 5, str([e.value for e in at.exception][:1]))
check("...none over 59 s (the 62 s pick was shortened)", all(r["end"] - r["start"] <= 59.0 for r in renders),
      str([round(r["end"] - r["start"], 2) for r in renders]))
check("...instant title, and Auto may use the explainer", all(r["title_fade_in"] is False and r["allow_explainer"] for r in renders))
next(b for b in at.button if "Re-render" in b.label).click().run()
check("A trim keeps the instant title", calls("rerender") and calls("rerender")[-1]["title_fade_in"] is False, str(calls("rerender")))

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
