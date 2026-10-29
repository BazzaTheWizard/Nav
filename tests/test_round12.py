"""Checks for round 12: sharper sources, consistent loudness, Rec. 709 color tags,
matched sharpening, delivery bitrate, bolder captions.
Run all tests: .venv/bin/python tests/run_all.py"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
for name in ("SOURCE_MAX_HEIGHT", "AUDIO_TARGET_LUFS", "CAPTION_STYLE"):
    os.environ.pop(name, None)

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

import core.downloader as D
import core.subtitle_engine as S
import core.video_renderer as V
from core.face_tracker import CropBox, CropPlan, Shot
from core.transcriber import Word

tmp = Path(tempfile.mkdtemp(prefix="r12-"))

# ---------- Source: best VP9 up to 1440p ----------
# Round 14: "proto" added (regular downloads before YouTube's chunked HLS streams).
check("Default: up to 1440p, VP9 preferred", D.video_format_sort() == ["res:1440", "vcodec:vp9", "proto", "ext:mp4:m4a"])
os.environ["SOURCE_MAX_HEIGHT"] = "2160"
check("SOURCE_MAX_HEIGHT=2160 allows 4K", D.video_format_sort()[0] == "res:2160")
os.environ["SOURCE_MAX_HEIGHT"] = "99999"
check("Out-of-range values are clamped", D.video_format_sort()[0] == "res:2160")
os.environ["SOURCE_MAX_HEIGHT"] = "abc"
check("Bad values fall back to 1440", D.video_format_sort()[0] == "res:1440")
os.environ.pop("SOURCE_MAX_HEIGHT")

import yt_dlp  # noqa: E402


def pick(formats):
    ydl = yt_dlp.YoutubeDL({"format": D.VIDEO_FORMAT, "format_sort": D.video_format_sort(), "quiet": True, "simulate": True})
    info = {"id": "x", "title": "t", "formats": formats, "extractor": "generic", "extractor_key": "Generic", "webpage_url": "http://x"}
    return ydl.process_ie_result(info, download=False).get("format_id")


def fmt(fid, h, vcodec, ext, tbr, dynamic_range="SDR"):
    return {"format_id": fid, "height": h, "width": h * 16 // 9, "vcodec": vcodec, "acodec": "none", "ext": ext,
            "url": f"http://x/{fid}", "protocol": "https", "tbr": tbr, "dynamic_range": dynamic_range}


audio = {"format_id": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "url": "http://x/a", "protocol": "https", "tbr": 128, "abr": 128}
youtube_like = [fmt("h264-1080", 1080, "avc1.640028", "mp4", 1200), fmt("vp9-1080", 1080, "vp9", "webm", 1500),
                fmt("vp9-1440", 1440, "vp9", "webm", 5000), fmt("av1-1440", 1440, "av01.0.12M.08", "mp4", 4000),
                fmt("vp9-2160", 2160, "vp9", "webm", 12000), fmt("hdr-1440", 1440, "vp09.02.51.10", "webm", 7000, "HDR10"),
                dict(audio)]
choice = pick(youtube_like)
check("Picks 1440p VP9 (not 4K, not AV1, not HDR, not 1080p H.264)", choice == "vp9-1440+m4a", choice)
check("No 1440p -> VP9 1080p over H.264 1080p",
      pick([youtube_like[0], youtube_like[1], dict(audio)]) == "vp9-1080+m4a")
check("Only H.264 and AV1 -> H.264 (AV1 is slow to decode)",
      pick([fmt("h264-1080", 1080, "avc1.640028", "mp4", 1200), fmt("av1-1080", 1080, "av01.0.08M.08", "mp4", 2000), dict(audio)]) == "h264-1080+m4a")
os.environ["SOURCE_MAX_HEIGHT"] = "2160"
check("With SOURCE_MAX_HEIGHT=2160 -> 4K VP9", pick(youtube_like) == "vp9-2160+m4a")
os.environ.pop("SOURCE_MAX_HEIGHT")

# ---------- Encoder and sharpening ----------
check("Sharpening matched to the enlargement: 1080p crop (1.78x) as before",
      V._sharpen(1920 / 1078) == ",unsharp=5:5:0.7:5:5:0.0", V._sharpen(1920 / 1078))
check("1440p crop (1.33x): lighter", V._sharpen(1920 / 1440) == ",unsharp=5:5:0.3:5:5:0.0", V._sharpen(1920 / 1440))
check("4K crop (downscaled): none", V._sharpen(1920 / 2160) == "")
real_run = V.subprocess.run
V.subprocess.run = lambda *a, **k: type("R", (), {"stdout": " V..... h264_videotoolbox VideoToolbox H.264 Encoder"})()
mac = V._video_encoder_args("ffmpeg-mac-test")
V.subprocess.run = real_run
check("Mac hardware encoder at 10 Mbps, High profile", mac[:4] == ("-c:v", "h264_videotoolbox", "-b:v", "10M") and "high" in mac, str(mac))
check("Software fallback: CRF 18, High profile", V._video_encoder_args("ffmpeg-missing-test")[:6] == ("-c:v", "libx264", "-preset", "veryfast", "-crf", "18"))

# ---------- Loudness and color: real renders ----------
src = tmp / "quiet.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=6",
                "-f", "lavfi", "-i", "sine=frequency=330:duration=6,volume=0.7", "-shortest", "-pix_fmt", "yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src)], check=True)
ass = S.create_ass_subtitles([Word("hello", 0.1, 0.5), Word("there.", 0.6, 1.2)], tmp / "s.ass", clip_duration=5)


def loudness(path):
    out = subprocess.run(["ffmpeg", "-nostats", "-hide_banner", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    lines = [l.split() for l in out.splitlines() if l.strip().startswith(("I:", "Peak:"))]
    return float(lines[-2][1]), float(lines[-1][1])


def probe(path, stream, entries):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", f"stream={entries}",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout
    return json.loads(out)["streams"][0]


before, _ = loudness(src)
pair = ((389.0, 523.0, 265.0), (1634.0, 420.0, 180.0))
plan = CropPlan(1920, 1080, 606, 1080, 0, (Shot(0, 5, 600, True, True, pair),))
out = V.render_clip(src, ass, plan.crop, 0.5, 5.5, tmp / "out.mp4", layout_mode="smart_crop", split_view=plan.split_view(0.5, 5.5))
level, peak = loudness(out)
check("Quiet clip raised to about -14 LUFS", abs(level + 14) <= 1.0 and before < -22, f"{before} -> {level} LUFS")
check("Peaks stay below -1 dBTP", peak <= -1.0, f"{peak} dBTP")
check("Audio at 48 kHz", probe(out, "a:0", "sample_rate")["sample_rate"] == "48000")
color = probe(out, "v:0", "color_space,color_primaries,color_transfer")
check("Split view now tagged Rec. 709 (it lost its tags before)",
      color == {"color_space": "bt709", "color_primaries": "bt709", "color_transfer": "bt709"}, str(color))
os.environ["AUDIO_TARGET_LUFS"] = "off"
check("AUDIO_TARGET_LUFS=off: no loudness change", "loudnorm" not in V._audio_filter("ffmpeg", src, 0, 5))
os.environ["AUDIO_TARGET_LUFS"] = "-16"
check("AUDIO_TARGET_LUFS=-16 is used", "loudnorm=I=-16.0" in V._audio_filter("ffmpeg", src, 0, 5))
os.environ.pop("AUDIO_TARGET_LUFS")
silent = tmp / "silent.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3", str(silent)], check=True)
check("Silence can't be measured -> left alone", V._audio_filter("ffmpeg", silent, 0, 3) == "aresample=async=1:first_pts=0")
whisper = tmp / "whisper.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=4,volume=0.02", str(whisper)], check=True)
check("Nearly silent clip isn't boosted 30+ dB (that would only raise noise)", "loudnorm" not in V._audio_filter("ffmpeg", whisper, 0, 4))
captured = {}
V._source_color = lambda path: {"color_transfer": "smpte2084"}
V.subprocess.run = lambda cmd, **k: captured.update(cmd=cmd) or type("R", (), {"stdout": "", "stderr": ""})()
V.render_clip(src, ass, CropBox(500, 0, 608, 1080), 0, 5, tmp / "hdr.mp4", layout_mode="smart_crop")
V.subprocess.run = real_run
graph = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
check("HDR source isn't mislabeled as Rec. 709", "setparams" not in graph and "-colorspace" not in captured["cmd"])

# ---------- Captions ----------
words = [Word("the", 0.0, 0.3), Word("real", 0.3, 0.6), Word("question", 0.6, 1.0), Word("is.", 1.0, 1.4), Word("Growth.", 2.0, 2.5)]
bold = S.create_ass_subtitles(words, tmp / "bold.ass", clip_duration=3).read_text()
style_line = next(l for l in bold.splitlines() if l.startswith("Style: ShortForm"))
fields = style_line.split(",")
check("Bold captions by default: thicker outline (6) and drop shadow (3)", fields[16] == "6" and fields[17] == "3", style_line)
# Round 13: the pulse grows the word's height and outline only (the line stays still).
check("Spoken word pulses (height and outline) and stays yellow",
      r"\c&H00FFFF&\fscy108\bord9\t(0,110,\fscy100\bord6)}QUESTION" in bold)
check("Soft edge kept after the spoken word", r"QUESTION{\r\blur0.6}" in bold)
check("One-word card pulses too", r"{\fscy108\bord9\t(0,110,\fscy100\bord6)}GROWTH." in bold)
check("Colors use the Rec. 709 matrix", "YCbCr Matrix: TV.709" in bold)
os.environ["CAPTION_STYLE"] = "classic"
classic = S.create_ass_subtitles(words, tmp / "classic.ass", clip_duration=3).read_text()
os.environ.pop("CAPTION_STYLE")
classic_fields = next(l for l in classic.splitlines() if l.startswith("Style: ShortForm")).split(",")
check("CAPTION_STYLE=classic brings the old look back", classic_fields[16] == "3" and classic_fields[17] == "1" and r"\fscx115" not in classic)
check("Seam captions still work with the new style",
      r"{\an5\pos(540,960)}" in S.create_ass_subtitles(words, tmp / "seam.ass", clip_duration=3, seam_spans=[(0, 3)]).read_text())

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
