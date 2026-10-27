# Round 4 review: where the project is leaving value on the table

I reviewed all ~7,700 lines and checked them against your real data: worker records, lead queue, downloads folder and installed packages. Nothing here has been changed yet. These are findings only.

**The short version:** speed isn't your problem. Your Mac finishes a lead in 1–3 minutes, according to the timestamps in `processed_creators.json`. The biggest gains are in **who** you find, **whether you can reach them**, and **not losing work when something fails**.

---

## Tier 1: biggest impact

### 1. None of your leads has an email address
- **Evidence:** 0 of 6 pending pitches and 0 of 8 queued leads have a `contact_email`. Every pitch falls back to the channel's About page.
- **Why:** `lead_harvester.py:619` only searches the **channel** description for an email. The harvester already downloads the descriptions of each creator's last 10 videos (`videos().list(part="snippet,…")`, line 538), which is where creators usually write "Business inquiries: …". Those descriptions are thrown away.
- **Fix:** also scan those video descriptions for emails, which costs zero extra YouTube quota. Then put leads that have an email first in the queue.
- **Effort:** small.

### 2. Half your leads are the wrong length
- **Evidence:** of the 8 queued leads, 4 are 7–10 minute videos (439–584 s). You want 20–60 minute videos.
- **Why:** `lead_harvester.py:608` takes the newest upload between 5 and 90 minutes. The searches (line 312) also have no length, date or language filter. Their results are often old or short videos from inactive channels that then fail the "2 long videos in 30 days" check, wasting part of each 100-unit search.
- **Fix:** pick the newest upload of 20–60 minutes (fall back to 12 minutes or more). Add `videoDuration`, `publishedAfter` (last 30 days) and `relevanceLanguage="en"` to the search. That gives more qualified creators per search at the same quota cost.
- **Effort:** small.

### 3. You skipped 6 of your 12 processed leads, after the full work was already done
- **Evidence:** 6 of 12 records are `user_skipped`.
- **Cost:** every skipped lead had already been downloaded, transcribed, picked by AI, rendered and uploaded to Drive.
- **Fix:** depends on why you skip.
  - **No way to contact them:** finding 1 fixes it.
  - **Wrong niche or size:** add a quick approve/skip on the creator before the worker processes them. The Scout tab would list candidates, and the worker only takes approved ones.
  - **Clips weren't good:** that's a clip-quality problem, a different fix.

### 4. Failures burn leads and redo all the work
`_handle_lead_failure` (`autonomous_worker.py:542`) puts every failed lead back in the queue up to 3 times. Each retry starts from zero, including a fresh full download (line 583). After the third failure the creator is marked failed and never contacted.
- **Errors that can never succeed are retried anyway:** private, members-only or age-restricted videos, or an expired Drive login.
- **A failed upload throws away the finished clips.** If the Drive upload fails, the rendered clips are deleted in the `finally` block (line 655).
- **The Drive login may die every week.** If your Google Cloud app's consent screen is in "Testing" mode, Google expires the Drive sign-in after 7 days. Then every lead fails 3 times and is marked failed for good. Check this in Google Cloud Console → APIs & Services → OAuth consent screen, and switch it to "In production" if it says Testing. After that, sign in once more.
- **No log file.** The worker only prints to the terminal, so overnight failures leave no record.
- **Fix:**
  - Check the Drive login before starting a lead, and pause with a clear message if it's dead.
  - Retry only the step that failed: AI call, upload or network.
  - Fail permanent errors immediately instead of retrying.
  - Keep finished clips until the upload succeeds.
  - Write a log file to `storage/logs/`.
- **Effort:** medium.

### 5. Discovery throws away creators it already paid for
- **Evidence:** in a simulated run, 12 creators qualified and 5 were returned. The other 7 were marked "seen" and will never be checked again.
- **Why:** `lead_harvester.py:646` stops as soon as it has `max_results` leads. Every creator in the batch had already been marked seen (lines 476–477), and their data had already been fetched.
- **Fix:** finish the batch and queue everyone who qualified. It's about a 2-line change.

---

## Tier 2: worth doing

### 6. Downloads: wrong format, too much data, duplicates
- **The format rule falls back to "best".** 6 of your 13 downloads are AV1, and one is **4K** (587 MB for a 14-minute video), even though the rule asks for H.264 at 1080p or below. AV1 and 4K are much slower to decode on a MacBook Air. The cause is the last fallback, `"bv*+ba/b"` (`downloader.py:24`). Fix: use yt-dlp's format sorting (`res:1080, vcodec:h264`), which always lands on the closest match and never on 4K.
- **The same video is downloaded again and again.** One 38-minute video is in `storage/raw` **5 times** (1.4 GB), another twice. That folder holds **3.7 GB** of leftovers. Fix: name downloads by YouTube video ID and reuse an existing file. The worker should also clean up old files the way the app does.
- **The whole video is downloaded, but only about 3 minutes is used.** The worker downloads the full video first (line 583). Better order: check the video's details first, which catches private, live, too-short or too-long videos before anything is downloaded. Then download the audio (about 1 MB a minute), transcribe and pick clips. Finally download only the 3 clip sections, with a few seconds of padding. That's about 90% less downloading on 40-minute videos, which finding 2 will bring more of.

### 7. Three copies of the main flow, each slightly different
`main.py`, `app.py` and the worker each do download → transcribe → pick their own way:

| | Uses free YouTube captions | Audio before video | Renders 2 clips at once | Backup clip picker |
|---|---|---|---|---|
| CLI (`main.py`) | ✅ | ✅ | ❌ | (uses the OpenAI picker only) |
| App (`app.py`) | ❌ | ✅ | ✅ | ✅ |
| Worker | ❌ | ❌ | ❌ | ❌ |

The worker is your main production path and has the fewest of these. Fix: one `process_video()` in `core/pipeline.py` that all three call, so every improvement lands everywhere. Round 2 did this for rendering; this finishes the job.

### 8. The AI step gives up too easily
- `gemini_resilience.py:45` only retries Google server errors (5xx). A rate limit (429) fails the whole lead, which then re-downloads everything (finding 4).
- No JSON schema is sent to Gemini, so a malformed answer also fails the lead.
- **Fix:** retry the AI call itself with a short wait, and pass `response_schema`.
- **Effort:** small.

### 9. "Clear Temp & Render Cache" can break a running worker
`purge_storage` (`storage_manager.py:13`, called at `app.py:572`) empties `storage/raw` and `storage/temp`, including the worker's in-progress job folder. Fix: skip `worker_*` folders and anything changed in the last hour.

---

## Tier 3: housekeeping that makes your AI tools more accurate

### 10. About 800 lines of dead code
- `core/lead_finder.py` (625 lines) isn't imported anywhere.
- These are unused too: `download_video_segment`, `ingest_video` (`downloader.py`), `qualify_channel_candidate` (`agent_brain.py`) and `YOUTUBE_QUOTA_WAIT_SECONDS`.
- `project_code_bundle.txt` (235 KB) and `autonomous_worker.py.orig` are leftovers.

Antigravity and Copilot read all of it and sometimes edit the wrong copy. Removing it is roughly 10% less code to confuse them.

### 11. Tests live only in `claude_review/`
Move them into a project `tests/` folder and add one command that runs them all. Then you can tell Antigravity "run the tests after every change", which catches breakage before you see it in the app.

---

## Not needed yet (as you scale)
- **Groq free tier:** 8 hours of audio a day, about 12 forty-minute videos. Past that the code falls back to slow CPU transcription. When you get there, either use free YouTube captions for picking and Groq only for the 3 clip windows, or pay as you go ($0.04 per audio hour, about 3¢ per 40-minute video).
- **Faster rendering:** the worker renders clips one at a time. Rendering 2 at once and using the Mac's hardware video decoder would cut render time, but at 1–3 minutes per lead it's not worth it yet.

## Checked and fine
- **Secrets:** `.env`, `token.json`, `client_secret.json` and `service_account.json` are git-ignored, and none is tracked.
- **Storage files:** all the JSON files are valid.
- **yt-dlp:** current (2026.10.21).
- **Audio levels:** consistent across sources (−14 to −16 LUFS on the two I measured), so no loudness fix is needed.
- **Speed:** 1–3 minutes per lead end to end.

## Suggested build order (round 4)
1. **Findings 1, 2, 5:** better leads with emails, no lost creators. Small and contained in `lead_harvester.py`.
2. **Finding 4:** stop burning leads (plus the 2-minute check of your Google consent screen).
3. **Findings 6 and 8:** downloads and AI retries.
4. **Finding 7:** one shared pipeline. This is the biggest change, best done after the above.
5. **Findings 9–11:** cleanup.
