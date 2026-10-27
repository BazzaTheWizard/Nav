# Hide finished leads

**What it does:** The Lead Finder tab stops showing creators whose status is `sent` or `completed`.

**What it doesn't do:** It doesn't delete anything. Those records stay in `storage/processed_creators.json`, so the worker still knows never to contact those creators again. The Outbox tab already only shows pending pitches, so it needed no change.

**Details**
- If a creator has several records (for example, a retry that later finished), only the newest one counts.
- The Scout tab line now reads `Active leads: X | Finished (hidden): Y | Pending in outbox: Z`.
- To also hide creators you skipped, change one line near the top of `app.py`:
  `FINISHED_LEAD_STATUSES = frozenset({"sent", "completed", "skipped"})`

**Files:** Only `app.py` changes (+32 / −3 lines).

## How to apply

1. Save a restore point:
   `git add -A && git commit -m "Before hide finished leads"`
2. Apply the patch from the project root:
   `patch -p1 < claude_review/hide_finished_leads/hide_finished_leads.patch`
   If it says a hunk failed, copy `patched_files/app.py` over your `app.py` instead. That only works if you haven't edited `app.py` since Round 2.
3. Check it:
   `python claude_review/hide_finished_leads/tests/test_hide_finished_leads.py .`
4. Restart Streamlit, look at the Lead Finder tab, then commit:
   `git commit -am "Hide finished leads"`

## Antigravity prompt (only if you'd rather it apply the patch)

> Apply `claude_review/hide_finished_leads/hide_finished_leads.patch` to `app.py` exactly as written. Don't change anything else. If a hunk doesn't apply cleanly, make the same change by hand using `claude_review/hide_finished_leads/patched_files/app.py` as the reference, then run `python claude_review/hide_finished_leads/tests/test_hide_finished_leads.py .` and show me the output.
