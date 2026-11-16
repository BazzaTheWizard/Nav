# Handoff: the Clip mode highlight brain (for a stronger model)

Branch: `gta-narrate`. Read `docs/GTA6_INTEGRATION_PLAN.md` first, then this.

## What exists (Rounds 59-61)
- Narrate mode: `core/narrate/*`, `tools/narrate.py` (tests 59, 60).
- Clip mode, mechanical part: `core/clip_mode/chat.py` (chat replay in), `core/clip_mode/signals.py`
  (chat + loudness spikes -> heatmap buckets), `tools/clip_stream.py` (VOD -> chat -> transcript ->
  heatmap -> `core.pipeline.pick_clips_with_agent(heatmap=...)` -> `render_selected_clip`),
  `CLIP_LENGTH=gaming` (15-60 s) in `core/clip_length.py` (test 61).

## The problem to solve
`pick_clips_with_agent` (core/pipeline.py) and its prompts (`core/agent_brain.py`,
`core/hook_selector.py`, the judge in `rescore_with_judge` / `judge_clips`) are tuned for B2B
podcasts: "a speaker lands a thesis", sentence-complete arcs, an outreach email. Stream highlights
are different:
- the payoff is often non-verbal (a crash, a clutch escape, a jump scare) and the words are
  reactions ("NO WAY", screaming, laughing), so sentence snapping and "idea-complete" logic cut wrong;
- the hook must be the 1-2 s before the moment, and the clip should end 2-5 s after the reaction peaks;
- the transcript of a stream is messy (game audio, music, overlapping voices);
- chat spikes lag the moment by about 2-8 s (people react after seeing it).

## What to build
1. A gaming mode for the picker, switched on by `CLIP_LENGTH=gaming` (or a new `CONTENT_KIND=stream`):
   - a stream-highlight prompt (hype, rage, funny, clutch, fail, wholesome; a reaction you can
     understand without context), returning moments with a payoff time;
   - candidates seeded from heatmap spikes (shift each chat spike ~5 s earlier for the moment itself),
     not only from the transcript;
   - boundaries: start a few seconds before the payoff with enough setup to understand it, end shortly
     after the reaction; don't require sentence-complete arcs;
   - no outreach email in this mode (skip that call to save tokens).
2. A gaming judge prompt for `judge_clips` (does it work with no context? is the payoff in the clip?
   is the hook in the first 2 s?).
3. Tune `core/clip_mode/signals.py` (bucket size, SPIKE_RATIO, AUDIO_SPIKE_DB, chat lag shift, hype
   regex) on one or two real VODs with chat, and record the chosen values and why.
4. Keep every existing test passing (`tests/run_all.py`, plus 59-61) and add `tests/test_round62.py`.

## Constraints
- The podcast pipeline's behaviour must not change when the gaming mode is off.
- Only licensed campaign VODs (tools/clip_stream.py requires --campaign).
- Also: a review pass over Rounds 59-61 for bugs and edge cases (empty library, VO longer than
  footage, odd frame sizes, VFR sources, files with no audio).
