"""Round 66: the gaming-focused app. NAV_MODE=gaming (the default) shows GTA, Make clips and
System; NAV_MODE=b2b is the app as it was. Nothing is deleted: docs/PRUNE_CANDIDATES.md lists
what the gaming entry points no longer reach, from tools/import_graph.py.

Run: .venv/bin/python -u tests/test_round66.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
TMP = Path(tempfile.mkdtemp(prefix="nav-r66-"))
os.environ["NAV_LEADS_DB"] = str(TMP / "leads.sqlite")
os.environ["NAV_NO_CLIP_PAGES"] = "1"
os.environ["NAV_MODE"] = ""  # set but empty: the default, whatever .env says
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


# ---------------------------------------------------------------- the import graph, on a toy project
import import_graph as IG  # noqa: E402

toy = TMP / "toy"
for rel, text in {
    "app.py": "from core import used\nfrom core import b2b_only\nfrom core.half import kept\n\n"
              "def main():\n    if gaming():\n        _main_gaming()\n    else:\n        _main_b2b()\n\n"
              "def gaming():\n    return True\n\n"
              "def _main_gaming():\n    used.go()\n    kept()\n\n"
              "def _main_b2b():\n    _outbox()\n\n"
              "def _outbox():\n    b2b_only.send()\n\n"
              "if __name__ == '__main__':\n    main()\n",
    "gta_tab.py": "def render_tab():\n    pass\n",
    "core/__init__.py": "",
    "core/used.py": "from core import imported_not_called\n\ndef go():\n    return 1\n\n"
                    "def never_called():\n    from core import lazy\n    return lazy.x()\n",
    "core/b2b_only.py": "def send():\n    return 1\n",
    "core/half.py": "def kept():\n    return 1\n\ndef dropped():\n    return 2\n\nif __name__ == '__main__':\n    dropped()\n",
    "core/lazy.py": "def x():\n    return 1\n",
    "core/imported_not_called.py": "def idle():\n    return 1\n",
    "core/orphan.py": "def alone():\n    return 1\n",
    "core/tool_jobs.py": "def start():\n    return 1\n",
    "tools/narrate.py": "from core import used\n\ndef main():\n    used.go()\n\nif __name__ == '__main__':\n    main()\n",
    "tests/test_orphan.py": "from core import orphan\nfrom core import used\n",
}.items():
    (toy / rel).parent.mkdir(parents=True, exist_ok=True)
    (toy / rel).write_text(text)
t = IG.analyse(toy)
files = lambda key: sorted(r["file"] for r in t[key])  # noqa: E731
check("graph: a module nothing imports, and one only app.py's B2B branch uses, are unreached",
      files("unreached") == ["core/b2b_only.py", "core/orphan.py"], files("unreached"))
check("graph: an import inside a function nobody calls does not load its module", files("never_loaded") == ["core/lazy.py"],
      files("never_loaded"))
check("graph: imported at the top but never mentioned", files("nothing_called") == ["core/imported_not_called.py"],
      files("nothing_called"))
half = [r for r in t["partly_used"] if r["file"] == "core/half.py"]
check("graph: a half-used module names what is left over and who still uses the rest",
      len(half) == 1 and half[0]["unreached"] == ["dropped"] and half[0]["used_from_outside"] == {"kept": "app._main_gaming"},
      t["partly_used"])
check("graph: app.py is read function by function", t["app"]["unreached_functions"] == ["_main_b2b", "_outbox"]
      and "_main_gaming" in t["app"]["reached_functions"] and t["app"]["only_unreached"] == {"core.b2b_only": ["_outbox"]},
      t["app"])
check("graph: a candidate's tests are listed, with what else they use",
      t["tests"] == [{"test": "test_orphan.py", "candidates": ["core.orphan"], "also_uses_kept": ["core.used"]}], t["tests"])
check("graph: used code is not a candidate", "core/used.py" not in files("unreached") + files("never_loaded") + files("nothing_called"))

# ---------------------------------------------------------------- the real project
real = IG.analyse(ROOT)
candidates = real["unreached"] + real["never_loaded"] + real["nothing_called"]
check("the gaming tools and tabs are reached", {"gta_tab", "core.leads", "core.clip_mode.windows", "core.narrate.script",
                                                "core.pipeline", "core.tool_jobs"} <= set(real["loaded"]))
check("nothing was deleted: every candidate is still a file", candidates and all((ROOT / r["file"]).is_file() for r in candidates))
for name in ("_render_outbox", "_render_sent_pitches", "_render_pipeline", "_render_header", "_main_b2b"):
    check(f"app.py: {name} is B2B only (not reached in gaming mode)", name in real["app"]["unreached_functions"])
for name in ("_main_gaming", "_render_gta_tab", "_render_clip_generator", "_render_system"):
    check(f"app.py: {name} is reached in gaming mode", name in real["app"]["reached_functions"])
doc = (ROOT / "docs" / "PRUNE_CANDIDATES.md").read_text()
missing = [r["file"] for r in candidates if r["file"] not in doc]
check("PRUNE_CANDIDATES.md lists every module the tool finds", not missing, missing)
missing = [row["test"] for row in real["tests"] if row["test"] not in doc]
check("... with their tests", not missing, missing)
missing = [r["file"] for r in real["partly_used"] if r["unreached_lines"] >= 250 and r["file"] not in doc]
check("... and the mostly-unused files", not missing, missing)
check("... and says nothing is deleted", "Nothing has been deleted" in doc and "tools/import_graph.py" in doc)
plan = (ROOT / "docs" / "GTA6_INTEGRATION_PLAN.md").read_text()
check("the integration plan records Rounds 64-66", all(x in plan for x in ("Round 64", "Round 65", "Round 66", "find_leads",
                                                                           "NAV_MODE")))

# ---------------------------------------------------------------- NAV_MODE
env = (ROOT / ".env.example").read_text()
check(".env.example: NAV_MODE=gaming, with b2b explained", "\nNAV_MODE=gaming\n" in env and "b2b" in env)
runner = (ROOT / "tests" / "run_all.py").read_text()
check("run_all.py: the suites before Round 66 run as the B2B app", 'env["NAV_MODE"] = "b2b"' in runner and "< 66" in runner)
check("the B2B code is still there", all(f"def {name}(" in (ROOT / "app.py").read_text() for name in (
    "_render_outbox", "_render_sent_pitches", "_render_pipeline", "_render_recently_passed", "_render_manual_pages")))
for module in ("core.follow_up_plan", "core.reply_stats", "core.marketing_contact", "core.linkedin_harvester", "autonomous_worker"):
    run = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=ROOT, capture_output=True, text=True)
    check(f"{module} still imports", run.returncode == 0, run.stderr[-200:])

try:
    import autonomous_worker  # noqa: E402

    for name, value in {"get_pending_outreach": lambda: [], "get_cached_leads": lambda: [],
                        "get_awaiting_reply": lambda: [], "get_lead_queue": lambda: [],
                        "get_recently_passed": lambda *a, **k: [], "pass_summary": lambda *a, **k: {},
                        "is_youtube_quota_exhausted": lambda: False}.items():
        setattr(autonomous_worker, name, value)
    from streamlit.testing.v1 import AppTest  # noqa: E402

    import app  # noqa: E402

    check("default: gaming", app.nav_mode() == "gaming" and app.TAB_LABELS == ("🎮 GTA", "🎬 Make clips", "🩺 System")
          and app.tab_labels() == app.GAMING_TAB_LABELS)
    for value, mode in (("b2b", "b2b"), (" B2B ", "b2b"), ("gaming", "gaming"), ("something", "gaming")):
        os.environ["NAV_MODE"] = value
        check(f"NAV_MODE={value!r} -> {mode}", app.nav_mode() == mode and (app.tab_labels() == app.B2B_TAB_LABELS) == (mode == "b2b"))
    check("b2b tabs are today's six, in today's order", app.B2B_TAB_LABELS == (
        "📬 Outbox", "📨 Sent", "🔎 Prospects", "🎬 Make clips", "🎮 GTA", "🩺 System"))

    os.environ["NAV_MODE"] = "gaming"
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    labels = [t.label for t in at.tabs]
    check("gaming: the app runs", not at.exception, [e.value for e in at.exception])
    check("gaming: GTA, Make clips, System, in that order", labels[:3] == ["🎮 GTA", "🎬 Make clips", "🩺 System"], labels)
    check("gaming: Outbox, Sent and Prospects are hidden", not {"📬 Outbox", "📨 Sent", "🔎 Prospects"} & set(labels), labels)
    check("gaming: no outreach header", not any(m.label in ("Ready to send", "Waiting for a reply", "Up next") for m in at.metric),
          [m.label for m in at.metric])
    modes = [r for r in at.radio if r.key == "gta_mode"]
    check("gaming: the GTA tab has Narrate, Stream clips and Find Streamers",
          len(modes) == 1 and list(modes[0].options) == ["🎙️ Narrate", "📺 Stream clips", "🔎 Find Streamers"],
          modes and modes[0].options)
    check("gaming: says how to get the B2B app back", any("NAV_MODE=b2b" in c.value for c in at.caption))
    tables = " ".join(str(t.value) for t in at.table)
    check("gaming: System shows the mode and what the GTA tools need, never a key's value",
          "NAV_MODE" in tables and "Twitch API" in tables and "TwitchDownloaderCLI" in tables
          and (os.getenv("TWITCH_CLIENT_SECRET") or "no-secret-set") not in tables, tables[:300])
    check("gaming: the outreach worker is tucked into an expander on System",
          any("LinkedIn outreach worker" in e.label for e in at.expander), [e.label for e in at.expander][:6])
    check("gaming: Make clips is still there", any(t.label == "🎬 Make clips" for t in at.tabs)
          and any("YouTube" in str(w.label) or "video" in str(w.label).lower() for w in at.text_input))

    os.environ["NAV_MODE"] = "b2b"
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    labels = [t.label for t in at.tabs]
    check("b2b: the app runs", not at.exception, [e.value for e in at.exception])
    check("b2b: all six tabs, as before", labels[:6] == list(app.B2B_TAB_LABELS), labels)
    check("b2b: the outreach header is back", {"Ready to send", "Waiting for a reply", "Up next"} <= {m.label for m in at.metric},
          [m.label for m in at.metric])
    check("b2b: the Worker section is a plain section again", any(s.value == "Worker" for s in at.subheader)
          and not any("LinkedIn outreach worker" in e.label for e in at.expander))
    check("b2b: the GTA tab keeps its three modes", any(r.key == "gta_mode" and len(r.options) == 3 for r in at.radio))
except ImportError as exc:  # streamlit missing
    check("streamlit installed for the app check", False, exc)

# ---------------------------------------------------------------- chat: the project-folder TwitchDownloaderCLI
from core.clip_mode import chat as C  # noqa: E402

local = ROOT / "TwitchDownloaderCLI"
found = C.twitch_downloader()
check("TwitchDownloaderCLI in the project folder is found without PATH",
      (found is not None) if local.is_file() and os.access(local, os.X_OK) else True, found)

import tools.clip_stream as CS  # noqa: E402
from core.clip_mode import windows as W  # noqa: E402

chat_dir = TMP / "chat-run"
chat_dir.mkdir()
progress = W.Progress(chat_dir / "progress.json")
real_download = C.download
tries: list[str] = []


def failing(url, out):
    tries.append(url)
    raise subprocess.CalledProcessError(1, ["TwitchDownloaderCLI", "chatdownload"])


def working(url, out):
    tries.append(url)
    (Path(out) / "chat_1.json").write_text('{"comments": [{"content_offset_seconds": 1, "message": {"body": "hi"}}]}')
    return Path(out) / "chat_1.json"


try:
    C.download = failing
    check("a chat download that fails does not stop the run", CS.chat_replay("https://www.twitch.tv/videos/1", chat_dir, progress) is None
          and not progress.done("chat", "https://www.twitch.tv/videos/1"))
    C.download = working
    got = CS.chat_replay("https://www.twitch.tv/videos/1", chat_dir, progress)
    check("... and the next run tries the chat again", got == chat_dir / "chat_1.json" and len(tries) == 2
          and progress.done("chat", "https://www.twitch.tv/videos/1"))
    check("a chat already downloaded is kept", CS.chat_replay("https://www.twitch.tv/videos/1", chat_dir, progress) == got
          and len(tries) == 2)
finally:
    C.download = real_download

print(f"\n{len(FAILED)} failed" if FAILED else "\nAll Round 66 checks passed.")
sys.exit(1 if FAILED else 0)
