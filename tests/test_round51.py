"""Round 51: prospects, direct pitches, no auto-dubbed videos, Outbox links, honest page views.

1. Auto-dubbed videos: never cut (the worker skips the lead before downloading; downloads always
   take the original audio track; the prospect finder never picks a non-English upload).
2. Prospects: 🚫 Pass on each, 🧹 Clear all at the top (gone for good, even from later harvests),
   ✉️ Direct pitch for ones with no upload to cut (an intro email straight to the Outbox).
3. Direct pitches in the Outbox and Sent: no clips, their own follow-ups and yes-reply, and they
   never hold up the worker's 5 Outbox slots.
4. The Outbox card: their YouTube channel's About page where the clip page button was, then every
   other link (website, LinkedIn, contact form, podcast, socials).
5. Page views: no pop-ups; your own networks never count (views, plays, downloads, copies).

Run: timeout 300 .venv/bin/python tests/test_round51.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for key in ("AUTO_DUB_CHECK", "VISUAL_OVERLAYS", "LEGACY_UI", "EXEC_EMAIL"):
    os.environ.pop(key, None)
os.environ["NAV_NO_CLIP_PAGES"] = "1"
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


import yt_dlp  # noqa: E402

import autonomous_worker as W  # noqa: E402
from core import clip_pages, outreach as O  # noqa: E402
from core import downloader as D  # noqa: E402
from core import linkedin_harvester as LH  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="round51_"))


def use_storage(folder: Path) -> None:
    W.STORAGE_DIR = folder
    folder.mkdir(parents=True, exist_ok=True)
    for attr, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, attr, folder / file)
    W.init_storage_files()


use_storage(TMP / "store")

# ============================== 1. Auto-dubbed videos ==============================
def tracks(*spec):
    out = []
    for lang, note, ext in spec:
        out.append({"format_id": f"{lang}-{ext}", "ext": ext, "acodec": "mp4a" if ext == "m4a" else "opus",
                    "vcodec": "none", "abr": 128, "language": lang, "format_note": note, "url": "http://x/a",
                    "protocol": "https", "language_preference": 10 if "original" in note else -1})
    return out


dubbed = {"formats": tracks(("es", "Spanish original (default)", "webm"), ("en", "English (United States)", "m4a"))}
check("A Spanish video with an English AI dub is auto-dubbed", (D.auto_dub_reason(dubbed) or "").startswith(D.AUTO_DUBBED)
      and "original audio is es" in D.auto_dub_reason(dubbed))
check("An English original that YouTube dubbed into other languages is fine (we use the original)",
      D.auto_dub_reason({"formats": tracks(("en", "English original (default)", "m4a"), ("es", "Spanish", "m4a"))}) is None)
check("...one audio language: fine", D.auto_dub_reason({"formats": tracks(("en", "English", "m4a"))}) is None)
check("...several languages and none marked original: skipped to be safe",
      "none marked original" in (D.auto_dub_reason({"formats": tracks(("en", "English", "m4a"), ("fr", "French", "m4a"))}) or ""))
check("...an audio-description track isn't a dub",
      D.auto_dub_reason({"formats": tracks(("en", "English original", "m4a"), ("en-desc", "English descriptive", "m4a"))}) is None)
video = [{"format_id": "303", "ext": "webm", "vcodec": "vp9", "acodec": "none", "height": 1080, "width": 1920,
          "url": "http://x/v", "protocol": "https"}]
info = {"id": "abc", "title": "t", "extractor": "youtube", "extractor_key": "Youtube", "webpage_url": "http://x",
        "formats": video + tracks(("en", "English original (default)", "webm"), ("es", "Spanish", "m4a"))}


def picked(sort: list[str]) -> str:
    ydl = yt_dlp.YoutubeDL({"format": D.VIDEO_FORMAT, "format_sort": sort, "quiet": True, "simulate": True})
    result = ydl.process_ie_result(json.loads(json.dumps(info)), download=False)
    return next(f.get("language") for f in result["requested_formats"] if f.get("acodec") != "none")


check("Downloads always take the original audio, even when a dub's file type ranks higher",
      picked(D.video_format_sort()) == "es" and picked(D.download_format_sort()) == "en"
      and D.download_format_sort()[0] == "lang" and D.video_format_sort()[0].startswith("res:"))

W.check_drive_access = lambda: None
calls: list[str] = []
W.fetch_video_info = lambda url: {"auto_dub": D.auto_dub_reason(dubbed)}
W.download_source_video = lambda url: calls.append(url) or (_ for _ in ()).throw(AssertionError("downloaded"))
lead = {"channel_id": "UCdub", "channel_title": "Dubbed Show", "latest_video_title": "Ep", "latest_video_url":
        "https://www.youtube.com/watch?v=abcdefghijk"}
try:
    W._process_lead(lead, SimpleNamespace(temp_dir=TMP / "work", raw_dir=TMP / "raw"))
    check("Worker: an auto-dubbed lead is skipped before anything is downloaded", False)
except W.NotAFit as exc:
    check("Worker: an auto-dubbed lead is skipped before anything is downloaded",
          str(exc).startswith(D.AUTO_DUBBED) and not calls, (str(exc), calls))
W.fetch_video_info = lambda url: (_ for _ in ()).throw(RuntimeError("network"))
D._DOWNLOAD_INFO[lead["latest_video_url"]] = {"auto_dub": D.auto_dub_reason(dubbed)}
W.download_source_video = lambda url: calls.append(url) or str(TMP / "v.mp4")
W.camera_check = lambda path: (_ for _ in ()).throw(AssertionError("went past the check"))
try:
    W._process_lead(lead, SimpleNamespace(temp_dir=TMP / "work", raw_dir=TMP / "raw"))
    check("...and if the lookup failed, the download's own tracks catch it", False)
except W.NotAFit as exc:
    check("...and if the lookup failed, the download's own tracks catch it", str(exc).startswith(D.AUTO_DUBBED) and calls)
state = W._processed_state()
state["records"].append({"channel_id": "UCdub", "status": "not a fit", "error": D.auto_dub_reason(dubbed),
                         "updated_at": datetime.now(timezone.utc).isoformat()})
W._write_json_atomic(W.PROCESSED_CREATORS_PATH, state)
check("Sent's pass summary counts them as auto-dubbed", W.pass_summary().get("auto_dubbed") == 1, W.pass_summary())


class FakeYT:
    """The YouTube API calls youtube_target makes."""
    def __init__(self, videos):
        self.videos_ = videos

    def channels(self):
        return SimpleNamespace(list=lambda **k: {"items": [{"id": "UCx", "snippet": {"title": "Acme"},
                                                            "statistics": {"subscriberCount": "100"},
                                                            "contentDetails": {"relatedPlaylists": {"uploads": "UU"}}}]})

    def playlistItems(self):
        return SimpleNamespace(list=lambda **k: {"items": [{"contentDetails": {"videoId": v["id"]}} for v in self.videos_]})

    def videos(self):
        return SimpleNamespace(list=lambda **k: {"items": self.videos_})


import core.lead_harvester as LeadH  # noqa: E402

LeadH._execute_youtube_request = lambda request: request
now_iso = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
videos = [{"id": "dubbed00001", "snippet": {"title": "Episodio 12", "publishedAt": now_iso, "defaultAudioLanguage": "es"},
           "contentDetails": {"duration": "PT40M"}},
          {"id": "english0001", "snippet": {"title": "Episode 11", "publishedAt": now_iso, "defaultAudioLanguage": "en-US"},
           "contentDetails": {"duration": "PT38M"}}]
target = LH.youtube_target(["https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx"], ask_api=FakeYT(videos))
check("Prospect finder: a non-English (or dubbed) upload is never the episode to cut",
      target.get("latest_video_url", "").endswith("english0001"), target)

# ============================== 2. Prospects: pass, clear, direct pitch ==============================
store = TMP / "prospects.json"
base = {"tier": "hot", "status": "new", "score": 60, "contact": {"name": "Dana Lee", "role": "Host", "email": "dana@acme.com",
        "status": "published"}, "calendar_angle": {"theme": "AI pricing"}}
LH.save_prospects([
    {**base, "id": "p-video", "show": "Video Show", "latest_video_url": "https://www.youtube.com/watch?v=abc",
     "channel_id": "UCv", "feed_url": "https://f/v"},
    {**base, "id": "p-novideo", "show": "Acme Revenue Show", "website": "https://acme.com", "domain": "acme.com",
     "linkedin_company": "https://www.linkedin.com/company/acme", "apple_url": "https://podcasts.apple.com/us/podcast/acme/id1",
     "contact_form": "https://acme.com/contact", "linkedin_people": ["https://www.linkedin.com/in/dana-lee"], "feed_url": "https://f/a"},
    {**base, "id": "p-meh", "show": "Meh Show", "feed_url": "https://f/m"},
    {**base, "id": "p-meh2", "show": "Meh Two", "feed_url": "https://f/m2"},
], store)
check("Has an upload to cut: only with a YouTube episode", LH.has_upload(LH.load_prospects(store)[0])
      and not LH.has_upload(LH.load_prospects(store)[1]))
check("🚫 Pass: gone from the list", LH.pass_prospects(["p-meh"], path=store) == 1
      and "p-meh" not in [p["id"] for p in LH.visible(LH.load_prospects(store))])
check("🧹 Clear: every one shown, gone", LH.pass_prospects(["p-meh2"], reason="cleared", path=store) == 1
      and [p["id"] for p in LH.visible(LH.load_prospects(store))] == ["p-video", "p-novideo"])
check("...and a later harvest never adds them back (they stay on file as known)",
      "feed:https://f/m" in LH._known_keys(LH.load_prospects(store)) and "feed:https://f/m2" in LH._known_keys(LH.load_prospects(store)))
added = LH.direct_pitch(["p-novideo"], path=store, add=W.add_direct_pitch)
record = next(r for r in json.loads(W.PENDING_OUTREACH_PATH.read_text()) if r.get("kind") == "intro")
check("✉️ Direct pitch: straight to the Outbox, the prospect marked", added == 1 and record["status"] == "pending"
      and LH.load_prospects(store)[1]["status"] == "direct" and record["clips"] == [])
body = record["full_email_body"]
check("...with an intro email: their show, their theme, what you do, a free sample, one question",
      body.startswith("Hi Dana,") and "Acme Revenue Show and your recent episodes on AI pricing" in body
      and "short vertical clips for LinkedIn" in body and "for free so you can see the quality first" in body
      and body.split("Best,")[0].count("?") == 1 and 50 <= len(body.split("Best,")[0].split()) <= 100
      and record["subject"] == "LinkedIn clips for Acme Revenue Show", body)
check("...and the contact (email, name), ready to copy", record["contact_email"] == "dana@acme.com"
      and record["contact_name"] == "Dana Lee")
check("...only once", LH.direct_pitch(["p-novideo"], path=store, add=W.add_direct_pitch) == 0
      and W.add_direct_pitch(record) is False)
check("It shows in the Outbox untouched (no template rewrite)",
      [r["record_id"] for r in W.get_pending_outreach()] == ["intro-p-novideo"]
      and W.get_pending_outreach()[0]["full_email_body"] == body)
pending = json.loads(W.PENDING_OUTREACH_PATH.read_text())
pending += [{"record_id": f"w{i}", "channel_id": f"UC{i}", "status": "pending", "clips": [{}]} for i in range(4)]
W._write_json_atomic(W.PENDING_OUTREACH_PATH, pending)
check("Direct pitches don't take one of the worker's 5 Outbox slots",
      sum(W._takes_a_slot(r) for r in pending) == 4 and not W._takes_a_slot(record))
W.mark_outreach_sent("intro-p-novideo")
waiting = next(r for r in W.get_awaiting_reply(datetime.now(timezone.utc) + timedelta(days=5)) if r["record_id"] == "intro-p-novideo")
check("Sent: its own follow-up (the free sample bump, day 4), then the last note",
      waiting["follow_up_step"] == "intro_bump" and waiting["follow_up_total"] == 2 and waiting["follow_up_due"]
      and "send me a recent recording" in waiting["follow_up_body"] and "clip page" not in waiting["follow_up_body"])
W.mark_follow_up("intro-p-novideo", "yes")
yes = next(r for r in W.get_replies() if r["record_id"] == "intro-p-novideo")
check("A yes gets a reply asking for a recording, then the offer",
      "Send me a recent recording" in yes["yes_reply_body"] and "$500/month" in yes["yes_reply_body"]
      and O.DRIVE_LINK_PLACEHOLDER not in yes["yes_reply_body"])

# ============================== 4. The Outbox card's links ==============================
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("nav_app", ROOT / "app.py")
app_src = (ROOT / "app.py").read_text()
prospect = LH.load_prospects(store)[1]
sys.argv = ["app"]
import streamlit  # noqa: E402,F401

app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
pitch = {"record_id": "r1", "channel_id": "UC1234567890abcdefghijkl", "channel_title": "Acme",
         "contact_name": "Dana Lee", "website": "https://acme.com/", "linkedin_url": "https://www.linkedin.com/company/acme",
         "social_links": ["https://x.com/acme", "https://www.instagram.com/acme/", "https://www.youtube.com/@acme",
                          "https://acme.com"], "page_url": "https://clips.navstudiolab.com/acme-k2m4qx"}
about, others = app.identity_links(pitch, prospect)
labels = [label for label, _url in others]
check("Outbox links: the YouTube channel's About page first",
      about == "https://www.youtube.com/channel/UC1234567890abcdefghijkl/about", about)
check("...then website, the person's and the company's LinkedIn, contact form, podcast and socials, no duplicates",
      labels == ["🏢 Website", "💼 Company LinkedIn", "👤 Team member on LinkedIn", "📝 Contact form",
                 "🎙 Apple Podcasts", "𝕏 X / Twitter", "📸 Instagram"], labels)
check("...the contact's own LinkedIn, named, when the prospect has it", app.identity_links(
    pitch, {**prospect, "contact": {**prospect["contact"], "linkedin": "https://www.linkedin.com/in/dana-lee"}})[1][1][0]
    == "👤 Dana Lee on LinkedIn")
check("...and never the clip page (your own visits would count as theirs)",
      "🌐 Their clip page" not in app_src.split("def _render_identity_links")[1].split("if _legacy_ui():")[0]
      and all("clips.navstudiolab.com" not in url for _l, url in others))

# ============================== 5. Page views ==============================
js = (ROOT / "cloudflare" / "clip-pages-worker.js").read_text()
check("No pop-up notifications for page views", "viewed their clips" not in app_src and "views_toasted" not in app_src)
check("The app marks this Mac's network as yours when it starts",
      "clip_pages.register_own_network()" in app_src and 'st.session_state["own_network_marked"]' in app_src)
if shutil.which("node"):
    (TMP / "w.mjs").write_text(js)
    (TMP / "r.mjs").write_text("""import worker from "./w.mjs";
const store = new Map([["acme-k2m4qx/page.json", "{}"]]);
const env = { CLIPS: { head: async (k) => store.has(k) ? {} : null, put: async (k) => { store.set(k, ""); },
  get: async (k) => store.has(k) ? { json: async () => ({ clips: [] }) } : null } };
const beacon = (ip, t) => worker.fetch(new Request("https://x.test/e", { method: "POST",
  body: JSON.stringify({ s: "acme-k2m4qx", t, c: 1 }), headers: { "user-agent": "Mozilla/5.0 Chrome/130", "cf-connecting-ip": ip } }), env);
await beacon("203.0.113.7", "open");                                  // a prospect
const mark = await (await worker.fetch(new Request("https://x.test/_nav_owner?app=1", { headers: { "cf-connecting-ip": "198.51.100.20" } }), env)).json();
await beacon("198.51.100.20", "open"); await beacon("198.51.100.20", "dl"); await beacon("198.51.100.20", "copy");  // you
await worker.fetch(new Request("https://x.test/_nav_owner", { headers: { "cf-connecting-ip": "2001:db8:1:2:aaaa::1" } }), env);
await beacon("2001:db8:1:2:bbbb::9", "play");                         // your phone, same home network, new address
const phonePage = await (await worker.fetch(new Request("https://x.test/_nav_owner", { headers: { "cf-connecting-ip": "192.0.2.1" } }), env)).text();
console.log(JSON.stringify({ keys: [...store.keys()], mark, phonePage }));
""")
    out = subprocess.run(["node", str(TMP / "r.mjs")], capture_output=True, text=True, timeout=30)
    result = json.loads(out.stdout or "{}")
    events = [k for k in result.get("keys", []) if k.startswith("_events/")]
    check("Worker: a prospect's view is recorded; your network's views, downloads and copies aren't",
          len(events) == 1 and "_open_1_" in events[0], (events, out.stderr[-300:]))
    check("...an IPv6 phone on your home network counts as yours too (same /64)", not any("_play_" in k for k in events))
    check("...events carry a short network hash, never the address", "203.0.113.7" not in json.dumps(result)
          and len(events[0].rsplit("_", 1)[1]) == 12)
    check("/_nav_owner marks a network and a browser as yours", result.get("mark", {}).get("ok") is True
          and 'localStorage.setItem("nav_me", "1")' in result.get("phonePage", "")
          and sum(k.startswith("_me/") for k in result.get("keys", [])) == 3)
else:
    print("SKIP  node not installed: the Worker checks need it")
check("Pages opened from a browser marked as yours keep re-marking its network", 'fetch("/_nav_owner?ping=1"' in js)


class FakeR2:
    def __init__(self, keys):
        self.keys = keys

    def list_objects_v2(self, Bucket, Prefix, **_k):
        return {"Contents": [{"Key": k} for k in self.keys if k.startswith(Prefix)]}


now = datetime(2026, 11, 8, 20, 0, tzinfo=timezone.utc)
ms = lambda h: int((now - timedelta(hours=h)).timestamp() * 1000)  # noqa: E731
keys = ["_me/aaaaaaaa", f"_events/acme-k2m4qx/{ms(1)}_open_0_aaaaaaaa1234", f"_events/acme-k2m4qx/{ms(1)}_dl_1_aaaaaaaa5678",
        f"_events/acme-k2m4qx/{ms(2)}_open_0_bbbbbbbb1234", f"_events/acme-k2m4qx/{ms(30)}_open_0_cccccccc"]
clip_pages._require = lambda: (SimpleNamespace(bucket="b"), FakeR2(keys))
activity = clip_pages.page_activity()["acme-k2m4qx"]
check("Sent: your own events are dropped, even ones recorded before you marked the network",
      activity["opens"] == 2 and not activity["downloaded"], activity)
check("'Start counting from now' leaves out everything before it",
      clip_pages.page_activity(since=now - timedelta(hours=10))["acme-k2m4qx"]["opens"] == 1)
clip_pages.VIEWS_SEEN_PATH = TMP / "seen.json"
clip_pages.start_counting_now(now)
check("...and is remembered", clip_pages.views_count_from() == now and clip_pages.views_seen_at() == now)

# ============================== The app ==============================
from streamlit.testing.v1 import AppTest  # noqa: E402

app_store = TMP / "app_store"
use_storage(app_store)
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [
    {**LH.direct_pitch_record(prospect), "kind": "intro", "status": "pending", "clips": []},
])
LH.save_prospects([p for p in LH.load_prospects(store)] + [
    {**base, "id": "p-fresh", "show": "Fresh Podcast", "feed_url": "https://f/fresh"}], store)
wrapper = TMP / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys; sys.path.insert(0, {str(ROOT)!r})
    from pathlib import Path
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(app_store)!r})
    for attr, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, attr, W.STORAGE_DIR / file)
    import core.linkedin_harvester as LH
    LH.PROSPECTS_PATH = Path({str(store)!r})
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
at = AppTest.from_file(str(wrapper), default_timeout=180).run()
check("App loads", not at.exception and not at.error, str([e.value for e in at.exception][:1] + [e.value for e in at.error][:1]))
links = {getattr(b.proto, "label", ""): getattr(b.proto, "url", "") for b in at.get("link_button")}
check("Outbox: the direct pitch with its links, no clip page button",
      "🌐 Their clip page" not in links and any("Direct pitch" in m.value for m in [*at.markdown, *at.caption]), list(links)[:6])
buttons = [b.key or "" for b in at.button]
check("Prospects: Direct pitch for one with no upload, Queue for one with, Pass on each, Clear all",
      "b2b_direct_p-fresh" in buttons and "b2b_queue_p-video" in buttons and "b2b_pass_p-fresh" in buttons
      and "b2b_pass_p-video" in buttons and "b2b_clear_all" in buttons, [b for b in buttons if b.startswith("b2b")])
next(b for b in at.button if b.key == "b2b_pass_p-fresh").click().run()
check("🚫 Pass removes it right away", "b2b_pass_p-fresh" not in [b.key for b in at.button]
      and LH.load_prospects(store)[-1]["status"] == "passed")
next(b for b in at.button if b.key == "b2b_clear_all").click().run()
check("🧹 Clear all removes everyone shown", not [b for b in at.button if (b.key or "").startswith("b2b_pass_")]
      and all(p["status"] in {"passed", "cleared", "direct", "queued"} for p in LH.load_prospects(store)))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(FAILED)} check(s) failed" if FAILED else "\nAll checks passed")
sys.exit(1 if FAILED else 0)
