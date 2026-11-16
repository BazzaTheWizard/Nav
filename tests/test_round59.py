"""Round 59: Narrate mode (core.narrate, tools/narrate.py). Synthetic footage and a synthetic
voiceover; the transcript is faked so no model or API key is needed.

Run: timeout 900 .venv/bin/python -u tests/test_round59.py
"""
from __future__ import annotations

import json
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
from core.narrate import script as S  # noqa: E402
from core.narrate.assemble import assemble, plan  # noqa: E402
from core.narrate.finish import finish, sound_cues  # noqa: E402
from core.narrate.library import Library, name_tags, pieces  # noqa: E402
from core.transcriber import Word  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="nav-r59-"))


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
                          str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout)


# --- 1. the script format ---------------------------------------------------------------
SCRIPT = """# a test script
HOOK: [driving, city, night] You have been selling cars wrong.
[beach, sunset] Most players sell at the first garage they find.
[TEXT: "SAVE $40K"] [ZOOM] But the price changes by neighbourhood.
Drive ten minutes inland first.
[SFX: tick] Same car, double the money.
SOURCE: https://example.com/garage-prices
"""
s = S.parse(SCRIPT)
check("parse: five spoken lines", len(s.lines) == 5, [l.text for l in s.lines])
check("parse: the hook line and its text", s.lines[0].hook and s.hook == "You have been selling cars wrong.", s.hook)
check("parse: shot tags", s.lines[0].tags == ["driving", "city", "night"] and s.lines[1].tags == ["beach", "sunset"])
check("parse: a line with no tags keeps the last ones", s.lines[2].tags == ["beach", "sunset"]
      and s.lines[3].tags == ["beach", "sunset"])
check("parse: markers are not spoken", s.lines[2].text == "But the price changes by neighbourhood."
      and [(m.kind, m.value) for m in s.lines[2].markers] == [("TEXT", "SAVE $40K"), ("ZOOM", "")])
check("parse: sources kept, not spoken", s.sources == ["https://example.com/garage-prices"]
      and "example.com" not in s.spoken)
check("check: a short script is flagged", any("spoken words" in p for p in S.check(s)), S.check(s))
try:
    S.parse("# nothing\n[city]\n")
    check("parse: no spoken line raises", False)
except ValueError:
    check("parse: no spoken line raises", True)

# --- 2. alignment: the captions are the script's words, on the heard timing ----------------
written = [w for w, _ in A.script_words(s)]
heard = []
t = 0.3
for w in written:
    text = {"neighbourhood.": "neighborhood.", "$40K": "40k"}.get(w, w)
    heard.append(Word(text=text, start=round(t, 2), end=round(t + 0.3, 2)))
    t += 0.4
heard.insert(10, Word(text="um", start=heard[9].end, end=heard[9].end + 0.05))  # an ad-lib
heard = [h for h in heard if h.text.lower() != "ten"]  # a word Whisper missed
words = A.align(TMP / "unused.wav", s, heard=heard)
check("align: one timed word per script word", [w.text for w in words] == written, [w.text for w in words][:8])
check("align: times only move forward", all(b.start >= a.end - 1e-6 for a, b in zip(words, words[1:])))
missed = written.index("ten")
check("align: a missed word sits between its neighbours",
      words[missed - 1].end <= words[missed].start and words[missed].end <= words[missed + 1].start + 1e-6)
spans = A.line_spans(s, words)
check("line spans: one per line, first starts at 0, back to back",
      len(spans) == 5 and spans[0].start == 0.0 and all(abs(a.end - b.start) < 1e-6 for a, b in zip(spans, spans[1:])),
      spans)

# --- 3. the library: cuts, tags, static pieces dropped ------------------------------------
ps = pieces(20.0, [0.8, 14.0])
check("pieces: no piece under 1.2 s or over 6 s", all(1.2 <= b - a <= 6.0 + 1e-6 for a, b in ps), ps)
check("name tags from folders", name_tags(Path("/f/beach_sunset/take_1.mp4"), Path("/f")) == ["beach", "sunset"])

foot = TMP / "footage"
(foot / "beach_sunset").mkdir(parents=True)
(foot / "city_night").mkdir(parents=True)
(foot / "menus").mkdir(parents=True)
ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=14", "-f", "lavfi", "-i",
       "sine=frequency=220:duration=14", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
       str(foot / "beach_sunset" / "a.mp4"))
ffmpeg("-f", "lavfi", "-i", "mandelbrot=size=1280x720:rate=30,eq=brightness=-0.35", "-t", "12", "-c:v", "libx264",
       "-pix_fmt", "yuv420p", str(foot / "city_night" / "b.mp4"))  # no audio stream on purpose
ffmpeg("-f", "lavfi", "-i", "color=c=gray:size=1280x720:rate=30:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p",
       str(foot / "menus" / "pause.mp4"))
lib = Library(TMP / "lib.sqlite")
stats = lib.ingest(foot)
shots = lib.shots()
check("ingest: shots added from the moving files", stats["shots"] >= 4 and stats["files"] == 3, stats)
check("ingest: the static pause screen is dropped", not any("pause" in sh.path for sh in shots) and stats["static"] >= 1,
      stats)
check("ingest: folder tags on the shots", any({"beach", "sunset"} <= set(sh.tags) for sh in shots)
      and any({"city", "night"} <= set(sh.tags) for sh in shots), [sh.tags for sh in shots])
check("ingest: running it again adds nothing", lib.ingest(foot)["shots"] == 0)
best = lib.pick(["beach", "sunset"])
check("pick: matching tags win", "beach_sunset" in best.path, best.path)

# --- 4. assemble: the picture follows the voiceover ---------------------------------------
voice = TMP / "voice.wav"
total = spans[-1].end
ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:duration={total + 0.5:.2f}", "-ar", "48000", str(voice))
pcs, cues = plan(s, spans, lib, avoid_recent=False)
check("plan: the pieces cover the voiceover exactly", abs(sum(p.duration for p in pcs) - total) < 0.02,
      (sum(p.duration for p in pcs), total))
check("plan: no piece longer than a cut (+ the merged tail)", all(p.duration <= 3.2 + 0.6 + 1e-6 for p in pcs))
check("plan: night lines use night footage first", "city_night" in pcs[0].path, pcs[0].path)
check("plan: the same shot is not repeated back to back", all(a.shot_id != b.shot_id for a, b in zip(pcs, pcs[1:])))
check("plan: markers become cues at their line", [(c.kind, c.line) for c in cues] ==
      [("TEXT", 2), ("ZOOM", 2), ("SFX", 4)], cues)
check("plan: zoom pieces are only on the [ZOOM] line", all(p.zoom == (p.line == 2) for p in pcs))
made = assemble(s, spans, lib, voice, TMP / "assembled.mp4", TMP)
check("assemble: a video as long as the voiceover", abs(duration(made.video) - total) < 0.15,
      (duration(made.video), total))
check("assemble: its plan is saved next to it", json.loads(made.video.with_suffix(".assembly.json").read_text())["cues"])
lib.record_render("t1", made.shot_ids)
check("variety guard: the shots just used count as reused", lib.reuse_share(made.shot_ids) == 1.0)
check("variety guard: recent shots are avoided next time while others remain",
      lib.pick([], exclude=lib.recent_shot_ids()).id not in set(made.shot_ids) or len(lib.shots()) <= len(set(made.shot_ids)))

# --- 5. finish: pop-ups and sound cues on the vertical clip --------------------------------
check("sound cues: whoosh on TEXT and ZOOM, the named SFX",
      [k for _, k in sound_cues(cues, lambda x: x)] == ["whoosh", "whoosh", "tick"])
vertical = TMP / "vertical.mp4"
ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=1080x1920:rate=30:duration={total:.2f}", "-f", "lavfi", "-i",
       f"sine=frequency=330:duration={total:.2f}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
       str(vertical))
final = finish(vertical, cues, lambda x: x, TMP / "final.mp4")
check("finish: a finished file the same length", abs(duration(final) - total) < 0.15, duration(final))

# --- 6. the real pipeline finishes an assembled video ---------------------------------------
try:
    from config import load_settings
    from core.hook_selector import ClipResult
    from core.pipeline import render_selected_clip

    settings = load_settings()
    clip = ClipResult(start_time=0.0, end_time=made.duration, viral_title=s.hook, caption=s.hook,
                      hashtags=[], virality_score=80)
    rendered = render_selected_clip(made.video, clip, words, settings, TMP / "clip.mp4",
                                    layout_mode="blurred_background", work_dir=TMP / "work", title_fade_in=False,
                                    climax_hook_on=False)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", str(rendered.path)], capture_output=True, text=True).stdout.strip()
    check("pipeline: a 1080x1920 clip from the assembled video", probe == "1080,1920", probe)
    out = finish(rendered.path, made.cues, rendered.output_time, TMP / "clip_final.mp4")
    check("pipeline + finish: final clip made", out.is_file() and duration(out) > total - 1.0, duration(out))
except Exception as exc:  # noqa: BLE001
    check("pipeline: render_selected_clip on an assembled video", False, repr(exc))

if not os.getenv("KEEP_R59"):
    shutil.rmtree(TMP, ignore_errors=True)
else:
    print("kept", TMP)
print(f"\n{'ALL PASSED' if not FAILED else f'{len(FAILED)} FAILED: ' + ', '.join(FAILED)}")
sys.exit(1 if FAILED else 0)
