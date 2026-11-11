"""Round 52: the Outbox trim keeps the clip's captions file and writes its posts for the new cut.

A real render: a pitch is saved from a test video, clip 1 is trimmed, then "Use this cut" swaps it
on the clip page. The page must keep the captions file (.srt) and get the new cut's posts.

Run: timeout 300 .venv/bin/python tests/test_round52.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.update(FRAME_GUARD="0", ENABLE_CLIMAX_HOOK="0", SPEAKER_CLOSEUPS="0", AUTO_GRAPHICS="0")
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


from config import load_settings  # noqa: E402
from core import clip_pages, pipeline  # noqa: E402
from core import pitch_files as PF  # noqa: E402
from core.face_tracker import CropPlan, Shot  # noqa: E402
from core.hook_selector import ClipResult  # noqa: E402
from core.transcriber import Word  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="round52_"))
PF.PITCHES_DIR = TMP / "pitches"
source = TMP / "source.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=60",
                "-f", "lavfi", "-i", "sine=frequency=220:duration=60", "-shortest", "-c:v", "libx264", "-preset",
                "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True, capture_output=True)
text = ("Pricing by outcome changed everything for us. Buyers pay for results, not logins. Small teams start "
        "without a big contract. Usage pricing follows the value they get every single month. ") * 6
words = [Word(text=w, start=0.5 + 0.42 * i, end=0.85 + 0.42 * i) for i, w in enumerate(text.split())]
plan = CropPlan(frame_width=1280, frame_height=720, crop_width=404, crop_height=720, y=0,
                shots=(Shot(start=0.0, end=60.0, x=438, has_face=True, group=False, speakers=None, moves=(), content=False),),
                cuts=())
pipeline.analyze_crop_plan = lambda *a, **k: plan
settings = load_settings()
settings = SimpleNamespace(**{**settings.__dict__, "temp_dir": TMP / "temp"})
record = {"record_id": "r52", "channel_title": "Acme Revenue Show", "video_title": "AI pricing: what buyers pay for",
          "pitch": {"theme": "AI pricing"}, "page_name": "acme-revenue-k2m4qx"}
clip = ClipResult(start_time=10.0, end_time=40.0, viral_title="Why pricing by outcome wins", caption="", hashtags=[],
                  virality_score=80)
first = pipeline.render_selected_clip(source, clip, words, settings, TMP / "clip_01.mp4", layout_mode="smart_crop",
                                      work_dir=TMP / "w", graphics=PF.video_info_for(record))
PF.save_pitch_files("r52", source, [first], words, 60.0)
check("Setup: the first render has its captions and both posts",
      first.path.with_suffix(".srt").is_file() and (TMP / "clip_01_linkedin_post.txt").is_file()
      and (TMP / "clip_01_social_post.json").is_file())

trimmed = PF.rerender_trim("r52", 1, 12.0, 38.0, settings, info=PF.video_info_for(record))
srt = trimmed.with_suffix(".srt")
check("Trim: the new cut keeps its captions file (it used to be deleted with the work folder)",
      trimmed.is_file() and srt.is_file() and "-->" in srt.read_text(), sorted(p.name for p in trimmed.parent.iterdir()))
duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 str(trimmed)], capture_output=True, text=True).stdout)
last_cue = srt.read_text().strip().split("\n\n")[-1].split("\n")[1].split(" --> ")[1]
h, m, rest = last_cue.split(":")
check("...captions end inside the new cut's length", int(h) * 3600 + int(m) * 60 + float(rest.replace(",", ".")) <= duration + 0.05,
      (last_cue, duration))
check("...and its LinkedIn post and Shorts & Reels caption are written for the new cut",
      (trimmed.parent / "clip_01.trim_linkedin_post.txt").is_file()
      and json.loads((trimmed.parent / "clip_01.trim_social_post.json").read_text())["hashtags"])


class FakeR2:
    def __init__(self):
        self.objects = {"acme-revenue-k2m4qx/page.json": json.dumps(
            {"clips": [{"start": 10.0, "end": 40.0, "post": "old post", "srt": True, "social": {"title": "old"}}]}).encode()}

    def upload_file(self, local, bucket, key, ExtraArgs=None):
        self.objects[key] = Path(local).read_bytes()

    def put_object(self, Bucket, Key, Body, **_k):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": SimpleNamespace(read=lambda: self.objects[Key])}


fake = FakeR2()
clip_pages._require = lambda: (SimpleNamespace(bucket="b", base_url="https://clips.test"), fake)
clip_pages._upload_poster = lambda *a, **k: 0.0
times = PF.accept_trim("r52", 1, lambda path, name: clip_pages.replace_clip(record["page_name"], name, path, 12.0, 38.0))
entry = json.loads(fake.objects["acme-revenue-k2m4qx/page.json"])["clips"][0]
check("Use this cut: the page keeps the captions file, now the new cut's",
      entry.get("srt") is True and fake.objects["acme-revenue-k2m4qx/captions_01.srt"] == srt.read_bytes(), entry)
check("...with the new cut's posts and times", entry.get("post") != "old post" and entry["social"]["title"] != "old"
      and (entry["start"], entry["end"]) == (12.0, 38.0) and times == {"start": 12.0, "end": 38.0}, entry)
check("...and the clip itself is the trimmed one", fake.objects["acme-revenue-k2m4qx/clip_01.mp4"]
      == (PF.pitch_dir("r52") / "clip_01.mp4").read_bytes())

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(FAILED)} check(s) failed" if FAILED else "\nAll checks passed")
sys.exit(1 if FAILED else 0)
