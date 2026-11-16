"""Round 63: the 🎮 GTA tab (both GTA modes as buttons, gta_tab.py + core/tool_jobs.py) and
caption-style rotation for Narrate mode (core/narrate/looks.py, new looks in core.subtitle_engine).

Run: .venv/bin/python -u tests/test_round63.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for k, v in {"NAV_NO_CLIP_PAGES": "1", "LINKEDIN_POSTS": "0", "AUTO_GRAPHICS": "0", "VISUAL_OVERLAYS": "none",
             "ENABLE_CLIMAX_HOOK": "0", "MIDCLIP_TRIM": "0", "CAPTION_STYLE": "bold"}.items():
    os.environ[k] = v
os.environ.pop("EMPHASIS_INTENSITY", None)  # each look's own emphasis is checked
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


import core.subtitle_engine as SE  # noqa: E402
from core import tool_jobs as J  # noqa: E402
from core.narrate import looks as L  # noqa: E402
from core.narrate.finish import POPUP_STYLES  # noqa: E402
from core.transcriber import Word  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="nav-r63-"))
WORDS = [Word(text=t, start=0.4 * i, end=0.4 * i + 0.35) for i, t in enumerate(
    "you have been selling cars wrong this whole time".split())]


def ass(style: str) -> str:
    return SE.create_ass_subtitles(WORDS, TMP / f"{style}.ass", clip_duration=4.0, style=style).read_text()


# ---------- caption looks ----------
bold, classic = ass("bold"), ass("classic")
check("bold header unchanged", "Style: ShortForm,Arial Black,72,&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,"
      "-1,0,0,0,100,100,0,0,1,6,3,2," in bold, bold.split("[Events]")[0][-400:])
check("classic header unchanged", "Style: ShortForm,Arial Black,72,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,"
      "-1,0,0,0,100,100,0,0,1,3,1,2," in classic)
check("bold spoken word still yellow", r"{\c&H00FFFF&" in bold)
for name in ("neon", "lime", "boxed"):
    text = ass(name)
    look = SE.caption_style(name)
    check(f"{name}: its own spoken-word colour", "{\\c" + look["highlight"] in text and r"{\c&H00FFFF&" not in text)
neon, boxed = ass("neon"), ass("boxed")
check("neon: Montserrat ExtraBold (a font in assets/fonts)", "Style: ShortForm,Montserrat ExtraBold,76," in neon)
check("neon: punchy pulse", r"\fscy112" in neon)
check("boxed: opaque box (BorderStyle 3) in its box colour",
      "Style: ShortForm,Montserrat ExtraBold,68,&H00FFFFFF,&H0000FFFF,&H28000000,&H00000000,-1,0,0,0,100,100,0,0,3,16,0,"
      in boxed)
check("boxed: no pulse (it would resize the box)", r"\fscy" not in boxed and r"\bord" not in boxed.split("[Events]")[1])
check("every Narrate look is a caption style", all(s in SE.CAPTION_STYLES for s in L.NARRATE_CAPTION_STYLES))
check("each Narrate look has its own colour", len({L.CAPTION_HUES[s] for s in L.NARRATE_CAPTION_STYLES}) ==
      len(L.NARRATE_CAPTION_STYLES))
check("a pop-up hue for every pop-up style", len(L.POPUP_HUES) == len(POPUP_STYLES))

# libass draws every look (fonts found, no parse errors)
video = TMP / "bg.mp4"
subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                "color=c=0x334455:s=1080x1920:d=4:r=30", "-pix_fmt", "yuv420p", str(video)], check=True)
fonts = ROOT / "assets" / "fonts"
for name in L.NARRATE_CAPTION_STYLES:
    out = TMP / f"burn_{name}.png"
    run = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "1.0", "-i", str(video), "-vf",
                          f"ass='{TMP / (name + '.ass')}':fontsdir='{fonts}'", "-frames:v", "1", str(out)],
                         capture_output=True, text=True)
    check(f"libass renders the {name} captions", run.returncode == 0 and out.is_file() and out.stat().st_size > 5000,
          run.stderr[-300:])

# ---------- rotation ----------
history = TMP / "looks.json"
picked = [L.caption_style_for(f"video{i}", None, history) for i in range(4)]
check("4 new videos get 4 different looks", len(set(picked)) == 4, picked)
check("then it cycles to the least recently used", L.caption_style_for("video4", None, history) == picked[0])
check("a video keeps its look when re-rendered", L.caption_style_for("video2", None, history) == picked[2])
check("re-rendering does not move the rotation", L.caption_style_for("video5", None, history) == picked[1])
saved = json.loads(history.read_text())
check("history saved", saved["assigned"]["video0"] == picked[0] and saved["order"][-1] == "video5", saved)

for popup, hue in enumerate(L.POPUP_HUES):
    fresh = TMP / f"clash_{popup}.json"
    got = {L.caption_style_for(f"v{i}", popup, fresh) for i in range(8)}
    check(f"pop-up style {popup} ({hue}): captions never the same colour",
          all(L.CAPTION_HUES[s] != hue for s in got) and len(got) == (4 if hue == "white" else 3), got)
clash = TMP / "kept_clash.json"
clash.write_text(json.dumps({"assigned": {"garage": "bold"}, "order": ["garage"]}))
check("a kept look that now clashes with the pop-up is replaced",
      L.caption_style_for("garage", 0, clash) != "bold")

fixed_history = TMP / "fixed.json"
check("a fixed look is used as it is", L.caption_style_for("x", None, fixed_history, "Neon") == "neon")
check("a fixed look is not recorded", not fixed_history.exists())
try:
    L.caption_style_for("x", None, fixed_history, "comic-sans")
    check("an unknown look is refused", False)
except ValueError:
    check("an unknown look is refused", True)
broken = TMP / "broken.json"
broken.write_text("{not json")
check("a broken history file starts over", L.caption_style_for("y", None, broken) == L.NARRATE_CAPTION_STYLES[0])

os.environ["CAPTION_STYLE"] = "bold"
with L.caption_env("neon"):
    inside = SE.caption_style_name()
check("caption_env sets CAPTION_STYLE while rendering", inside == "neon")
check("and restores it after", os.environ.get("CAPTION_STYLE") == "bold")
os.environ.pop("CAPTION_STYLE")
with L.caption_env("lime"):
    pass
check("and removes it when it was not set", "CAPTION_STYLE" not in os.environ)
os.environ["CAPTION_STYLE"] = "bold"

# ---------- tools/narrate.py ----------
import tools.narrate as N  # noqa: E402

helps = subprocess.run([sys.executable, "tools/narrate.py", "render", "--help"], cwd=ROOT, capture_output=True,
                       text=True)
check("narrate render --captions", "--captions" in helps.stdout and "rotate" in helps.stdout, helps.stderr[-300:])
source = (ROOT / "tools" / "narrate.py").read_text()
check("--captions choices are the looks", all(c in source for c in L.CHOICES))
check("render_one uses the rotation and the pop-up style together",
      "looks.caption_style_for(name, popup" in source and "style=popup" in source and "caption_env(captions)" in source)

# ---------- core/tool_jobs.py ----------
jobs_dir = TMP / "jobs"
ok_job = J.start("t", "counting", [sys.executable, "-c", "import time\nfor i in range(3):\n print('step', i)\n"
                                    " time.sleep(0.2)\nprint('progress 10%', end='\\r'); print('progress 100%')"],
                 jobs_dir=jobs_dir)
check("a job starts running", ok_job.status() == "running")
deadline = time.time() + 15
while ok_job.status() == "running" and time.time() < deadline:
    time.sleep(0.2)
check("and ends done", ok_job.status() == "done" and ok_job.exit_code() == 0, ok_job.tail())
check("its output is kept (progress redraws show their last state)",
      ok_job.tail().splitlines()[-4:] == ["step 0", "step 1", "step 2", "progress 100%"], ok_job.tail())
bad = J.start("t", "failing", [sys.executable, "-c", "raise SystemExit(3)"], jobs_dir=jobs_dir)
deadline = time.time() + 15
while bad.status() == "running" and time.time() < deadline:
    time.sleep(0.2)
check("a failing tool shows failed", bad.status() == "failed" and bad.exit_code() == 3)
long = J.start("t", "sleeping", [sys.executable, "-c", "import time; time.sleep(60)"], jobs_dir=jobs_dir)
time.sleep(0.6)
check("running() lists it", [j.label for j in J.running("t", jobs_dir)] == ["sleeping"])
J.stop(long)
deadline = time.time() + 15
while long.status() == "running" and time.time() < deadline:
    time.sleep(0.2)
check("Stop stops it", long.status() == "stopped")
check("jobs() newest first, by kind", [j.label for j in J.jobs("t", jobs_dir)] == ["sleeping", "failing", "counting"]
      and J.jobs("other", jobs_dir) == [])
missing = J.start("t", "no such program", ["/no/such/program"], jobs_dir=jobs_dir)
deadline = time.time() + 15
while missing.status() == "running" and time.time() < deadline:
    time.sleep(0.2)
check("a command that can't start fails with a reason", missing.status() == "failed" and "Could not start" in
      missing.tail())
check("the wrapper imports nothing from Nav", "from core" not in (ROOT / "core" / "tool_jobs.py").read_text()
      .split('if __name__')[0].split('"""', 2)[2])

# ---------- gta_tab.py ----------
import gta_tab as G  # noqa: E402
import tools.clip_stream as CS  # noqa: E402

check("same folders as the tools", G.LIBRARY_DB == N.LIBRARY_DB and G.NARRATE_OUT == N.OUT_DIR
      and G.CAPTION_HISTORY == N.CAPTION_HISTORY and G.STREAMS_OUT == CS.OUT_ROOT)
check("caption choices match the tool", set(G.CAPTION_LOOKS.values()) == set(L.CHOICES))
check("the app never imports the tools (their env defaults would leak)",
      "import tools" not in (ROOT / "gta_tab.py").read_text() and "from tools" not in (ROOT / "gta_tab.py").read_text())


def raises(fn, *a, **k) -> bool:
    try:
        fn(*a, **k)
    except ValueError:
        return True
    return False


check("stream: a campaign is required", raises(G.stream_argv, url="https://www.twitch.tv/videos/1"))
check("stream: a link or a file is required", raises(G.stream_argv, campaign="Whop: x"))
check("stream: a missing VOD file is caught", raises(G.stream_argv, video="/no/vod.mp4", campaign="Whop: x"))
check("stream: a bad link is caught", raises(G.stream_argv, url="twitch.tv/x", campaign="Whop: x"))
argv = G.stream_argv(url="https://www.twitch.tv/videos/123", campaign="Whop: Big", count=4, name="My Run!",
                     layout="smart_crop", judge=False, plan=True)
check("stream argv", argv[1:] == [str(G.STREAM_TOOL), "https://www.twitch.tv/videos/123", "--campaign", "Whop: Big",
                                  "--count", "4", "--layout", "smart_crop", "--name", "my-run", "--no-judge", "--plan"],
      argv)
vod = TMP / "vod.mp4"
vod.write_bytes(b"x")
chat = TMP / "chat.csv"
chat.write_text("1,hi\n")
argv2 = G.stream_argv(video=str(vod), chat=str(chat), campaign="Vyro: A", streamer="pdotz", game="GTA V",
                      audio_signal=False)
stream_help = subprocess.run([sys.executable, "tools/clip_stream.py", "--help"], cwd=ROOT, capture_output=True,
                             text=True).stdout
flags = {a for a in argv + argv2 if a.startswith("--")}
check("every stream flag the app sends exists in the tool", all(f in stream_help for f in flags),
      [f for f in flags if f not in stream_help])

scripts_dir, voice_dir = TMP / "scripts", TMP / "voice"
scripts_dir.mkdir()
one, two = scripts_dir / "garage.txt", scripts_dir / "heist.txt"
one.write_text("HOOK: x\n")
two.write_text("HOOK: y\n")
check("render: a script without a voiceover is caught",
      raises(G.narrate_render_argv, [one], voice_dir=voice_dir))
G.save_voice(one, b"a", ".WAV", voice_dir)
G.save_voice(one, b"b", ".m4a", voice_dir)
check("a new voiceover take replaces the old one", G.voice_for(one, voice_dir) == voice_dir / "garage.m4a"
      and not (voice_dir / "garage.wav").exists())
G.save_voice(two, b"c", ".mp3", voice_dir)
rargv = G.narrate_render_argv([one, two], "vertical", "smart_crop", "boxed", -18.0, game_audio=False,
                              allow_reuse=True, voice_dir=voice_dir)
check("render argv", rargv[2:] == ["render", str(one), str(two), "--voice-dir", str(voice_dir), "--frame", "vertical",
                                   "--layout", "smart_crop", "--captions", "boxed", "--game-db", "-18",
                                   "--no-game-audio", "--allow-reuse"], rargv)
render_help = helps.stdout
check("every render flag exists in the tool", all(f in render_help for f in rargv if f.startswith("--")))
check("render: nothing picked is caught", raises(G.narrate_render_argv, []))
script_help = subprocess.run([sys.executable, "tools/narrate.py", "script", "--help"], cwd=ROOT, capture_output=True,
                             text=True).stdout
sargv = G.narrate_script_argv("Garage trick", "tips", scripts_dir / "g.txt", TMP / "brief.txt")
check("script argv flags exist", all(f in script_help for f in sargv if f.startswith("--")), sargv)
check("script: a topic is required", raises(G.narrate_script_argv, "  ", "tips", one))
check("ingest: a missing folder is caught", raises(G.narrate_ingest_argv, "/no/footage"))
check("ingest argv", G.narrate_ingest_argv(str(TMP), True)[2:] == ["ingest", str(TMP), "--no-ai"])
check("new script names never overwrite", G.new_script_path("Garage!", scripts_dir).name == "garage-2.txt")
check("scripts listed", {p.name for p in G.scripts(scripts_dir)} == {"garage.txt", "heist.txt"})

db = TMP / "lib.sqlite"
check("an empty library reads as empty", G.library_summary(db)["shots"] == 0)
from core.narrate.library import Library  # noqa: E402

lib = Library(db)
lib.db.executemany("INSERT INTO shots (path, start, end, tags, motion, brightness, used_count) VALUES (?,?,?,?,?,?,0)",
                   [("a.mp4", 0, 30, json.dumps(["city", "night"]), 1, 1), ("a.mp4", 30, 60, json.dumps(["city"]), 1, 1)])
lib.db.commit()
lib.record_render("garage", [1])
lib.close()
summary = G.library_summary(db)
check("library summary", summary["shots"] == 2 and abs(summary["minutes"] - 1.0) < 1e-6
      and summary["tags"][0] == ("city", 2) and summary["renders"] == 1, summary)

runs = TMP / "streams"
(runs / "old").mkdir(parents=True)
(runs / "old" / "moments.json").write_text("{}")
time.sleep(0.05)
(runs / "new").mkdir()
(runs / "new" / "picks.json").write_text("{}")
(runs / "work-only").mkdir()
check("stream runs: newest first, empty folders hidden", [d.name for d in G.stream_runs(runs)] == ["new", "old"])

# ---------- the app ----------
try:
    import autonomous_worker  # noqa: E402

    for name, value in {"get_pending_outreach": lambda: [], "get_cached_leads": lambda: [],
                        "get_awaiting_reply": lambda: [], "get_lead_queue": lambda: [],
                        "get_recently_passed": lambda *a, **k: [], "pass_summary": lambda *a, **k: {},
                        "is_youtube_quota_exhausted": lambda: False}.items():
        setattr(autonomous_worker, name, value)
    from streamlit.testing.v1 import AppTest  # noqa: E402

    import app  # noqa: E402

    check("a 🎮 GTA tab", "🎮 GTA" in app.TAB_LABELS)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    check("the app runs with the GTA tab", not at.exception, [e.value for e in at.exception])
    check("the tabs show", [t.label for t in at.tabs][:len(app.TAB_LABELS)] == list(app.TAB_LABELS),
          [t.label for t in at.tabs])
    radios = [r for r in at.radio if r.key == "gta_mode"]
    check("the mode picker is there", len(radios) == 1)
    if radios:
        check("Narrate opens first, at the footage library", any("Footage library" in s.value for s in at.subheader),
              [s.value for s in at.subheader])
        radios[0].set_value("📺 Stream clips").run()
        check("Stream clips renders", not at.exception, [e.value for e in at.exception])
        inputs = {t.label for t in at.text_input}
        check("Stream clips: link, file and campaign boxes",
              {"Twitch / YouTube VOD link", "Or a VOD on this Mac (its path)", "Clipping campaign (required)"} <= inputs,
              inputs)
        before = {j.id for j in J.jobs("stream")}
        # Make clips with no campaign: an error, and no job started
        for t in at.text_input:
            if t.label == "Twitch / YouTube VOD link":
                t.set_value("https://www.twitch.tv/videos/1")
        make = [b for b in at.button if "Make clips" in str(b.label)]
        if make:
            make[0].click().run()
            check("no campaign: refused with a message",
                  any("campaign" in e.value.lower() for e in at.error) and {j.id for j in J.jobs("stream")} == before,
                  [e.value for e in at.error])
        else:
            check("a Make clips button", False, [b.label for b in at.button])
except ImportError as exc:  # streamlit missing
    check("streamlit installed for the app check", False, exc)

print(f"\n{len(FAILED)} failed" if FAILED else "\nAll Round 63 checks passed.")
sys.exit(1 if FAILED else 0)
