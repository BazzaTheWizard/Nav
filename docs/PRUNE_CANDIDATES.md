# Prune candidates: what the gaming app no longer reaches (Round 66)

**Nothing has been deleted.** This is a list to decide from after launch. Every file named here
is still in the repo and still passes its tests, and `NAV_MODE=b2b` still runs the whole LinkedIn
app.

Made with `tools/import_graph.py` on the `gaming-pivot` branch (run it again after any change:
`python tools/import_graph.py`, or `--json` for everything). It reads the code without running
it. The numbers below are from 95 project files, 41,475 lines (tests and the tool itself not
counted).

## Short version

| | Files | Lines | Safe to delete as they stand? |
|---|---|---|---|
| 1. No import reaches them | 4 | 701 | Two yes; two need 2 import lines and 5 hidden functions removed from `app.py` first |
| 2. Imported, but nothing in them is ever called | 2 | 494 | After removing the import lines that name them |
| 3. Mostly unused: a few functions still called | 7 | 5,347 unreached of 8,126 | No. The used functions need a new home first |
| 4. The hidden half of `app.py` | 50 functions | 1,403 | Yes, together with `_main_b2b`, if you never want `NAV_MODE=b2b` again |

The honest headline: **a plain import graph finds very little**, because the three GTA tools
render through `core.pipeline`, and `core.pipeline` imports almost the whole editing stack (91 of
95 files are reachable by imports alone). The real B2B weight is in section 3: files the gaming app
still loads for two or three small functions.

## Entry points

`app.py` in gaming mode (`main()` without `_main_b2b`, read function by function), `gta_tab.py`,
`tools/narrate.py`, `tools/clip_stream.py`, `tools/find_leads.py`, `core/tool_jobs.py` (it runs
itself as the job wrapper).

## 1. No import reaches them

| File | Lines | Who imports it | Tests |
|---|---|---|---|
| `core/brand_outro.py` | 158 | only `tools/brief_clips.py` | `test_round58.py` |
| `tools/brief_clips.py` | 172 | nobody: it is its own command-line tool (the Round 58 client-brief clipper), not a gaming one | `test_round58.py` |
| `core/follow_up_plan.py` | 208 | `app.py`, used only by `_render_follow_up_first`, `_render_header`, `_render_marketer`, `_render_sent_pitches` | `test_round53.py` |
| `core/reply_stats.py` | 163 | `app.py`, used only by `_render_reply_stats` | `test_round54.py` |

- `brand_outro.py` + `brief_clips.py` + `test_round58.py` can go together with no other edit.
  (`core/brief.py` stays: `clip_stream.py` uses its `save_words` / `load_words`.)
- `follow_up_plan.py` and `reply_stats.py` are imported at the top of `app.py`, so deleting them
  breaks the app at start-up unless the two import lines and the hidden functions that use them go
  too (section 4).

## 2. Imported, but nothing in them is ever called

| File | Lines | Why it is never called | Tests |
|---|---|---|---|
| `core/character_faces.py` | 165 | Only imported inside `core/face_tracker.py` functions that nothing in gaming calls (the channel face memory from Round 34) | `test_round23.py` |
| `core/marketing_contact.py` | 329 | Imported by `app.py` (used only by `_render_b2b_prospects`, `_render_marketer`) and by `core/linkedin_harvester.py`, whose callers are B2B only | `test_round53.py` |

This tier comes from following names, not import lines, so it is a strong hint, not a proof (see
"What the tool cannot see").

## 3. Mostly unused: what is left that gaming still calls

"Unreached" = functions and classes nothing in gaming mentions. The right-hand column is everything
outside the file that still uses it: move those few functions somewhere small and the rest of the
file is a candidate.

| File | Unreached | Lines unreached / total | Still used from outside | Tests that touch it |
|---|---|---|---|---|
| `autonomous_worker.py` | 85 of 104 | 1,952 / 2,598 | System tab: `get_worker_status`, `start_worker`, `stop_worker`, `drive_backup_on`, `WORKER_LOG_PATH`, `MIN_CLIP_SCORE`. Make clips: `load_cached_transcript`, `save_cached_transcript`. The YouTube quota flag (`is_/set_youtube_quota_exhausted`) via `b2b_sources` / `lead_harvester` | 34 |
| `core/linkedin_harvester.py` | 59 of 62 | 1,143 / 1,564 | `fetch`, `search_podcasts`, both only from `core/b2b_sources.py` | 5: rounds 46, 47, 51, 53, 54 |
| `core/lead_harvester.py` | 37 of 45 | 870 / 1,241 | System tab's settings table: `lead_profile`, `subscriber_range`. `b2b_sources`: `_youtube_client`, `_execute_youtube_request` | 5: rounds 4, 23, 25, 30, 51 |
| `core/pitch_files.py` | 29 of 33 | 496 / 645 | Make clips: `make_preview` (series batches). System: `pitch_files_bytes` | 7: rounds 15, 17, 19, 22, 23, 48, 52 |
| `core/outreach.py` | 29 of 53 | 446 / 1,039 | Make clips' batch email (`build_outreach_email`, `build_subject`, `greeting_name_of`, `subject_for`, `fill_drive_link`); `core/social_post.py` and `core/agent_brain.py` use its text cleaners (`clean_channel_name`, `clean_format`, `clean_usual_take`, `episode_title`) | 24 |
| `core/clip_pages.py` | 18 of 44 | 298 / 804 | Make clips: `publish_page`, `record_manual_page`, `update_manual_page`, `remove_clip`, `covers_on`; `main()`: `configured`, `register_own_network` | 10: rounds 16, 19, 26, 45, 46, 49, 50, 51, 52, 56 |
| `main.py` | 3 of 5 | 142 / 235 | Make clips: `_audio_duration`, `_video_duration` | 1: round 46 |

Also loaded only for their helpers: `core/brief.py` (17 of 19 unreached, 220 of 340 lines; gaming
uses `save_words` / `load_words`), `core/b2b_sources.py` (reached only through the System tab's
"B2B harvester" settings row), `core/fit.py` (through `marketing_contact` / `reply_stats` and the
hidden functions).

The 34 tests that touch `autonomous_worker.py` and the 24 that touch `core/outreach.py` are most of
the pre-Round-55 suite (`test_agent_worker.py`, `test_hide_finished_leads.py`,
`test_outreach_email.py`, rounds 2-25, 32, 36, 44-54). They test the LinkedIn workflow; they go
when it goes, not before.

## 4. The hidden half of `app.py`

50 of its 111 top-level functions (1,403 of 4,026 lines) cannot run in gaming mode. They are the
Outbox, Sent and Prospects tabs and the outreach header:

`_main_b2b`, `_render_header`, `_render_worker_status`, `_render_outbox`, `_render_pitch_card`,
`_render_pitch_clip`, `_render_pitch_clips`, `_render_pitch_badges`, `_render_contact`,
`_render_creator_contact`, `_render_identity_links`, `identity_links`, `_render_marketer`,
`_marketer_for`, `_render_linkedin_messages`, `_render_poster_picker`, `_poster_image`,
`_auto_poster_time`, `_render_cut_words`, `_render_remove_clip`, `_rerun_this_clip`,
`_clip_replacer`, `_render_clear_controls`, `_on_draft_edit`, `_on_greeting_change`,
`_render_pass_menu`, `_render_pass_summary`, `_render_recently_passed`, `_render_sent_pitches`,
`_render_page_views`, `_page_activity`, `_opened`, `_played`, `_render_follow_up_first`,
`_render_reply_stats`, `_render_outreach_numbers`, `_render_replies`, `_render_waiting_pitch`,
`_render_pipeline`, `_render_b2b_prospects`, `_render_b2b_prospect`, `_prospect_of`,
`_prospects_by_id`, `_render_lead_queue`, `_render_forceable_leads`, `_latest_lead_records`,
`_is_finished`, `_ignore`, `_about_url`, `_render_manual_pages`.

Modules that only these functions use inside `app.py`: `core.fit`, `core.follow_up_plan`,
`core.linkedin_harvester`, `core.marketing_contact`, `core.reply_stats`. 37 test files open
`app.py`; which of them drive these tabs has to be read test by test (the tool only sees that they
import `app`).

## What only Make clips and the System tab keep loaded

The three GTA tools on their own (Narrate, Stream clips, Find Streamers, without `app.py`) load 74
files. These 16 are loaded only because the app also has Make clips and System (9,460 lines with
`app.py`'s own share left out): `autonomous_worker`, `core.b2b_sources`, `core.clip_pages`,
`core.drive_uploader`, `core.fit`, `core.follow_up_plan`, `core.lead_harvester`,
`core.linkedin_harvester`, `core.long_video`, `core.made_clips`, `core.marketing_contact`,
`core.pitch_files`, `core.poster`, `core.reply_stats`, `core.series`, `core.storage_manager`,
plus `main`. Some of these are Make clips itself (`series`, `long_video`, `made_clips`, `poster`,
`storage_manager`): they are not B2B, they stay as long as Make clips does.

## What the tool cannot see

- **The outreach worker is its own program.** `autonomous_worker.py` is started as a separate
  process (`start_worker`), so "85 of 104 unreached" means "unreached from the gaming app", not
  dead: if the worker keeps running for existing LinkedIn clients, all of section 3 is live code.
- Code reached by name at run time: `getattr`, a function looked up in a dict by a string, a
  script started with `subprocess`. The tool follows names written in the code.
- Methods. A class counts as used, whole, as soon as its name is mentioned.
- Saved state. `storage/*.json` (the worker's queue, sent pitches, prospects) is not code and is
  not in this list; pruning code does not touch it, and it is not needed by the gaming app.
- `website/` and `cloudflare/` (the offer page and the clip-page Worker) are not Python and were
  not analysed. They belong to the B2B side.
- `requirements.txt`: `google-api-python-client` and `google-auth-oauthlib` are imported only by
  `core/drive_uploader.py` and `core/lead_harvester.py`, `boto3` only by `core/clip_pages.py`,
  `yfinance` only by `core/market_data.py` and `playwright` by `core/cards.py` and `app.py` (the
  data-card graphics, which Make clips still offers). They can go only when those files do.

## A possible order, after launch

1. `core/brand_outro.py`, `tools/brief_clips.py`, `tests/test_round58.py`: no other edit needed.
2. Decide whether `NAV_MODE=b2b` and the worker are ever coming back. If not:
3. Remove section 4 from `app.py` with its import lines, then `core/follow_up_plan.py`,
   `core/reply_stats.py`, `core/marketing_contact.py` and tests 53-54.
4. Move the handful of still-used functions in section 3 into small modules (worker status and
   transcript cache out of `autonomous_worker.py`; the text cleaners out of `core/outreach.py`),
   then the rest of those files and their tests.
5. Re-run `python tools/import_graph.py` and `python tests/run_all.py` after each step.
