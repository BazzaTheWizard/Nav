# Round 4: better leads, decided by data

Files changed: `core/lead_harvester.py`, `autonomous_worker.py`, `app.py`. Nothing else is touched. The worker stays fully automatic.

## Your logic, built in
**"Skip creators whose Shorts clearly work, but don't miss the ones who stopped."** The code now judges what a channel is doing with Shorts **now**, not how many it has ever posted. For each creator it reads the newest 50 Shorts and their dates (1 API call) and sorts them into one of these groups:

| Status | Meaning | What happens |
|---|---|---|
| **Stopped** | 3+ Shorts ever, none in the last 30 days (your "2,000 Shorts but quit" case) | Kept, top priority |
| **Slowing down** | Now posting a third or less of their earlier pace (which was at least about 1 a week) | Kept, top priority |
| No Shorts / tried once or twice | | Kept |
| Occasional | 1–3 in the last 30 days | Kept |
| Active but weak | About 1 a week or more, but their Shorts get fewer views than their long videos | Kept, lower priority |
| **Already working** | About 1 a week or more **and** their Shorts get at least as many views as their long videos | **Skipped**, checked again in 60 days in case they stop |
| Couldn't check | YouTube didn't answer | Kept (it never skips without evidence) |

**Why these cut-offs:**
- **About 1 a week:** a 2026 study found that around 5 Shorts a month is where Shorts start lifting a channel. So about one a week means the creator has a real Shorts habit.
- **"At least as many views as their long videos":** on big channels the typical Short gets about half the views of a long video, so this flags only channels where Shorts clearly carry their reach.
- **Fair comparison:** views are compared with the creator's **own** long videos, because typical view counts vary hugely between niches. Shorts less than a week old are ignored, since they're still collecting views.

## Other changes
- **No more lost creators.** Every creator that qualifies in a search batch is now kept. Before, only the first 5–10 were kept and the rest were lost for good.
- **Emails from video descriptions.** The harvester now also searches each creator's recent video descriptions, where "Business inquiries: …" usually is. It costs no extra quota. Image names like `logo@2x.png` are ignored.
- **Wide scope kept.** Nothing new is rejected for length. From the videos it already accepted, it now picks the newest 20–60 minute one when it exists, then 12 minutes or more, then 5 minutes or more as before.
- **Second chances.** Every rejection is saved with its reason in `storage/channel_checks.json`. Creators rejected for something that can change (too few uploads, Shorts working, subscriber count, …) are checked again after 60 days. Only "not English" is permanent.
- **Best lead first.** The queue is ordered by Shorts status, then email found (+2), then a 20–60 minute video (+1). A lead that fails drops back so it can't block the queue.
- **One-click Skip, before any work.** The Autonomous Lead Scout tab now shows "Up next" with each creator's Shorts status, email, video and length (see `up_next_preview.png`; the creators in it are made-up samples). Skip anyone you don't want. The worker never waits for you.
- **Data for tuning.** Each processed lead keeps its Shorts numbers. After a week or two we can compare them with what you skip and adjust the cut-offs, which are named constants at the top of `lead_harvester.py`.

**Quota cost:** about 1 extra unit for each creator that passes the subscriber filter (the Shorts list), plus a little for view counts, only on channels that post Shorts actively. A discovery run goes from about 650 to roughly 700–750 units out of 10,000 a day.

**Leads already in your queue** were found before this update. They show "Shorts: not checked" and go to the back. Skip any you don't want.

## How to apply (about 3 minutes)
From the project folder in Terminal:

1. Save your current work (this also commits the title cards if you haven't yet):
   `git add -A && git commit -m "Before round 4"`
2. Apply the patch:
   `patch -p1 < claude_review/round4/round4_leads.patch`
   If a hunk fails, copy the three files from `claude_review/round4/patched_files/` over yours instead. That's only safe if you haven't changed them since this morning.
3. Run the tests. Each one should end with "All checks passed":
   - `source .venv/bin/activate`
   - `python claude_review/round4/tests/test_round4.py .`
   - `python claude_review/round4/tests/test_hide_finished_leads.py .`

   The second test is an updated copy of the hide-finished-leads test. The old copy expected skipped leads to show, but since you added "skipped" to the hidden list, the old copy now fails. That's expected.
4. One live check against real YouTube. It uses your key and costs about 4 of your 10,000 daily units:
   `python claude_review/round4/check_youtube_lists.py`
   It should print "All good". If it says "Something is off", send me the output; the code falls back safely either way.
5. Restart the worker and Streamlit, open Autonomous Lead Scout, then:
   `git add -A && git commit -m "Round 4"`

To undo before step 5: `git checkout -- .`

## Antigravity prompt (only if you'd rather it apply the change)
> Apply `claude_review/round4/round4_leads.patch` exactly as written. It changes only `core/lead_harvester.py`, `autonomous_worker.py` and `app.py`; don't change anything else. If a hunk doesn't apply cleanly, make the same change by hand using the files in `claude_review/round4/patched_files/` as the reference. Then run `python claude_review/round4/tests/test_round4.py .` and show me the output.
