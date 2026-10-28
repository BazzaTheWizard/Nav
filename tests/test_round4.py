"""Round 4 checks: Shorts-gap leads, emails, no lost creators, rechecks, priority queue.

Run from the project root:  .venv/bin/python tests/test_round4.py .
Uses a simulated YouTube and a temporary storage folder. It never calls YouTube
and never touches your real storage files.
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(root))

import autonomous_worker as W  # noqa: E402
import core.lead_harvester as H  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


NOW = datetime.now(timezone.utc)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- temporary storage
tmp = Path(tempfile.mkdtemp(prefix="round4-"))
(tmp / "storage").mkdir()
H.PROJECT_ROOT = tmp
H.SEEN_CHANNELS_PATH = tmp / "storage" / "seen_channels.json"
H.CHANNEL_CHECKS_PATH = tmp / "storage" / "channel_checks.json"
W.STORAGE_DIR = tmp / "storage"
W.PROCESSED_CREATORS_PATH = tmp / "storage" / "processed_creators.json"
W.PENDING_OUTREACH_PATH = tmp / "storage" / "pending_outreach.json"
W.LOCK_PATH = tmp / "storage" / ".lock"
W.QUOTA_STATUS_PATH = tmp / "storage" / "quota_status.json"
W.SKIPPED_LEADS_PATH = tmp / "storage" / "skipped_leads.json"
W.SEEN_CHANNELS_PATH = H.SEEN_CHANNELS_PATH
W.LEAD_QUEUE_PATH = tmp / "storage" / "lead_queue.json"

# ---------------------------------------------------------------- simulated YouTube
class NotFound(Exception):
    def __init__(self):
        super().__init__("404 playlistNotFound")
        self.resp = type("R", (), {"status": 404})()


class ServerError(Exception):
    def __init__(self):
        super().__init__("500 backendError")
        self.resp = type("R", (), {"status": 500})()


CHANNELS, PLAYLISTS, VIDEOS = {}, {}, {}
CALLS = {"playlistItems": 0, "videos": 0, "channels": 0}


def long_video(vid, days, minutes, views, description=""):
    VIDEOS[vid] = {
        "id": vid,
        "snippet": {"title": f"English title {vid}", "publishedAt": ago(days), "description": description},
        "contentDetails": {"duration": f"PT{minutes}M"},
        "statistics": {"viewCount": str(views)},
    }
    return (vid, days)


def short_video(vid, days, views):
    VIDEOS[vid] = {
        "id": vid,
        "snippet": {"title": f"Short {vid}", "publishedAt": ago(days)},
        "contentDetails": {"duration": "PT45S"},
        "statistics": {"viewCount": str(views)},
    }
    return (vid, days)


def channel(cid, subs, longs, shorts=None, shorts_total=None, description="", uulf=True, shorts_error=False):
    suffix = cid[2:]
    CHANNELS[cid] = {
        "id": cid,
        "snippet": {"title": cid, "description": description},
        "statistics": {"subscriberCount": str(subs)},
        "contentDetails": {"relatedPlaylists": {"uploads": f"UU{suffix}"}},
    }
    PLAYLISTS[f"UU{suffix}"] = (longs + (shorts or []), None)
    if uulf:
        PLAYLISTS[f"UULF{suffix}"] = (longs, None)
    if shorts_error:
        PLAYLISTS[f"UUSH{suffix}"] = "error"
    elif shorts:
        PLAYLISTS[f"UUSH{suffix}"] = (shorts, shorts_total or len(shorts))


def recent_longs(prefix, minutes=35, views=10_000, count=4, description=""):
    return [long_video(f"{prefix}L{i}", 3 + i * 7, minutes, views, description if i == 1 else "") for i in range(count)]


# 1. Your example: 2,000 Shorts but stopped 4 months ago. Email only in a video description.
channel(
    "UClapsed", 40_000,
    recent_longs("lap", description="Business inquiries: team@lapsedcreator.com\nlogo@2x.png"),
    shorts=[short_video(f"lapS{i}", 120 + i, 30_000) for i in range(50)], shorts_total=2000,
)
# 2. Active Shorts that clearly work (5x their long-video views) -> skipped
channel("UCworking", 60_000, recent_longs("work"), shorts=[short_video(f"workS{i}", 1 + i * 3, 50_000) for i in range(12)])
# 3. Active Shorts that don't work (5% of long-video views) -> kept, low priority
channel("UCweak", 30_000, recent_longs("weak"), shorts=[short_video(f"weakS{i}", 1 + i * 3, 500) for i in range(10)])
# 4. No Shorts at all (no Shorts list -> 404); only 8-minute videos
channel("UCnone", 20_000, recent_longs("none", minutes=8))
# 5. Fading: ~10 a month before, only 2 in the last 30 days
channel(
    "UCfading", 25_000, recent_longs("fade"),
    shorts=[short_video(f"fadeR{i}", 5 + i * 10, 2_000) for i in range(2)]
    + [short_video(f"fadeP{i}", 31 + i * 3, 2_000) for i in range(30)],
)
# 6. No long-video list: falls back to all uploads and filters out Shorts by length
channel("UCnoLF", 15_000, recent_longs("nolf"), shorts=[short_video(f"nolfS{i}", 200 + i, 1_000) for i in range(3)], uulf=False)
# 7. Too big
channel("UCbig", 500_000, recent_longs("big"))
# 8. Only one long video this month
channel("UCquiet", 20_000, [long_video("quietL0", 3, 30, 5_000), long_video("quietL1", 50, 30, 5_000)])
# 9. Shorts list errors out -> "couldn't check", never skipped
channel("UCshortsErr", 18_000, recent_longs("serr"), shorts_error=True)


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeYouTube:
    def channels(self):
        return self

    def videos(self):
        return self

    def playlistItems(self):
        return self

    def list(self, part, id=None, maxResults=50, playlistId=None):
        if playlistId is not None:
            def fetch():
                CALLS["playlistItems"] += 1
                data = PLAYLISTS.get(playlistId)
                if data is None:
                    raise NotFound()
                if data == "error":
                    raise ServerError()
                items, total = data
                newest = sorted(items, key=lambda item: item[1])[:maxResults]
                return {
                    "items": [{"contentDetails": {"videoId": v, "videoPublishedAt": ago(d)}} for v, d in newest],
                    "pageInfo": {"totalResults": total if total is not None else len(items)},
                }
            return Req(fetch)
        ids = id.split(",")
        if part.startswith("snippet,statistics"):
            CALLS["channels"] += 1
            return Req(lambda: {"items": [CHANNELS[c] for c in ids if c in CHANNELS]})
        CALLS["videos"] += 1
        return Req(lambda: {"items": [VIDEOS[v] for v in ids if v in VIDEOS]})


H._youtube_client = lambda: FakeYouTube()
H._search_candidates = lambda youtube, query: [
    {"channel_id": c, "video_id": f"{c}-hit", "channel_title": c, "video_title": "t"} for c in CHANNELS
]

# ---------------------------------------------------------------- discovery
leads = H.discover_channels(["any query"], max_results=2)
by_id = {lead["channel_id"]: lead for lead in leads}
checks = json.loads(H.CHANNEL_CHECKS_PATH.read_text())

check("No qualified creator is thrown away (max_results=2, all kept)", len(leads) == 6, sorted(by_id))
check("Your case: 2,000 Shorts but stopped -> kept as 'lapsed'",
      by_id.get("UClapsed", {}).get("shorts", {}).get("status") == "lapsed",
      by_id.get("UClapsed", {}).get("shorts_summary", ""))
check("…and it is the top priority lead",
      max(leads, key=lambda lead: lead["priority"])["channel_id"] == "UClapsed",
      str({k: v["priority"] for k, v in by_id.items()}))
check("Email found in a video description (image file name ignored)",
      by_id.get("UClapsed", {}).get("contact_email") == "team@lapsedcreator.com",
      str(by_id.get("UClapsed", {}).get("contact_email")))
check("Active Shorts that clearly work -> skipped", "UCworking" not in by_id and checks["UCworking"]["result"] == "shorts_already_working",
      checks.get("UCworking", {}).get("shorts", {}).get("summary", ""))
check("…as a soft skip (rechecked later in case they stop)", checks["UCworking"]["soft"] is True)
check("Active but weak Shorts -> kept", by_id.get("UCweak", {}).get("shorts", {}).get("status") == "active_weak",
      by_id.get("UCweak", {}).get("shorts_summary", ""))
check("No Shorts at all -> kept", by_id.get("UCnone", {}).get("shorts", {}).get("status") == "none")
check("8-minute videos still accepted (wide scope kept)", by_id.get("UCnone", {}).get("duration_seconds") == 480)
check("Slowing down -> kept as 'fading'", by_id.get("UCfading", {}).get("shorts", {}).get("status") == "fading",
      by_id.get("UCfading", {}).get("shorts_summary", ""))
check("No long-video list -> falls back and still qualifies", "UCnoLF" in by_id and by_id["UCnoLF"]["duration_seconds"] == 2100)
check("Shorts list error -> 'couldn't check', never skipped", by_id.get("UCshortsErr", {}).get("shorts", {}).get("status") == "unknown")
check("Rejections are recorded with a reason",
      checks["UCbig"]["result"] == "subscribers_out_of_range" and checks["UCquiet"]["result"] == "too_few_recent_long_videos")
check("A 20-60 min video is picked when there is one", by_id["UClapsed"]["duration_seconds"] == 35 * 60)
check("View counts fetched only where they decide something", CALLS["videos"] <= 2, f"{CALLS}")

# ---------------------------------------------------------------- target video choice
def v(minutes, vid):
    return {"id": vid, "contentDetails": {"duration": f"PT{minutes}M"}}

choice = H.choose_target_video([v(8, "newest8"), v(15, "newer15"), v(40, "older40")])
check("Prefers a 20-60 min video even if it isn't the newest", choice["id"] == "older40")
choice = H.choose_target_video([v(8, "newest8"), v(15, "newer15")])
check("Otherwise prefers 12+ min", choice["id"] == "newer15")
choice = H.choose_target_video([v(8, "newest8"), v(2, "short")])
check("Otherwise still takes a 5+ min video", choice["id"] == "newest8")

# ---------------------------------------------------------------- email picking
check("Business line wins over a random address",
      H._best_email(["fan mail: hi@fan.com\nFor business: deals@creator.co"]) == "deals@creator.co")
check("No email -> None", H._best_email(["no contact here", "logo@2x.png"]) is None)

# ---------------------------------------------------------------- rechecks
H._write_json_atomic(H.SEEN_CHANNELS_PATH, ["UCold", "UCrecent", "UChard", "UClegacy"])
H._write_channel_checks({
    "UCold": {"checked_at": (NOW - timedelta(days=61)).isoformat(), "result": "too_few_recent_long_videos", "soft": True},
    "UCrecent": {"checked_at": (NOW - timedelta(days=10)).isoformat(), "result": "shorts_already_working", "soft": True},
    "UChard": {"checked_at": (NOW - timedelta(days=200)).isoformat(), "result": "not_english", "soft": False},
})
seen = H._read_seen_channel_ids()
check("Soft rejection older than 60 days is checked again", "UCold" not in seen)
check("Recent soft rejection is not", "UCrecent" in seen)
check("Hard rejection (not English) is never rechecked", "UChard" in seen)
check("Old seen channels without a record stay seen", "UClegacy" in seen)

# ---------------------------------------------------------------- worker queue
W._write_json_atomic(W.LEAD_QUEUE_PATH, [])
W._enqueue_leads(leads + [{"channel_id": "UColdstyle", "channel_title": "old", "latest_video_url": "https://www.youtube.com/watch?v=x1",
                           "latest_video_title": "t"}])
queue = W.get_lead_queue()
check("App queue shows best first", queue[0]["channel_id"] == "UClapsed", [q["channel_id"] for q in queue])
check("Leads queued before this update still work (priority 0, last)", queue[-1]["channel_id"] == "UColdstyle")
check("Skip from the queue works", W.skip_queued_lead("UCweak") is True and all(q["channel_id"] != "UCweak" for q in W.get_lead_queue()))
skipped = json.loads(W.SKIPPED_LEADS_PATH.read_text())
check("…and the creator is never found again", skipped[-1]["channel_id"] == "UCweak" and "UCweak" in H._read_seen_channel_ids())
first = W._pop_fresh_lead()
check("Worker takes the top lead", first["channel_id"] == "UClapsed")
record = json.loads(W.PROCESSED_CREATORS_PATH.read_text())["records"][-1]
check("Shorts data saved on the record for later tuning", record.get("shorts", {}).get("status") == "lapsed" and record.get("has_email") is True)
second = W._pop_fresh_lead()
W._handle_lead_failure(second, RuntimeError("network blip"))
order = [q["channel_id"] for q in W.get_lead_queue()]
check("A failed lead drops back instead of blocking the queue",
      second["channel_id"] in order and order.index(second["channel_id"]) > order.index("UCnone"), f"{second['channel_id']} -> {order}")

# ---------------------------------------------------------------- app: Up next list + Skip
from streamlit.testing.v1 import AppTest  # noqa: E402

at = AppTest.from_file(str(root / "app.py"), default_timeout=120).run()
check("App runs", not at.exception, str([e.value for e in at.exception]))
rows = [m.value for m in at.markdown if m.value.startswith("**1. [")]
check("Scout tab lists the queue, best first", bool(rows) and "UCnoLF" in rows[0], rows[:1])
captions = " ".join(c.value for c in at.caption)
top_summary = W.get_lead_queue()[0]["shorts_summary"]
check("Each row shows the Shorts status", top_summary in captions, top_summary)
skip_buttons = [b for b in at.button if (b.key or "").startswith("skip_queued_")]
before = [q["channel_id"] for q in W.get_lead_queue()]
skip_buttons[0].click().run()
after = [q["channel_id"] for q in W.get_lead_queue()]
check("One click on Skip removes that creator", len(after) == len(before) - 1 and before[0] not in after, f"{before[0]} skipped")

print("\nAll checks passed." if not fails else f"\n{fails} check(s) failed.")
sys.exit(1 if fails else 0)
