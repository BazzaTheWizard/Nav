"""Checks for round 9: re-render file names never grow; re-renders keep full-frame shots.
Run all tests: .venv/bin/python tests/run_all.py"""
import sys, tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

import core.clipper as C
from core.face_tracker import CropBox, CropPlan, Shot
from core.transcriber import Word

first = Path("/x/storage/output/clip_20261027_133220_870016_faae38_01.mp4")
once = C.rerender_output_path(first)
check("First trim: original name + _edit_<8 hex>", once.name.startswith("clip_20261027_133220_870016_faae38_01_edit_") and len(once.stem) == len(first.stem) + 14, once.name)
path = first
for _ in range(100):
    path = C.rerender_output_path(path)
check("100 trims later the name is the same length", len(path.name) == len(once.name), path.name)
legacy = Path("/x/clip_20261027_133220_870016_faae38_01" + "".join(f"_adjusted_{i:08x}_rerender_{i:032x}" for i in range(6)) + ".mp4")
check("Old over-long names are cleaned on the next trim", len(legacy.name) > 255 and C.rerender_output_path(legacy).name.startswith("clip_20261027_133220_870016_faae38_01_edit_"))
check("Stays in the same folder and keeps the clip_ start (Discard still finds it)",
      once.parent == first.parent and once.name.startswith("clip_"))
check("Very long base names are capped", len(C.rerender_output_path(Path("/x/" + "a" * 300 + ".mp4")).name) <= C.MAX_CLIP_STEM + 18)

# rerender_clip: short temporary name, and full-frame shots passed along
captured = {}
def fake_render(source, subtitle, crop, start, end, output, **kwargs):
    captured["output"] = Path(output); captured.update(kwargs)
    Path(output).write_bytes(b"x")
    return Path(output)
C.render_clip = fake_render
C.prepare_title_overlay = lambda *a, **k: None
tmp = Path(tempfile.mkdtemp())
src = tmp / "src.mp4"; src.write_bytes(b"x")
plan = CropPlan(1920, 1080, 606, 1078, 1, (Shot(100.0, 110.0, 600, True, False), Shot(110.0, 115.0, 600, False, False), Shot(115.0, 140.0, 600, True, False)))
words = [Word(f"w{i}", 100 + i * 0.4, 100.3 + i * 0.4) for i in range(90)]
dest = tmp / ("clip_run_01" + "_adjusted_0123abcd" * 10 + ".mp4")
out = C.rerender_clip(src, 100.0, 135.0, words, C.rerender_output_path(dest), layout_mode="smart_crop", crop_plan=plan, source_duration=200.0, cached_crop=CropBox(600, 1, 606, 1078))
check("Temporary render file has a short name", captured["output"].name.startswith("tmp_rerender_") and len(captured["output"].name) < 40, captured["output"].name)
check("Result is the short final name", out.exists() and len(out.name) < 40, out.name)
check("Re-render keeps full-frame shots (slides)", captured.get("full_frame_ranges") == [(10.0, 15.0)], str(captured.get("full_frame_ranges")))
C.rerender_clip(src, 100.0, 135.0, words, tmp / "clip_run_02.mp4", layout_mode="blurred_background", crop_plan=plan, source_duration=200.0, cached_crop=CropBox(600, 1, 606, 1078))
check("Other layouts: no full-frame spans", captured.get("full_frame_ranges") is None)
app_source = (ROOT / "app.py").read_text()
check("App uses the short naming for trims", "rerender_output_path(clip_path)" in app_source and "_adjusted_" not in app_source)

print("\nAll checks passed." if not fails else f"\n{fails} check(s) failed.")
sys.exit(1 if fails else 0)
