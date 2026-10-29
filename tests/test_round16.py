"""Checks for round 16: personal clip pages (clips.navstudiolab.com/<page>).
Uses a fake R2 bucket in memory and temp folders: nothing is uploaded anywhere.
Run all tests: .venv/bin/python tests/run_all.py"""
import io, json, os, subprocess, sys, tempfile, textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ.pop("NAV_NO_CLIP_PAGES", None)  # this file uses a fake bucket
for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "CLIP_PAGES_URL", "DRIVE_BACKUP"):
    os.environ[name] = ""  # set (empty) so your .env can't fill them in

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class FakeR2:
    """Just enough of boto3's S3 client; lists 2 keys per page to exercise paging."""
    def __init__(self):
        self.objects, self.log = {}, []
    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.log.append(("upload", key)); self.objects[key] = (Path(filename).read_bytes(), dict(ExtraArgs or {}))
    def put_object(self, Bucket, Key, Body, **meta):
        self.log.append(("put", Key)); self.objects[Key] = (Body if isinstance(Body, bytes) else Body.encode(), meta)
    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key][0])}
    def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None, MaxKeys=None):
        # Like S3/R2: the token marks the last key returned, not a position.
        keys = sorted(k for k in self.objects if k.startswith(Prefix) and (ContinuationToken is None or k > ContinuationToken))
        page = keys[:2]
        more = len(keys) > 2
        return {"Contents": [{"Key": k} for k in page], "IsTruncated": more, **({"NextContinuationToken": page[-1]} if more else {})}
    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)


import core.clip_pages as C

r2 = FakeR2()
C._client = lambda settings: r2
check("Off until all five settings are in .env", not C.configured())
os.environ.update(R2_ACCOUNT_ID="acct", R2_ACCESS_KEY_ID="key", R2_SECRET_ACCESS_KEY="secret", R2_BUCKET="nav-clips",
                  CLIP_PAGES_URL="https://clips.navstudiolab.com/")
check("On with all five (trailing slash ignored)", C.configured() and C.page_settings().base_url == "https://clips.navstudiolab.com")
os.environ["NAV_NO_CLIP_PAGES"] = "1"
check("NAV_NO_CLIP_PAGES (tests) turns them off", not C.configured())
os.environ.pop("NAV_NO_CLIP_PAGES")

name = C.make_page_name("Leighton Grey - Grey Matter Podcast 🇨🇦")
check("Page name: readable + 6 random characters", name.startswith("leighton-grey-grey-matter-podcast-") and len(name.rsplit("-", 1)[1]) == 6, name)
check("Page names follow the Worker's rule", all(C.PAGE_NAME.fullmatch(C.make_page_name(t)) for t in ("Ünïcødé Ça", "!!!", "A" * 200, "Coach JB Show")))
check("Two pages for the same creator get different addresses", C.make_page_name("Coach") != C.make_page_name("Coach"))

tmp = Path(tempfile.mkdtemp(prefix="r16-"))
clips = []
for i in range(3):
    path = tmp / f"clip_{i + 1:02d}.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=3", "-f", "lavfi", "-i", "sine=d=3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    clips.append({"path": path, "start": 100 + i * 60, "end": 140 + i * 60})

# ---------- Publishing ----------
name, url = C.publish_page("Coach Dana", "Knee Pain Fixes", clips, kind="episode")
check("Page address", url == f"https://clips.navstudiolab.com/{name}")
keys = sorted(k for k in r2.objects if k.startswith(name + "/"))
check("Uploaded: 3 clips, 3 posters, page.json", keys == sorted([f"{name}/clip_0{i}.mp4" for i in (1, 2, 3)] + [f"{name}/poster_0{i}.jpg" for i in (1, 2, 3)] + [f"{name}/page.json"]), str(keys))
check("page.json goes up last (the page only appears once complete)", r2.log[-1] == ("put", f"{name}/page.json"))
check("Clips stored as video/mp4", r2.objects[f"{name}/clip_01.mp4"][1]["ContentType"] == "video/mp4")
check("Clips are the exact rendered files", r2.objects[f"{name}/clip_02.mp4"][0] == clips[1]["path"].read_bytes())
check("Posters are JPEGs", r2.objects[f"{name}/poster_01.jpg"][0][:3] == b"\xff\xd8\xff")
info = json.loads(r2.objects[f"{name}/page.json"][0])
check("page.json: creator, video, episode, clip times",
      info["channel_title"] == "Coach Dana" and info["video_title"] == "Knee Pain Fixes" and info["kind"] == "episode"
      and info["clips"] == [{"start": 100, "end": 140}, {"start": 160, "end": 200}, {"start": 220, "end": 260}])

broken = [dict(clips[0]), {"path": tmp / "missing.mp4", "start": 0, "end": 30}]
before = set(r2.objects)
try:
    C.publish_page("Half Done", "x", broken)
    check("A failed upload leaves nothing behind", False)
except Exception:
    check("A failed upload leaves nothing behind", set(r2.objects) == before)

# ---------- After a trim ----------
new_clip = tmp / "new.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=1080x1920:r=30:d=2", "-c:v", "libx264", "-pix_fmt",
                "yuv420p", str(new_clip)], check=True)
C.replace_clip(name, "clip_02.mp4", new_clip, 158.5, 199.0)
check("Trim: same address, new clip", r2.objects[f"{name}/clip_02.mp4"][0] == new_clip.read_bytes())
check("Trim: new times on the page", json.loads(r2.objects[f"{name}/page.json"][0])["clips"][1] == {"start": 158.5, "end": 199.0})
for bad in ("clip_10.mp4", "../page.json", "poster_01.jpg"):
    try:
        C.replace_clip(name, bad, new_clip)
        check(f"Trim refuses {bad}", False)
    except ValueError:
        check(f"Trim refuses {bad}", True)

# ---------- Plays ----------
now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
ms = lambda hours_ago: int((now - timedelta(hours=hours_ago)).timestamp() * 1000)
for key in (f"{ms(5)}_open_0_aaaa1111", f"{ms(4.9)}_play_1_bbbb2222", f"{ms(4.8)}_half_1_cccc3333", f"{ms(4.5)}_play_3_dddd4444",
            f"{ms(4.4)}_end_3_eeee5555", f"{ms(3)}_dl_2_ffff6666", "junk", f"{ms(1)}_hack_1_gggg7777"):
    r2.objects[f"_events/{name}/{key}"] = (b"", {})
r2.objects[f"_events/other-page-zzzzzz/{ms(30)}_open_0_hhhh8888"] = (b"", {})
activity = C.page_activity()
mine = activity[name]
check("Plays read from R2 (all pages, one listing)", mine["played"] == {1, 3} and mine["finished"] == {3} and mine["half"] == {1}
      and mine["downloaded"] == {2} and mine["opens"] == 1 and "other-page-zzzzzz" in activity)
check("Activity line", C.describe_activity(mine, now) == "▶ Played Shorts 1 and 3 (watched 3 to the end) · ⬇ downloaded 2 · 3 h ago",
      C.describe_activity(mine, now))
check("Opened, no plays", C.describe_activity(activity["other-page-zzzzzz"], now) == "👀 Opened, no plays yet · 30 h ago",
      C.describe_activity(activity["other-page-zzzzzz"], now))
check("Nothing yet", C.describe_activity(None) == "Not opened yet")

# ---------- Delete ----------
removed = C.delete_page(name)
check("Delete: page files and its plays (7 + 8, across list pages)", removed == 15 and not any(k.startswith((name + "/", f"_events/{name}/")) for k in r2.objects), str(removed))
check("Delete: other pages untouched", any(k.startswith("_events/other-page-zzzzzz/") for k in r2.objects))
try:
    C.delete_page("../")
    check("Delete refuses odd names", False)
except ValueError:
    check("Delete refuses odd names", True)

# ---------- Setup check: https certificates, and no crash when the site can't be reached ----------
import ssl, urllib.error, urllib.request
ctx = C._https_context()
check("Setup check uses certifi's certificate list (python.org Python has none of its own)",
      isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.cert_store_stats()["x509_ca"] > 50,
      str(ctx.cert_store_stats()))
contexts, lines = [], []
real_urlopen = urllib.request.urlopen
def failing_urlopen(request, timeout=None, context=None):
    contexts.append(context)
    raise urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
urllib.request.urlopen = failing_urlopen
try:
    result = C.check_setup(out=lines.append)
    crashed = False
except Exception as exc:
    result, crashed = None, exc
urllib.request.urlopen = real_urlopen
check("Unreachable page -> ✗ lines and a clear ending, not a crash", crashed is False and result is False
      and any(l.startswith("✗ The page opens") for l in lines) and any(l.startswith("✗ Plays are recorded") for l in lines)
      and lines[-1].startswith("Something above needs fixing"), str(crashed or lines))
check("Every request used the certifi list", contexts and all(c is not None for c in contexts), str(contexts))
check("The test page is still cleaned up", "✓ Test page deleted" in lines and not any(k.startswith("setup-check-") for k in r2.objects))

# ---------- Worker ----------
import autonomous_worker as W
from core import outreach as O
W.STORAGE_DIR = tmp / "storage"; W.STORAGE_DIR.mkdir()
for attr, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                   ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                   ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
    setattr(W, attr, W.STORAGE_DIR / file)
W.init_storage_files()
DRIVE = "https://drive.google.com/drive/folders/abc"
drive_calls = []
W.check_drive_access = lambda: drive_calls.append("check")
W.upload_clips_to_drive = lambda *a, **k: drive_calls.append("upload") or DRIVE
W.download_source_video = lambda url: str(tmp / "src.mp4")
(tmp / "src.mp4").write_bytes(b"x")
W.on_camera_share = lambda path: 1.0
W.extract_audio_only = lambda s, d: d
W.transcribe_audio = lambda audio, settings: []
W._video_duration = lambda path: 1800.0
W.video_info_and_peaks = lambda url: ({}, [])
from core.hook_selector import ClipResult
picked = [ClipResult(start_time=100 + i * 60, end_time=140 + i * 60, viral_title="t", caption="c", hashtags=["x"], virality_score=80) for i in range(3)]
W.pick_clips_with_agent = lambda *a, **k: (picked, O.build_outreach_email("Coach Dana", "Knee Pain Fixes", "fixing knee pain", content_format="episode"), "3 Shorts")
def fake_render(source, clips_, job_dir, settings, words, layouts_out=None, rendered_out=None):
    paths = []
    for i, clip in enumerate(clips_, start=1):
        out = job_dir / f"clip_{i:02d}.mp4"; out.write_bytes(clips[i - 1]["path"].read_bytes()); paths.append(out)
    return paths
W._render_selected_clips = fake_render
settings = SimpleNamespace(temp_dir=tmp / "temp", raw_dir=tmp)
lead = {"channel_id": "UC1", "channel_title": "Coach Dana", "latest_video_title": "Knee Pain Fixes",
        "latest_video_url": "https://www.youtube.com/watch?v=abc"}

record = W._process_lead(dict(lead), settings)
check("Worker: the email links to the clip page", record["page_url"] in record["full_email_body"] and DRIVE not in record["full_email_body"]
      and record["page_url"].startswith("https://clips.navstudiolab.com/coach-dana-"), record["page_url"])
check("Worker: Drive still gets a backup copy", record["drive_link"] == DRIVE and drive_calls == ["check", "upload"])
check("Worker: page knows it's an episode", json.loads(r2.objects[f"{record['page_name']}/page.json"][0])["kind"] == "episode")

os.environ["DRIVE_BACKUP"] = "off"
drive_calls.clear()
record2 = W._process_lead(dict(lead, channel_id="UC2"), settings)
check("DRIVE_BACKUP=off: no Drive check or upload, page link only", drive_calls == [] and record2["drive_link"] == ""
      and record2["page_url"] in record2["full_email_body"])

real_publish = C.publish_page
C.publish_page = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("R2 down"))
os.environ["DRIVE_BACKUP"] = "on"
record3 = W._process_lead(dict(lead, channel_id="UC3"), settings)
check("Page fails -> the email uses the Drive link", record3["page_url"] is None and DRIVE in record3["full_email_body"])
os.environ["DRIVE_BACKUP"] = "off"
try:
    W._process_lead(dict(lead, channel_id="UC4"), settings)
    check("Page fails and no Drive backup -> the lead is retried later, not sent linkless", False)
except RuntimeError as exc:
    check("Page fails and no Drive backup -> the lead is retried later, not sent linkless", "no link" in str(exc))
C.publish_page = real_publish
os.environ["DRIVE_BACKUP"] = "on"

pages_before = {k.split("/")[0] for k in r2.objects if not k.startswith("_events")}
real_fill = W.fill_drive_link
W.fill_drive_link = lambda *a: (_ for _ in ()).throw(RuntimeError("late failure"))
try:
    W._process_lead(dict(lead, channel_id="UC5"), settings)
except RuntimeError:
    pass
W.fill_drive_link = real_fill
check("A pitch that fails after its page was made leaves no page behind",
      {k.split("/")[0] for k in r2.objects if not k.startswith("_events")} == pages_before)

# Follow-ups and "Passed recently" use the page link
pending = json.loads(W.PENDING_OUTREACH_PATH.read_text())
for r in pending:
    if r["record_id"] == record["record_id"]:
        r["status"], r["sent_at"] = "sent", (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
W._write_json_atomic(W.PENDING_OUTREACH_PATH, pending)
waiting = [r for r in W.get_awaiting_reply() if r["record_id"] == record["record_id"]]
check("Follow-up email links to the page", waiting and waiting[0]["follow_up_due"] and record["page_url"] in waiting[0]["follow_up_body"])
W.skip_lead("UC2", pass_reason=next(iter(W.PASS_REASONS)))
check("Pass keeps the page for now ('Passed recently' can still restore it)",
      any(k.startswith(record2["page_name"] + "/") for k in r2.objects)
      and any(r["record_id"] == record2["record_id"] for r in W.get_recently_passed()))

# Cleanup of old pages
r2.objects["unknown-page-qqqqqq/page.json"] = (b"{}", {})
def set_times(sent_days=None, skipped_days=None, replied=False, rid=None):
    path = W.PENDING_OUTREACH_PATH if sent_days is not None else W.SKIPPED_LEADS_PATH
    records = json.loads(path.read_text())
    for r in records:
        if r["record_id"] == rid:
            if sent_days is not None:
                r["status"], r["sent_at"] = "sent", (datetime.now(timezone.utc) - timedelta(days=sent_days)).isoformat()
                if replied:
                    r["replied_at"] = datetime.now(timezone.utc).isoformat()
            else:
                r["skipped_at"] = (datetime.now(timezone.utc) - timedelta(days=skipped_days)).isoformat()
    W._write_json_atomic(path, records)
set_times(sent_days=10, rid=record["record_id"])
check("Cleanup: a 10-day-old sent page stays", W.prune_clip_pages() == 0 and any(k.startswith(record["page_name"] + "/") for k in r2.objects))
set_times(skipped_days=15, rid=record2["record_id"])
set_times(sent_days=61, rid=record["record_id"])
check("Cleanup: sent 61 days ago without a reply, and passed 15 days ago -> both removed", W.prune_clip_pages() == 2
      and not any(k.startswith((record["page_name"] + "/", record2["page_name"] + "/")) for k in r2.objects))
check("Cleanup: marked so it isn't redone", all(r.get("page_removed_at") for r in json.loads(W.PENDING_OUTREACH_PATH.read_text()) if r["record_id"] == record["record_id"]))
check("Cleanup: a page on no record is never touched", "unknown-page-qqqqqq/page.json" in r2.objects)
record6 = W._process_lead(dict(lead, channel_id="UC6"), settings)
W.mark_outreach_sent(record6["record_id"])
set_times(sent_days=90, replied=True, rid=record6["record_id"])
check("Cleanup: a page whose creator replied stays", W.prune_clip_pages() == 0 and any(k.startswith(record6["page_name"] + "/") for k in r2.objects))

record7 = W._process_lead(dict(lead, channel_id="UC7"), settings)  # waiting in the Outbox, with a page

# ---------- App ----------
import app as A
calls = []
A.clip_pages.replace_clip = lambda *a: calls.append(("page",) + tuple(str(x) for x in a))
A.replace_clip_in_drive = lambda *a: calls.append(("drive",) + tuple(str(x) for x in a))
replace = A._clip_replacer({"page_name": "coach-dana-abcdef", "drive_link": DRIVE}, 98.5, 140.0)
replace(Path("/x/clip_01.trim.mp4"), "clip_01.mp4")
check("Trim in the app: updates their page (with new times) and the Drive copy",
      calls == [("page", "coach-dana-abcdef", "clip_01.mp4", "/x/clip_01.trim.mp4", "98.5", "140.0"), ("drive", DRIVE, "clip_01.mp4", "/x/clip_01.trim.mp4")], str(calls))
check("Nothing to replace for a record without links", A._clip_replacer({}, 0, 1) is None)

from streamlit.testing.v1 import AppTest  # noqa: E402
wrapper = tmp / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys; sys.path.insert(0, {str(ROOT)!r})
    from pathlib import Path
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(W.STORAGE_DIR)!r})
    for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, name, W.STORAGE_DIR / file)
    import core.clip_pages as C
    C.page_activity = lambda: {{{record6['page_name']!r}: {{"opens": 2, "played": {{2}}, "half": set(), "finished": set(), "downloaded": set(), "last": None}}}}
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
pending = json.loads(W.PENDING_OUTREACH_PATH.read_text())
for r in pending:
    if r["record_id"] == record6["record_id"]:
        r["sent_at"] = datetime.now(timezone.utc).isoformat()
        r.pop("replied_at", None)
W._write_json_atomic(W.PENDING_OUTREACH_PATH, pending)
at = AppTest.from_file(str(wrapper), default_timeout=120).run()
check("App runs", not at.exception, str([e.value for e in at.exception]))
links = {getattr(b.proto, "label", "") for b in at.get("link_button")}
check("Outbox: 'Open their page' and 'Drive backup' buttons", "🌐 Open their page (what they'll see)" in links and "📁 Drive backup" in links, str(links))
check("Sent: shows what they did on the page", any("▶ Played Short 2" in c.value for c in at.caption), str([c.value for c in at.caption][:12]))
check("System: clip pages on, with the address",
      any("on: https://clips.navstudiolab.com" in str(t.value) for t in at.table))

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
