"""Run every test file in this folder, each in its own process.

From the project root:  .venv/bin/python tests/run_all.py
Tell Antigravity/Copilot: "run tests/run_all.py after every change".
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent.parent
failed = []
for test in sorted(Path(__file__).parent.glob("test_*.py")):
    # Tests never upload to your real clip-page bucket (test_round16 uses a fake one).
    # Round 36: each test file gets its own temp folder (renders, caches, made-clips list),
    # deleted when it ends, so a full run never fills the disk.
    scratch = Path(tempfile.mkdtemp(prefix=f"nav-{test.stem}-"))
    env = {**os.environ, "NAV_NO_CLIP_PAGES": "1", "TMPDIR": str(scratch), "TEMP": str(scratch), "TMP": str(scratch),
           # Round 32: Make clips' batch list (Sent) goes to a temp file, never your real one.
           "NAV_MADE_CLIPS": str(scratch / "made_clips.json")}
    # Round 40: the suites before it check Round 39's behaviour, so they run with the climax
    # teaser off and the classic palette (test_round40 checks both features itself).
    number = re.match(r"test_round(\d+)", test.stem)
    if not number or int(number.group(1)) < 40:
        env.update({"ENABLE_CLIMAX_HOOK": "0", "PALETTE_MODE": "classic"})
    # Round 41: the signature edit (the split-screen gate, proof cards, camera energy) for its
    # own suite; the ones before it check the editing they were written for.
    if not number or int(number.group(1)) < 41:
        env["SIGNATURE_EDIT"] = "0"
    # Round 42: the flowing camera and the tightened opening / cold open, likewise
    if not number or int(number.group(1)) < 42:
        env.update({"CAMERA_FLOW": "0", "OPENING_HOOK": "0"})
    # Round 43: slide talks as full-screen reels (auto never letterboxes them)
    if not number or int(number.group(1)) < 43:
        env["REEL_LAYOUT"] = "0"
    # Round 45: LinkedIn B2B defaults (executive captions, no mid-clip graphics); the suites
    # before it check the creator captions and the graphics director they were written for.
    if not number or int(number.group(1)) < 45:
        env.update({"CAPTION_STYLE": "bold", "VISUAL_OVERLAYS": "full"})
    # Round 46: mid-clip silence and filler trimming
    if not number or int(number.group(1)) < 46:
        env["MIDCLIP_TRIM"] = "0"
    # Round 48: the steady executive camera, plain trim cuts, no creator title pill, speaker
    # close-ups and the blank-frame guard; the suites before it check what they were written for.
    if not number or int(number.group(1)) < 48:
        env.update({"EXEC_CAMERA": "flow", "MIDCLIP_ZOOM": "1", "EXEC_TITLE_CARD": "1", "SPEAKER_CLOSEUPS": "0",
                    "FRAME_GUARD": "0"})
    # Round 49: cover images on the clip pages; the suites before it check the exact files a
    # page uploads and the Make-clips download buttons they were written for.
    if not number or int(number.group(1)) < 49:
        env.update({"CLIP_COVERS": "0", "EXEC_EMAIL": "0"})
    # Round 51: the auto-dub check asks YouTube about the video before downloading; the suites
    # before it fake the download, so it's off for them.
    if not number or int(number.group(1)) < 51:
        env["AUTO_DUB_CHECK"] = "0"
    # Round 53: buyer fit gates the worker's own leads and follow-ups follow page activity; the
    # suites before it check the queue and the fixed day 3 / day 12 follow-ups they were written for.
    if not number or int(number.group(1)) < 53:
        env.update({"FIT_GATE": "0", "ADAPTIVE_FOLLOW_UPS": "0"})
    # Round 55: idea-complete clips of 35-180 s (no 60 s cap) are the default; the suites before
    # it check the 30-60 s picks and the 59 s cap they were written for.
    if not number or int(number.group(1)) < 55:
        env["CLIP_LENGTH"] = "legacy"
    try:
        result = subprocess.run([sys.executable, str(test), str(root)], cwd=root, capture_output=True, text=True, env=env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    lines = (result.stdout + result.stderr).splitlines()
    # Two styles are in use: "[PASS] name" (older suites) and "PASS  name" (Round 49 on).
    passes = sum(line.startswith(("[PASS]", "PASS  ")) for line in lines)
    fails = [line for line in lines if line.startswith(("[FAIL]", "FAIL  "))]
    ok = result.returncode == 0
    print(f"{'OK  ' if ok else 'FAIL'} {test.name}: {passes} passed, {len(fails)} failed")
    for line in fails:
        print("     ", line[:160])
    if not ok and not fails:
        print("      " + "\n      ".join(lines[-5:]))
    if not ok:
        failed.append(test.name)
print("\nAll test files passed." if not failed else f"\n{len(failed)} test file(s) failed: {', '.join(failed)}")
sys.exit(1 if failed else 0)
