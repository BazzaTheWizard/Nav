"""Round 58: clips cut to a client's brief (core.brief), branded with a CTA end card (core.brand_outro),
run by tools/brief_clips.py. The model is faked here; every hard rule is checked in code.

Run: timeout 600 .venv/bin/python -u tests/test_round58.py
"""
from __future__ import annotations

import importlib.util
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

from core import brand_outro, brief as B  # noqa: E402
from core.transcriber import Word  # noqa: E402

# A transcript: sentences of known text, one word every 0.4 s, a 0.6 s pause between sentences.
SENTENCES = [
    "AI is a tool, not a strategy.",                                                         # S0
    "Too many owners hand the whole plan to a chatbot.",                                      # S1
    "The tool can draft, but it cannot decide who your customer is.",                          # S2
    "That judgment still has to come from you and your team.",                                 # S3
    "So use it to move faster on the work you already understand.",                           # S4
    "And we keep going from there.",                                                          # S5 (dangling start)
    "Here is how a small shop can use it every week.",                                         # S6
    "Brainstorm ten post ideas in two minutes and keep the best three.",                       # S7
    "Ask it to outline a month of content around your busy season.",                           # S8
    "Have it research what your competitors say on their websites.",                           # S9
    "Then rewrite your homepage copy in your own voice.",                                      # S10
    "What happens when everyone uses the same prompt?",                                        # S11 (question)
    "Every business ends up sounding exactly the same.",                                       # S12
    "Generic posts with no brand voice get scrolled past.",                                    # S13
    "Worse, the tool will state wrong facts with total confidence.",                           # S14
    "If you publish without checking, your name is on that mistake.",                          # S15
    "Read every line before it goes out, and make it sound like you.",                        # S16
    "Okay let's take a quick break and...",                                                    # S17 (trails off)
]
words, t, spans = [], 1.0, []
for s in SENTENCES:
    first = t
    for w in s.split():
        words.append(Word(w, round(t, 3), round(t + 0.32, 3)))
        t += 0.4
    spans.append((first, t - 0.08))
    t += 0.6

# =========================== 1. Valid windows (code, not the model) ===========================
valid = B.windows(words, 8.0, 16.0)
ordered, sp = B.sentences(words)
check("Sentence spans found: one per sentence", len(sp) == len(SENTENCES), len(sp))
ends = {w[1] for w in valid}
starts = {w[0] for w in valid}
check("Never ends on a question (S11)", 11 not in ends)
check("Never ends trailing off (S17 'break and...')", 17 not in ends)
check("Never starts mid-thought ('And we...', 'So use it...', 'Then rewrite...')",
      not starts & {4, 5, 10}, sorted(starts))
check("Every window is 8-16 s and ends on . or !",
      all(8.0 <= e - s <= 16.0 and B.H._sentence_text(ordered, sp, j).rstrip().endswith((".", "!"))
          for _i, j, s, e in valid), [(i, j, round(e - s, 1)) for i, j, s, e in valid][:5])
check("A clip whose next sentence lands the point is not offered (S3 before 'So use it...' is fine; S4 'So' start isn't)",
      all(B.closure_problem(ordered, sp, i, j) is None for i, j, _s, _e in valid))
check("Closure problem named for a question ending", B.closure_problem(ordered, sp, 6, 11) is not None,
      B.closure_problem(ordered, sp, 6, 11))

# =========================== 2. Snapping and hooks ===========================
w = B.snap(valid, spans[6][0], spans[10][1])
check("A proposal snaps to the best-overlapping valid window", w is not None and w[0] == 6, w)
check("...and nothing snaps if the overlap is tiny", B.snap(valid, 200.0, 210.0) is None)
check("Hook: 4-6 words accepted, quotes and period stripped", B.clean_hook('"AI won\'t replace your strategy."') == "AI won't replace your strategy")
check("Hook: 3 or 7 words rejected", B.clean_hook("Use AI wisely") is None
      and B.clean_hook("Seven words is far too many here") is None)

# =========================== 3. pick() with a fake model ===========================
themes = [B.Theme("AI is not a replacement for strategy", "thoughtful use"),
          B.Theme("How small businesses use AI", "brainstorming, planning"),
          B.Theme("The biggest AI content mistake", "generic, inaccurate")]
calls: list[str] = []


def fake(system: str, prompt: str) -> dict:
    calls.append(system[:20])
    if "candidate moments" in system:
        return {"themes": [
            {"theme": 1, "candidates": [{"start_sentence": 0, "end_sentence": 3, "why": "the core argument"}]},
            # theme 2's first idea overlaps theme 1 (S2-S4 ends on 'So'-led S4, gets snapped), second is the real one
            {"theme": 2, "candidates": [{"start_sentence": 1, "end_sentence": 3, "why": "dup"},
                                        {"start_sentence": 6, "end_sentence": 10, "why": "use cases"}]},
            # theme 3: first candidate ends on the question S11 (snaps elsewhere), and the model checks it off-theme
            {"theme": 3, "candidates": [{"start_sentence": 7, "end_sentence": 11, "why": "weak"},
                                        {"start_sentence": 12, "end_sentence": 16, "why": "mistakes"}]}]}
    out = []
    for n, chunk in enumerate(prompt.split("\n\nClip ")[0:], 1):
        theme3_weak = "Brainstorm" in chunk and "mistake" in chunk
        long_hook = "AI is a tool" in chunk and "MUST" not in system
        out.append({"clip": n, "on_theme": 3 if theme3_weak else 8, "standalone": 8, "complete": 9,
                    "hook": "This hook is far too long to use" if long_hook else
                    ("AI won't replace your strategy" if "AI is a tool" in chunk else
                     "Five weekly AI marketing wins" if "Brainstorm" in chunk else "Generic AI content costs you")})
    return {"clips": out}


picks = B.pick(words, themes, min_s=8.0, max_s=22.0, ask=fake)
check("One clip per theme", all(p is not None for p in picks), picks)
if all(picks):
    check("Clips never overlap", all(min(a.end, b.end) <= max(a.start, b.start)
                                     for k, a in enumerate(picks) for b in picks[k + 1:]),
          [(p.start, p.end) for p in picks])
    check("Theme 2 skipped the candidate that overlapped theme 1", picks[1].why == "use cases", picks[1].why)
    check("Theme 3 fell back from the off-theme candidate", picks[2].why == "mistakes" and picks[2].on_theme == 8,
          (picks[2].why, picks[2].on_theme))
    check("Theme 1's 7-word hook was re-asked and fixed", picks[0].hook == "AI won't replace your strategy", picks[0].hook)
    check("Every pick ends on a complete declarative sentence",
          all(p.last_sentence.endswith((".", "!")) for p in picks), [p.last_sentence for p in picks])
ov = B.pick(words, themes[:1], min_s=8.0, max_s=22.0, ask=fake, overrides={0: (spans[12][0], spans[16][1])})
check("Override: your times, snapped inside them and checked",
      ov[0] is not None and spans[12][0] - 0.01 <= ov[0].start and ov[0].end <= spans[16][1] + 0.01, ov)

# =========================== 4. Brief file ===========================
brief = B.Brief.load(ROOT / "storage" / "briefs" / "district_consulting.json")
check("District Consulting brief loads: 3 themes, 20-45 s", len(brief.themes) == 3 and brief.min_seconds == 20
      and brief.max_seconds == 45)
check("Speech range leaves room for the 3 s end card (19-42 s)", brief.speech_range() == (19.0, 42.0), brief.speech_range())

# =========================== 5. Branding + CTA end card on a real clip ===========================
tmp = Path(tempfile.mkdtemp(prefix="r58_")).resolve()
FF = shutil.which("ffmpeg")
clip = tmp / "clip_1.mp4"
subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x303030:size=1080x1920:rate=30:duration=6",
                "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=6", "-c:v", "libx264", "-preset",
                "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)], check=True, stdin=subprocess.DEVNULL)
clip.with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nHi\n")
final = brand_outro.finish(clip, "District Consulting", "Book your free AI marketing consult",
                           sub="District Consulting", accent="#1F3A5F")
seconds, _r = brand_outro._probe(final)
check("Final = clip + 3 s end card", abs(seconds - 9.0) < 0.2, seconds)


def frame(path: Path, at: float) -> np.ndarray:
    raw = subprocess.run([FF, "-v", "error", "-ss", f"{at:.2f}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, stdin=subprocess.DEVNULL).stdout
    return np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3).astype(int)


mid = frame(final, 3.0)
check("Brand pill under the captions (bright text near y=1300), nothing at the top",
      mid[1285:1315, 300:780].max() > 200 and mid[150:600].max() < 80, (int(mid[1285:1315].max()), int(mid[150:600].max())))
end = frame(final, 8.0)
check("End card: the accent colour fills the frame", abs(end[300, 540, 2] - 0x5F) < 30 and end[300, 540, 2] > end[300, 540, 0],
      end[300, 540].tolist())
check("...with white CTA text in the middle", end[700:1300, 100:980].max() > 220)
check(".srt copied under the final name", final.with_suffix(".srt").is_file())
probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "json",
                        str(final)], capture_output=True, text=True).stdout
check("9:16, video + audio", '"width": 1080' in probe and '"height": 1920' in probe and '"audio"' in probe, probe)

# =========================== 6. The tool end to end (fake model, real render) ===========================
spec = importlib.util.spec_from_file_location("brief_clips", ROOT / "tools" / "brief_clips.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
check("--override parses 13:32.4-14:05", tool._times(["2=13:32.4-14:05"]) == {1: (812.4, 845.0)})
src = tmp / "workshop.mp4"
subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x404040:size=1920x1080:rate=30:duration={t + 1:.0f}",
                "-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=48000:duration={t + 1:.0f}", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)], check=True,
               stdin=subprocess.DEVNULL)
out = tmp / "out"
out.mkdir()
B.save_words(words, out / "transcript.json")
small = tmp / "brief.json"
small.write_text(json.dumps({"client": "District Consulting", "cta": "Book your free AI marketing consult",
                             "min_seconds": 11, "max_seconds": 25, "themes": [{"title": t_.title, "look_for": t_.look_for}
                                                                              for t_ in themes]}))
B.gemini_or_openai = lambda: fake
sys.argv = ["brief_clips.py", "--brief", str(small), "--video", str(src), "--out", str(out), "--dry-run"]
tool.main()
md = (out / "picks.md").read_text()
check("Dry run: picks.md with every hook and ending, nothing rendered",
      "AI won't replace your strategy" in md and "Ends on" in md and not list(out.glob("clip_*.mp4")), md[:300])
sys.argv = ["brief_clips.py", "--brief", str(small), "--video", str(src), "--out", str(out), "--only", "2",
            "--layout", "blurred_background"]
tool.main()
made = out / "clip_2_final.mp4"
check("Render: clip 2 rendered, branded, with its end card", made.is_file(), sorted(p.name for p in out.iterdir()))
if made.is_file():
    secs, _r = brand_outro._probe(made)
    check("...within the brief's 11-25 s", 11 <= secs <= 25, secs)
    old_probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream_side_data=rotation", str(src)],
                               capture_output=True).returncode != 0  # FFmpeg 4.4: the title card can't be drawn
    if old_probe:
        print("SKIP  Hook header check: this ffprobe is too old for the title card; run on the Mac")
    else:
        check("...the hook on screen in the first seconds (bright header near the top)",
              frame(made, 0.5)[170:700].max() > 200, int(frame(made, 0.5)[170:700].max()))
    check("...picks.md lists the final file and its length", "clip_2_final.mp4" in (out / "picks.md").read_text())

shutil.rmtree(tmp, ignore_errors=True)
print("\nAll checks passed" if not FAILED else f"\n{len(FAILED)} check(s) failed: {FAILED}")
sys.exit(1 if FAILED else 0)
