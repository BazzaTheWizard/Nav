"""Checks for the new hook title card.

Run from the project root:  .venv/bin/python tests/test_title_cards.py .
Needs FFmpeg. Makes its own test video in a temp folder; touches nothing else.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(root))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from core import title_card as T  # noqa: E402
from core.face_tracker import CropBox  # noqa: E402
from core.subtitle_engine import create_ass_subtitles  # noqa: E402
from core.transcriber import Word  # noqa: E402
from core.video_renderer import render_clip  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# --- Text clean-up and line breaks -------------------------------------------------
check("Font file is in place", T.FONT_PATH.is_file(), str(T.FONT_PATH))
check("Emoji are removed", T.clean_title("🔥 Stop Doing This 💀") == "Stop Doing This", T.clean_title("🔥 Stop Doing This 💀"))
long_title = "This is a very long title that the model wrote which keeps going and going well past the limit"
check("Long titles are shortened", len(T.clean_title(long_title)) <= T.MAX_CHARACTERS + 1 and T.clean_title(long_title).endswith("…"))

font, lines = T._layout_text("How to Squat Pain-Free With Knee Arthritis", T.FONT_SIZES[0])
widths = [font.getlength(line) for line in lines]
check("Two balanced lines", len(lines) == 2 and min(widths) / max(widths) > 0.7, f"{lines}")
font, lines = T._layout_text("Your Knees Will Thank You", T.FONT_SIZES[0])
check("Short title stays short", len(lines) <= 2, f"{lines}")
font, lines = T._layout_text("The One Squat Fix That Finally Made My Knee Pain Disappear for Good", T.FONT_SIZES[-1])
check("Very long title uses up to 3 lines at the small size", 2 <= len(lines) <= 3, f"{lines}")

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)

    # --- One box, rounded corners ----------------------------------------------------
    path, card_w, card_h = T.render_title_card("Stop Squatting Too Deep If Your Knees Hurt", tmp / "card.png")
    alpha = np.array(Image.open(path))[:, :, 3]
    m = T.SHADOW_MARGIN
    inside = alpha[m + 1 : m + card_h - 1, m + card_w // 2]  # centre column, top edge to bottom edge
    check("One solid box behind all lines (no gaps between lines)", inside.min() == 255, f"min alpha {inside.min()}")
    check("Rounded corners", alpha[m, m] < 255)
    check("Card fits the frame width", card_w <= T.MAX_CARD_WIDTH)

    # --- Placement -------------------------------------------------------------------
    face = [[(250.0, 560.0)]] * 12  # face (with hair and neck margins) at 250-560 px in every frame
    top, covered = T._best_slot(200, face)
    check("Moves the card off the face", covered == 0 and top >= 560, f"top={top}")
    check("Stays above the captions", top + 200 <= T.CAPTION_ZONE_TOP)
    top, covered = T._best_slot(200, [[]] * 12)
    check("No face: card goes to the top", top == T.SAFE_TOP and covered == 0)
    high_face = [[(150.0, 1100.0)]] * 12
    top, covered = T._best_slot(200, high_face)
    check("No free spot: still placed inside the safe area", T.SAFE_TOP <= top <= T.CAPTION_ZONE_TOP - 200)
    top, label, _ = T._place("blurred_background", 200, (1920, 1080), [])
    check("Blurred layout: card sits above the video", top + 200 <= 656, f"top={top} ({label})")
    top, label, _ = T._place("stacked", 200, (1920, 1080), [])
    check("Stacked layout: card sits between the panels", 700 < top + 100 < 980, f"top={top} ({label})")

    # --- Off switches ------------------------------------------------------------------
    common = dict(layout="smart_crop", plan=None, source_video=tmp / "missing.mp4", clip_start=0.0, clip_duration=10.0, work_dir=tmp, name="x")
    check("Generic title is skipped", T.prepare_title_overlay("Clip", 4.0, **common) is None)
    check("HOOK_TITLE_SECONDS=0 turns it off", T.prepare_title_overlay("Real Title Here", 0.0, **common) is None)

    # --- Real render -----------------------------------------------------------------
    source = tmp / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=1920x1080:r=30:d=6",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(source)],
        check=True,
    )
    words = [Word(text=w, start=0.2 + i * 0.4, end=0.5 + i * 0.4) for i, w in enumerate("this is a quick test of captions".split())]
    subs = create_ass_subtitles(words, tmp / "subs.ass", clip_duration=6.0)
    overlay = T.prepare_title_overlay(
        "How to Squat Pain-Free With Knee Arthritis", 4.0, layout="smart_crop", plan=None,
        source_video=source, clip_start=0.0, clip_duration=6.0, work_dir=tmp, name="clip", style="light",
    )
    check("Title card prepared", overlay is not None and overlay.image_path.is_file())
    out = render_clip(
        source, subs, CropBox(x=656, y=0, width=608, height=1080), 0.0, 6.0, tmp / "out.mp4",
        layout_mode="smart_crop", title=overlay,
    )
    check("Clip renders with the title", out.is_file() and out.stat().st_size > 10_000)

    def pixel(at_seconds):
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", str(at_seconds), "-i", str(out), "-frames:v", "1",
             "-vf", f"format=gray,crop=2:2:540:{overlay.card_top + 6}", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
            check=True, capture_output=True,
        ).stdout
        return raw[0] if raw else -1

    during, after = pixel(1.5), pixel(5.0)
    check("Card is visible while it should be", during > 230, f"brightness {during}")
    check("Card is gone after the hook seconds", after < 180, f"brightness {after}")

print("\nAll checks passed." if not fails else f"\n{fails} check(s) failed.")
sys.exit(1 if fails else 0)
