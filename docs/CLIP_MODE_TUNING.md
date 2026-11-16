# Clip mode: how the highlight signals were tuned (Round 62)

Tuned on one real VOD: `https://www.twitch.tv/videos/2923997997` ("desktop/GTA V marathon",
pdotz, 2 h 12 min, 1584 five-second buckets). Audio and a local Whisper transcript (13,175
words, about 100 words a minute) were used; the chat replay could not be fetched from the
build machine, so the chat signal is tuned on synthetic bursts only. **Re-check the chat
numbers on the first real VOD with chat** (see "What to check" below).

## Loudness (`signals.audio_values`)

What the stream sounded like: short-term loudness median -37.5 LUFS, 10th percentile -48.6,
90th -31.3. Momentary peaks: median -28.4, 90th -25.4, loudest -19.4.

- First version: mean short-term loudness per bucket against a 2-minute rolling median,
  +9 dB = 1.0. Result: **266 hot buckets (17 % of the stream)**. The median fell during
  quiet stretches (loading screens, AFK), so the next ordinary sentence read as a spike,
  while the real screams were only 4-6 dB above normal loud talking.
- Now: the bucket's **loudest momentary (400 ms) value** against the **70th percentile of
  short-term loudness over the last 10 minutes**, floored at the stream's own 40th percentile.
  +4 dB starts to count, +13 dB is 1.0. Buckets outside the loudest 6 % of the stream are
  scaled by 0.45, so they can never be hot on their own.
  Result: **59 hot buckets (3.7 %)**, 11 at 0.9+, spread across the stream, and the top ones
  were real: a firefight ("Where are they? Bro. Oh my days"), a chain of "no no no no", "should
  I punch this dog? Oh no, not the guy".

## Speech (`signals.speech_values`, new)

Reaction words in the transcript ("oh my days", "no way", "oh shit", "let's go", "bro", "wait"...),
repeats ("no no no", capped at 4 per bucket because Whisper sometimes loops one word 20 times)
and exclamation marks, smoothed over neighbouring buckets, against a 2-minute rolling median;
6 points = 1.0. On the VOD: 3 hot buckets alone, 49 above 0.3. Its top hits were the "no no no"
chain and "oh my god, oh my days, yo, wait". It is weaker than chat (weight 0.7) and mainly
confirms loudness: with both agreeing a bucket gets +0.25.

The regex has UK phrases from this streamer; add a streamer's own catchphrases to
`signals.REACTION` when you clip them regularly.

## Chat (`signals.chat_values`)

Unchanged in strength (hype-weighted messages against a 2-minute rolling median, 4x = 1.0),
but **shifted one bucket (5 s) earlier** (`CHAT_LAG_BUCKETS`): chat reacts after it sees the
moment, so the shifted value sits on the moment itself, where loudness and speech spikes sit.
The picker's chat sample for the model looks in the 5 s after the moment for the busiest
12 s window, so the model sees the reaction, not the chatter before it.

## Fused (`signals.fuse`)

max(chat, 0.8 x loudness, 0.7 x speech) + 0.25 when any two are at 0.5 or more. On the VOD
with loudness + speech only: **20 hot buckets in 17 runs**, 13 moments seeded after the 45 s
spacing. A hot run is a spike at 0.65+, the same bar `core.heatmap_scorer` uses for YouTube's
"Most replayed".

## The picker (`core/clip_mode/picker.py`)

Seeds from the hot runs (strongest first, 45 s apart, at most 24 shown to the model). For each
one the model sees 25 s of words before and 20 s after, plus up to 14 chat messages of the
reaction, and returns keep/drop, a label, the payoff second, the setup needed (2-14 s), a hook
title and a 1-10 score. Boundaries: payoff - setup to the end of the hot run + 2 s (3-20 s of
reaction), each edge moved up to 2.5 s to the nearest pause of 0.35 s+ between words, 15-60 s.
With no model key the spikes are kept as they are (`--plan` shows them).

On the VOD without a model, the 13 moments came out 15-21 s long (the floor: a dry run has
no setup or payoff from the model).

## What to check on the first VOD with chat

1. `python tools/clip_stream.py <url> --campaign x --plan` and read `moments.json`: are the
   top moments where chat went wild? If chat spikes land 5-10 s after the moment even after the
   shift, raise `CHAT_LAG_BUCKETS` to 2.
2. If a very busy chat makes everything hot, raise `SPIKE_RATIO` (4 -> 6). If a quiet chat
   never gets hot, lower it (4 -> 3) or rely on loudness + speech (they work without chat).
3. Render 2-3 and watch them: if clips start too late (the setup is missing), raise
   `SETUP_DEFAULT`; if they run on after the reaction, lower `REACTION_MAX`.

---

# Long VODs: transcribe only the windows (Round 65)

Code: `core/clip_mode/windows.py`, the flow in `tools/clip_stream.py` (`analyse()`), downloads in
`core/clip_mode/vod.py`. Checked on the same real VOD as above (pdotz, 2 h 12 min), which still had
its audio and a whole transcript on disk, and this time its real chat replay (`chat.json`, 307
messages). The signals themselves (`signals.py`) are unchanged; `speech_values` only gained an
optional `covered` argument.

## Why

Transcribing is the step with a hard limit. Measured on this Mac:

| Step | 2 h 12 min VOD | 7 h VOD (scaled) |
|---|---|---|
| Loudness, whole stream (FFmpeg ebur128, audio only) | 6.3 s | about 20 s |
| Chat replay | one small download | the same |
| Transcript, whole stream (Groq, 1.5 s per minute of audio) | 3.3 min | 10.5 min of work, **but 25,200 s of audio** |
| Transcript, windows only | 68 s for 45 min | the same 68 s |

Groq's published base limits for `whisper-large-v3` and `-turbo` (console.groq.com/docs/rate-limits,
read 2026-11-16) are **7,200 audio-seconds an hour and 28,800 a day**, 20 requests a minute. One
7-hour VOD is 3.5 hours of that allowance and most of a day's; the tool stops with "Groq's
transcription limit is used up" long before the end. 45 minutes of windows is 2,700 s: two VODs an
hour, ten a day, on the same key. (Your own limits are on console.groq.com/settings/limits.)

## The numbers

| Constant | Value | Why |
|---|---|---|
| `WARM` | 0.40 | Windows are chosen before any words exist, so the bar is not "hot" (0.65) but "can still become hot": `fuse` adds 0.25 when speech agrees, and 0.65 - 0.25 = 0.40. That is loudness at 0.5 or chat at 0.4. |
| `PAD` | 60 s each side, with chat | The picker reads 25 s of words before a moment and may add 14 s of setup and 2.5 s to reach a pause (41.5 s); after it, 20 s + 20 s of reaction + 2.5 s (42.5 s). `EDGE` = 45 s covers both, and 60 leaves 15 s for a spike that sits a little off the moment. |
| `PAD_NO_CHAT` | 90 s each side | With loudness alone the spike is the scream, not the thing that caused it, and there is no chat to confirm it; the wider window gives the speech signal 45 s of room each side to move the moment. |
| `CORE_MAX` | 45 s | A hot run can last minutes (a raid, a firefight). A clip is 60 s at most and the picker starts from the run's beginning, so only its first 45 s get a window. |
| `MAX_AUDIO` | 45 min | See "Why". Also see the recall figures below: on the real VOD 30 min found the same moments as 45, so the cap is not what limits quality. |
| `MAX_WINDOWS` | 30 | The picker shows the model 24 moments at most (`picker.MAX_SEEDS`); 30 merged windows is more than it can use. With isolated spikes the 45 minutes run out first: about 21 windows with chat (125 s each), 14 without (185 s each). |
| `DEAD_DB`, `DEAD_BUCKETS` | 12 dB under the stream's median, for 20 s, with no chat message | See "Dead air". |

Windows are taken strongest first (peak value, then total heat), merged where they overlap, and one
that would break either cap is left out (weaker ones that still fit are tried).

## What it finds, against the whole transcript (real VOD)

80 runs reach `WARM`. With the whole transcript: 24 hot buckets, 16 moments seeded, and a 6-clip
dry-run plan.

| Setting | Windows | Audio | Seeded moments also found | Same clips in the 6-clip plan |
|---|---|---|---|---|
| no chat, ±90 s, 45 min (the default without chat) | 9 | 45.0 min (34 %) | 12 of 16 | 5 of 6 |
| chat, ±60 s, 45 min (the default with chat) | 13 | 44.9 min (34 %) | 12 of 16 | 5 of 6 |
| no chat, ±90 s, 30 min | 6 | 30.0 min (23 %) | 12 of 16 | |
| no chat, ±90 s, 60 min | 10 | 60.0 min (46 %) | 13 of 16 | |
| no chat, no cap at all | 11 | 98.2 min (74 %) | 14 of 16 | |

- The 2 moments no cap recovers are **speech-only**: nothing loud, no chat, just the words
  ("no no no no"). They are the price of not reading everything. `--full-transcript` finds them.
- On a 2-hour stream the windows merge into a third of the audio. On 7 hours the same 45 minutes are
  11 % of it, and the cap decides: only the 14-21 strongest moments get a window. That is still
  more than the 6-12 clips usually asked for, but a weaker moment that would have become hot from
  its words is not seen.
- Two real windows (385 s) went through Groq and back: the words landed a median 0.02-0.04 s from
  where the whole transcript has them, so times map back correctly on real audio.

## Dead air

A bucket is dead when its short-term loudness is 12 dB under the stream's median **and** no chat
message falls in it; only the inside of a stretch of 4 or more such buckets (20 s) is cut, the
first and last bucket stay (a word can straddle them; short-term loudness also trails by 3 s).

On the real VOD the median is -37.5 LUFS, so dead is under -49.5: about 7-11 % of buckets are that
quiet, but they still hold 2-3 words each (the stream averages 9), because this streamer rarely
stops talking. Requiring 20 s in a row leaves 2-6 stretches, 0.8-2.2 minutes in all, with 9-20 words
inside them. So on a talker it saves almost nothing (0-10 s of the 45 minutes) and loses almost
nothing; it is there for AFK and loading stretches inside a window. A "be right back" screen with
music is not quiet and is not cut.

## Chat, on its first real replay

307 messages in 2 h 12 min (2.3 a minute). `chat_values` peaked at 0.5 and never reached hot;
3 buckets reached 0.3. Only one bucket had chat and loudness both raised (0.3+) with no shift, and
none with chat shifted 5, 10 or 15 s earlier, so this chat is too thin to check `CHAT_LAG_BUCKETS` on. That is what to expect from the 30-150 viewer
streamers the lead finder looks for: **their clips will come from loudness and speech**, and the
windows from loudness alone. The chat numbers still need a busy replay (hundreds of viewers); the
steps under "What to check on the first VOD with chat" stand.

## Resuming

Everything is kept in the stream's output folder and `progress.json` records each step with the
inputs it was made from: the audio (re-fetched only if it is shorter than the VOD), the chat,
`loudness.npy`, `windows.json`, one JSON of words per window under `windows/` (written as each one
finishes), `clips.json` (the picks, so a re-run renders the same clips), and each finished
`clip_N.mp4`. Run the same command again and finished steps are skipped; change `--count` and only
the picking is redone. When Groq's limit stops a run, the message says when to run it again.

## Downloads

yt-dlp skips a fragment it cannot fetch unless told not to, which gives a file that plays but is
short, with every later timestamp wrong. Now: fragments are never skipped (10 retries each), a
failed download is tried 4 times (5, 20, 60 s apart, carrying on from the fragments already there),
and a file counts as complete only when FFmpeg reads it and it is within 15 s (or 1 %) of the VOD's
length. The same check guards each clip's video section.
