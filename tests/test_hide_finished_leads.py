"""Checks that the Lead Finder tab hides sent/completed/skipped leads (updated for your "skipped" change).

Run from the project root:  .venv/bin/python tests/test_hide_finished_leads.py .
Uses fake lead records in memory; your real storage files are only read, never changed.
"""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(root))

import autonomous_worker  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

FAKE_RECORDS = [
    {"channel_id": "UC_A", "channel_title": "Alpha", "video_id": "a1", "status": "completed"},
    {"channel_id": "UC_B", "channel_title": "Bravo", "video_id": "b1", "status": "sent"},
    {"channel_id": "UC_C", "channel_title": "Charlie", "video_id": "c1", "status": "failed"},
    {"channel_id": "UC_D", "channel_title": "Delta", "video_id": "d1", "status": "processing"},
    {"channel_id": "UC_E", "channel_title": "Echo", "video_id": "e1", "status": "retry 1/2 queued"},
    {"channel_id": "UC_F", "channel_title": "Foxtrot", "video_id": "f1", "status": "skipped"},
    # Echo was retried and then finished, so its newest record wins and it is hidden.
    {"channel_id": "UC_E", "channel_title": "Echo", "video_id": "e1", "status": "Completed"},
]

autonomous_worker.get_cached_leads = lambda: [dict(r) for r in FAKE_RECORDS]
autonomous_worker.get_pending_outreach = lambda: []

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


at = AppTest.from_file(str(root / "app.py"), default_timeout=120).run()
check("App runs without errors", not at.exception, str([e.value for e in at.exception]))

statuses = [c.value for c in at.caption if c.value.startswith("Status:")]
check("Only unfinished leads are shown",
      statuses == ["Status: failed", "Status: processing"], str(statuses))
check("Sent and completed leads are hidden",
      not any(s.lower() in ("status: sent", "status: completed") for s in statuses))

summary = [c.value for c in at.caption if c.value.startswith("Active leads")]
check("Scout tab shows active and hidden counts",
      summary == ["Active leads: 2 | Finished (hidden): 4 | Pending in outbox: 0"], str(summary))

print("\nAll checks passed." if not fails else f"\n{fails} check(s) failed.")
sys.exit(1 if fails else 0)
