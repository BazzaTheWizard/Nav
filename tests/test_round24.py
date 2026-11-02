"""Checks for round 24: the explainer layout, 3-7 highlights with a 59 s cap, finishing a
long video (sound, captions file, chapters and titles) and the quote grabber.
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
from core import quotes as Q  # noqa: E402
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

# =========================== 3. Finish a long video ===========================
cues = L.caption_cues([Word("Costco", 0.3, 0.7), Word("has", 0.7, 0.9), Word("charged", 0.9, 1.3), Word("$1.50.", 1.3, 2.0),
                       Word("That", 2.1, 2.3), Word("is", 2.3, 2.4), Word("forty", 2.4, 2.8), Word("years.", 2.8, 3.3),
                       Word("Why?", 6.0, 6.4)])
check("Caption cues end at sentences and long pauses", [c[2] for c in cues] == ["Costco has charged $1.50.", "That is forty years.", "Why?"],
      str(cues))
many = [Word(f"supercalifragilistic{i}", i * 0.3, i * 0.3 + 0.28) for i in range(40)]
srt = L.words_to_srt(many)
blocks = srt.strip().split("\n\n")
check("Captions file: numbered cues, HH:MM:SS,mmm times", blocks[0].startswith("1\n00:00:00,000 --> 00:00:0") and "," in blocks[0].split("\n")[1])
check("...two lines at most per cue", all(len(block.split("\n")) <= 4 for block in blocks))
times = [(b.split("\n")[1].split(" --> ")) for b in blocks]
check("...no cue overlaps the next", all(a[1] <= b[0] for a, b in zip(times, times[1:])))
check("Timestamps as YouTube writes them", (L.timestamp(5), L.timestamp(725), L.timestamp(3729)) == ("0:05", "12:05", "1:02:09"))
raw = [{"start": 12, "title": "The hook"}, {"start": 95, "title": "Membership fees"}, {"start": 99, "title": "Too close"},
       {"start": 300, "title": "Where the profit is"}, {"start": 595, "title": "Too late"}, {"start": "x", "title": "Bad"}]
check("Chapters: first moved to 0:00, too-close and too-late ones dropped",
      L.valid_chapters(raw, 600) == [(0.0, "The hook"), (95.0, "Membership fees"), (300.0, "Where the profit is")])
check("...a first chapter far from the start gets an Intro at 0:00",
      L.valid_chapters([{"start": 60, "title": "A"}, {"start": 200, "title": "B"}], 600)[0] == (0.0, "Intro"))
check("...fewer than 3 left: none (YouTube wouldn't show them)", L.valid_chapters([{"start": 0, "title": "A"}, {"start": 5, "title": "B"}], 600) == [])

finished = L.finish_long_video(slides, tmp / "long", words, 35.0, suggest=lambda w: {
    "chapters": [{"start": 0, "title": "Hook"}, {"start": 11, "title": "Fees"}, {"start": 22, "title": "Profit"}],
    "titles": ["Why Costco's $1.50 Hot Dog Never Changes", "Why Costco's $1.50 Hot Dog Never Changes", "The Membership Trick"]})
check("Finished file: same picture, copied (same codec and frame count)",
      ffprobe(finished.video, "stream=codec_name,nb_frames") == ffprobe(slides, "stream=codec_name,nb_frames"))
check("...same length, sound and picture", abs(float(ffprobe(finished.video, "stream=duration", "a:0")) - 35.0) < 0.1
      and abs(float(ffprobe(finished.video, "stream=duration")) - 35.0) < 0.1)
stats = subprocess.run([FFMPEG, "-nostats", "-i", str(finished.video), "-map", "0:a", "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
summary = stats[stats.rfind("Summary"):]
loud = float(summary.split("I:")[1].split("LUFS")[0]); peak = float(summary.split("Peak:")[1].split("dBFS")[0])
check("...sound at -14 LUFS, under -1 dBTP", abs(loud + 14) <= 1.0 and peak <= -1.0, f"{loud} LUFS, {peak} dBTP")
check("...captions file written", finished.captions.read_text().startswith("1\n"))
notes = finished.notes.read_text()
check("...chapters and titles file (duplicates dropped)", "0:00 Hook\n0:11 Fees\n0:22 Profit" in notes
      and notes.count("Why Costco's") == 1 and "2. The Membership Trick" in notes, notes)
quiet = L.finish_long_video(slides, tmp / "long2", words, 35.0, suggest=lambda w: None)
check("No AI answer: the video and captions are still made, and it says so", quiet.video.is_file() and quiet.problems)
broken = L.finish_long_video(slides, tmp / "long3", words, 35.0, suggest=lambda w: 1 / 0)
check("An AI error doesn't stop it either", broken.video.is_file() and broken.problems)
check("Same file, same fingerprint; another file, another", L.file_fingerprint(slides) == L.file_fingerprint(slides)
      and L.file_fingerprint(slides) != L.file_fingerprint(busy))

import core.agent_brain as A  # noqa: E402
os.environ["GEMINI_API_KEY"] = "test-key"
A.load_dotenv = lambda *a, **k: None
A._request_gemini = lambda key, prompt, system: json.dumps({"chapters": [{"start": 0, "title": "Hook"}], "titles": ["A", " "]})
check("AI drafts: chapters and titles parsed", A.suggest_chapters_and_titles(words) == {"chapters": [{"start": 0.0, "title": "Hook"}], "titles": ["A"]})
A._request_gemini = lambda key, prompt, system: "not json"
check("...an unusable answer gives None", A.suggest_chapters_and_titles(words) is None)
os.environ.pop("GEMINI_API_KEY")

# =========================== 4. Quote grabber ===========================
lines = [(10.0, 13.0, "and the reason the hot dog"), (13.0, 16.0, "costs a dollar fifty is the"),
         (16.0, 19.0, "membership. That's the whole business."), (40.0, 43.0, "Subscriptions are the moat.")]
found = Q.search_captions(lines, "hot dog dollar fifty")
check("Caption search finds a line spanning two caption events", found and found[0][:2] == (10.0, 16.0), str(found))
check("...case, punctuation and word starts don't matter", Q.search_captions(lines, "SUBSCRIPTION moat!")[0][0] == 40.0)
check("...nothing for words that aren't there", Q.search_captions(lines, "bitcoin") == [])
pair_or_single = [(0.0, 3.0, "so we went back and"), (3.2, 6.0, "the hot dog costs a dollar fifty")]
check("...a line with every word by itself is listed by itself, not joined to the one before",
      Q.search_captions(pair_or_single, "dollar fifty") == [(3.2, 6.0, "the hot dog costs a dollar fifty")])
check("...two lines far apart in time are never joined", Q.search_captions(lines, "business subscriptions") == [])
check("Times: 12:05, 1:02:09, 725; not 'soon' or 12:75",
      (Q.parse_time("12:05"), Q.parse_time("1:02:09"), Q.parse_time("725"), Q.parse_time("soon"), Q.parse_time("12:75"))
      == (725.0, 3729.0, 725.0, None, None))
check("Only the line plus 10 s each side is downloaded", Q.section_for(100.0, 112.0) == (90.0, 122.0)
      and Q.section_for(5.0, 9.0) == (0.0, 19.0) and Q.section_for(3590.0, 3595.0, 3600) == (3580.0, 3600.0))
section_words = [Word("noise", 1.0, 1.4), Word("and", 10.1, 10.3), Word("the", 10.3, 10.5), Word("reason", 10.5, 11.0),
                 Word("business.", 18.0, 18.6), Word("later", 25.0, 25.5)]
check("The word picker starts on the found line", Q.words_near(section_words, 10.0, 19.0) == (1, 4))

section = tmp / "section.mp4"  # a white box shows only from 4.8 to 5.2 s
subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30:d=30",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=30",
                "-vf", "drawbox=x=800:y=400:w=300:h=300:color=white:t=fill:enable='between(t,4.8,5.2)'",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(section)], check=True)
quote = Q.cut_quote(section, 5.0, 9.0, tmp / "Quotes" / "q.mp4")
length = float(ffprobe(quote, "format=duration"))
check("Quote cut: 4 s of words plus the 0.15 s lead-in and 0.4 s tail", abs(length - 4.55) < 0.1, f"{length:.2f} s")
check("...16:9 at 1080 tall", ffprobe(quote, "stream=width,height") == "1920,1080")
check("...starting exactly there (frame 0 is 4.85 s into the part)", frame_at(quote, 0)[550, 950].min() > 200)

DownloadError = type("DownloadError", (Exception,), {})
seen = {}
class FakeYDL:
    def __init__(self, options): self.options = options; seen["options"] = options
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def extract_info(self, url, download=True):
        if download:
            Path(self.options["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"part")
        return seen["info"]
    def urlopen(self, url):
        seen["fetched"] = url
        return SimpleNamespace(read=lambda: json.dumps({"events": [{"tStartMs": 12000, "dDurationMs": 2500, "segs": [{"utf8": "the reason "}, {"utf8": "is fees"}]},
                                                                    {"tStartMs": 15000, "segs": [{"utf8": "\n"}]}]}).encode())
real_ytdlp = sys.modules.get("yt_dlp")
sys.modules["yt_dlp"] = SimpleNamespace(YoutubeDL=FakeYDL, DownloadError=DownloadError)
seen["info"] = {"title": "Ep 200", "channel": "All-In", "duration": 3600,
                "subtitles": {"en": [{"ext": "vtt", "url": "u-vtt"}, {"ext": "json3", "url": "u-own"}]},
                "automatic_captions": {"en": [{"ext": "json3", "url": "u-auto"}]}}
details, caption_lines = D.fetch_captions("https://www.youtube.com/watch?v=abcdefghijk")
check("Captions read without downloading: the uploader's own track first", seen["fetched"] == "u-own"
      and seen["options"].get("skip_download") and details["title"] == "Ep 200")
check("...parsed into timed lines (empty events skipped)", caption_lines == [(12.0, 14.5, "the reason is fees")], str(caption_lines))
seen["info"] = {"title": "Ep 1", "automatic_captions": {"en-orig": [{"ext": "json3", "url": "u-orig"}]}}
D.fetch_captions("https://www.youtube.com/watch?v=abcdefghijk")
check("...else YouTube's automatic captions", seen["fetched"] == "u-orig")
seen["info"] = {"title": "Ep 2"}
check("...and no captions at all is an empty list, not an error", D.fetch_captions("https://www.youtube.com/watch?v=abcdefghijk")[1] == [])
sys.modules["yt_dlp.utils"] = SimpleNamespace(download_range_func=lambda chapters, ranges: ("ranges", tuple(ranges)))
got = D.download_section("https://www.youtube.com/watch?v=abcdefghijk", 90.0, 122.0, tmp / "dl" / "part.mp4")
check("Only that part is downloaded, cut at exactly those times", seen["options"]["download_ranges"] == ("ranges", ((90.0, 122.0),))
      and seen["options"]["force_keyframes_at_cuts"] is True and got.read_bytes() == b"part")
sys.modules.pop("yt_dlp.utils")
if real_ytdlp is not None:
    sys.modules["yt_dlp"] = real_ytdlp

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
        note("render", start=clip.start_time, end=clip.end_time, source=str(source), **{{k: v for k, v in kw.items() if k != "webinar_options"}})
        Path(output).parent.mkdir(parents=True, exist_ok=True); Path(output).write_bytes(b"clip")
        return P.RenderedClip(path=Path(output), clip=clip, layout_mode="explainer", crop=CropBox(0, 0, 2, 2), crop_plan=None)
    P.render_selected_clip = render
    def rerender(**kw):
        note("rerender", title_fade_in=kw.get("title_fade_in"), layout=kw.get("layout_mode"))
        Path(kw["output_path"]).write_bytes(b"trimmed"); return Path(kw["output_path"])
    C.rerender_clip = rerender
    A.suggest_chapters_and_titles = lambda words: {{"chapters": [{{"start": 0, "title": "Hook"}}, {{"start": 11, "title": "Fees"}},
                                                                 {{"start": 22, "title": "Profit"}}], "titles": ["Costco's $1.50 Secret"]}}
    D.fetch_captions = lambda url: note("captions") or ({{"title": "All-In Ep 200", "channel": "All-In", "duration": 3600}},
        [(10.0, 13.0, "and the reason the hot dog"), (13.0, 16.0, "costs a dollar fifty is the membership.")])
    def section(url, start, end, output):
        note("section", start=start, end=end); shutil.copy({str(section)!r}, output); return Path(output)
    D.download_section = section
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
(tmp / "temp").mkdir(); (tmp / "raw").mkdir()
calls = lambda kind: [r for r in map(json.loads, log.read_text().splitlines()) if r["kind"] == kind] if log.exists() else []
at = AppTest.from_file(str(wrapper), default_timeout=180).run()
check("App runs", not at.exception, str([e.value for e in at.exception][:1]))
check("Make clips modes: Highlights, Series, Finish a long video, Quote grabber",
      list(at.radio(key="clip_mode").options) == ["🎯 Highlights", "🎞️ Series: the whole video in order", "🧾 Finish a long video", "✂️ Quote grabber"])
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

at.radio(key="clip_mode").set_value("🧾 Finish a long video").run()
at.text_input(key="finish_path").set_value(f"'{slides}'").run()
at.button(key="finish_go").click().run()
check("Finish a long video: runs", not at.exception, str([e.value for e in at.exception][:1]))
folder = out / "Long videos" / "slides"
check("...saves the finished video, captions and notes", (folder / "slides (finished).mp4").is_file() and (folder / "slides.srt").is_file()
      and (folder / "slides - chapters and titles.txt").is_file(), str(list(folder.glob("*")) if folder.exists() else "no folder"))
check("...shows the chapters and title ideas", at.text_area(key="finish_chapters_text").value == "0:00 Hook\n0:11 Fees\n0:22 Profit"
      and any("Costco's $1.50 Secret" in m.value for m in at.markdown))
check("...and two downloads (captions, notes)", [b.label for b in at.get("download_button")][-2:] == ["⬇ Captions file (.srt)", "⬇ Chapters and titles (.txt)"])
groq_before = len(calls("groq"))
at.button(key="finish_to_shorts").click().run()
check("🎯 Make Shorts from it: Highlights with the finished file and 5 clips",
      at.radio(key="clip_mode").value == "🎯 Highlights" and at.text_input(key="video_path_input").value.endswith("slides (finished).mp4")
      and at.select_slider(key="clip_count").value == 5)
next(b for b in at.button if b.label == "Make 5 vertical clips").click().run()
renders = calls("render")[-5:]
check("...made from that file, with no second Groq call", not at.exception and len(calls("groq")) == groq_before
      and all(r["source"].endswith(".mp4") for r in renders), f"groq calls {groq_before} -> {len(calls('groq'))}")

at.radio(key="clip_mode").set_value("✂️ Quote grabber").run()
at.text_input(key="quote_url").set_value("https://www.youtube.com/watch?v=abcdefghijk").run()
at.button(key="quote_find").click().run()
at.text_input(key="quote_query").set_value("hot dog dollar fifty").run()
check("Quote grabber: finds the line in the captions", at.radio(key="quote_pick").options
      and at.radio(key="quote_pick").options[0].startswith("0:10 · and the reason the hot dog costs a dollar fifty"),
      str(at.radio(key="quote_pick").options if at.get("radio") else ""))
at.button(key="quote_get").click().run()
check("...downloads only that part (0:00-0:26)", calls("section") and (calls("section")[-1]["start"], calls("section")[-1]["end"]) == (0.0, 26.0),
      str(calls("section")))
check("...and transcribes only that part", calls("groq")[-1]["file"].startswith("quote_"))
check("...the word picker starts on the line ('and' to 'fees.')", at.select_slider(key="quote_words").value == (1, 5))
at.select_slider(key="quote_words").set_value((3, 5)).run()
at.button(key="quote_save").click().run()
saved = list((out / "Quotes").glob("*.mp4"))
check("✂️ Save quote: an MP4 from 'reason' to 'fees.'", not at.exception and len(saved) == 1
      and abs(float(ffprobe(saved[0], "format=duration")) - (11.8 - 10.5 + 0.55)) < 0.12,
      str([e.value for e in at.exception][:1]) + str(saved))

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
