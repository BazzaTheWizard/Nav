# New outreach email (Nav Studios)

All outreach emails now come from one place, `core/outreach.py`. The worker's drafts, the app's drafts and the app's fallback all use it, so you edit the wording once and it changes everywhere. The AI no longer writes any email text. It only returns the video's topic (for example "squatting pain-free with knee arthritis"), and the code places it in the template.

## The email (4 sentences)
```
Hi {channel name},

I run Nav, a short-form post-production and marketing agency that turns long-form podcasts and video backlogs into high-retention vertical clips. I already cut 3 sample Shorts from your recent video on {topic}, and they're in a private Google Drive folder for you: {Drive link}

You can check out examples of our hook retention cuts, dynamic captions, and production standards here: https://navstudios.base44.app/#samples

No channel passwords or API access needed: we deliver raw MP4 files only, so just reply if you'd like this for every new upload.

Best,
Barry Navan
Nav Studios
```
- **Subject line:** "3 ready-to-post Shorts from your latest video". It's now shown in the Outbox too.
- **Removed:** "aspiring entrepreneur", the student framing and the phone number.
- **If the AI gives no topic,** the email uses the video's title in quotes instead. Before, that failed the whole lead.
- **Your 5 drafts waiting in the Outbox** were written with the old template. Each one is rewritten with the new email the first time the app or worker reads it. Sent emails are never changed.

## Files
- New: `core/outreach.py`, where the wording, link, subject and sign-off live.
- Changed: `core/agent_brain.py` (AI prompt), `autonomous_worker.py`, `app.py`.

## How to apply
Paste these into Terminal one line at a time:

1. `cd "/Users/barrynavan/Short form content project"`
2. Save a restore point. This also saves round 4, which is applied but not committed yet:
   `git add -A && git commit -m "Round 4 + before email update"`
3. Apply the change. No output means it worked:
   `git apply claude_review/outreach_email/outreach_email.patch`
4. Run the tests. Each should end with "All checks passed":
   - `.venv/bin/python claude_review/outreach_email/tests/test_outreach_email.py .`
   - `.venv/bin/python claude_review/outreach_email/tests/test_agent_worker.py .`

   The second test is an updated copy of the round 2 test. The old copy checked for the old email's wording, so it now fails on that one check. That's expected.
5. Restart the worker and Streamlit (Ctrl+C in their windows, then start them again):
   - `.venv/bin/python autonomous_worker.py --continuous-queue`
   - `.venv/bin/python -m streamlit run app.py`
6. Save it:
   `git add -A && git commit -m "Nav Studios outreach email"`

To undo before step 6: `git checkout -- . && git clean -f core/outreach.py`

## Antigravity prompt (only if you'd rather it apply the change)
> Apply `claude_review/outreach_email/outreach_email.patch` exactly as written (use `git apply`). It adds `core/outreach.py` and changes `core/agent_brain.py`, `autonomous_worker.py` and `app.py`; don't change anything else. If it doesn't apply cleanly, copy the files from `claude_review/outreach_email/patched_files/` instead. Then run `.venv/bin/python claude_review/outreach_email/tests/test_outreach_email.py .` and show me the output.
