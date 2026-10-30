"""Checks for round 21: 📌 pinned title card, no zip download, YouTube sign-in
fallback (age-restricted videos, bot check), and the webinar layout.
Temp folders, fakes and small FFmpeg renders only: nothing is downloaded or uploaded.
Run all tests: .venv/bin/python tests/run_all.py"""
import dataclasses, inspect, json, os, shutil, subprocess, sys, tempfile, textwrap
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["NAV_NO_CLIP_PAGES"] = "1"
for name in ("YTDLP_COOKIES_PATH", "YTDLP_COOKIES_BROWSER", "YTDLP_COOKIES_IN_WORKER", "HOOK_TITLE_STYLE"):
    os.environ[name] = ""  # this Mac's .env must not change the results

import numpy as np  # noqa: E402

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

tmp = Path(tempfile.mkdtemp())
FFMPEG = shutil.which("ffmpeg")

# =================== 3. YouTube sign-in: cookies only when YouTube asks ===================
import core.downloader as D  # noqa: E402
from core.service_pause import ServicePause  # noqa: E402

AGE = ("ERROR: [youtube] 3YSgyAziCiY: Sign in to confirm your age. This video may be inappropriate for some users. "
       "Use --cookies-from-browser or --cookies for the authentication.")
BOT = "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies for the authentication."
DownloadError = type("DownloadError", (Exception,), {})
calls = []
behaviour = {}  # what each attempt does: a list of errors (None = works)


class FakeYDL:
    def __init__(self, options):
        self.options = options
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def _attempt(self):
        calls.append({k: self.options.get(k) for k in ("cookiefile", "cookiesfrombrowser")})
        outcome = behaviour["plan"][min(len(calls), len(behaviour["plan"])) - 1]
        if outcome:
            raise DownloadError(outcome)
    def extract_info(self, url, download=True):
        self._attempt()
        if download:
            Path(self.options["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"video")
        return {"title": "Inferior STEMI", "channel": "Med Pod", "duration": 60, "heatmap": None, "format_id": "18"}
    def download(self, urls):
        self._attempt()
        Path(self.options["outtmpl"].replace("%(ext)s", "m4a")).write_bytes(b"audio")


real_ytdlp = sys.modules.get("yt_dlp")
sys.modules["yt_dlp"] = SimpleNamespace(YoutubeDL=FakeYDL, DownloadError=DownloadError)
D.load_settings = lambda: SimpleNamespace(raw_dir=tmp / "raw", temp_dir=tmp / "temp")
(tmp / "raw").mkdir()

def run(plan, what="info", **env):
    calls.clear(); behaviour["plan"] = plan
    for key in ("YTDLP_COOKIES_PATH", "YTDLP_COOKIES_BROWSER"):
        os.environ[key] = env.get(key, "")
    try:
        if what == "info":
            return D.fetch_video_info("https://www.youtube.com/watch?v=3YSgyAziCiY")
        if what == "audio":
            return D.download_audio_only("https://www.youtube.com/watch?v=3YSgyAziCiY", tmp / "audio")
        return D.download_source_video("https://example.com/video.mp4")
    except Exception as exc:  # noqa: BLE001
        return exc

check("Normal videos: one try, no cookies", run([None])["title"] == "Inferior STEMI" and calls == [{"cookiefile": None, "cookiesfrombrowser": None}])
result = run([AGE, None], "audio")
check("Age-restricted: tried again with Chrome's sign-in, and it works", isinstance(result, str) and result.endswith(".m4a")
      and calls[1]["cookiesfrombrowser"] == ("chrome",), str(calls))
cookie_file = tmp / "cookies.txt"; cookie_file.write_text("# Netscape HTTP Cookie File\n")
result = run([AGE, AGE, None], "video", YTDLP_COOKIES_PATH=str(cookie_file))
check("cookies.txt first, then the browser (download_source_video)", isinstance(result, str) and result.endswith(".mp4")
      and [c["cookiefile"] for c in calls] == [None, str(cookie_file), None] and calls[2]["cookiesfrombrowser"] == ("chrome",), str(calls))
result = run([AGE, "ERROR: could not find chrome cookies database in \"/Users/x\""], "audio", YTDLP_COOKIES_BROWSER="brave:Profile 2")
check("Still age-restricted after every try: a clear SignInRequired, not a raw RuntimeError",
      isinstance(result, D.SignInRequired) and result.age_restricted and "Tried: Brave's YouTube sign-in" in str(result)
      and "YTDLP_COOKIES_PATH" in str(result) and calls[1]["cookiesfrombrowser"] == ("brave", "Profile 2"), str(result)[:300])
result = run([AGE], "info", YTDLP_COOKIES_BROWSER="off")
check("Browser off and no file: says no sign-in is set up (one try only)",
      isinstance(result, D.SignInRequired) and "No YouTube sign-in is set up" in str(result) and len(calls) == 1)
result = run([BOT, BOT], "audio")
check("Bot check that cookies don't fix: the worker's YouTube pause, with the sign-in tip",
      isinstance(result, ServicePause) and result.service == "youtube" and "YTDLP_COOKIES_PATH" in result.hint and len(calls) == 2)
result = run([BOT, None], "info")
check("Bot check fixed by cookies", isinstance(result, dict) and len(calls) == 2)
result = run(["ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader"], "audio")
check("Other errors: no cookie retry", isinstance(result, RuntimeError) and not isinstance(result, D.SignInRequired) and len(calls) == 1)
result = run(["ERROR: unable to download video data: HTTP Error 403: Forbidden"], "video")
check("A 403 stays DownloadForbidden (no cookie retry)", isinstance(result, D.DownloadForbidden) and len(calls) == 1)
D.allow_browser_cookies(False)
result = run([AGE, None], "info", YTDLP_COOKIES_PATH=str(cookie_file))
check("Worker without YTDLP_COOKIES_IN_WORKER: the cookies file only, never the browser",
      isinstance(result, dict) and calls[1] == {"cookiefile": str(cookie_file), "cookiesfrombrowser": None} and len(calls) == 2)
check("...and the System tab says so", "Browser cookies in the worker: off" in D.sign_in_summary())
D.allow_browser_cookies(True)
os.environ["YTDLP_COOKIES_PATH"] = ""
os.environ["YTDLP_COOKIES_BROWSER"] = "netscape"
check("A browser yt-dlp doesn't know is ignored (with a log line)", [label for label, _o in D.cookie_sources()] == [])
os.environ["YTDLP_COOKIES_BROWSER"] = ""
if real_ytdlp is not None:
    sys.modules["yt_dlp"] = real_ytdlp

import autonomous_worker as W  # noqa: E402
check("Worker: browser cookies only with YTDLP_COOKIES_IN_WORKER=1",
      "allow_browser_cookies(" in inspect.getsource(W.main) and "YTDLP_COOKIES_IN_WORKER" in inspect.getsource(W.main))
W.download_source_video = lambda url: (_ for _ in ()).throw(D.SignInRequired("needs sign-in", age_restricted=True))
real_configured = W.clip_pages.configured
W.clip_pages.configured = lambda: True  # skips the Drive sign-in check
try:
    W._process_lead({"channel_id": "UCage", "channel_title": "Age Pod", "latest_video_title": "T",
                     "latest_video_url": "https://www.youtube.com/watch?v=vidage00001"}, SimpleNamespace(temp_dir=tmp / "jobs", raw_dir=tmp))
    check("Worker: an age-restricted video is skipped as not a fit (Force can retry it later)", False)
except W.NotAFit as exc:
    check("Worker: an age-restricted video is skipped as not a fit (Force can retry it later)", str(exc).startswith(W.AGE_RESTRICTED), str(exc))
W.download_source_video = lambda url: (_ for _ in ()).throw(D.SignInRequired("bot", age_restricted=False))
try:
    W._process_lead({"channel_id": "UCbot", "channel_title": "Bot Pod", "latest_video_title": "T",
                     "latest_video_url": "https://www.youtube.com/watch?v=vidbot00001"}, SimpleNamespace(temp_dir=tmp / "jobs", raw_dir=tmp))
except W.NotAFit:
    check("...but not other sign-in errors", False)
except D.SignInRequired:
    check("...but not other sign-in errors", True)
W.clip_pages.configured = real_configured

# =================== 1. 📌 Pinned title card ===================
import core.title_card as TC  # noqa: E402
overlay = TC.TitleOverlay(image_path=tmp / "t.png", x=0, y=0, seconds=4.0, card_top=0, card_height=10, placement="top")
check("Normal card: fades out at the end of its seconds", "fade=t=out:st=3.750" in TC.overlay_filter("base", 1, overlay, "titled")
      and "eof_action=pass" in TC.overlay_filter("base", 1, overlay, "titled"))
pinned = dataclasses.replace(overlay, seconds=60.0, pinned=True)
check("Pinned card: no fade-out, holds its last frame", "fade=t=out" not in TC.overlay_filter("base", 1, pinned, "titled")
      and "eof_action=repeat" in TC.overlay_filter("base", 1, pinned, "titled") and "fade=t=in" in TC.overlay_filter("base", 1, pinned, "titled"))
check("Pinned over a long clip: face check samples at most 90 frames", TC._face_sample_fps(4) == 3 and abs(TC._face_sample_fps(180) * 180 - 90) < 1e-6)

# ---- test video: slides (still between changes) + a live webcam in the top-right corner ----
def make_webinar(path, slide_w=1920, cam_x=1410, cam_y=24, distractor=True, seconds=10):
    """1920x1080: a slide that changes every 4 s, a 480x270 'webcam' (moving, noisy, with a
    magenta 'face') at cam_x,cam_y with a white border, and (optionally) a small moving
    animation in the bottom-left of the slide that has no face."""
    slide = (f"color=c=0xF5F5F0:s={slide_w}x1080:d={seconds}:r=30,"
             "drawbox=x=0:y=0:w=iw:h=150:color=0x123460:t=fill,"
             "drawbox=x=120:y=250:w=900:h=40:color=0x202020:t=fill:enable='lt(t,4)',"
             "drawbox=x=120:y=380:w=700:h=40:color=0x202020:t=fill:enable='gte(t,4)',"
             "drawbox=x=120:y=500:w=800:h=40:color=0x902020:t=fill,"
             f"pad=1920:1080:0:0:black[s]")
    cam = (f"color=c=0x8A8478:s=480x270:r=30:d={seconds},noise=alls=16:allf=t,"  # a live camera: noise everywhere
           "drawbox=x='200+16*sin(3*t)':y=70:w=80:h=96:color=0xFF00FF:t=fill,pad=486:276:3:3:white[c]")
    graph = f"{slide};{cam};[s][c]overlay={cam_x}:{cam_y}[v1]"
    if distractor:
        graph += f";life=s=200x120:r=30:mold=10,trim=duration={seconds}[d];[v1][d]overlay=60:880[v]"
    else:
        graph += ";[v1]null[v]"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
                    "-filter_complex", graph, "-map", "[v]", "-map", "0:a", "-t", str(seconds),
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-c:a", "aac", str(path)], check=True)

class MagentaFaces:
    """Stands in for the face detector: the magenta box is the face."""
    available = True
    def detect(self, frame):
        b, g, r = frame[..., 0].astype(int), frame[..., 1].astype(int), frame[..., 2].astype(int)
        ys, xs = np.nonzero((r > 232) & (b > 232) & (g < 45))
        if xs.size < 30:
            return []
        return [((xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2, xs.max() - xs.min() + 1, ys.max() - ys.min() + 1, 0.95)]

import core.webinar as WB  # noqa: E402
WB._face_detector = lambda: MagentaFaces()
video = tmp / "webinar.mp4"
make_webinar(video)

def frame_at(path, seconds):
    raw = subprocess.run([FFMPEG, "-v", "error", "-ss", str(seconds), "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3)

def magenta(pixels):
    return int(((pixels[..., 0] > 232) & (pixels[..., 2] > 232) & (pixels[..., 1] < 45)).sum())

def is_light(pixels):
    return float(pixels.mean()) > 200

# =================== 4. Webinar layout ===================
layout = WB.plan_webinar(video, 0.5, 9.5, "auto")
true_cam = (1413, 27, 480, 270)  # inside the white border
inside = (layout.camera.x >= true_cam[0] - 2 and layout.camera.y >= true_cam[1] - 2
          and layout.camera.x + layout.camera.width <= true_cam[0] + true_cam[2] + 2
          and layout.camera.y + layout.camera.height <= true_cam[1] + true_cam[3] + 2)
cover = layout.camera.width * layout.camera.height / (true_cam[2] * true_cam[3])
check("Auto finds the webcam window in the top-right (not the slide animation bottom-left)",
      layout.corner == "top-right" and layout.found == "webcam window", f"{layout.corner}, {layout.found}")
check("...the crop is the window itself: inside it, no slide pixels, ≥ 85% of it", inside and cover >= 0.85, f"{layout.camera}, {cover:.0%}")
check("Slides: the whole frame, fitted to the width (1080x606)", (layout.content.width, layout.content.height) == (1920, 1080)
      and (layout.content_width, layout.content_height) == (1080, 606))
check("Title under the slides, captions under that", layout.title_top == 460 + 606 + 28 and layout.caption_top == 460 + 606 + 36)
picked = WB.plan_webinar(video, 0.5, 9.5, "bottom-left")
check("A corner you pick is used (bottom-left: no face there, so a default webcam-size box)",
      picked.corner == "bottom-left" and picked.found == "no face: default corner box"
      and picked.camera.x < 100 and picked.camera.y + picked.camera.height > 1000, str(picked.camera))
side = tmp / "side.mp4"
make_webinar(side, slide_w=1440, cam_x=1446, cam_y=10, distractor=False)
zoom = WB.plan_webinar(side, 0.5, 9.5, "auto")
check("Zoom-style (4:3 slide, webcam in the dark strip beside it): the strip is left out, the slide shows bigger",
      zoom.found == "webcam window" and abs(zoom.content.width - 1440) <= 4 and zoom.content.x == 0
      and (zoom.content_width, zoom.content_height) == (852, 640), f"{zoom.content} -> {zoom.content_width}x{zoom.content_height}")
WB._face_detector = lambda: SimpleNamespace(available=True, detect=lambda frame: [])
blind = WB.plan_webinar(video, 0.5, 9.5, "auto")
check("No face anywhere: the top-right default box (renders anyway)", blind.corner == "top-right" and blind.found.startswith("no face"))
WB._face_detector = lambda: MagentaFaces()
graph = layout.filter_graph()
check("One filter graph: split, crop, scale, overlay, vstack, divider", all(part in graph for part in (
    "split=3", "crop=w=", "force_original_aspect_ratio=decrease", "overlay=", "vstack=inputs=2", "drawbox=x=0:y=458")))

# Real renders through core.pipeline (captions, title card, encoder) with this layout.
import core.pipeline as P  # noqa: E402
from config import load_settings  # noqa: E402
from core.hook_selector import ClipResult  # noqa: E402
from core.transcriber import Word  # noqa: E402
settings = dataclasses.replace(load_settings(), hook_title_seconds=2.0, hook_title_style="light", temp_dir=tmp / "work")
words = [Word("Check", 0.6, 0.9), Word("the", 0.9, 1.1), Word("leads.", 1.1, 1.5)]  # nothing said at the end
clip = ClipResult(start_time=0.5, end_time=6.5, viral_title="Inferior STEMI: check the right-sided leads",
                  caption="", hashtags=[], virality_score=90)
started = __import__("time").time()
result = P.render_selected_clip(video, clip, words, settings, tmp / "web.mp4", layout_mode="webinar", webinar_corner="auto")
seconds_taken = __import__("time").time() - started
probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "json",
                                   str(result.path)], capture_output=True, text=True).stdout)["streams"][0]
check("Webinar render: 1080x1920, layout kept for trimming", (probe["width"], probe["height"]) == (1080, 1920)
      and result.layout_mode == "webinar" and result.webinar is not None, f"{probe} in {seconds_taken:.0f}s")
early = frame_at(result.path, 1.0)
web = result.webinar
check("Top band: the speaker (the 'face' is there, sharp and big)", magenta(early[:460]) > 4 * max(1, magenta(early[460:])), f"{magenta(early[:460])} vs {magenta(early[460:])}")
check("A thin dark line between speaker and slides", float(early[458:462].mean()) < 60, f"{early[458:462].mean():.0f}")
check("Slides right under it, the full width", is_light(early[600:900, 300:800]) and is_light(early[600:900, 20:60]))
CARD = slice(web.title_top + 6, web.title_top + 16)  # the card's top padding, above the text
check("Title card under the slides while it shows", is_light(early[CARD, 400:680]))
late = frame_at(result.path, 5.7)
check("...and gone after its 2 s (not pinned)", float(late[CARD, 400:680].mean()) < 60)
check("Below: dark (captions and the title never cover a slide)", float(late[1650:1900].mean()) < 30)
result = P.render_selected_clip(video, clip, words, settings, tmp / "web_pin.mp4", layout_mode="webinar", pin_title=True)
last = frame_at(result.path, 5.9)
check("📌 Pinned: the card is still there in the last frame", is_light(last[CARD, 400:680]))
plain = P.render_selected_clip(video, clip, words, settings, tmp / "plain_pin.mp4", layout_mode="blurred_background", pin_title=True)
title = TC.prepare_title_overlay("Inferior STEMI", 0.0, layout="blurred_background", plan=None, source_video=video,
                                 clip_start=0.5, clip_duration=6.0, work_dir=tmp, name="pinzero", pinned=True)
check("...and even with HOOK_TITLE_SECONDS=0: pinned means the whole clip", title is not None and title.pinned and title.seconds == 6.0)
card_top = title.card_top
check("📌 Pinned works in the other layouts too (blurred background: card still on at the end)", is_light(frame_at(plain.path, 5.9)[card_top + 6:card_top + 16, 420:660]))

# Captions: under the slides; under the title card while it shows.
from core.subtitle_engine import create_ass_subtitles  # noqa: E402
ass = create_ass_subtitles([Word("one", 0.2, 0.5), Word("two.", 3.2, 3.6)], tmp / "w.ass", clip_duration=5.0,
                           highlight_active_word=False, seam_title=(2.0, 1300), caption_top=1102).read_text()
check("Captions: just under the title card while it shows, then under the slides",
      "\\an8\\pos(540,1314)}ONE" in ass.replace("{\\blur0.6}", "") and "\\an8\\pos(540,1102)}" in ass, ass[-300:])

# =================== Make clips tab ===================
from streamlit.testing.v1 import AppTest  # noqa: E402
import core.clipper as C  # noqa: E402
log = tmp / "app_calls.json"
# The clips folder is reached through a symlinked parent, like macOS temp folders
# (/var -> /private/var): the app must find a clip's saved framing however its path is spelled.
(tmp / "private_home").mkdir()
(tmp / "home").symlink_to(tmp / "private_home")
out = tmp / "home" / "Desktop"
wrapper = tmp / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys, json; sys.path.insert(0, {str(ROOT)!r})
    from pathlib import Path
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(tmp / "storage")!r}); W.STORAGE_DIR.mkdir(exist_ok=True)
    for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, name, W.STORAGE_DIR / file)
    import core.downloader as D, core.transcriber as T, core.pipeline as P, core.clipper as C, core.storage_manager as SM, main
    from core.hook_selector import ClipResult
    from core.transcriber import Word
    from core.face_tracker import CropBox
    LOG = Path({str(log)!r})
    def note(kind, **kw):  # two clips render at once: append one line each
        with LOG.open("a") as f: f.write(json.dumps({{"kind": kind, **kw}}) + chr(10))
    D.download_audio_only = lambda url, output_dir="", on_progress=None: Path(output_dir).write_bytes(b"wav") or str(output_dir)
    D.download_source_video = lambda url, output_path=None, on_progress=None: {str(video)!r}
    main._audio_duration = lambda path: 600.0
    T.transcribe_audio = lambda path, settings, on_progress=None: [Word("hello.", 1.0, 1.5)]
    P.video_info_and_peaks = lambda url: ({{"title": "Webinar", "channel": "Med Pod"}}, [])
    P.pick_clips_with_agent = lambda *a, **k: ([ClipResult(start_time=10.0 * i, end_time=10.0 * i + 30, viral_title=f"Clip {{i}}",
                                                caption="why", hashtags=[], virality_score=90) for i in (1, 2, 3)], "email", "subject")
    def render(source, clip, words, settings, output, **kw):
        note("render", title=clip.viral_title, **kw)
        Path(output).parent.mkdir(parents=True, exist_ok=True); Path(output).write_bytes(b"clip")
        return P.RenderedClip(path=Path(output), clip=clip, layout_mode=kw.get("layout_mode"), crop=CropBox(0, 0, 2, 2),
                              crop_plan=None, webinar="LAYOUT" if kw.get("layout_mode") == "webinar" else None)
    P.render_selected_clip = render
    def rerender(**kw):
        note("rerender", pin_title=kw.get("pin_title"), webinar=kw.get("webinar"), layout=kw.get("layout_mode"))
        Path(kw["output_path"]).write_bytes(b"trimmed"); return Path(kw["output_path"])
    C.rerender_clip = rerender
    SM.render_output_dir = lambda: Path({str(out)!r})
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
at = AppTest.from_file(str(wrapper), default_timeout=120).run()
check("App runs", not at.exception, str([e.value for e in at.exception]))
pins = [box for box in at.checkbox if box.label == "📌 Pin title card for full video duration"]
check("Make clips: the 📌 Pin title card checkbox (off by default)", len(pins) == 1 and pins[0].value is False)
layout_box = at.selectbox(key="crop_mode")
webinar_label = next((o for o in layout_box.options if "Webinar" in o), None)
check("Layout list has 'Webinar / screen + speaker'", webinar_label is not None, str(layout_box.options))
check("Webcam corner: automatic or one of the 4 corners", at.selectbox(key="webcam_corner").options
      == ["Find it automatically", "Top right", "Top left", "Bottom right", "Bottom left"])
at.text_input(key="video_url_input").set_value("https://www.youtube.com/watch?v=abcdefghijk")
layout_box.set_value(webinar_label)
at.selectbox(key="webcam_corner").set_value("Bottom left")
pins[0].check()
next(b for b in at.button if b.label == "Make 3 vertical clips").click().run()
check("Clips made without errors", not at.exception, str([e.value for e in at.exception]))
rows = [json.loads(line) for line in log.read_text().splitlines()]
renders = [r for r in rows if r["kind"] == "render"]
check("Each clip rendered with the webinar layout, the corner and the pin",
      len(renders) == 3 and all(r["layout_mode"] == "webinar" and r["webinar_corner"] == "bottom-left" and r["pin_title"] for r in renders), str(renders)[:300])
check("Downloads: one raw .mp4 per clip, no zip", [b.label for b in at.get("download_button")] == ["Download clip"] * 3)
trim = next(b for b in at.button if "Re-render" in b.label)
trim.click().run()
rerenders = [r for r in map(json.loads, log.read_text().splitlines()) if r["kind"] == "rerender"]
check("Trimming a clip keeps the pin and the webinar layout", rerenders and rerenders[-1] == {
    "kind": "rerender", "pin_title": True, "webinar": "LAYOUT", "layout": "webinar"}, str(rerenders))

at.radio(key="clip_mode").set_value("🎞️ Series: the whole video in order").run()
check("Series: the 📌 pin checkbox too", any(box.label == "📌 Pin title card for full video duration" for box in at.checkbox))
check("Series: the corner box only shows for the webinar layout", not any(s.key == "series_webcam_corner" for s in at.selectbox))
at.selectbox(key="series_layout").set_value(webinar_label).run()
check("...and shows when it's picked", any(s.key == "series_webcam_corner" for s in at.selectbox))
import app as A  # noqa: E402
check("No zip code left in the app", not hasattr(A, "_zip_parts") and "zip)" not in inspect.getsource(A))

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
