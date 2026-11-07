"""Run every test file in this folder, each in its own process.

From the project root:  .venv/bin/python tests/run_all.py
Tell Antigravity/Copilot: "run tests/run_all.py after every change".
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent.parent
failed = []
for test in sorted(Path(__file__).parent.glob("test_*.py")):
    # Tests never upload to your real clip-page bucket (test_round16 uses a fake one).
    env = {**os.environ, "NAV_NO_CLIP_PAGES": "1",
           # Round 32: Make clips' batch list (Sent) goes to a temp file, never your real one.
           "NAV_MADE_CLIPS": str(Path(tempfile.mkdtemp(prefix="made-clips-")) / "made_clips.json")}
    result = subprocess.run([sys.executable, str(test), str(root)], cwd=root, capture_output=True, text=True, env=env)
    lines = (result.stdout + result.stderr).splitlines()
    passes = sum(line.startswith("[PASS]") for line in lines)
    fails = [line for line in lines if line.startswith("[FAIL]")]
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
