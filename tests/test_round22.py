"""Checks for round 22: trimmed clips keep captions in sync (Outbox and Make clips),
audio cleanup and loudness, sharper scaling, webinar slide shape and dividers,
the Outbox's cut-point words and poster frame picker.
Temp folders, fakes and small FFmpeg renders only: nothing is uploaded.
Run all tests: .venv/bin/python tests/run_all.py"""
import dataclasses, json, os, random, shutil, subprocess, sys, tempfile, textwrap
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["NAV_NO_CLIP_PAGES"] = "1"
for name in ("AUDIO_CLEANUP", "AUDIO_TARGET_LUFS", "HOOK_TITLE_STYLE", "CAPTION_STYLE", "EMPHASIS_INTENSITY"):
    os.environ[name] = ""  # this Mac's .env must not change the results

import numpy as np  # noqa: E402

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

tmp = Path(tempfile.mkdtemp())
FFMPEG = shutil.which("ffmpeg")

# ======================= 2. Captions stay in sync after a trim =======================
# A test source: 30000/1001 fps with a keyframe every 75 frames (2.5025 s), so many
# keyframe times written to the millisecond land just before the real keyframe (the
# old bug). A beep at known, irregular times, and one caption word per beep.
random.seed(22)
beeps, t = [], 2.0
while t < 86:
    beeps.append(round(t, 3)); t += random.uniform(0.9, 1.8)
source = tmp / "source.mp4"
volume = "+".join(f"between(t,{b},{b + 0.12})" for b in beeps)
subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x1E1E1E:s=1280x720:r=30000/1001:d=90",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=90",
                "-filter_complex", f"[0:v]drawbox=x='100+t*8':y=300:w=120:h=120:color=0x404040:t=fill[v];"
                f"[1:a]volume='if({volume},0.5,0)':eval=frame[a]",
                "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-g", "75", "-keyint_min", "75",
                "-sc_threshold", "0", "-c:a", "aac", "-b:a", "128k", str(source)], check=True)
from core.transcriber import Word  # noqa: E402
words = [Word(f"W{i}.", b, b + 0.3) for i, b in enumerate(beeps)]

import core.pitch_files as PF  # noqa: E402
keyframes = [float(x.strip(",")) for x in subprocess.run(
    ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey", "-show_entries", "frame=pts_time",
     "-of", "csv=p=0", str(source)], capture_output=True, text=True).stdout.split()]
rounds_down = [k for k in keyframes if 20 < k < 40 and float(f"{k:.3f}") < k - 1e-7]
check("Test source: keyframes whose millisecond time lands before the keyframe (the old bug's trigger)",
      bool(rounds_down), str(keyframes[:12]))
key = rounds_down[0]
old_slice = tmp / "old_slice.mp4"
subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{key:.3f}", "-i", str(source), "-t", "20", "-c", "copy",
                "-avoid_negative_ts", "make_zero", str(old_slice)], check=True)
first_key_old = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                      str(old_slice)], capture_output=True, text=True).stdout)
check("Before the fix: a cut written as the rounded time starts one keyframe early", first_key_old > 20 + 2.0,
      f"asked for 20 s from {key:.4f}, got {first_key_old:.2f} s (2.5 s extra = one keyframe earlier)")

def beep_onsets(path):
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    a = np.abs(np.frombuffer(raw, np.int16).astype(float))
    env = a[: a.size // 40 * 40].reshape(-1, 40).mean(axis=1)  # 5 ms
    on = env > 0.25 * env.max()
    # From 0.2 s: a beep that began just before the cut leaves a sliver in the first frames.
    return [i * 0.005 for i in range(1, on.size) if on[i] and not on[i - 1] and i * 0.005 >= 0.2]

def caption_onsets(path, zone):
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-vf", "fps=30000/1001,scale=270:480,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, 480, 270)
    lit = (frames[:, zone[0] // 4:zone[1] // 4] > 190).sum(axis=(1, 2)) > 25
    return [i * 1001 / 30000 for i in range(1, lit.size) if lit[i] and not lit[i - 1]]

def in_sync(path, zone=(1000, 1250)):
    sound, text = beep_onsets(path), caption_onsets(path, zone)
    pairs = [(b, min(text, key=lambda c: abs(c - b))) for b in sound if text]
    worst = max((abs(b - c) for b, c in pairs), default=99)
    return len(pairs) >= 8 and worst <= 0.1, f"{len(pairs)} beeps, worst gap {worst:.3f} s"

import core.pipeline as P  # noqa: E402
from config import load_settings  # noqa: E402
from core.hook_selector import ClipResult  # noqa: E402
settings = dataclasses.replace(load_settings(), hook_title_seconds=0.0, caption_highlight=False, temp_dir=tmp / "work")
clip_start = key + 30 + 0.4  # the slice is cut from the keyframe 30 s before: `key`
clip = ClipResult(start_time=clip_start, end_time=clip_start + 20, viral_title="", caption="", hashtags=[], virality_score=80)
first = P.render_selected_clip(source, clip, words, settings, tmp / "first.mp4", layout_mode="blurred_background")
ok, detail = in_sync(first.path)
check("The first render is in sync (baseline)", ok, detail)

PF.PITCHES_DIR = tmp / "pitches"
PF.save_pitch_files("sync1", source, [first], words, 90.0)
item = PF.load_pitch("sync1")["clips"][0]
found = PF.find_clip_in_slice(item["path"].parent / item["source"], item["path"])
check("Slice cut from just after the keyframe: it starts where it says (checked by its sound)",
      found is not None and abs((item["start"] - found) - item["slice_start"]) <= 0.011 and item["slice_checked"],
      f"says {item['slice_start']:.3f}, sound says {item['start'] - found:.3f}" if found is not None else "no match")
trimmed = PF.rerender_trim("sync1", 1, clip_start - 3.0, clip_start + 15.0, settings)
ok, detail = in_sync(trimmed)
check("Outbox trim (earlier start, earlier end): captions on the beat", ok, detail)
expected_first_beep = min(b for b in beeps if b >= clip_start - 3.0 + 0.2) - (clip_start - 3.0)
check("...and the sound is where it should be (first beep within 30 ms: no filter delay)",
      abs(beep_onsets(trimmed)[0] - expected_first_beep) <= 0.03, f"{beep_onsets(trimmed)[0]:.3f} vs {expected_first_beep:.3f}")

# A pitch saved by the old code: its slice really starts a keyframe before what pitch.json says.
manifest_path = tmp / "pitches" / "sync1" / "pitch.json"
manifest = json.loads(manifest_path.read_text())
saved = manifest["clips"][0]
gop = 75 * 1001 / 30000
saved["slice_start"] = round(saved["slice_start"] + gop, 3)
saved["slice_end"] = round(saved["slice_end"] + gop, 3)
saved["words"] = [[w, round(a - gop, 3), round(b - gop, 3)] for w, a, b in saved["words"]]
saved.pop("slice_checked")
saved.pop("trim", None)
manifest_path.write_text(json.dumps(manifest))
trimmed = PF.rerender_trim("sync1", 1, clip_start + 1.0, clip_start + 18.0, settings)
repaired = json.loads(manifest_path.read_text())["clips"][0]
ok, detail = in_sync(trimmed)
check("A pitch made before the fix is repaired on its next trim (in sync)", ok, detail)
check("...its saved slice start is corrected and marked checked", abs(repaired["slice_start"] - (saved["slice_start"] - gop)) <= 0.011
      and repaired["slice_checked"], f"{repaired['slice_start']}")

from core.clipper import rerender_clip  # noqa: E402
made = rerender_clip(source, clip_start - 2.0, clip_start + 16.0, words, tmp / "made_trim.mp4", layout_mode="blurred_background",
                     cached_crop=first.crop, hook_seconds=0.0, highlight_active_word=False)
ok, detail = in_sync(made)
check("Make clips trim: captions on the beat", ok, detail)

# ======================= 1. Trim keeps the pin and the webinar layout (Outbox too) =======================
import app as A  # noqa: E402
link = tmp / "link"; link.symlink_to(tmp)
check("Clip paths spelled through a symlinked folder find the same saved framing",
      A._clip_key(link / "first.mp4") == A._clip_key(tmp / "first.mp4"))
calls = []
real_render = P.render_selected_clip
P.render_selected_clip = lambda *a, **k: calls.append(k) or real_render(*a, **{x: y for x, y in k.items() if x not in {"pin_title", "webinar_layout"}})
pinned = dataclasses.replace(first, pin_title=True)
PF.save_pitch_files("pin1", source, [pinned], words, 90.0)
PF.rerender_trim("pin1", 1, clip_start - 1.0, clip_start + 17.0, settings)
check("Outbox trim of a pinned clip renders pinned again", calls and calls[-1].get("pin_title") is True, str(calls[-1:]))
P.render_selected_clip = real_render

# ======================= 3. Audio: cleanup, -14 LUFS, -1 dBTP =======================
import core.video_renderer as V  # noqa: E402
chain = V._audio_filter(FFMPEG, source, clip_start, 10)
check("Audio chain: high-pass and denoiser before two-pass loudnorm at -14 LUFS",
      chain.index("highpass=f=80") < chain.index("afftdn") < chain.index("loudnorm=I=-14") and "measured_I=" in chain, chain[:160])
check("Loudnorm aims 0.3 dB under -1.5 dBTP (AAC adds about 0.2 dB; Round 36 broadcast ceiling)", "TP=-1.8" in chain)
os.environ["AUDIO_CLEANUP"] = "off"
check("AUDIO_CLEANUP=off: no cleanup", "afftdn" not in V._audio_filter(FFMPEG, source, clip_start, 10))
os.environ["AUDIO_CLEANUP"] = ""
stats = subprocess.run([FFMPEG, "-nostats", "-i", str(trimmed), "-map", "0:a", "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
summary = stats[stats.rfind("Summary"):]
loud = float(summary.split("I:")[1].split("LUFS")[0]); peak = float(summary.split("Peak:")[1].split("dBFS")[0])
check("Delivered clip: -14 LUFS and under -1.0 dBTP", abs(loud + 14) <= 1.0 and peak <= -1.0, f"{loud} LUFS, {peak} dBTP")

noisy = tmp / "noisy.mp4"
subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=640x360:r=30:d=12",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
                "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.03:r=48000:d=12",
                "-f", "lavfi", "-i", "anoisesrc=color=brown:amplitude=0.08:r=48000:d=12",
                "-filter_complex", "[1:a]volume='if(lt(mod(t,2),1),0.3,0)':eval=frame[s];[3:a]lowpass=f=70[r];"
                "[s][2:a][r]amix=inputs=3:normalize=0[a]", "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset",
                "ultrafast", "-c:a", "aac", "-shortest", str(noisy)], check=True)

def tone_to_noise(path):
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, np.float32).astype(float)
    sec = 48000
    tone = np.concatenate([x[i * sec + 4800:(i + 1) * sec - 4800] for i in range(2, 10, 2)])
    gaps = np.concatenate([x[i * sec + 4800:(i + 1) * sec - 4800] for i in range(3, 10, 2)])
    return 10 * np.log10((tone ** 2).mean() / (gaps ** 2).mean())

cleanup = {}
from core.subtitle_engine import create_ass_subtitles  # noqa: E402
empty_ass = create_ass_subtitles([Word("x", 0.1, 0.2)], tmp / "empty.ass", clip_duration=12.0)
for mode in ("off", ""):
    os.environ["AUDIO_CLEANUP"] = mode
    out = tmp / f"noisy_{mode or 'on'}.mp4"
    V.render_clip(noisy, empty_ass, V.CropBox(0, 0, 640, 360), 0.0, 12.0, out, layout_mode="blurred_background")
    cleanup[mode or "on"] = tone_to_noise(out)
os.environ["AUDIO_CLEANUP"] = ""
check("Noisy recording: voice-to-noise improves by 4+ dB (rumble and fan hiss)",
      cleanup["on"] - cleanup["off"] >= 4, f"{cleanup['off']:.1f} dB -> {cleanup['on']:.1f} dB")

# ======================= 3. Sharper scaling =======================
check("Sharpening: 0.8 from 1.4x up (unsharp=5:5:0.8), lighter below, none when shrinking",
      V._sharpen(1.78) == ",unsharp=5:5:0.8:5:5:0.0" and V._sharpen(1.2) == ",unsharp=5:5:0.4:5:5:0.0" and V._sharpen(0.9) == "")
graphs = {}
real_run = V.subprocess.run
def capture(command, *a, **k):
    if "-filter_complex" in command and "-f" not in command[:6]:
        graphs[current] = command[command.index("-filter_complex") + 1]
        Path(command[-1] if Path(command[-1]).is_absolute() else Path(k.get("cwd", ".")) / command[-1]).write_bytes(b"x")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return real_run(command, *a, **k)
V.subprocess.run = capture
for current in ("blurred_background", "wide_blurred_background", "stacked", "smart_crop"):
    V.render_clip(source, empty_ass, V.CropBox(440, 0, 404, 720), 10.0, 14.0, tmp / f"g_{current}.mp4", layout_mode=current,
                  full_frame_ranges=[(0.0, 2.0)] if current == "smart_crop" else None)
V.subprocess.run = real_run
plain = {name: [part for part in graph.split(",") if part.startswith("scale=") or "]scale=" in part] for name, graph in graphs.items()}
picture = {name: [p for p in parts if "270:480" not in p and "1080:1920[bg]" not in p and "scale=1080:1920" != p.split("]")[-1]]
           for name, parts in plain.items()}
check("Every picture scale uses lanczos (only the blurred background doesn't)",
      all(all("lanczos" in p for p in parts) for parts in picture.values()) and len(graphs) == 4, json.dumps(picture)[:400])
check("Enlarged pictures get matched sharpening (720p source: face crop 2.7x, stacked face panel)",
      "unsharp=5:5:0.8" in graphs["smart_crop"] and graphs["stacked"].count("unsharp") >= 1, graphs["stacked"][:300])

# ======================= 4. Webinar: slide shape and divider =======================
import core.webinar as WB  # noqa: E402
slides = tmp / "slides.mp4"
subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xF0F0F0:s=1920x1080:r=30:d=6",
                "-vf", "drawbox=x=0:y=0:w=iw:h=140:color=0x123460:t=fill", "-c:v", "libx264", "-preset", "ultrafast", str(slides)], check=True)
WB._face_detector = lambda: SimpleNamespace(available=True, detect=lambda frame: [])
four = WB.plan_webinar(slides, 0.5, 5.5, "auto", slides="4:3")
check("Slides 4:3: a centered 4:3 area of the frame", (four.content.width, four.content.height) == (1440, 1080) and four.content.x == 240,
      str(four.content))
check("...scaled evenly, never stretched", abs(four.content_width / four.content_height - 4 / 3) < 0.01, f"{four.content_width}x{four.content_height}")
wide = WB.plan_webinar(slides, 0.5, 5.5, "auto", slides="16:9")
check("Slides 16:9: the whole frame", (wide.content.width, wide.content.height) == (1920, 1080) and (wide.content_width, wide.content_height) == (1080, 606))
tall = WB._slide_shape(WB.CropBox(0, 0, 1000, 1000), "16:9")
check("A narrower area is cropped in height instead (still exact)", tall.width == 1000 and abs(tall.width / tall.height - 16 / 9) < 0.01, str(tall))
accent = WB.plan_webinar(slides, 0.5, 5.5, divider="accent", accent="ff3366")
gap = WB.plan_webinar(slides, 0.5, 5.5, divider="gap")
shadow = WB.plan_webinar(slides, 0.5, 5.5, divider="shadow")
check("Divider styles: accent color line, soft shadow, dark gap", "color=0xFF3366@1.0" in accent.filter_graph()
      and shadow.filter_graph().count("drawbox") == len(WB.SHADOW_STEPS) and "drawbox" not in gap.filter_graph())
check("The gap moves the slides, title and captions down 24 px", gap.content_top == 484 and gap.title_top == wide.title_top + 24
      and gap.caption_top == wide.caption_top + 24)
check("Bad accent colors fall back to the default", WB.clean_accent("blue") == WB.DEFAULT_ACCENT and WB.clean_accent("#a1b2c3") == "#A1B2C3")
check("The layout is saved and restored exactly (Outbox trims)", WB.layout_from_dict(WB.layout_to_dict(accent)) == accent)
render = P.render_selected_clip(slides, ClipResult(start_time=0.5, end_time=5.5, viral_title="", caption="", hashtags=[], virality_score=1),
                                [Word("Hi.", 1.0, 1.4)], settings, tmp / "accent.mp4", layout_mode="webinar",
                                webinar_options={"slides": "4:3", "divider": "accent", "accent": "#FF3366"})
raw = subprocess.run([FFMPEG, "-v", "error", "-ss", "2", "-i", str(render.path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                     capture_output=True).stdout
frame = np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3).astype(int)
line = frame[458:462, 100:980].reshape(-1, 3).mean(axis=0)
check("Rendered: the accent line is that color", abs(line[0] - 255) < 30 and line[1] < 90 and abs(line[2] - 102) < 40, str(line.round()))
check("Rendered: the 4:3 slide is centered with dark sides", frame[700, 40].mean() < 30 and frame[700, 540].mean() > 200)

# ======================= 5. Outbox: cut-point words and poster frame =======================
item = {"slice_start": 100.0, "slice_end": 160.0, "start": 110.0, "end": 140.0,
        "words": [["So", 9.0, 9.3], ["the", 9.4, 9.6], ["absolutely", 10.0, 10.8], ["key", 10.9, 11.2],
                  ["point.", 39.0, 39.5], ["Next", 40.2, 40.6]]}
report = PF.cut_report(item, 110.3, 139.2)
check("Cut words: what the clip opens and ends on", report["opens"].startswith("absolutely") and report["closes"].endswith("point."), str(report))
check("A cut inside a word is named, and snapping takes the whole word",
      report["start_word"] == "absolutely" and abs(report["snap_start"] - 109.95) < 1e-6
      and report["end_word"] == "point." and abs(report["snap_end"] - 139.55) < 1e-6, str(report))
clean = PF.cut_report(item, 109.8, 139.8)
check("Cuts between words: nothing to fix", clean["start_word"] is None and clean["end_word"] is None and clean["snap_start"] == 109.8)
check("A picked poster stays on the same moment after a trim", PF.poster_after_trim({"poster_at": 3.0, "start": 110.0}, 108.0, 140.0) == 5.0
      and PF.poster_after_trim({"poster_at": 1.0, "start": 110.0}, 112.0, 140.0) is None
      and PF.poster_after_trim({"start": 110.0}, 108.0, 140.0) is None)

# The automatic pick: a 'face' whose eyes are open (busy) only from 2.0 to 2.6 s.
eyes = tmp / "eyes.mp4"
subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xC8A080:s=1080x1920:r=30:d=5",
                "-vf", "drawbox=x=360:y=600:w=360:h=40:color=0xB08868:t=fill,"
                "drawbox=x=380:y=600:w=40:h=40:color=black:t=fill:enable='between(t,2.0,2.6)',"
                "drawbox=x=660:y=600:w=40:h=40:color=black:t=fill:enable='between(t,2.0,2.6)',"
                "drawbox=x=420:y=600:w=30:h=40:color=white:t=fill:enable='between(t,2.0,2.6)',"
                "drawbox=x=630:y=600:w=30:h=40:color=white:t=fill:enable='between(t,2.0,2.6)'",
                "-c:v", "libx264", "-preset", "ultrafast", str(eyes)], check=True)
import core.poster as PO  # noqa: E402
scale = 640 / 1080
fake_face = (540 * scale, 700 * scale, 400 * scale, 480 * scale, 0.95, (420 * scale, 620 * scale), (660 * scale, 620 * scale))
PO._face_detector = lambda: SimpleNamespace(faces_with_eyes=lambda frame: [fake_face])
best = PO.best_poster_time(eyes)
check("Poster: picks a frame with the eyes open, not a blink", 2.0 <= best <= 2.6, f"{best} s")
PO._face_detector = lambda: SimpleNamespace(faces_with_eyes=lambda frame: [])
check("No face: the old 1.2 s", PO.best_poster_time(eyes) == PO.DEFAULT_POSTER_AT)
check("Poster image: a JPEG of that frame", PO.poster_jpeg(eyes, 2.3)[:2] == b"\xff\xd8")

# The Outbox, with a real pitch: words at the cut, snap, poster picker.
from streamlit.testing.v1 import AppTest  # noqa: E402
import autonomous_worker as W  # noqa: E402
store = tmp / "store"; store.mkdir()
PF.PITCHES_DIR = store / "pitches"
PF.save_pitch_files("ui1", source, [first], words, 90.0)
W._write_json_atomic(store / "pending.json", [{
    "record_id": "ui1", "channel_id": "UCabcdefghijklmnopqrstuv", "channel_title": "Beep Pod", "contact_email": "a@b.co",
    "page_name": "beep-pod-abc123", "page_url": "https://clips.example.com/beep-pod-abc123", "subject": "3 Shorts",
    "full_email_body": "Hi,\n\nBody", "status": "pending", "clips": [{"start": clip_start, "end": clip_start + 20, "judge": 80}]}])
log = tmp / "poster_calls.json"
wrapper = tmp / "outbox_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys, json; sys.path.insert(0, {str(ROOT)!r})
    from pathlib import Path
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(store)!r})
    for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, name, W.STORAGE_DIR / file)
    import core.pitch_files as P, core.clip_pages as C, core.poster as PO
    P.PITCHES_DIR = W.STORAGE_DIR / "pitches"
    PO.best_poster_time = lambda clip, window=None: 2.5
    def replace_poster(name, index, clip, at):
        Path({str(log)!r}).write_text(json.dumps({{"name": name, "index": index, "at": at}})); return at
    C.replace_poster = replace_poster
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
at = AppTest.from_file(str(wrapper), default_timeout=120).run()
check("Outbox runs with the new panels", not at.exception, str([e.value for e in at.exception]))
start_input = next(n for n in at.number_input if n.key == "trim_start_ui1_1")
mid_word = next(b for b in beeps if b > clip_start + 1) + 0.15 - clip_start  # inside a word
start_input.set_value(round(mid_word, 2)).run()
texts = [m.value for m in at.markdown] + [c.value for c in at.caption]
check("Trim: the words at each end of the cut", any(t.startswith("Opens on “W") for t in texts), str([t for t in texts if "Opens" in t]))
check("Trim: a cut inside a word is flagged", any("cuts into" in t for t in texts))
next(b for b in at.button if b.key == "trim_snapgo_ui1_1").click().run()
snapped = next(n for n in at.number_input if n.key == "trim_start_ui1_1").value
word_start = next(b for b in beeps if b > clip_start + 1) - clip_start
check("Snap to whole words: the start moves to just before that word", abs(snapped - (word_start - 0.05)) < 0.011, f"{snapped} vs {word_start - 0.05:.3f}")
check("Poster panel reads nothing until it's opened", "poster_at_ui1_1" not in at.session_state)
at.session_state["poster_open_ui1_1"] = True
at.run()
slider = next((s for s in at.slider if s.key == "poster_at_ui1_1"), None)
check("Poster panel: a frame slider starting on the automatic pick", slider is not None and abs(slider.value - 2.5) < 1e-6,
      str(slider.value if slider else None))
texts = [c.value for c in at.caption]
check("...with the still shown (and what their page shows now)", len(at.get("image")) >= 1
      and any("Their page shows the frame at 2.5 s" in t for t in texts) and not any("Couldn't show" in t for t in texts),
      str([t for t in texts if "frame" in t.lower()]))
slider.set_value(4.2).run()
next(b for b in at.button if b.key == "poster_use_ui1_1").click().run()
sent = json.loads(log.read_text()) if log.exists() else {}
saved_at = PF.load_pitch("ui1")["clips"][0].get("poster_at")
check("Use this frame: their page gets that poster, and it's remembered", sent == {"name": "beep-pod-abc123", "index": 1, "at": 4.2}
      and saved_at == 4.2, f"{sent} / {saved_at}")

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
