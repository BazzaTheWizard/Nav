"""Checks for round 13: steady caption emphasis (no sideways shake), EMPHASIS_INTENSITY presets,
and the reorganized app (5 tabs, one status header, no sidebar).
Run all tests: .venv/bin/python tests/run_all.py"""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
for name in ("CAPTION_STYLE", "EMPHASIS_INTENSITY"):
    os.environ.pop(name, None)

fails = 0
def check(name, cond, detail=""):
    global fails; fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

import core.subtitle_engine as S
from core.transcriber import Word

tmp = Path(tempfile.mkdtemp(prefix="r13-"))

# ---------- Presets ----------
check("Presets: off, subtle, balanced, punchy", set(S.EMPHASIS_PRESETS) == {"off", "subtle", "balanced", "punchy"})
check("Pulse sizes stay in the 105-112% range",
      all(105 <= p["height"] <= 112 for p in S.EMPHASIS_PRESETS.values() if p))
check("Bold look uses 'balanced' by default", S.emphasis_preset() == S.EMPHASIS_PRESETS["balanced"])
check("Classic look has no pulse (color change only)", S.emphasis_preset(style=S.caption_style("classic")) is None)
os.environ["EMPHASIS_INTENSITY"] = "Punchy "
check("EMPHASIS_INTENSITY=punchy is read (spaces/case ignored)", S.emphasis_preset() == S.EMPHASIS_PRESETS["punchy"])
os.environ["EMPHASIS_INTENSITY"] = "wild"
check("Unknown EMPHASIS_INTENSITY falls back to balanced", S.emphasis_preset() == S.EMPHASIS_PRESETS["balanced"])
os.environ.pop("EMPHASIS_INTENSITY")
check("Function argument beats the env setting", S.emphasis_preset("off") is None)

# ---------- Pulse timing follows the word ----------
balanced = S.EMPHASIS_PRESETS["balanced"]
check("Width never changes (that's what moved the whole line)", r"\fscx" not in S._pulse(balanced, 6, 400))
check("Long word: pulse capped at 110 ms", S._pulse(balanced, 6, 600) == r"\fscy108\bord9\t(0,110,\fscy100\bord6)")
check("Medium word (200 ms): pulse is 40% of it = 80 ms", S._pulse(balanced, 6, 200).endswith(r"\t(0,80,\fscy100\bord6)"))
check("Fast word (100 ms): no pulse, color only (no flicker)", S._pulse(balanced, 6, 100) == "")
check("Exactly 120 ms still pulses (48 ms)", r"\t(0,48," in S._pulse(balanced, 6, 120))
check("Subtle preset", S._pulse(S.EMPHASIS_PRESETS["subtle"], 6, 600) == r"\fscy105\bord8\t(0,90,\fscy100\bord6)")
check("Punchy preset", S._pulse(S.EMPHASIS_PRESETS["punchy"], 6, 600) == r"\fscy112\bord10\t(0,130,\fscy100\bord6)")

words = [Word("the", 0.0, 0.3), Word("real", 0.3, 0.6), Word("question", 0.6, 1.0), Word("is", 1.0, 1.1),
         Word("simple.", 1.1, 1.6)]
ass = S.create_ass_subtitles(words, tmp / "b.ass", clip_duration=2).read_text()
check("Normal word pulses in the file", r"\c&H00FFFF&\fscy108\bord9\t(0,110,\fscy100\bord6)}QUESTION" in ass)
check("Fast word 'IS' (100 ms) turns yellow without a pulse", r"{\c&H00FFFF&}IS{" in ass)
check("No width scaling anywhere in the captions", r"\fscx" not in ass)
off = S.create_ass_subtitles(words, tmp / "off.ass", clip_duration=2, emphasis="off").read_text()
check("emphasis='off': yellow word, no pulse", r"{\c&H00FFFF&}QUESTION" in off and r"\fscy108" not in off)
punchy = S.create_ass_subtitles(words, tmp / "p.ass", clip_duration=2, emphasis="punchy").read_text()
check("emphasis='punchy' in the file", r"\fscy112\bord10\t(0,130,\fscy100\bord6)}QUESTION" in punchy)

# ---------- Rendered: the line doesn't move sideways while a word pulses ----------
def has_ass_filter():
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return " ass " in out

if has_ass_filter():
    import numpy as np
    from PIL import Image

    line = [Word("the", 0.0, 0.3), Word("real", 0.3, 0.6), Word("question", 0.6, 1.0), Word("is.", 1.0, 1.4)]
    S.create_ass_subtitles(line, tmp / "move.ass", clip_duration=2, emphasis="punchy")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x404040:s=1080x1920:r=100:d=0.8",
                    "-vf", f"ass={tmp / 'move.ass'}", "-start_number", "0", str(tmp / "f_%03d.png")], check=True)

    def edges(i):
        a = np.asarray(Image.open(tmp / f"f_{i:03d}.png").convert("L"))
        cols = np.where((a > 200).any(axis=0))[0]
        return (int(cols.min()), int(cols.max())) if cols.size else None

    settled, pulsing = edges(55), [edges(i) for i in range(60, 75)]  # QUESTION starts at frame 60 (0.60 s)
    moved = max((abs(e[0] - settled[0]) for e in pulsing if e and settled), default=99)
    check("Rendered (punchy): caption line moves ≤1 px sideways during the pulse", settled is not None and moved <= 1,
          f"moved {moved} px")
else:
    print("[SKIP] Render check (ffmpeg with the 'ass' filter not found)")

# ---------- App layout ----------
import autonomous_worker  # noqa: E402

RECORD = {"record_id": "r1", "channel_id": "UC1", "channel_title": "Coach Dana", "status": "pending",
          "drive_link": "https://drive.google.com/drive/folders/x", "subject": "3 Shorts from your knee pain video",
          "full_email_body": "Hi Dana,\n\nBody", "contact_email": "dana@example.com",
          "clips": [{"judge": 82, "layout": "smart_crop"}, {"judge": 74, "layout": "stacked"}],
          "subscriber_count": 41200, "video_seconds": 1830}
autonomous_worker.get_pending_outreach = lambda: [dict(RECORD)]
autonomous_worker.get_cached_leads = lambda: []
autonomous_worker.get_awaiting_reply = lambda: []
autonomous_worker.get_lead_queue = lambda: []
autonomous_worker.get_recently_passed = lambda *a, **k: []
autonomous_worker.pass_summary = lambda *a, **k: {}
autonomous_worker.is_youtube_quota_exhausted = lambda: False

from streamlit.testing.v1 import AppTest  # noqa: E402
import app  # noqa: E402

at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
check("App runs", not at.exception, str([e.value for e in at.exception]))
check("Title is 'Nav Studios'", [t.value for t in at.title] == ["Nav Studios"])
check("Five workflow tabs", [t.label for t in at.tabs] == list(app.TAB_LABELS), str([t.label for t in at.tabs]))
labels = [m.label for m in at.metric]
check("Status header: 4 numbers", labels[:4] == ["Ready to send", "Waiting for a reply", "Up next", "Passed (14 days)"],
      str(labels))
check("Ready to send shows n / max", at.metric[0].value == f"1 / {autonomous_worker.MAX_PENDING_LEADS}")
badges = " ".join(m.value for m in at.markdown)
check("Pitch card badges: clips, AI scores, framing, subs, length",
      all(s in badges for s in (":blue-badge[2 clips]", ":green-badge[AI scores 82 · 74]", ":gray-badge[face-tracked, stacked]",
                                 ":gray-badge[41,200 subs]", ":gray-badge[30 min video]")), badges[:300])
check("Sidebar is gone", len(at.sidebar.children) == 0)
check("Old leftovers removed (target URL box, auto-process)",
      not any("target" in (t.key or "") for t in at.text_input) and not any("auto" in (c.key or "") for c in at.checkbox))
check("Mark as Sent and Pass still on the card",
      any(b.label == "✅ Mark as Sent" for b in at.button)
      and any(p.proto.popover.label == "❌ Pass (not pitching)" for p in at.get("popover")))
check("Settings table on the System tab", any("EMPHASIS_INTENSITY" in str(t.value) for t in at.table))

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
