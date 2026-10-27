"""One-time live check (about 4 YouTube quota units, out of 10,000 a day).

Confirms YouTube still answers the per-channel "long videos only" and "Shorts only"
lists that round 4 uses, and shows what the Shorts check says about one channel.
Uses your own .env key on your machine. It changes nothing in storage/.

Run from the project root:  python claude_review/round4/check_youtube_lists.py [CHANNEL_ID]
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
import core.lead_harvester as H  # noqa: E402

channel_id = sys.argv[1] if len(sys.argv) > 1 else "UCBR8-60-B28hp2BmDPdntcQ"  # YouTube's own channel
suffix = channel_id[2:]
ok = True
for label, playlist, size in (("Long videos only", f"UULF{suffix}", 10), ("Shorts only", f"UUSH{suffix}", H.SHORTS_LOOKBACK)):
    try:
        items, total = H._playlist_items(playlist, size)
        print(f"[OK] {label}: {len(items)} newest read, about {total} in total")
    except Exception as exc:  # 404 just means the channel has none of that kind
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == 404:
            print(f"[OK] {label}: this channel has none (404), which the code treats correctly")
        else:
            ok = False
            print(f"[PROBLEM] {label}: {exc}")

uploads = H._channel_uploads(channel_id, f"UU{suffix}")
profile = H.shorts_profile(uploads.shorts, uploads.shorts_total, datetime.now(timezone.utc))
print(f"\nShorts check (posting pattern only): {profile['summary']}")
print("\nAll good: round 4 can use these lists." if ok else "\nSomething is off: send me the output above.")
