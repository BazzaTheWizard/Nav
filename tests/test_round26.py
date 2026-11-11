"""Round 26 checks: 🌐 Publish to Client Preview Page in 🎬 Make clips.

Run from the project root:  .venv/bin/python tests/test_round26.py .
Uses a fake R2 and temporary files. It never uploads anything.
"""
import json
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


from core import clip_pages as C  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="round26-"))
BASE = "https://clips.navstudiolab.com"

# ---------- publish_page: the slug, the same upload as the worker ----------
uploads, objects = [], {}
client = SimpleNamespace(put_object=lambda **kw: objects.update({kw["Key"]: kw["Body"]}))
C._require = lambda: (SimpleNamespace(bucket="nav-clips", base_url=BASE), client)
C._upload_clip = lambda client, settings, name, index, path: uploads.append((name, index, Path(path).name))
clips = [{"path": str(tmp / "a.mp4"), "start": 10, "end": 40}, {"path": str(tmp / "b.mp4"), "start": 60, "end": 95}]
name, url = C.publish_page("Dana Growth Co", "Ep 41", clips, kind="episode", slug="Dana Growth!")
check("Custom slug + a random 6-character end", name.startswith("dana-growth-") and len(name) == len("dana-growth-") + 6, name)
check("Address on the clip-page domain", url == f"{BASE}/{name}", url)
check("Every clip uploaded the worker's way, then the page", [u[1] for u in uploads] == [1, 2] and f"{name}/page.json" in objects)
info = json.loads(objects[f"{name}/page.json"])
check("Page says who it's for and 'episode'", info["channel_title"] == "Dana Growth Co" and info["kind"] == "episode"
      and info["clips"] == [{"start": 10, "end": 40}, {"start": 60, "end": 95}])
name2, _ = C.publish_page("Dana Growth Co", "Ep 41", clips)
check("No slug: the channel name; never the same address twice", name2.startswith("dana-growth-co-") and name2 != name)

# ---------- the local list of published pages ----------
C.MANUAL_PAGES_PATH = tmp / "storage" / "manual_pages.json"
C.record_manual_page("old-page-aaaaaa", f"{BASE}/old-page-aaaaaa", "Old", "T", 3)
entry = C.record_manual_page(name, url, "Dana Growth Co", "Ep 41", 2)
check("Saved to storage/manual_pages.json, newest first",
      [p["page_name"] for p in C.manual_pages()] == [name, "old-page-aaaaaa"] and entry["page_url"] == url)

# ---------- the app ----------
video = tmp / "clip_01.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=1",
                "-pix_fmt", "yuv420p", str(video)], check=True)
store = tmp / "app_storage"
store.mkdir()
wrapper = tmp / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys; sys.path.insert(0, {str(ROOT)!r})
    from pathlib import Path
    from types import SimpleNamespace
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(store)!r})
    for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, name, W.STORAGE_DIR / file)
    W.get_worker_status = lambda: {{"running": False}}
    from core import clip_pages as C
    C.configured = lambda: True
    C.page_settings = lambda: SimpleNamespace(base_url={BASE!r})
    C.MANUAL_PAGES_PATH = Path({str(store / "manual_pages.json")!r})
    C.page_activity = lambda since=None: {{"dana-growth-k7m2qx": {{"opens": 1, "played": {{1}}, "half": set(), "finished": set(),
                                                        "downloaded": set(), "last": None}}}}
    calls = Path({str(store / "calls.json")!r})
    def fake_publish(channel, title, clips, kind="video", slug=""):
        calls.write_text(__import__("json").dumps([channel, title, len(clips), kind, slug]))
        return "dana-growth-k7m2qx", {BASE!r} + "/dana-growth-k7m2qx"
    C.publish_page = fake_publish
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
from streamlit.testing.v1 import AppTest  # noqa: E402
from core.hook_selector import ClipResult  # noqa: E402

at = AppTest.from_file(str(wrapper), default_timeout=120)
at.session_state["generated_clips"] = [(video, ClipResult(start_time=12.0, end_time=41.5, viral_title="Price on outcomes",
                                                          caption="", hashtags=[], virality_score=90))]
at.session_state["agent_channel_name"] = "Dana Growth Co"
at.session_state["agent_video_title"] = "Ep 41: pricing retainers"
at.run()
check("App runs", not at.exception, str([e.value for e in at.exception][:1]))
inputs = {t.label: t for t in at.text_input}
check("Form is filled from the video", inputs["Client / channel name"].value == "Dana Growth Co"
      and inputs["Episode title"].value == "Ep 41: pricing retainers", str(list(inputs)))
publish = next((b for b in at.button if b.label == "🌐 Publish to Client Preview Page"), None)
check("🌐 Publish to Client Preview Page button", publish is not None)
inputs["Custom slug / prospect name (optional)"].set_value("dana-growth")
publish.click().run()
sent = json.loads((store / "calls.json").read_text())
check("Publishes the rendered clip with your slug", sent == ["Dana Growth Co", "Ep 41: pricing retainers", 1, "episode", "dana-growth"], str(sent))
check("Success banner with the live address",
      any(f"{BASE}/dana-growth-k7m2qx" in str(s.value) for s in at.success), str([s.value for s in at.success]))
check("Saved for tracking", json.loads((store / "manual_pages.json").read_text())[0]["page_name"] == "dana-growth-k7m2qx")
check("Listed with what the visitor did", any("Published preview pages (1)" in e.label for e in at.expander)
      and any("Played Short 1" in str(c.value) for c in at.caption))
check("No error shown", not at.error, str([e.value for e in at.error]))

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
