# Code review: short-form clip generator

Reviewed Oct 27, 2026. Covers every `.py` file in the project, `requirements.txt` and the state of `.venv` and `storage/`.

## What's in this folder

| File | What it is |
|---|---|
| `claude_fixes.patch` | All code changes as one patch (12 files) |
| `patched_files/` | Full copies of every changed file, in case a patch hunk doesn't apply |
| `tests/` | Checks for the fixes. Both need to print `0 failure(s)` |
| `subtitles_before_after.png` | The same subtitle data rendered by the old code and the new code |

## How to apply

From the project root:

```bash
patch -p1 --dry-run < claude_review/claude_fixes.patch   # should list 12 files, no errors
patch -p1 < claude_review/claude_fixes.patch

source .venv/bin/activate
pip uninstall -y opencv-contrib-python opencv-python-headless   # see problem 6
pip install -r requirements.txt

python claude_review/tests/test_fixes.py .
python claude_review/tests/test_agent_worker.py .
```

### Prompt for Antigravity

> Apply `claude_review/claude_fixes.patch` to this project exactly as written by running `patch -p1 < claude_review/claude_fixes.patch` from the project root. Do not rewrite, reformat or "improve" the patched code. If any hunk fails, make that file match `claude_review/patched_files/<same path>` for that hunk only, and keep any edits I made after Oct 27. Then run `pip uninstall -y opencv-contrib-python opencv-python-headless` and `pip install -r requirements.txt` in `.venv`. Finally run `python claude_review/tests/test_fixes.py .` and `python claude_review/tests/test_agent_worker.py .`. Both must print `0 failure(s)`. Tell me which, if any, hunks needed manual work.

## Problems found, most important first

### 1. Subtitles showed whole sentences when Groq is used
`GROQ_API_KEY` is set, so Groq does all transcription. The request asked for **segment** timestamps only, so each "word" was really a full sentence. The subtitle engine's 4-word / 24-character limit never kicked in. Each sentence appeared as one 3–4 line block of 72pt text (see `subtitles_before_after.png`).
**Fix:** request word and segment timestamps from Groq. Each word keeps its sentence's start and end, so clip selection still snaps to sentence ends. As a backup, the subtitle engine now splits any multi-word entry into short cards with estimated timing. *(transcriber.py, subtitle_engine.py)*

### 2. YouTube links with auto-captions produced zero clips (CLI)
`main.py` uses YouTube captions when they exist. Auto-generated captions have no punctuation, so the clip selector found no sentence ends and returned nothing. Test: the old code returned **0 clips** on a 30-minute unpunctuated transcript. The new code returns 3.
**Fix:** when punctuation is missing, pauses and caption-line breaks count as boundaries. *(hook_selector.py)*

### 3. The CLI downloaded the full video once per clip
For URLs, `main.py` called `download_video_segment` for each clip. That function downloads the **whole** video (up to 4K) and then trims it, so 3 clips meant 3 full downloads.
**Fix:** download once and reuse it for all clips, as `app.py` already does. Delete it at the end. *(main.py)*

### 4. Downloads piled up in storage/raw (4.5 GB)
`storage/raw` holds 17 videos. Several are byte-for-byte copies of the same download (one 286 MB video is there 5 times). The app only deleted a run's source video if you pressed Discard.
**Fix:** a new run deletes the previous run's source. Downloads and temp files older than 24 hours in `storage/raw` and `storage/temp` are cleaned up automatically. Everything currently in `storage/raw` is leftover and safe to delete. *(app.py, main.py, storage_manager.py)*

### 5. The worker burned YouTube quota and threw leads away
Each discovery run makes 5 searches, about 500 of your 10,000 daily API units. In `--continuous-queue` mode:
- Only the first qualified lead was used. The rest (up to 9) were marked "seen" and would never come back.
- When nothing qualified, discovery ran again after 5 seconds. That can use up the day's quota in minutes.
- After a quota error it retried every 60 seconds. Quota resets once a day.

**Fix:** every qualified lead is saved to `storage/lead_queue.json` and used before searching again. Empty searches back off from 5 minutes up to 60. Quota errors pause discovery for 15 minutes. `--batch` mode uses the same queue. *(autonomous_worker.py)*

### 6. Two OpenCV packages are installed on top of each other
`.venv` has both `opencv-contrib-python 5.0` and `opencv-python-headless 4.10`. They write to the same `cv2` folder, which is now a mix of both. OpenCV 5 removed `cv2.CascadeClassifier` from the main package. If the 5.0 files win after a reinstall, face tracking quietly falls back to a center crop and only logs a warning.
**Fix:** `requirements.txt` now pins `opencv-python-headless>=4.10,<5`. Uninstall both packages, then reinstall (commands above). I also added `google-genai`, which the code imports but `requirements.txt` was missing.

### 7. Downloads picked AV1, often 4K
`bestvideo[ext=mp4]` matches YouTube's AV1 streams. 9 of the 17 files in `storage/raw` are AV1, including one 4K file (615 MB), and the leftover `.part` file is format 399 (AV1 1080p). Output is 1080×1920, so 4K adds download size and decode time but no quality in the default layouts.
**Fix:** prefer H.264 up to 1080p, then fall back to other formats. *(downloader.py)*

### 8. Every clip was transcribed a second time
After transcribing the whole video, each clip's audio was cut out and transcribed again. That's 3 extra Groq calls or 3 local Whisper runs per video. The full transcript already has the word timings.
**Fix:** clip subtitles reuse the full transcript. Clips are only re-transcribed when the transcript came from caption lines with no word timing. *(app.py, main.py, autonomous_worker.py, transcriber.py)*

### 9. The fallback clip selector wasted tokens and dropped good picks
- The prompt sent one line per word, about 9,000 lines for a 60-minute video. It now sends one line per sentence, roughly 3× fewer characters, and the model reads whole sentences.
- If the model proposed a 28-second or 63-second clip, the pick was thrown out. When too many were dropped, you got generic "Key Highlight" clips. Proposals from 10 to 120 seconds are now snapped to valid 30–60 second sentence boundaries.
- The prompt said "Select 3" while asking for 6 candidates. That conflict is fixed.

*(hook_selector.py, transcriber.py)*

### 10. The Gemini clip picker had no overlap check, and the email field could break it
- Nothing stopped the 3 AI-picked clips from overlapping. Overlaps are now rejected, which triggers your existing fallback.
- The model had to write the full outreach email, which the code then threw away and rebuilt from the template. If the model left it out, the whole selection failed. The model now only returns the two fill-in fields.

*(agent_brain.py)*

### 11. Audio over about 69 minutes skipped Groq
The upload was always compressed to 48 kbps. Past roughly 69 minutes the file went over Groq's 24 MB limit, and the code silently fell back to slow local Whisper.
**Fix:** the bitrate now adjusts to fit, which works up to about 3 hours. *(transcriber.py)*

### 12. TLS certificate checks were turned off
`nocheckcertificate: True` (yt-dlp) and `ssl._create_unverified_context()` (face-tracker download) disabled HTTPS checks. Both are back on, using `certifi`. If you get `CERTIFICATE_VERIFY_FAILED` on your Mac, run `Install Certificates.command` in `/Applications/Python 3.14/` once.

### 13. Smaller fixes
- The renderer only worked on a Mac (`h264_videotoolbox`). It now falls back to `libx264` elsewhere.
- Core modules no longer import Streamlit. The CLI and worker stop printing "missing ScriptRunContext" warnings.
- Clip transcription was hard-coded to English. It now follows `WHISPER_LANGUAGE`, with English as the default.
- `Any` was used without being imported in transcriber.py.
- Video duration now comes from ffprobe. OpenCV's frame-count estimate can be wrong.
- The CLI help said 15–90 s clips, but the code makes 30–60 s clips.
- `client.beta.chat.completions.parse` changed to `client.chat.completions.parse`, the non-beta path.
- The Gemini model name in agent_brain.py now reads `GEMINI_MODEL` from `.env`, like lead_harvester.py does.

## Not changed, but worth knowing

- **Your finished clips get deleted.** Each run starts by deleting clips in `~/Desktop/ShortFormClips` that are older than 48 hours or beyond the newest 15. The "Clear Temp & Render Cache" button deletes that folder's clips too. Move clips you want to keep, or raise the limits in `prune_output_folder`.
- `core/lead_finder.py` (625 lines) isn't imported anywhere.
- Your phone number is hard-coded in two email templates (agent_brain.py and app.py). Moving it to `.env` would keep it out of the code.
- Downloading videos with yt-dlp is against YouTube's terms. Sending clips to the creator first, as the workflow already does, is the safer route. Don't post clips without their OK.

## Biggest improvement left (not in this patch)

**Per-shot framing.** If the face tracker sees any camera cut, the whole clip switches to the small letterboxed layout. Multi-camera podcasts nearly always have cuts, so most of those clips end up as a small 16:9 box in the middle. Picking a crop per camera shot would let them use full-screen, face-centered framing, which usually holds viewers better. A cheaper second step is highlighting the active word in captions. The yellow `SecondaryColour` is already defined in the subtitle style but isn't used.

## How this was tested

- **30 automated checks pass** (`tests/`). They cover Groq parsing, upload sizes for 30–180 minute audio, subtitle splitting, unpunctuated captions, snapping, prompt size, the overlap check, the lead queue, discovery backoff, storage cleanup and the encoder fallback.
- **End-to-end render** on one of your downloaded videos (38 minutes, 720p) through `main.run_pipeline`. It produced 3 clips at 1080×1920, 31–35 seconds each, with no second transcription.
- **The Streamlit app** loads with no errors in a headless test.
- **Not tested live:** YouTube downloads and Groq, OpenAI, Gemini and Google Drive calls. My environment can't reach those services, and I didn't use your API keys, so transcription and AI picks were stubbed. On your first real run, check that the subtitles show 1–4 words at a time.
