"""Checks for round 3 (punch-in zooms + whoosh). Run: python claude_review/round3/tests/test_round3.py ."""
import json, os, sys, tempfile, wave
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["OPENAI_API_KEY"] = "test"; os.environ.pop("GEMINI_API_KEY", None)

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

from core.transcriber import Word
from core import effects as E, face_tracker as F

# 40 s of speech: sentences of 8 words, 0.4 s pauses between them
words, t = [], 0.2
for s in range(30):
    for j in range(8):
        words.append(Word("w." if j == 7 else "w", t, t + 0.3)); t += 0.33
    t += 0.4
    if t > 40: break
starts = sorted({w.start for i, w in enumerate(words) if i == 0 or words[i - 1].text.endswith(".")})

# ---------- Choosing moments ----------
heur = E.plan_punch_ins(words, 40.0)
check("Punch-ins found without AI moments", 3 <= len(heur) <= 6, str([(round(a, 1), round(b, 1)) for a, b in heur]))
check("Nothing in the first 1.5 s (hook stays clean)", all(a >= 1.5 for a, _ in heur))
check("Each starts on a sentence start", all(any(abs(a - s) < 1e-6 for s in starts) for a, _ in heur))
check("Holds 1.2–3.5 s and stays inside the clip", all(1.2 - 1e-6 <= b - a <= 3.5 + 1e-6 and b <= 40 for a, b in heur))
check("At least 3 s between punch-ins", all(b2[0] - b1[0] >= 3 and b2[0] - b1[1] >= 1.5 for b1, b2 in zip(heur, heur[1:])))
ai = E.plan_punch_ins(words, 40.0, [starts[3] + 0.4, starts[3] + 1.0, 30.0])
check("AI moments snap to the nearest sentence start", round(ai[0][0], 3) == round(starts[3], 3), str([(round(a, 2), round(b, 2)) for a, b in ai]))
check("AI moments too close together are dropped", sum(1 for a, _ in ai if abs(a - starts[3]) < 2) == 1)
check("AI moments win over filler punch-ins", any(abs(a - 30.6) < 0.05 for a, _ in ai), str([(round(a, 2), round(b, 2)) for a, b in ai]))
check("No words → no punch-ins", E.plan_punch_ins([], 40.0) == [])

# ---------- Aiming the zoom at the face ----------
shots = (F.Shot(10, 20, 552, True, False, face_x=856, face_y=418), F.Shot(20, 40, 748, True, False, face_x=1052, face_y=413))
plan = F.CropPlan(1920, 1080, 608, 1080, 0, shots)
spec = E.zoom_spec("smart_crop", plan, 10, 40, 1.15, [(3.0, 5.0)])
import re
first = lambda expr: int(re.search(r"lt\(t,[\d.]+\),(\d+)", expr).group(1))
zx = first(spec.x); zy = first(spec.y); zw = int(spec.width); zh = int(spec.height)
before = ((856 - 552) / 608, 418 / 1080); after = ((856 - zx) / zw, (418 - zy) / zh)
check("Face stays in the same spot on screen when zoomed", abs(before[0] - after[0]) < 0.01 and abs(before[1] - after[1]) < 0.01, f"{before} → {after}")
check("Zoom window follows camera cuts", spec.x.startswith("if(lt(t,10.000),") and spec.width == "528", spec.x)
check("Blurred layout without face data zooms on the center", E.zoom_spec("blurred_background", None, 0, 30, 1.15, [(3, 5)]).x == "(iw-ow)/2")
check("No zoom for stacked layout or zoom 1.0", E.zoom_spec("stacked", plan, 10, 40, 1.15, [(3, 5)]) is None and E.zoom_spec("smart_crop", plan, 10, 40, 1.0, [(3, 5)]) is None)

# ---------- Effects bundle + whoosh ----------
fx = E.build_effects(words, 0, 40, "blurred_background", None, zoom_factor=1.15, sfx_enabled=True, sfx_volume=0.35)
check("A whoosh at every punch-in start", fx.zoom is not None and fx.sfx_times == tuple(a for a, _ in fx.zoom.intervals))
with wave.open(str(fx.sfx_path)) as handle:
    info = (handle.getnchannels(), handle.getframerate(), round(handle.getnframes() / handle.getframerate(), 2))
check("Whoosh file is generated (no licence needed)", info == (1, 48000, 0.42), str(info))
check("Sound can be turned off", E.build_effects(words, 0, 40, "smart_crop", plan, zoom_factor=1.15, sfx_enabled=False).sfx_times == ())
check("Punch-ins can be turned off", E.build_effects(words, 0, 40, "smart_crop", plan, zoom_factor=1.0).zoom is None)

# ---------- Renderer command ----------
import core.video_renderer as R
captured = {}
def fake_run(cmd, **kwargs):
    if "-filter_complex" in cmd: captured["cmd"] = cmd
    stdout = "1\n" if "-select_streams" in cmd else ""
    return type("Result", (), {"stdout": stdout})()
real_run = R.subprocess.run
R.subprocess.run = fake_run
with tempfile.TemporaryDirectory() as d:
    ass = Path(d) / "s.ass"; ass.write_text("x"); src = Path(d) / "v.mp4"; src.write_bytes(b"x")
    R.render_clip(src, ass, F.CropBox(552, 0, 608, 1080), 0, 30, Path(d) / "a.mp4", layout_mode="smart_crop", effects=E.ClipEffects(
        zoom=E.ZoomSpec(((4.66, 6.61), (13.12, 15.73)), "528", "938", "600", "40"), sfx_times=(4.66, 13.12), sfx_path=Path(d) / "w.wav"))
    cmd = captured["cmd"]; graph = cmd[cmd.index("-filter_complex") + 1]
    R.render_clip(src, ass, F.CropBox(552, 0, 608, 1080), 0, 30, Path(d) / "b.mp4", layout_mode="smart_crop")
    plain = captured["cmd"]
R.subprocess.run = real_run
check("Zoomed crop switched on only during punch-ins", "enable='between(t,4.660,6.610)+between(t,13.120,15.730)'" in graph and graph.count("crop=") == 2)
check("Whooshes delayed to each punch-in and mixed without lowering the voice", "adelay=4660" in graph and "adelay=13120" in graph and "amix=inputs=3" in graph and "normalize=0" in graph and "[aout]" in cmd)
check("Without effects the command is unchanged", "amix" not in " ".join(plain) and "-af" in plain and "overlay" not in plain[plain.index("-filter_complex") + 1])

# ---------- Settings ----------
import config
os.environ.update({"PUNCH_IN_ZOOM": "1", "PUNCH_IN_SFX": "0", "SFX_VOLUME": "0.2"})
settings = config.load_settings()
check("Settings read from .env", (settings.punch_in_zoom, settings.punch_in_sfx, settings.sfx_volume) == (1.0, False, 0.2))
for key in ("PUNCH_IN_ZOOM", "PUNCH_IN_SFX", "SFX_VOLUME"): os.environ.pop(key)
check("Defaults: 1.15x zoom with whoosh", (config.load_settings().punch_in_zoom, config.load_settings().punch_in_sfx) == (1.15, True))

# ---------- AI pickers return moments ----------
import core.agent_brain as A, core.pipeline as P
A.load_dotenv = lambda *a, **k: None
segs_words, t = [], 0.0
for i in range(200):
    for j in range(10):
        segs_words.append(Word(f"w{i}{'.' if j == 9 else ''}", t, t + 0.3)); t += 0.32
    t += 0.5
clip = {"start_time": 100, "end_time": 140, "hook_title": "Best", "reason": "r", "punch_ins": [104.0, "bad", 500.0, 120.0]}
A._request_openai = lambda key, prompt: json.dumps({"clips": [clip], "channel_content_summary": "s", "specific_moment_reference": "m"})
picked, _email = P.pick_clips_with_agent(segs_words, t, 1, "Chan", "Title")
check("Gemini/OpenAI moments kept when inside the clip", picked[0].punch_ins == [104.0, 120.0], str(picked[0].punch_ins))
from openai.lib._pydantic import to_strict_json_schema
from core.hook_selector import ClipSelection
schema = to_strict_json_schema(ClipSelection)
check("OpenAI picker schema asks for punch_ins", "punch_ins" in schema["$defs"]["ClipResult"]["required"])

print(f"\n{fails} failure(s)"); sys.exit(1 if fails else 0)
