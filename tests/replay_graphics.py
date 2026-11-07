"""Round 32: run the graphics director on a clip of a video whose transcript is saved, with
the real AI, and print what it adds (Bento visuals and name cards) and why.

Make clips and the worker save each YouTube video's transcript in storage/transcripts/
(<video id>.json). After making clips from a video, check any clip again:

    .venv/bin/python tests/replay_graphics.py <video id> <start> <end> --title "..." [--channel "..."] [--description "..."]

<start> and <end> are the clip's times in the video (seconds, or m:ss). Not a test file:
tests/run_all.py doesn't run it (it calls the AI).
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import graphics_director as G  # noqa: E402
from core.transcriber import Word  # noqa: E402


def seconds(text: str) -> float:
    parts = [float(p) for p in text.split(":")]
    return sum(p * 60 ** k for k, p in enumerate(reversed(parts)))


parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
parser.add_argument("video_id")
parser.add_argument("start")
parser.add_argument("end")
parser.add_argument("--title", default="")
parser.add_argument("--channel", default="")
parser.add_argument("--description", default="")
parser.add_argument("--hook", type=float, default=4.0, help="seconds the title card is up (HOOK_TITLE_SECONDS)")
args = parser.parse_args()
path = ROOT / "storage" / "transcripts" / f"{args.video_id}.json"
if not path.is_file():
    sys.exit(f"No saved transcript at {path}. Make clips from the video first (Round 32 saves it).")
words = [Word(*row) for row in json.loads(path.read_text(encoding="utf-8"))]
start, end = seconds(args.start), seconds(args.end)
direction = G.direct([w for w in words if start - 0.05 <= w.start < end], start, end,
                     info=G.VideoInfo(args.title, args.channel, args.description), hook_seconds=args.hook,
                     video_words=words)
print(f"Clip {args.start}–{args.end} ({end - start:.1f} s):")
for card in direction.edit.cards:
    items = "; ".join(" ".join(x for x in (r.label, r.value, r.detail) if x) for r in card.rows)
    print(f"  {card.start - start:5.1f}–{card.end - start:5.1f} s  {card.template:16} {items}")
print("Why:")
for note in direction.notes:
    print("  -", note)
