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
