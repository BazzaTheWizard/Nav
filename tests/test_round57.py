"""Round 57: clean/dirty deliverables and the project file (NYT Opinion Video asks, Nov 2026).

1. The clean (textless) copy: TEXTLESS_MASTER=1 renders every clip a second time with the same
   decisions, minus captions, title card, data cards and lower third, into <clip>_clean/ with the
   .srt, the dialogue as 24-bit WAV and the teaser's sound effect as its own stem. Same frame
   count as the dirty copy, so the .srt lines up on both. Off by default (it doubles render time).
2. The project file <clip>.project.json (core.project_state, schema nav.project/1): every time in
   source seconds; the output timeline only in timeline.segments; to_source/to_output map a review
   comment's output time to the source and back.

Run: timeout 600 .venv/bin/python -u tests/test_round57.py
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["NAV_NO_CLIP_PAGES"] = "1"
os.environ.pop("TEXTLESS_MASTER", None)
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


import numpy as np  # noqa: E402

from core import project_state as PS, textless  # noqa: E402
from core.climax_hook import Teaser  # noqa: E402

# =========================== 1. Time mapping (no rendering) ===========================
smap = PS.SourceMap(100.0, [(0.0, 5.0), (7.0, 10.0)])  # a 10 s clip at 100 s, 5-7 s cut out
check("Trimmed second 6 is source 108 (after the 2 s cut)", smap.point(6.0) == 108.0, smap.point(6.0))
check("A range across the cut splits in two", smap.ranges(4.0, 6.0) == [(104.0, 105.0, 4.0), (107.0, 108.0, 5.0)],
      smap.ranges(4.0, 6.0))
tz = Teaser(start=6.0, cut=7.0, frames=30, rate=30.0, hinge="", line="", score=0.0, reason="")
segs = PS.segments(smap, (0.0, 8.0), tz)
check("Teaser first, then the body in two pieces",
      [s["kind"] for s in segs] == ["teaser", "body", "body"], [(s["kind"], s["source_in"], s["source_out"]) for s in segs])
proj = {"schema": PS.SCHEMA, "timeline": {"segments": segs}, "output": {"duration": 9.0, "fps": 30.0},
        "layers": {"captions": {"words": [{"id": "w-1", "text": "hi", "start": 108.2, "end": 108.4}]}, "cards": []},
        "framing": {"camera": []}}
check("Segments tile 0-9 s with no gaps: valid", PS.validate(proj) == [], PS.validate(proj))
check("Body source 108.3 plays at output 7.3 (1 s teaser + 5 s + 1.3 s)", PS.to_output(proj, 108.3) == 7.3,
      PS.to_output(proj, 108.3))
check("...and output 7.3 maps back to source 108.3", PS.to_source(proj, 7.3)[0] == 108.3, PS.to_source(proj, 7.3))
check("A cut-out second has no output time", PS.to_output(proj, 106.0) is None)
check("Output 0.5 (the teaser, trimmed 6-7 s = source 108-109) is source 108.5", PS.to_source(proj, 0.5) == (108.5, segs[0]),
      PS.to_source(proj, 0.5))
broken = json.loads(json.dumps(proj))
broken["timeline"]["segments"][2]["output_in"] += 0.5
broken["layers"]["cards"] = [{"id": "w-1"}]
problems = PS.validate(broken)
check("validate() catches a gap and a duplicate id",
      any("gap" in p for p in problems) and any("duplicate" in p for p in problems), problems)

# =========================== 2. Switches ===========================
check("Clean copy is off by default", not textless.enabled())
os.environ["TEXTLESS_MASTER"] = "1"
check("...TEXTLESS_MASTER=1 turns it on", textless.enabled())
import core.video_renderer as V  # noqa: E402

with textless.textless_pass():
    master = V._video_encoder_args(shutil.which("ffmpeg") or "ffmpeg")
dirty_args = V._video_encoder_args(shutil.which("ffmpeg") or "ffmpeg")
check("During the clean pass: a keyframe every 15 frames", master[master.index("-g") + 1] == "15", master)
check("...and back to the dirty settings after it", dirty_args[dirty_args.index("-g") + 1] == "60", dirty_args)

# =========================== 3. Real renders ===========================
import core.pipeline as P  # noqa: E402
from config import load_settings  # noqa: E402
from core.hook_selector import ClipResult  # noqa: E402
from core.transcriber import Word  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
# .resolve(): on a Mac the temp folder (/var/folders/...) is a symlink to /private/var/..., and the
# renderer returns resolved paths, so compare against the resolved folder.
tmp = Path(tempfile.mkdtemp(prefix="r57_")).resolve()
src = tmp / "talk.mp4"  # a flat grey frame: any bright pixel in the caption band is a caption
subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x404040:size=1920x1080:rate=30:duration=20",
                "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=20", "-c:v", "libx264", "-preset",
                "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)], check=True)
words = [Word(f"point{i}." if i % 6 == 5 else f"point{i}", 0.3 + i * 0.4, 0.62 + i * 0.4) for i in range(45)]
settings = dataclasses.replace(load_settings(), hook_title_seconds=2.0, caption_highlight=False, temp_dir=tmp / "work")
clip = ClipResult(start_time=2.0, end_time=8.0, viral_title="Why the Margin Story Is Wrong", caption="",
                  hashtags=[], virality_score=90)


def frame(path: Path, t: float) -> np.ndarray:
    raw = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3)


out = tmp / "out"
out.mkdir()
made = P.render_selected_clip(src, clip, words, settings, out / "clip_r57_1.mp4", layout_mode="blurred_background",
                              title_fade_in=False, climax_hook_on=False)
c = made.clean
check("Clean copy made, in clip_r57_1_clean/", c is not None and c.folder == out / "clip_r57_1_clean", c)
if c is not None:
    check("...with the textless video, the .srt and 24-bit dialogue",
          c.video.is_file() and c.srt is not None and c.srt.is_file() and c.dialogue is not None and c.dialogue.is_file())
    check("Same frame count as the dirty copy (the .srt fits both)", c.matches, (c.frames, c.dirty_frames))
    check("The app's clip list still sees one clip", sorted(p.name for p in out.glob("clip_*.mp4")) == ["clip_r57_1.mp4"],
          sorted(p.name for p in out.glob("clip_*.mp4")))
    d, k = frame(made.path, 4.0), frame(c.video, 4.0)
    check("Dirty copy has captions (bright text in the frame)", d.max() > 200, int(d.max()))
    check("Clean copy has none: no bright pixel anywhere", k.max() < 150, int(k.max()))
    title_d = frame(made.path, 0.5)
    check("...and no title card at 0.5 s either", frame(c.video, 0.5).max() < 150 and title_d.max() > 200,
          (int(frame(c.video, 0.5).max()), int(title_d.max())))
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_fmt,bits_per_raw_sample",
                            "-of", "json", str(c.dialogue)], capture_output=True, text=True).stdout
    check("Dialogue WAV is 24-bit PCM", "pcm_s24le" in probe, probe)
    check("No sound-effect stem when nothing was added", not c.stems, c.stems)

pf = made.project_file
check("Project file next to the clip", pf is not None and pf == out / "clip_r57_1.project.json", pf)
if pf is not None:
    project = PS.load(pf)
    check("Project file is valid", PS.validate(project) == [], PS.validate(project) + project.get("notes", []))
    segs = project["timeline"]["segments"]
    check("One body segment from the clip's (tightened) start", len(segs) == 1 and segs[0]["kind"] == "body"
          and abs(segs[0]["source_in"] - made.clip.start_time) < 1e-6, segs)
    w = project["layers"]["captions"]["words"]
    check("Caption words are in source seconds (point5. said at 2.3 s)",
          any(x["text"] == "point5." and abs(x["start"] - 2.3) < 0.02 for x in w), w[:6])
    check("Deliverables listed, clean ones under the folder",
          project["deliverables"].get("dirty") == "clip_r57_1.mp4"
          and project["deliverables"].get("clean") == "clip_r57_1_clean/clip_r57_1_textless.mp4", project["deliverables"])
    check("Output frames recorded", project["output"]["frames"] == (c.dirty_frames if c else -1), project["output"])

# ------------------ with a climax teaser (forced) ------------------
# The teaser stitch needs FFmpeg 5.1+ (-fps_mode); an older FFmpeg can't render it at all.
NEW_FFMPEG = "fps_mode" in subprocess.run([FFMPEG, "-hide_banner", "-h", "long"], capture_output=True, text=True).stdout
if not NEW_FFMPEG:
    print("SKIP  Teaser checks: this FFmpeg is older than 5.1 (no -fps_mode); run them on the Mac")
real_find = P.find_teaser
P.find_teaser = lambda *a, **k: Teaser(start=6.0, cut=7.5, frames=45, rate=30.0, hinge="", line="", score=1.0,
                                       reason="test")
try:
    teased = None if not NEW_FFMPEG else P.render_selected_clip(src, clip, words, settings, out / "clip_r57_2.mp4",
                                    layout_mode="blurred_background", title_fade_in=False, climax_hook_on=True)
finally:
    P.find_teaser = real_find
if teased is not None:
    check("Teaser clip rendered with its teaser", teased.teaser is not None)
c2 = teased.clean if teased is not None else None
if teased is not None:
    check("Clean copy of the teaser clip: same frame count", c2 is not None and c2.matches,
          (c2.frames, c2.dirty_frames) if c2 else None)
from core import audio_polish  # noqa: E402

if c2 is not None and audio_polish.enabled():
    check("The teaser's hit is a separate sfx stem, not in the clean mix", "sfx" in c2.stems and c2.stems["sfx"].is_file(),
          c2.stems)
if teased is not None and teased.project_file is not None:
    project = PS.load(teased.project_file)
    kinds = [s["kind"] for s in project["timeline"]["segments"]]
    check("Project timeline: the teaser, then the body", kinds[0] == "teaser" and "body" in kinds, kinds)
    check("...valid, segments add up to the file's length", PS.validate(project) == [], PS.validate(project))
    body = next(s for s in project["timeline"]["segments"] if s["kind"] == "body")
    check("The body starts at output 1.5 s (45 teaser frames)", abs(body["output_in"] - 1.5) < 1e-6, body)
    check("A comment at output 3.0 s anchors to source start + 1.5 s",
          abs(PS.to_source(project, 3.0)[0] - (teased.clip.start_time + 1.5)) < 1e-6, PS.to_source(project, 3.0))

os.environ.pop("TEXTLESS_MASTER", None)
off = P.render_selected_clip(src, clip, words, settings, out / "clip_r57_3.mp4", layout_mode="blurred_background",
                             title_fade_in=False, climax_hook_on=False)
check("Off: no clean copy, the clip as before (project file still written)",
      off.clean is None and not (out / "clip_r57_3_clean").exists() and off.project_file is not None)

shutil.rmtree(tmp, ignore_errors=True)
print("\nAll checks passed" if not FAILED else f"\n{len(FAILED)} check(s) failed: {FAILED}")
sys.exit(1 if FAILED else 0)
