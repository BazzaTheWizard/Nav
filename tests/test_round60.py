"""Round 60: Narrate Phase 2, the action-following crop (core.narrate.reframe) and the "tall" /
"vertical" frames in core.narrate.assemble, plus the rotating pop-up style.

Run: timeout 900 .venv/bin/python -u tests/test_round60.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k, v in {"NAV_NO_CLIP_PAGES": "1", "LINKEDIN_POSTS": "0", "AUTO_GRAPHICS": "0", "VISUAL_OVERLAYS": "none",
             "ENABLE_CLIMAX_HOOK": "0", "MIDCLIP_TRIM": "0", "CAPTION_STYLE": "bold"}.items():
    os.environ[k] = v
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


from core.narrate import align as A  # noqa: E402
from core.narrate import reframe as R  # noqa: E402
from core.narrate import script as S  # noqa: E402
from core.narrate.assemble import assemble  # noqa: E402
from core.narrate.finish import POPUP_STYLES, style_for  # noqa: E402
from core.narrate.library import Library  # noqa: E402
from core.transcriber import Word  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="nav-r60-"))


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def probe(path: Path) -> tuple[str, float]:
    size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
                          str(path)], capture_output=True, text=True).stdout.strip()
    return size, float(dur)


def mover(path: Path, x: str, seconds: int = 8) -> None:
    """A dark textured scene with a bright box moving around x (an FFmpeg expression)."""
    ffmpeg("-f", "lavfi", "-i", f"color=c=0x303030:s=1280x720:r=30:d={seconds}", "-f", "lavfi", "-i",
           f"color=c=white:s=110x110:r=30:d={seconds}", "-filter_complex",
           f"[0:v]noise=alls=6:allf=t[bg];[bg][1:v]overlay=x='{x}':y='300+180*sin(2.3*t)'",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path))


right, left, still = TMP / "right.mp4", TMP / "left.mp4", TMP / "still.mp4"
mover(right, "1060+60*sin(3*t)")
mover(left, "80+60*sin(3*t)")
ffmpeg("-f", "lavfi", "-i", "color=c=0x303030:s=1280x720:r=30:d=8", "-vf", "noise=alls=6:allf=t",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", str(still))

tall = 1080 / 1350
tr = R.track(str(right), 1.0, 3.0, tall)
tl = R.track(str(left), 1.0, 3.0, tall)
ts = R.track(str(still), 1.0, 3.0, tall)
check("track: action on the right -> crop right of centre", tr.start > 0.6 and tr.end > 0.6, tr)
check("track: action on the left -> crop left of centre", tl.start < 0.4 and tl.end < 0.4, tl)
check("track: no action -> centred", ts == R.Track(0.5, 0.5), ts)
share = 720 * tall / 1280
check("track: the crop never leaves the frame", all(share / 2 - 1e-6 <= c <= 1 - share / 2 + 1e-6
                                                     for c in (tr.start, tr.end, tl.start, tl.end)))
check("track: a full-width crop is always centred", R.track(str(right), 1.0, 3.0, 16 / 9) == R.Track(0.5, 0.5))
check("track: the pan speed is limited", abs(tr.end - tr.start) <= R.MAX_PAN * 3.0 + 1e-6, tr)
f = R.crop_filter(str(right), R.Track(0.7, 0.72), "tall", 3.0)
check("crop filter: crops 4:5 and scales to 1080x1350", f.startswith("crop=576:720:") and "scale=1080:1350" in f, f)
fz = R.crop_filter(str(right), R.Track(0.5, 0.5), "vertical", 3.0, zoom=1.12)
check("crop filter: zoom crops tighter", fz.startswith("crop=") and int(fz.split("=")[1].split(":")[1]) < 720, fz)

# assemble in each frame
foot = TMP / "footage" / "city_night"
foot.mkdir(parents=True)
shutil.copy(right, foot / "a.mp4")
shutil.copy(left, foot / "b.mp4")
lib = Library(TMP / "lib.sqlite")
lib.ingest(TMP / "footage")
script = S.parse("HOOK: [city, night] One. Two three.\n[ZOOM] Four five six.\nSeven eight nine ten.")
words, t = [], 0.2
for w, _ in A.script_words(script):
    words.append(Word(text=w, start=round(t, 2), end=round(t + 0.3, 2)))
    t += 0.45
spans = A.line_spans(script, words)
voice = TMP / "voice.wav"
ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:duration={spans[-1].end + 0.5:.2f}", "-ar", "48000", str(voice))
for frame, size in (("tall", "1080,1350"), ("vertical", "1080,1920"), ("wide", "1920,1080")):
    made = assemble(script, spans, lib, voice, TMP / f"as_{frame}.mp4", TMP, frame=frame)
    got, dur = probe(made.video)
    check(f"assemble {frame}: {size}, as long as the voiceover", got == size and abs(dur - spans[-1].end) < 0.15,
          (got, dur, spans[-1].end))
    if frame != "wide":
        check(f"assemble {frame}: pieces carry their crop centres",
              all(0.0 < p.centre[0] < 1.0 for p in made.pieces) and any(p.centre != (0.5, 0.5) for p in made.pieces),
              [p.centre for p in made.pieces])
try:
    assemble(script, spans, lib, voice, TMP / "bad.mp4", TMP, frame="square")
    check("assemble: an unknown frame is refused", False)
except ValueError:
    check("assemble: an unknown frame is refused", True)

# the pipeline finishes a tall assembly into a 9:16 clip with the gameplay large on screen
try:
    from config import load_settings
    from core.hook_selector import ClipResult
    from core.pipeline import render_selected_clip

    made = assemble(script, spans, lib, voice, TMP / "as_pipe.mp4", TMP, frame="tall")
    clip = ClipResult(start_time=0.0, end_time=made.duration, viral_title=script.hook, caption=script.hook,
                      hashtags=[], virality_score=80)
    rendered = render_selected_clip(made.video, clip, words, load_settings(), TMP / "clip.mp4",
                                    layout_mode="blurred_background", work_dir=TMP / "work", title_fade_in=False,
                                    climax_hook_on=False)
    got, _ = probe(rendered.path)
    check("pipeline: tall assembly -> 1080x1920 clip", got == "1080,1920", got)
except Exception as exc:  # noqa: BLE001
    check("pipeline: tall assembly -> 1080x1920 clip", False, repr(exc))

# pop-up styles rotate between videos, stable per video
names = [f"script_{i}" for i in range(40)]
check("pop-up style: the same name always gets the same style", style_for("garage") == style_for("garage"))
check("pop-up style: names spread over every style", {style_for(n) for n in names} == set(range(len(POPUP_STYLES))))

if not os.getenv("KEEP_R60"):
    shutil.rmtree(TMP, ignore_errors=True)
else:
    print("kept", TMP)
print(f"\n{'ALL PASSED' if not FAILED else f'{len(FAILED)} FAILED: ' + ', '.join(FAILED)}")
sys.exit(1 if FAILED else 0)
