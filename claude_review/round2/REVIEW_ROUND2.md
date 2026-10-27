# Round 2: framing, captions, speed and cleanup

Built Oct 27, 2026 on top of your files as they were after Antigravity applied round 1.

## Your two hand-applied hunks

Both are correct. `run_worker()` and `run_continuous_queue()` match the intended logic and still call `init_storage_files()`. The `--continuous` alias works. All round-1 checks still pass on your current code.

One leftover: `autonomous_worker.py.orig` is the backup `patch` made when hunks failed. Delete it.

## What's in this folder

| File | What it is |
|---|---|
| `claude_fixes_round2.patch` | All code changes (15 files, including one new file: `core/pipeline.py`) |
| `patched_files/` | Full copies of every changed file, for any hunk that won't apply |
| `face_detection_yunet_2023mar.onnx` | Face-detection model (230 KB). Goes in `models/` |
| `tests/` | 54 checks across 3 files. All must print `0 failure(s)` |
| `layouts_preview.png` | Rows: Auto (face crop), Stacked, Blurred. Frames from just before and after camera cuts |

## How to apply

Run these from the project root, in order.

**1. Turn on git first,** so you can see and undo every change:

```bash
cat >> .gitignore <<'EOF'
# macOS and patch leftovers
.DS_Store
*.orig
*.rej
# Old code dump: AI tools may read it instead of the real files
project_code_bundle.txt
# Worker state (contains creator contact details)
storage/*.json
storage/.autonomous_worker.lock
EOF
git init && git add -A && git commit -m "Before round 2"
```

**2. Add the model and apply the patch:**

```bash
mkdir -p models && cp claude_review/round2/face_detection_yunet_2023mar.onnx models/
patch -p1 --dry-run < claude_review/round2/claude_fixes_round2.patch   # 15 files, no errors
patch -p1 < claude_review/round2/claude_fixes_round2.patch
```

**3. Run the checks, then commit:**

```bash
source .venv/bin/activate
python claude_review/round2/tests/test_fixes.py .
python claude_review/round2/tests/test_agent_worker.py .
python claude_review/round2/tests/test_round2.py .
git add -A && git commit -m "Round 2: per-shot framing, captions, shared pipeline"
```

**4. Clean up by hand.** I left these out of the patch because deleting files is your call:

```bash
rm autonomous_worker.py.orig project_code_bundle.txt core/lead_finder.py
rm service_account.json
```

`service_account.json` is a Google private key that no code uses. If you ever uploaded it or pasted it anywhere, including into an AI tool, also delete the key in Google Cloud Console (IAM → Service Accounts → Keys).

### Prompt for Antigravity

> Do these steps in order, from the project root. 1) Append the lines in step 1 of `claude_review/round2/REVIEW_ROUND2.md` to `.gitignore`, run `git init`, and commit everything as "Before round 2". 2) Run `mkdir -p models && cp claude_review/round2/face_detection_yunet_2023mar.onnx models/`. 3) Apply `claude_review/round2/claude_fixes_round2.patch` exactly as written with `patch -p1 < claude_review/round2/claude_fixes_round2.patch`. Do not rewrite, reformat or "improve" the patched code. If a hunk fails, make that hunk match `claude_review/round2/patched_files/<same path>` and keep any edits I made after the patch was built. 4) Run the three test files in `claude_review/round2/tests/` with `.` as the argument. All three must print `0 failure(s)`. 5) Commit as "Round 2". Tell me which hunks, if any, needed manual work. Do not delete any files.

## What changed

Numbers match the list from our chat.

1. **Captions moved out from under the app UI.** They used to sit 140 px from the bottom, where TikTok, Reels and Shorts draw the username and buttons. They now sit 680 px up, just above the zone those apps cover. *(subtitle_engine.py)*
2. **Framing follows camera cuts.** Each clip is split into camera shots, cut to within one frame, and every shot gets its own face-centered crop. The crop only moves on a cut, so it never visibly pans. *(face_tracker.py, video_renderer.py)*
3. **Hook title on screen.** The clip's AI title shows at the top for the first 4 seconds, in a dark box, below the area the apps cover. Generic fallback titles like "Key Highlight" are never shown. *(subtitle_engine.py)*
4. **Active-word highlight.** The word being spoken turns yellow inside each caption card. *(subtitle_engine.py)*
5. **"Most replayed" data feeds clip picking.** For YouTube links, the peaks are sent to both AI pickers as hints. It's one extra metadata request with no extra download, and it's skipped silently when a video has no heatmap. *(downloader.py, hook_selector.py, agent_brain.py)*
6. **The AI picker is more forgiving.** Gemini now returns 5 ranked candidates. Each is snapped to sentence boundaries with the same rules as the other picker. The best 3 that don't overlap are kept, and any shortfall is filled. One bad pick no longer throws out all three or triggers a second AI request. *(agent_brain.py, hook_selector.py, pipeline.py)*
7. **A better face detector.** OpenCV's YuNet model replaces the old Haar cascade. It handles side profiles and smaller faces, and Haar is still the fallback. If `models/` is missing the file, it downloads once and is checked against a checksum. *(face_tracker.py)*
8. **Faster blurred background,** about 2.4× faster for that step. *(video_renderer.py)*
9. **Faster framing analysis.** Each clip is decoded once at low resolution, not seeked frame by frame, and the analysis is skipped entirely for the blurred layouts. On the 22-second 1080p test clip it took 3 seconds. *(face_tracker.py, pipeline.py)*
10. **Download buttons load the file only when clicked.** They no longer read every clip into memory on every page rerun. *(app.py)*
11. **Gemini thinking set to "low"** for clip picking and search queries. Change it with `GEMINI_THINKING_LEVEL` in `.env`. *(gemini_resilience.py)*
12. **Git and `.gitignore`.** Step 1 above.
13. **One shared pipeline.** `core/pipeline.py` now holds the steps that were copied into `main.py`, `app.py` and `autonomous_worker.py`. The duplicate transcript-grouping function is gone too. A fix now lands in one place. *(new: core/pipeline.py)*
14. **Failed leads are retried.** A lead that fails goes back to the end of the queue, up to 3 attempts in total, before it's marked failed. *(autonomous_worker.py)*
15. **Cleanup.** Step 4 above.

**Extra bug fixed:** the **Stacked Split-Screen** layout squeezed a tall 9:16 face crop into a 1080×960 panel, so faces came out about 2× too wide. It now crops a 9:8 window around the face. You can see it in row 2 of `layouts_preview.png`.

## How "Auto" decides now

- **Full-screen face crop** when the source is at least about 1080p, a face is found in at least 60% of the clip, wide two-person shots are under 35%, and there are no more than 40 cuts.
- **Blurred background** otherwise. That includes 720p sources, where a 9:16 crop would look soft.

The app's Auto used to always pick a blurred layout. For 1080p talking-head videos, expect full-screen faces now. Pick "Blurred Background" in the app if you want the old look.

## New optional `.env` settings

```
HOOK_TITLE_SECONDS=4         # 0 turns the on-screen hook title off
CAPTION_HIGHLIGHT=1          # 0 turns the yellow active word off
GEMINI_THINKING_LEVEL=low    # low / medium / high, or empty for the model default
```

## How this was tested

- **54 automated checks pass:** round 1's 30 (updated for the new behavior) and 24 new ones.
- **Multi-camera test:** I built a 1080p video from your footage with 5 hard cuts. The cuts include mirrored angles with identical colors and a shot with no face. All 5 cuts were found to within one frame, and the crop stayed on the face in every shot (see `layouts_preview.png`).
- **Your 38-minute 720p video** through the CLI: 3 clips in 89 seconds, against 158 seconds in round 1 on the same 2-core machine. That's with the new framing analysis included. Auto chose the blurred layout, as expected for 720p.
- **The Streamlit app** loads with no errors in a headless test, including when an old saved layout choice is restored.

**Not tested live:** YouTube downloads and heatmaps, and Groq, OpenAI, Gemini and Drive calls. I couldn't reach those services from here, so transcription and AI picks were stubbed. If Gemini ever rejects the thinking setting on its fallback model, set `GEMINI_THINKING_LEVEL=` (empty) in `.env`.
