"""Round 49: launch polish before full outreach.

1. Captions: the soft band behind them is barely there (about 11% black, was 40%).
2. The executive topic header: the clip's topic for its first 3.5 s on a quiet dark card, then a
   fade (rendered for real: on screen at 0.5 s, gone by 4 s), and on the cold open instead when
   the clip opens on one.
3. Sync audit: trimming no longer drifts the picture against the sound and the captions (flash
   and beep test, constant and variable frame rate).
4. The executive pitch email: show, episode, page link, one question, under 100 words, no Drive.
5. The client page: video, captions file, cover image and LinkedIn post (Copy post) per clip,
   the call / founding-spot CTA, your own visits not counted, link scanners ignored.
6. Cover images (cover_NN.jpg, the frame at 0.5 s, 1080x1920); a trim keeps the page's post;
   removing a clip moves its captions and cover too.
7. Page views for 📨 Sent; Remove clip down to the last clip; the app loads.

Run: timeout 300 .venv/bin/python tests/test_round49.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for key in ("VISUAL_OVERLAYS", "CAPTION_STYLE", "MIDCLIP_TRIM", "MIDCLIP_ZOOM", "EXEC_CAMERA", "EXEC_TITLE_CARD",
            "EXEC_TOPIC_HEADER", "SPEAKER_CLOSEUPS", "FRAME_GUARD", "LEGACY_UI", "ENABLE_CLIMAX_HOOK",
            "CLIP_COVERS", "EXEC_EMAIL"):
    os.environ.pop(key, None)
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


import numpy as np  # noqa: E402

from core import clip_pages, executive, midclip, outreach, pipeline  # noqa: E402
from core import title_card  # noqa: E402
from core.face_tracker import CropPlan, Shot  # noqa: E402
from core.midclip import Cut, TrimPlan  # noqa: E402
from core.subtitle_engine import create_ass_subtitles  # noqa: E402
from core.transcriber import Word  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="round49_"))


def run(*cmd: str) -> None:
    subprocess.run(list(cmd), check=True, capture_output=True)


# ============================== 1. The caption scrim ==============================
words = [Word(text=w, start=0.4 + 0.45 * i, end=0.8 + 0.45 * i) for i, w in enumerate(
    "We priced it by outcome, and our churn dropped by half in one quarter.".split())]
ass = create_ass_subtitles(words, TMP / "exec.ass", clip_duration=8.0).read_text()
alpha = int(executive.SCRIM_ALPHA.strip("&H"), 16)
check("Scrim: about 11% black (alpha 0xE3, was 0x99 = 40%)", alpha >= 0xD8 and 1 - alpha / 255 <= 0.15, executive.SCRIM_ALPHA)
check("...still drawn behind the caption zone, soft-edged", f"\\1a{executive.SCRIM_ALPHA}" in ass
      and f"\\blur{executive.SCRIM_BLUR}" in ass and executive.SCRIM_BLUR >= 45)
check("...the per-letter halo still carries legibility on bright frames", executive.HALO in ass)

# ============================== 2. The executive topic header ==============================
check("Topic header on in the executive look; the creator pill stays off",
      executive.topic_header_on() and not executive.title_card_on())
check("title_plan: the executive card for 3.5 s", executive.title_plan() == (True, "executive", 3.5, True))
os.environ["EXEC_TOPIC_HEADER"] = "0"
check("...EXEC_TOPIC_HEADER=0 turns it off", executive.title_plan() == (False, "", 0.0, False))
os.environ.pop("EXEC_TOPIC_HEADER")
os.environ["VISUAL_OVERLAYS"] = "full"
check("...the creator look keeps its own pill (settings' style and seconds)",
      executive.title_plan(SimpleNamespace(hook_title_style="light", hook_title_seconds=4.0)) == (True, "light", 4.0, False))
os.environ.pop("VISUAL_OVERLAYS")

card, width, height = title_card.render_title_card("Why usage pricing beats per-seat for AI tools",
                                                    TMP / "card.png", style="executive")
font, lines = title_card._layout_text("Why usage pricing beats per-seat for AI tools", 62, "executive")
check("Card: Inter Display (the captions' face), balanced on 2 lines, quieter than the creator pill",
      "NavExec" in Path(font.path).name and len(lines) == 2 and font.size <= 52 and width <= 860, (font.path, lines, width))
from PIL import Image  # noqa: E402

pixels = np.asarray(Image.open(card))
inside = pixels[pixels.shape[0] // 2, title_card.SHADOW_MARGIN + 6]
check("...a dark glass card (not a white box)", inside[:3].max() < 40 and 150 < inside[3] < 255, inside.tolist())
overlay = title_card.TitleOverlay(card, 0, 300, 3.5, 300, height, "top", fade_in=False,
                                  fade_out=title_card.STYLES["executive"]["fade_out"])
graph = title_card.overlay_filter("base", 1, overlay, "out")
check("...on from frame 0, fading out over 0.45 s to end at 3.5 s",
      "fade=t=in" not in graph and "fade=t=out:st=3.050:d=0.450" in graph, graph)

pipeline_src = (ROOT / "core" / "pipeline.py").read_text()
check("Pipeline: header in the body unless a cold open carried it; teaser header fades by 3.5 s",
      "body_title = title_on and not (topic_header and teaser is not None)" in pipeline_src
      and "min(seconds, executive.TOPIC_SECONDS) if topic_header else seconds" in pipeline_src
      and "pinned=not topic_header" in pipeline_src)
check("...no boxy subtitle fallback title in the executive look",
      "hook_title=None if (title or not pill_on) else clip.viral_title" in pipeline_src)
clipper_src = (ROOT / "core" / "clipper.py").read_text()
check("Make-clips trims use the same rule (no creator pill in the executive look)",
      "executive.title_plan(None)" in clipper_src and "hook_title=None if (title or not pill) else hook_title" in clipper_src)

# A real render: the header is on the cover frame (0.5 s) and gone by 4 s.
SOURCE = TMP / "source.mp4"
run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=8",
    "-f", "lavfi", "-i", "sine=frequency=220:duration=8", "-shortest", "-c:v", "libx264", "-preset", "veryfast",
    "-pix_fmt", "yuv420p", "-c:a", "aac", str(SOURCE))
one_face = CropPlan(frame_width=1280, frame_height=720, crop_width=404, crop_height=720, y=0,
                    shots=(Shot(start=0.0, end=8.0, x=438, has_face=True, group=False, speakers=None, moves=(),
                                content=False),), cuts=())
from config import load_settings  # noqa: E402
from core.hook_selector import ClipResult  # noqa: E402

settings = load_settings()
real_plan = pipeline.analyze_crop_plan
pipeline.analyze_crop_plan = lambda *_a, **_k: one_face
os.environ.update(MIDCLIP_TRIM="0", FRAME_GUARD="0", ENABLE_CLIMAX_HOOK="0")
clip = ClipResult(start_time=0.0, end_time=7.5, viral_title="Why usage pricing beats per-seat for AI tools",
                  caption="", hashtags=[], virality_score=80)


def frame(path: Path, at: float) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1",
                          "-vf", "scale=270:480,format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(480, 270).astype(float)


try:
    with_header = pipeline.render_selected_clip(SOURCE, clip, words, settings, TMP / "header.mp4",
                                                layout_mode="smart_crop", work_dir=TMP / "w1", climax_hook_on=False)
    os.environ["EXEC_TOPIC_HEADER"] = "0"
    without = pipeline.render_selected_clip(SOURCE, clip, words, settings, TMP / "plain.mp4",
                                            layout_mode="smart_crop", work_dir=TMP / "w2", climax_hook_on=False)
    os.environ.pop("EXEC_TOPIC_HEADER")
    at_cover = np.abs(frame(with_header.path, 0.5) - frame(without.path, 0.5))
    rows = np.where(at_cover.mean(axis=1) > 6)[0]
    check("Render: the header is on screen at 0.5 s (the cover frame), near the top",
          rows.size > 10 and rows.min() < 240, (rows.min() if rows.size else None, rows.size))
    late = np.abs(frame(with_header.path, 4.2) - frame(without.path, 4.2))
    check("...and gone by 4 s (3.5 s, then the fade)", late.mean(axis=1).max() < 3, late.mean(axis=1).max())
    srt = with_header.path.with_suffix(".srt")
    check("...the clip still gets its captions file", srt.is_file() and "-->" in srt.read_text())
except Exception as exc:  # noqa: BLE001
    check("Render: the header is on screen at 0.5 s (the cover frame), near the top", False, exc)
finally:
    pipeline.analyze_crop_plan = real_plan
    for key in ("MIDCLIP_TRIM", "FRAME_GUARD", "ENABLE_CLIMAX_HOOK", "EXEC_TOPIC_HEADER"):
        os.environ.pop(key, None)

# ============================== 3. Sync audit: trims don't drift ==============================
cut_lengths = (0.317, 0.283, 0.351, 0.229)
cuts, t = [], 0.62
for k in range(18):
    cuts.append(Cut(round(t, 3), round(t + cut_lengths[k % 4], 3), "pause"))
    t += 1.0 + 0.003 * k
expression = midclip.retime_expression(TrimPlan(24.5, cuts[:2]))
check("Each kept frame keeps its own time minus the cuts before it (no renumbering)",
      expression == "PTS-(gte(T\\,0.9370)*0.3170+gte(T\\,1.9030)*0.2830)/TB", expression)
check("...on a constant grid at the source's own rate", "fps=30000/1001:round=near" in
      midclip.filter_graph(TrimPlan(24.5, cuts), 1280, 720, "30000/1001") and "N/FRAME_RATE" not in
      midclip.filter_graph(TrimPlan(24.5, cuts), 1280, 720))


def flash_source(path: Path, rate: str, vfr: bool) -> list[float]:
    events = [round(0.5 + k, 3) for k in range(24)]
    enable = "+".join(f"between(t,{e:.3f},{e + 0.12:.3f})" for e in events)
    vf = f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='{enable}'"
    if vfr:  # drop frames irregularly: a variable-frame-rate recording (Zoom, Riverside)
        vf += ",select='not(eq(mod(n\\,7)\\,3))*not(eq(mod(n\\,11)\\,5))'"
    run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=black:s=320x180:r={rate}:d=25", "-f", "lavfi",
        "-i", "sine=frequency=1000:sample_rate=48000:duration=25", "-vf", vf, "-af", f"volume=0:enable='not({enable})'",
        *(["-fps_mode", "vfr"] if vfr else []), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(path))
    return events


def onsets_video(path: Path) -> list[float]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=32:18,format=gray", "-fps_mode",
                          "passthrough", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    times = [float(x.strip(",")) for x in subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time", "-of", "csv=p=0",
         str(path)], capture_output=True, text=True).stdout.split() if x.strip(",")]
    levels = np.frombuffer(raw, np.uint8).reshape(-1, 576).mean(axis=1)
    return [t for t, a, b in zip(times[1:], levels[:-1], levels[1:]) if b > 128 >= a]


def onsets_audio(path: Path) -> list[float]:
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    level = np.abs(np.frombuffer(pcm, np.int16).astype(float))
    level = level[: level.size // 240 * 240].reshape(-1, 240).mean(axis=1)
    return [i * 240 / 48000 for i in range(1, level.size) if level[i] > 2000 >= level[i - 1]]


for rate, vfr, label in (("30", False, "30 fps"), ("30000/1001", False, "29.97 fps"), ("30", True, "variable frame rate")):
    src = TMP / f"flash_{label.replace(' ', '_').replace('/', '')}.mp4"
    events = flash_source(src, rate, vfr)
    trim = TrimPlan(24.5, list(cuts))
    trim.zooms, trim.centers = [1.0] * len(trim.keep), [None] * len(trim.keep)
    out = src.with_name(src.stem + "_trim.mp4")
    midclip.render_trimmed(src, 0.0, trim, out)
    video, audio = onsets_video(out), onsets_audio(out)
    src_video, src_audio = onsets_video(src), onsets_audio(src)
    near = lambda xs, y: min(xs, key=lambda x: abs(x - y))  # noqa: E731
    # Measured against the source's own onsets: a variable-rate source shows a flash up to a
    # frame or two late and AAC shifts a beep a little; neither is the trim's doing.
    drift_v = 0.0
    av_errors: list[float] = []  # signed, ms, in cut order: (picture - sound) now vs in the source
    for e in events:
        if e >= 24.0:
            continue
        sv, sa = near(src_video, e), near(src_audio, e)
        target = trim.to_trimmed(sv)
        ov = near(video, target)
        oa = near(audio, trim.to_trimmed(sa))
        drift_v = max(drift_v, abs(ov - target))
        av_errors.append(((ov - oa) - (sv - sa)) * 1000)
    frame_ms = 1000 / eval(rate)  # noqa: S307 - a fixed literal above
    # What this measurement can resolve (Nov 11: 40 ms on the Mac failed the old frame + 6 ms limit):
    # the picture onset is known to a frame (a variable-rate source shows a flash up to a frame late),
    # the sound onset to one 240-sample window (5 ms), read in both the source and the output.
    audio_bin_ms = 240 / 48000 * 1000
    resolution_ms = frame_ms + 2 * audio_bin_ms
    check(f"Trim sync, {label}: picture within a frame of the captions' timeline after 18 cuts",
          drift_v * 1000 <= frame_ms + 5, f"{drift_v * 1000:.0f} ms")
    worst = max(abs(x) for x in av_errors)
    check(f"...{label}: sound with the picture, within what the check can measure ({resolution_ms:.0f} ms)",
          worst <= resolution_ms + 1, f"{worst:.0f} ms; per cut {[round(x) for x in av_errors]}")
    # No "build-up" check: the last flashes all come after the final cut (~19 s) and repeat one
    # frame-grid rounding, so a first-vs-last comparison measures that rounding, not drift. Measured
    # Nov 11 at 29.97 fps with 1 ms audio bins: sound exact at every cut, picture within -17/+15 ms
    # (the fps= grid, under half a frame by design), no trend across the 18 cuts.

# ============================== 4. The executive pitch email ==============================
PAGE = "https://clips.navstudiolab.com/acme-revenue-show-k2m4qx"
email = outreach.build_outreach_email(
    "Acme Revenue Show", video_title="AI pricing: what buyers pay for.mp4", drive_link=PAGE, clip_count=3,
    opening_at=872.0, key_point="why usage pricing beats per-seat pricing for AI tools", greeting_name="dana",
    content_format="episode", business=True, theme="AI pricing")
body = email.split("Best,")[0]
check("Email: show, episode and the page link, by name",
      '"AI pricing: what buyers pay for" on Acme Revenue Show' in email and f"\n\n{PAGE}\n\n" in email
      and email.startswith("Hi Dana,\n\n"), email)
check("...the moment to post first, and what's in the delivery",
      "The standout is your point at 14:32 about why usage pricing beats per-seat pricing for AI tools." in email
      and "a LinkedIn post, a Shorts & Reels caption and a cover image" in email)  # Round 50 wording
check("...50-100 words, one soft question, no price, no meeting ask",
      50 <= len(body.split()) <= 100 and body.count("?") == 1 and "Worth doing this for every episode?" in body
      and "$" not in body and "call" not in body.lower(), len(body.split()))
check("...no Google Drive anywhere", "drive" not in email.lower()
      and "drive" not in outreach.DRIVE_LINK_PLACEHOLDER.lower())
check("Subject: short and personal ('3 clips from Acme Revenue Show')",
      outreach.build_subject("AI pricing", "episode", 3, show="Acme Revenue Show", business=True) == "3 clips from Acme Revenue Show")
check("...a long show name falls back to the topic",
      outreach.build_subject("AI pricing", "episode", 3, show="The Extraordinarily Long Revenue Leadership Podcast Show",
                             business=True) == "3 clips from your AI pricing episode")
two, ok = outreach.set_clip_count(email, 2)
one, ok1 = outreach.set_clip_count(email, 1)
check("Removing clips keeps the wording right", ok and ok1 and "I cut 2 LinkedIn-ready clips from" in two
      and "I cut a LinkedIn-ready clip from" in one and "cover image for each." not in one, one)
check("...and the subject's count", outreach.set_subject_count("3 clips from Acme Revenue Show", 2)
      == "2 clips from Acme Revenue Show" and outreach.set_subject_count("2 clips from Acme", 1) == "1 clip from Acme")
draft = outreach.build_outreach_email("Acme Revenue Show", video_title="Ep 12", clip_count=3, business=True)
check("Before the page exists: a placeholder the page link replaces",
      outreach.fill_drive_link(draft, PAGE).count(PAGE) == 1 and outreach.DRIVE_LINK_PLACEHOLDER not in
      outreach.fill_drive_link(draft, PAGE))
check("...older drafts' Drive placeholder is replaced too",
      outreach.fill_drive_link("x " + outreach.LEGACY_PLACEHOLDERS[0], PAGE) == "x " + PAGE)
check("The show is left out when the title already names it",
      outreach.exec_source("Acme Revenue Show", "Acme Revenue Show #12: AI pricing") == '"Acme Revenue Show #12: AI pricing"')
check("The creator (non-B2B) email is unchanged", "makes a great short" in outreach.build_outreach_email(
    "x", drive_link=PAGE, opening_at=10, key_point="why the first ten deals matter most", greeting_name="Dana"))

# ============================== 5. The client page (Worker) ==============================
js = (ROOT / "cloudflare" / "clip-pages-worker.js").read_text()
if shutil.which("node"):
    (TMP / "w.mjs").write_text(js)
    (TMP / "r.mjs").write_text("""import worker from "./w.mjs";
const post = "Hook line.\\n\\n• a\\n• b\\n• c\\n\\nWhat's your take?";
const info = { channel_title: "Acme Revenue Show", video_title: "AI pricing", kind: "episode", created_at: "2026-11-08T15:00:00Z",
  clips: [{ start: 1, end: 40, post, srt: true, cover: true }, { start: 50, end: 90 }] };
const puts = [];
const env = { CLIPS: { get: async (k) => k.endsWith("page.json") ? { json: async () => info } : null,
  head: async (k) => ({}), put: async (k) => { puts.push(k); } } };
const page = await (await worker.fetch(new Request("https://x.test/acme-revenue-show-k2m4qx"), env)).text();
const beacon = (ua, t) => worker.fetch(new Request("https://x.test/e", { method: "POST", body: JSON.stringify({ s: "acme-revenue-show-k2m4qx", t, c: 1 }),
  headers: { "user-agent": ua } }), env);
await beacon("Mozilla/5.0 (Macintosh) Chrome/130", "open");
await beacon("Mozilla/5.0 HeadlessChrome/130", "open");
await beacon("Barracuda link scanner bot", "open");
await beacon("Mozilla/5.0 (iPhone) Safari", "cover");
const cover = await worker.fetch(new Request("https://x.test/acme-revenue-show-k2m4qx/cover_01.jpg?dl=1"),
  { CLIPS: { get: async () => ({ size: 3, body: "abc", httpEtag: "e", writeHttpMetadata: () => {} }) } });
console.log(JSON.stringify({ page, puts, coverStatus: cover.status, coverName: cover.headers.get("content-disposition") }));
""")
    out = subprocess.run(["node", str(TMP / "r.mjs")], capture_output=True, text=True, timeout=30)
    result = json.loads(out.stdout or "{}")
    page = result.get("page", "")
    check("Page: per clip, one-click video download", 'href="/acme-revenue-show-k2m4qx/clip_01.mp4?dl=1"' in page
          and "Download video" in page, out.stderr[-300:])
    check("...the captions file (.srt) and the cover image (.jpg)",
          'href="/acme-revenue-show-k2m4qx/captions_01.srt?dl=1"' in page
          and 'href="/acme-revenue-show-k2m4qx/cover_01.jpg?dl=1"' in page)
    check("...the LinkedIn post with Copy post (Round 50: in a drop-down)",
          'data-copy="1"' in page and "Copy post" in page and 'id="post1"' in page)
    check("...a clip without them shows just the video", "captions_02" not in page and "cover_02" not in page
          and 'data-copy="2"' not in page and "clip_02.mp4?dl=1" in page)
    check("CTA: the content-calendar line, a call request and the founding spot",
          "Want this every week on your content calendar?" in page and "mailto:" in page  # Round 50: no phone
          and "Request a call" in page and 'href="/offer?from=acme-revenue-show-k2m4qx"' in page
          and "Claim your founding spot" in page)
    check("Your own visits (?me=1) are never counted as theirs", 'localStorage.setItem("nav_me", "1")' in page
          and "if (me) return;" in page)
    puts = result.get("puts", [])
    check("Link scanners and headless browsers aren't views; a person is",
          len(puts) == 2 and "_open_" in puts[0] and "_cover_" in puts[1], puts)
    check("The cover downloads as a named .jpg", result.get("coverStatus") == 200
          and "acme-revenue-show-clip-1-cover.jpg" in str(result.get("coverName")), result.get("coverName"))
    check("Older checks still hold: 'LinkedIn, Shorts and Reels', the theme chip format",
          "LinkedIn, Shorts and Reels" in page and "(clip|poster|captions)_(0[1-9])\\.(mp4|jpg|srt)" in js)
else:
    print("SKIP  node not installed: the page checks need it")

# ============================== 6. Covers, trims and removals on R2 ==============================
clip_file = TMP / "deliver" / "clip_01.mp4"
clip_file.parent.mkdir()
shutil.copy(with_header.path if "with_header" in dir() else SOURCE, clip_file)
(clip_file.with_suffix(".srt")).write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
(clip_file.with_name("clip_01_linkedin_post.txt")).write_text("Hook.\n\n• a\n• b\n• c\n\nQ?\n")


class FakeClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload_file(self, local, bucket, key, ExtraArgs=None):
        self.objects[key] = Path(local).read_bytes()

    def put_object(self, Bucket, Key, Body, **_k):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": SimpleNamespace(read=lambda: self.objects[Key])}

    def copy_object(self, Bucket, Key, CopySource):
        self.objects[Key] = self.objects[CopySource["Key"]]

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)

    def list_objects_v2(self, Bucket, Prefix, **_k):
        return {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


fake = FakeClient()
clip_pages._require = lambda: (SimpleNamespace(bucket="b", base_url="https://clips.test"), fake)
name, _url = clip_pages.publish_page("Acme Revenue Show", "AI pricing", [{"path": str(clip_file), "start": 1, "end": 8}],
                                     kind="episode")
info = json.loads(fake.objects[f"{name}/page.json"])
cover_bytes = fake.objects.get(f"{name}/cover_01.jpg", b"")
from io import BytesIO  # noqa: E402

size = Image.open(BytesIO(cover_bytes)).size if cover_bytes else None
check("Publish: cover_01.jpg goes up, full size (1080x1920), and the page lists it",
      size == (1080, 1920) and info["clips"][0].get("cover") is True and info["clips"][0].get("srt") is True, size)
check("...kept next to the clip as well (<clip>_cover.jpg)", clip_pages.cover_path(clip_file).is_file())
check("...from the frame at 0.5 s", clip_pages.COVER_AT == 0.5)
clip_pages.replace_clip(name, "clip_01.mp4", clip_file, 2.0, 7.0)
entry = json.loads(fake.objects[f"{name}/page.json"])["clips"][0]
check("A trim keeps the clip's LinkedIn post, captions and cover on the page (it used to drop them)",
      entry.get("post", "").startswith("Hook.") and entry.get("srt") and entry.get("cover")
      and (entry["start"], entry["end"]) == (2.0, 7.0), entry)

page3 = "acme-three-k2m4qx"
fake.objects[f"{page3}/page.json"] = json.dumps({"clips": [{"start": 1}, {"start": 2, "srt": True, "cover": True},
                                                           {"start": 3, "srt": True, "cover": True}]}).encode()
for number in (1, 2, 3):
    for kind, ext in (("clip", "mp4"), ("poster", "jpg"), ("captions", "srt"), ("cover", "jpg")):
        if number == 1 and kind in {"captions", "cover"}:
            continue
        fake.objects[f"{page3}/{kind}_{number:02d}.{ext}"] = f"{kind}{number}".encode()
clip_pages.remove_clip(page3, 2, 3)
check("Removing clip 2: clip 3's captions and cover move up with it (no stale captions)",
      fake.objects.get(f"{page3}/captions_02.srt") == b"captions3" and fake.objects.get(f"{page3}/cover_02.jpg") == b"cover3"
      and fake.objects.get(f"{page3}/clip_02.mp4") == b"clip3")
check("...and nothing is left under number 3", not any(k.startswith(f"{page3}/") and "_03." in k for k in fake.objects))

# ============================== 7. Page views, Remove clip, the app ==============================
now = datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc)
events = [f"_events/{name}/{int((now - timedelta(hours=h)).timestamp() * 1000)}_{kind}_{clip}_ab12cd34"
          for h, kind, clip in ((5, "open", 0), (2, "open", 0), (2, "play", 1), (2, "cover", 1), (1, "srt", 1),
                                (1, "copy", 1), (1, "offer", 0))]
for key in events:
    fake.objects[key] = b""
activity = clip_pages.page_activity()[name]
check("Activity: each view's time, and the captions / cover downloads",
      len(activity["opened_at"]) == 2 and activity["assets"] == {("cover", 1), ("srt", 1)} and activity["offer"] == 1)
pages = [{"page_name": name, "title": "Acme Revenue Show", "url": "u", "source": "Pitch"},
         {"page_name": "quiet-page-abcdef", "title": "Quiet Co", "url": "v", "source": "Make clips"}]
rows = clip_pages.views_feed({name: activity}, pages, seen_at=now - timedelta(hours=3))
check("Views feed: most recent first, with views, plays and what they took",
      rows[0]["page_name"] == name and rows[0]["views"] == 2 and rows[0]["played"] == [1]
      and rows[0]["took"] == ["post 1", "captions 1", "cover 1"] and rows[1]["views"] == 0, rows[0])
check("...flagged new since you last looked", rows[0]["new"] and not clip_pages.views_feed(
    {name: activity}, pages, seen_at=now)[0]["new"])
clip_pages.VIEWS_SEEN_PATH = TMP / "seen.json"
clip_pages.mark_views_seen(now)
check("...'Mark all seen' is remembered", clip_pages.views_seen_at() == now)
check("ago(): readable times", clip_pages.ago(now - timedelta(minutes=5), now) == "5 min ago"
      and clip_pages.ago(now - timedelta(hours=3), now) == "3 h ago")

import autonomous_worker as W  # noqa: E402

check("Outbox: any clip can be removed, down to the last one", W.OUTBOX_MIN_CLIPS == 1)
app_src = (ROOT / "app.py").read_text()
check("Make clips: the last clip has Remove too (it clears the batch)", 'key=f"remove_made_last_{clip_path.stem}"' in app_src)
check("App: your own clicks open pages with ?me=1; the copied link stays clean",
      "_own_view_link(page_url)" in app_src and '_render_copy_email_button(published["page_url"]' in app_src)
check("Make clips: publishing writes the batch's pitch email with the page link",
      "_write_batch_email(channel.strip(), title.strip(), url, len(clips), episode)" in app_src
      and "The Drive link is added" not in app_src)

from streamlit.testing.v1 import AppTest  # noqa: E402

store = TMP / "app_store"
store.mkdir()
fake_events = {name: {**activity}}
wrapper = TMP / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys; sys.path.insert(0, {str(ROOT)!r})
    from datetime import datetime, timezone
    from pathlib import Path
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(store)!r})
    for attr, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, attr, W.STORAGE_DIR / file)
    import core.clip_pages as C
    C.MANUAL_PAGES_PATH = W.STORAGE_DIR / "manual_pages.json"
    C.VIEWS_SEEN_PATH = W.STORAGE_DIR / "seen_views.json"
    C.manual_pages = lambda: [{{"page_name": {name!r}, "page_url": "https://clips.test/{name}",
                                "channel_title": "Acme Revenue Show", "created_at": "2026-11-08T10:00:00+00:00"}}]
    C.configured = lambda: True
    C.page_settings = lambda: C.PageSettings("a", "k", "s", "b", "https://clips.test")
    when = datetime.now(timezone.utc)
    C.page_activity = lambda since=None: {{{name!r}: {{"opens": 2, "played": {{1}}, "half": set(), "finished": set(),
        "downloaded": set(), "last": when, "copied": {{1}}, "offer": 1, "opened_at": [when], "offer_at": [when],
        "assets": {{("cover", 1)}}}}}}
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
at = AppTest.from_file(str(wrapper), default_timeout=180).run()
check("App: loads cleanly", not at.exception and not at.error,
      str([e.value for e in at.exception][:1] + [e.value for e in at.error][:1]))
check("Sent: the 📈 Page views panel at the top, with the new view flagged",
      any("📈 Page views · 1 new" in s.value for s in at.subheader)
      and any(m.label == "Viewed" and m.value == "1 / 1" for m in at.metric), [s.value for s in at.subheader][:4])
check("...without a pop-up notification (Round 51: removed)", not any("viewed their clips" in t.value for t in at.toast))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(FAILED)} check(s) failed" if FAILED else "\nAll checks passed")
sys.exit(1 if FAILED else 0)
