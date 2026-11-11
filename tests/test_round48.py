"""Round 48: no more blank grey clips, no breathing camera, and real close-ups.

1. The grey-screen bug: a face-tracked clip without a full-frame span was cropped to the 2x2
   placeholder (an `else` under the wrong `if` since Round 43). Rendered for real here.
2. The blank-frame guard (core.frame_guard) and the renderer's refusal of a placeholder crop.
3. A stage (people with bodies on a dark set with a banner) is not a slide; webcam tiles over a
   page still are; the explainer layout is for faceless slides only.
4. The executive camera holds still: no emphasis/payoff pushes, no 1.00x/1.06x toggling on the
   trimmed pauses, at most one opening drift; no drift on a letterbox; no creator title pill.
5. core.speaker_director: who's talking (aligned mouth motion), the cuts (establish, close-up on
   a 3.5 s turn, breathers, back-and-forth stays wide), the close-up geometry, and a real render
   that cuts from the two-shot to a close-up.
6. run_all pins the older suites to the old behaviour.

Run: timeout 180 .venv/bin/python tests/test_round48.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for key in ("VISUAL_OVERLAYS", "CAPTION_STYLE", "MIDCLIP_TRIM", "MIDCLIP_ZOOM", "EXEC_CAMERA", "EXEC_TITLE_CARD",
            "SPEAKER_CLOSEUPS", "FRAME_GUARD", "LEGACY_UI"):
    os.environ.pop(key, None)
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


import cv2  # noqa: E402
import numpy as np  # noqa: E402

from core import camera_path, executive, frame_guard, midclip, pipeline  # noqa: E402
from core import speaker_director as D  # noqa: E402
from core import video_renderer as R  # noqa: E402
from core.face_tracker import CropBox, CropPlan, Shot, SplitView, _content_like, on_stage  # noqa: E402
from core.transcriber import Word  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="round48_"))


def make_source(path: Path, seconds: float = 8.0, size: str = "1280x720") -> Path:
    """A textured test video with sound (a picture everywhere, like a real camera)."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=24:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}", "-shortest",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
                   check=True, capture_output=True)
    return path


def frames_of(path: Path, fps: float = 2.0) -> list[np.ndarray]:
    return frame_guard._frames(path, 0.0, 60.0, 135, 240)


SOURCE = make_source(TMP / "source.mp4")
WORDS = [Word(text=w, start=0.4 + 0.5 * i, end=0.8 + 0.5 * i) for i, w in enumerate(
    "So we have enormous scale just to make the graph run every day and it works.".split())]

# ============================== 1. The grey-screen bug, rendered ==============================
pipeline_src = (ROOT / "core" / "pipeline.py").read_text()
check("The placeholder crop belongs to the non-crop layouts, not to the reel's `if`",
      re.search(r"split = plan\.split_view\(start, end\)\n        else:\n            crop, x_expression = _PLACEHOLDER_CROP",
                pipeline_src) is not None)
one_face = CropPlan(frame_width=1280, frame_height=720, crop_width=404, crop_height=720, y=0,
                    shots=(Shot(start=0.0, end=8.0, x=438, has_face=True, group=False, speakers=None, moves=(),
                                content=False),), cuts=())
captured: dict = {}
real_render = pipeline.render_clip
real_plan = pipeline.analyze_crop_plan


def spy_render(*args, **kwargs):
    captured.setdefault("crops", []).append(args[2])
    return real_render(*args, **kwargs)


os.environ.update(MIDCLIP_TRIM="0", FRAME_GUARD="0")
pipeline.render_clip, pipeline.analyze_crop_plan = spy_render, (lambda *_a, **_k: one_face)
from config import load_settings  # noqa: E402
from core.hook_selector import ClipResult  # noqa: E402

settings = load_settings()
clip = ClipResult(start_time=0.0, end_time=7.5, viral_title="Scaling to 260K RPS", caption="", hashtags=[],
                  virality_score=80)
try:
    made = pipeline.render_selected_clip(SOURCE, clip, WORDS, settings, TMP / "face.mp4", layout_mode="smart_crop",
                                         work_dir=TMP / "work1", climax_hook_on=False)
    crops = captured.get("crops") or []
    check("A face-tracked clip without full-frame spans renders its real crop (404x720), not 2x2",
          made.layout_mode == "smart_crop" and crops and crops[0].width == 404 and crops[0].height == 720,
          [(c.width, c.height) for c in crops])
    check("...and its frames have a picture (none blank)",
          not any(frame_guard.is_blank(f) for f in frames_of(made.path)))
except Exception as exc:  # noqa: BLE001
    check("A face-tracked clip without full-frame spans renders its real crop (404x720), not 2x2", False, exc)
finally:
    pipeline.render_clip, pipeline.analyze_crop_plan = real_render, real_plan
    os.environ.pop("MIDCLIP_TRIM", None)
    os.environ.pop("FRAME_GUARD", None)

# ============================== 2. The guard ==============================
try:
    R.render_clip(SOURCE, TMP / "none.ass", CropBox(0, 0, 2, 2), 0.0, 2.0, TMP / "x.mp4", layout_mode="smart_crop")
    check("The renderer refuses a crop layout on a 2x2 placeholder crop", False)
except ValueError as exc:
    check("The renderer refuses a crop layout on a 2x2 placeholder crop", "real crop window" in str(exc), exc)
except Exception as exc:  # noqa: BLE001 - the .ass doesn't exist: the check must come first
    check("The renderer refuses a crop layout on a 2x2 placeholder crop", False, exc)
grey = TMP / "grey.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xCDD5D1:size=1080x1920:rate=24:duration=6",
                "-vf", "drawbox=x=60:y=1150:w=960:h=140:color=0x6e7270:t=fill,"
                       "drawtext=text='We are hitting around 250':x=300:y=1200:fontsize=40:fontcolor=white",
                "-pix_fmt", "yuv420p", str(grey)], check=True, capture_output=True)
spans = frame_guard.blank_spans(grey, SOURCE, 0.0, 6.0)
check("Guard: a flat grey render with captions, over a source with a picture, is blank", bool(spans) and spans[0][0] == 0.0
      and spans[-1][1] >= 5.5, spans)
check("...a render with a picture isn't", frame_guard.blank_spans(SOURCE, SOURCE, 0.0, 6.0) == [])
flat_source = TMP / "flat.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:size=1280x720:rate=24:duration=6",
                "-pix_fmt", "yuv420p", str(flat_source)], check=True, capture_output=True)
check("...nor one whose source is itself blank there (a fade, a black screen)",
      frame_guard.blank_spans(grey, flat_source, 0.0, 6.0) == [])
check("...nor spans shown full-frame over a blur on purpose", frame_guard.blank_spans(grey, SOURCE, 0.0, 6.0, skip=[(0.0, 6.0)]) == [])
check("The pipeline checks every crop-layout render and re-renders a blank one full-frame",
      "frame_guard.blank_spans(rendered, source_video, start, end, skip=list(full_frame))" in pipeline_src
      and 'layout, crop, x_expression = "blurred_background", _PLACEHOLDER_CROP, None' in pipeline_src)

# ============================== 3. A stage is not a slide ==============================
rng = np.random.default_rng(7)
stage = np.full((360, 640, 3), 18, np.uint8)  # a dark set
cv2.rectangle(stage, (250, 40), (380, 330), (40, 45, 45), -1)  # a banner
for row in range(120, 300, 30):
    cv2.putText(stage, "AGM 2026", (262, row), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (230, 230, 230), 1)
people = []
for cx in (140, 490):  # two people: a face, and a textured body (clothes, a chair) below it
    stage[60:115, cx - 25:cx + 25] = (rng.random((55, 50, 3)) * 120 + 80).astype(np.uint8)
    stage[115:340, cx - 50:cx + 50] = (rng.random((225, 100, 3)) * 200 + 30).astype(np.uint8)
    people.append((cx * 4.0, 0.0, 88 * 4.0, 25 * 4.0))
gray_stage = cv2.cvtColor(stage, cv2.COLOR_BGR2GRAY)
check("People with bodies under their faces are on a stage", on_stage(gray_stage, people, 4.0))
check("...so a dark set with a banner isn't read as a slide (Round 30 did)", not _content_like(stage, people, 4.0, 2560))
slide = np.full((360, 640, 3), 245, np.uint8)
for row in range(60, 300, 28):
    cv2.putText(slide, "Revenue grew 40% to $12M in Q3 2025", (120, row), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (20, 20, 20), 1)
tiles = [(30 * 3, 0.0, 60 * 3, 30 * 3), (610 * 3, 0.0, 60 * 3, 30 * 3)]
check("...while webcam tiles beside a page of text are still a slide", _content_like(slide, tiles, 3.0, 1920)
      and not on_stage(cv2.cvtColor(slide, cv2.COLOR_BGR2GRAY), tiles, 3.0))
from core import explainer  # noqa: E402

real_people = explainer._has_people
explainer._has_people = lambda frame: True
check("The explainer (slide) layout is never used with people on camera",
      explainer.explainer_fit(make_source(TMP / "wide.mp4", 4.0, "1920x1080"), 0.0, 4.0) is False)
explainer._has_people = real_people

# ============================== 4. A still camera ==============================
check("Executive mode: the camera holds still (EXEC_CAMERA=flow brings Round 42 back)", camera_path.steady())
os.environ["EXEC_CAMERA"] = "flow"
check("...EXEC_CAMERA=flow", not camera_path.steady())
os.environ.pop("EXEC_CAMERA")
os.environ["VISUAL_OVERLAYS"] = "full"
check("...the creator look keeps its flowing camera", not camera_path.steady())
os.environ.pop("VISUAL_OVERLAYS")
drift = camera_path.opening_drift(45.0, [])
check("One opening drift (1.00x -> 1.03x over 3 s), then held to the end: never released",
      len(drift) == 1 and drift[0].push == camera_path.OPENING_SECONDS and drift[0].release == 0.0
      and abs(camera_path.zoom_at(2.0, drift) - camera_path.zoom_at(44.0, drift)) > 0
      and camera_path.zoom_at(10.0, drift) == camera_path.zoom_at(44.0, drift) == 1.0 + camera_path.OPENING_DRIFT)
check("...none when the clip opens on a two-shot / close-up switch", camera_path.opening_drift(45.0, [(0.0, 45.0)]) == [])
check("The pipeline uses no emphasis, payoff or thesis pushes in the B2B modes",
      "steady = camera_path.steady(settings)" in pipeline_src
      and pipeline_src.index("if steady:") < pipeline_src.index("elif flowing:"))
check("A letterboxed or slide clip's cold open doesn't drift; a framed one does",
      pipeline.teaser_camera(3.5, "blurred_background") == {} and pipeline.teaser_camera(3.5, "explainer") == {}
      and pipeline.teaser_camera(3.5, "smart_crop")["camera"][0].kind == "teaser")
trim = midclip.plan([Word(text=w, start=s, end=s + 0.3) for w, s in
                     (("one", 0.2), ("two", 0.6), ("three", 2.4), ("four", 2.8), ("five", 5.0), ("six", 5.4))], 6.0, None)
check("Trimmed pauses are plain cuts: no 1.00x / 1.06x toggling", trim.cuts and set(trim.zooms) == {1.0}, trim.zooms)
os.environ["MIDCLIP_ZOOM"] = "1"
trim = midclip.plan([Word(text=w, start=s, end=s + 0.3) for w, s in
                     (("one", 0.2), ("two", 0.6), ("three", 2.4), ("four", 2.8), ("five", 5.0), ("six", 5.4))], 6.0, None)
check("...MIDCLIP_ZOOM=1 brings Round 46's back", midclip.ZOOM in trim.zooms)
os.environ.pop("MIDCLIP_ZOOM")
check("No creator title pill in the executive look", not executive.title_card_on())
os.environ["EXEC_TITLE_CARD"] = "1"
check("...EXEC_TITLE_CARD=1 brings it back", executive.title_card_on())
os.environ.pop("EXEC_TITLE_CARD")

# ============================== 5. The director ==============================
right = D.closeup_box((1942.0, 306.0, 132.0), 2560, 1440, 1)
left = D.closeup_box((578.0, 339.0, 101.0), 2560, 1440, 0)
check("Close-up on a stage face (GTMnow, 132 px): 600x1066, face 22% of the width, head near the top",
      right is not None and (right.width, right.height) == (600, 1066) and right.x <= 1942 - 66 and right.y == 0, right)
check("...a smaller face is held at 2x at most (540 px wide)", left is not None and left.width == 540, left)
check("...faces too small for a sharp close-up get none", D.closeup_box((300.0, 200.0, 30.0), 640, 360) is None)
times = np.arange(0, 30, 1 / 15)


def energies(talking: list[tuple[float, float, int]]) -> np.ndarray:
    rows = np.full((2, len(times)), 0.2)
    for a, b, who in talking:
        rows[who][(times >= a) & (times <= b)] = 0.5
    return rows


def words_for(spans: list[tuple[float, float]]) -> list[Word]:
    out = []
    for a, b in spans:
        t = a
        while t + 0.3 <= b:
            out.append(Word(text="word.", start=t, end=t + 0.3))
            t += 0.4
    return out


mono = words_for([(0.5, 29.5)])
turns = D.turns_from(mono, times, energies([(0.0, 30.0, 1)]))
check("Who's talking: the mouth that moves more, phrase by phrase", [t.speaker for t in turns] == [1], turns)
cuts = D.plan_cuts(turns, (0.0, 30.0), mono)
check("A 29 s monologue: two-shot for the first 3 s, then the speaker's close-up, a breather, back",
      cuts and cuts[0][0] >= D.ESTABLISH - 0.3 and all(c[2] == 1 for c in cuts) and len(cuts) == 2
      and all(b - a <= D.MAX_CLOSEUP + 0.5 for a, b, _s in cuts) and cuts[-1][1] == 30.0, cuts)
quick = words_for([(0.5, 2.5), (2.9, 4.6), (5.0, 7.0), (7.4, 9.2), (9.6, 11.5)])
quick_turns = D.turns_from(quick, times, energies([(0.5, 2.5, 0), (2.9, 4.6, 1), (5.0, 7.0, 0), (7.4, 9.2, 1), (9.6, 11.5, 0)]))
check("Back-and-forth (2 s replies) stays on the two-shot", D.plan_cuts(quick_turns, (0.0, 12.0), quick) == [], quick_turns)
duel = words_for([(0.5, 9.5), (9.9, 18.0)])
duel_cuts = D.plan_cuts(D.turns_from(duel, times, energies([(0.5, 9.5, 0), (9.9, 18.0, 1)])), (0.0, 18.0), duel)
check("Two long turns: close-up on one, then straight to the other (cut between their words)",
      [c[2] for c in duel_cuts] == [0, 1] and duel_cuts[0][1] == duel_cuts[1][0] and 9.4 <= duel_cuts[0][1] <= 10.0,
      duel_cuts)
unsure = D.turns_from(mono, times, np.full((2, len(times)), 0.3))
check("When it can't tell who's talking, no close-up (never the wrong person full-screen)",
      unsure[0].speaker is None and D.plan_cuts(unsure, (0.0, 30.0), mono) == [])
lost = np.full((2, len(times)), np.nan)
check("...nor when a face is lost", D.turns_from(mono, times, lost)[0].speaker is None)
check("Captions leave the seam while a close-up is on", D.subtract([(0.0, 30.0)], [(3.0, 12.0), (15.0, 30.0)])
      == [(0.0, 3.0), (12.0, 15.0)])


class FakeYuNet:
    """Two faces with their mouth corners where the synthetic frames draw them."""

    def setInputSize(self, size):  # noqa: N802 - OpenCV's name
        self.size = size

    def detect(self, frame):
        h, w = frame.shape[:2]
        faces = []
        for cx in (200, 600):
            if abs(cx - (w / 2 if w < 400 else cx)) < 1e9 and w >= 60:
                x = cx if w > 400 else w / 2
                faces.append([x - 30, 70, 60, 60, x - 15, 85, x + 15, 85, x, 100, x - 12, 115, x + 12, 115, 0.9])
        return 2, np.array(faces[:1] if w < 400 else faces, np.float32)


class FakeDetector:
    def __init__(self):
        self._yunet, self._yunet_size = FakeYuNet(), None


synthetic = []
for k in range(45):
    frame = np.full((300, 800, 3), 90, np.uint8)
    for cx, moving in ((200, False), (600, True)):
        cv2.rectangle(frame, (cx - 30, 70), (cx + 30, 130), (150, 150, 150), -1)
        mouth = 3 + (6 * (k % 3) if moving else 0)
        cv2.ellipse(frame, (cx, 115), (12, mouth), 0, 0, 360, (30, 30, 30), -1)
    synthetic.append(frame)
times_m, energy_m, _ = D.mouth_energy(Path("unused"), 0.0, 3.0, [(200.0, 100.0, 60.0), (600.0, 100.0, 60.0)], (800, 300),
                                      detector=FakeDetector(), frames=iter(synthetic))
check("Mouth motion: the talking mouth (opening and closing) far outscores the still one",
      len(times_m) == 45 and np.nanmean(energy_m[1]) > 3 * np.nanmean(energy_m[0]),
      (np.nanmean(energy_m[0]), np.nanmean(energy_m[1])))

# A real render: the two-shot, then a cut to the right speaker's close-up (a 1080p source).
WIDE = make_source(TMP / "wide1080.mp4", 8.0, "1920x1080")
pair_plan = CropPlan(frame_width=1920, frame_height=1080, crop_width=606, crop_height=1080, y=0,
                     shots=(Shot(start=0.0, end=8.0, x=1100, has_face=True, group=True,
                                 speakers=((480.0, 300.0, 120.0), (1440.0, 300.0, 120.0)), moves=(), content=False),), cuts=())
split = SplitView(spans=((0.0, 7.5),), width=660, height=586, top_x="150", top_y="0", bottom_x="1110", bottom_y="0",
                  windows=((0.0, 7.5, 150, 0, 1110, 0),))
long_words = words_for([(0.3, 7.4)])
fake_energy = lambda *_a, **_k: (np.arange(0, 7.5, 1 / 15), np.vstack([np.full(113, 0.2), np.full(113, 0.6)]),  # noqa: E731
                                 None)
direction = D.direct(WIDE, 0.0, 7.5, pair_plan, split, long_words, energy_fn=fake_energy)
check("direct(): the right speaker's close-up after the establishing two-shot",
      direction is not None and len(direction.layers) == 1 and direction.layers[0][0].speaker == 1
      and abs(direction.layers[0][1][0][0] - D.ESTABLISH) < 1.0, direction and direction.describe())
ass = TMP / "c.ass"
ass.write_text("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\nFormat: Name, Fontname, "
               "Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
               "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
               "Style: Default,Arial,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1\n\n"
               "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
if direction is not None and direction.layers:
    out = R.render_clip(WIDE, ass, CropBox(1100, 0, 606, 1080), 0.0, 7.5, TMP / "director.mp4", layout_mode="smart_crop",
                        split_view=split, closeups=direction.layers)
    shots = frames_of(out)
    early, late = shots[2], shots[-2]
    check("Render: the two-shot first, then the close-up (a different picture, no blank frame)",
          float(np.abs(early.astype(int) - late.astype(int)).mean()) > 10
          and not any(frame_guard.is_blank(f) for f in shots), len(shots))
check("The pipeline and the cold open both call the director; captions follow it off the seam",
      pipeline_src.count("speaker_director.direct(") == 2 and pipeline_src.count("subtract(seams, closeups.windows())") == 2)

# ============================== 6. Fixing a pitch already in the Outbox ==============================
from core import pitch_files as PF  # noqa: E402

calls: list = []
real = (PF.load_pitch, PF.rerender_trim, PF.accept_trim)
PF.load_pitch = lambda rid: {"clips": [{"index": 1, "file": "clip_01.mp4", "start": 138.46, "end": 185.28},
                                       {"index": 2, "file": "clip_02.mp4", "start": 246.74, "end": 286.3}]}
PF.rerender_trim = lambda rid, i, a, b, st: calls.append(("render", i, a, b))
PF.accept_trim = lambda rid, i, rep: calls.append(("use", i, rep is not None))
done = PF.rerender_pitch("ef9ff765b5f44bb8a8634756d84cd5bf", settings, {"page_name": "apollo-graphql-gdre29"},
                         lambda record, clip: (lambda path, name: None))
PF.load_pitch, PF.rerender_trim, PF.accept_trim = real
check("rerender: every clip of a pending pitch again at its own times, then swapped on its clip page",
      done == [1, 2] and calls == [("render", 1, 138.46, 185.28), ("use", 1, True), ("render", 2, 246.74, 286.3),
                                   ("use", 2, True)], calls)
check("...from the command line: python -m core.pitch_files rerender <channel>",
      'sub.add_parser("rerender"' in (ROOT / "core" / "pitch_files.py").read_text())

# ============================== 6. The older suites ==============================
run_all = (ROOT / "tests" / "run_all.py").read_text()
check("run_all: suites before 48 keep their camera, zooms, title card and framing",
      "int(number.group(1)) < 48" in run_all
      and all(f'"{k}": "{v}"' in run_all for k, v in (("EXEC_CAMERA", "flow"), ("MIDCLIP_ZOOM", "1"),
                                                       ("EXEC_TITLE_CARD", "1"), ("SPEAKER_CLOSEUPS", "0"),
                                                       ("FRAME_GUARD", "0"))))
check(".env sets no legacy flags for the app and the worker (the old suites' pins live in run_all only)",
      not (ROOT / ".env").exists() or not re.search(r"^\s*(VISUAL_OVERLAYS\s*=\s*full|CAPTION_STYLE\s*=\s*bold)",
                                                  (ROOT / ".env").read_text(), re.M))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(FAILED)} check(s) failed" if FAILED else "\nAll checks passed")
sys.exit(1 if FAILED else 0)
