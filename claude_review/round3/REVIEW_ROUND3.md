# Round 3: punch-in zooms and whoosh sound effects

Built Oct 27, 2026 on top of your "Round 2" commit. Nothing had changed since.

## What's in this folder

| File | What it is |
|---|---|
| `claude_fixes_round3.patch` | All changes (9 files, including one new file: `core/effects.py`) |
| `patched_files/` | Full copies of every changed file, for any hunk that won't apply |
| `tests/` | 78 checks across 4 files (rounds 1–3). All must print `0 failure(s)` |
| `preview_punch_ins.mp4` | 22-second test render: face crop, camera cuts, 2 punch-ins with whoosh. Turn the sound on |
| `punch_ins_preview.png` | Frames before, during and after each punch-in. Top row: face crop. Bottom row: blurred layout |

## How to apply

Git is already set up, so you can undo this with `git reset --hard` if needed. From the project root:

```bash
patch -p1 --dry-run < claude_review/round3/claude_fixes_round3.patch   # 9 files, no errors
patch -p1 < claude_review/round3/claude_fixes_round3.patch
source .venv/bin/activate
for t in test_fixes test_agent_worker test_round2 test_round3; do python claude_review/round3/tests/$t.py .; done
git add -A && git commit -m "Round 3: punch-in zooms and whoosh"
```

### Prompt for Antigravity

> Apply `claude_review/round3/claude_fixes_round3.patch` exactly as written by running `patch -p1 < claude_review/round3/claude_fixes_round3.patch` from the project root. Do not rewrite, reformat or "improve" the patched code. If a hunk fails, make that hunk match `claude_review/round3/patched_files/<same path>` and keep any edits I made after the "Round 2" commit. Then run the four test files in `claude_review/round3/tests/` with `.` as the argument (`test_fixes`, `test_agent_worker`, `test_round2`, `test_round3`). All four must print `0 failure(s)`. Commit as "Round 3: punch-in zooms and whoosh". Tell me which hunks, if any, needed manual work. Do not delete any files.

## What it does

**Punch-ins.** At moments that land a point, the video cuts to a tighter crop (1.15× by default) and holds it until that sentence ends:
- Each punch-in starts on a sentence start and holds 1.2–3.5 seconds.
- Punch-ins are at least 3 seconds apart.
- None happen in the first 1.5 seconds, so the hook title stays clean.
- The zoom is aimed at the speaker's face, so the face gets bigger without moving on screen. In the face-crop layout it follows camera cuts.
- It works in the face-crop and blurred layouts. Stacked is left alone because it's already busy.

**Who picks the moments.** The AI picks them in the request it already makes, so there's no extra API call. Gemini and the OpenAI picker now return `punch_ins` for each clip: the start times of the sentences that land the key point, like a bold claim, a surprising number or the punchline. AI moments are placed first. Long stretches without one get a punch-in at a sentence start about every 7 seconds, so every clip has some movement even if the AI returns none.

**Whoosh.** A short whoosh plays at the start of each punch-in, about 10–13 dB quieter than the voice. The voice itself isn't changed or lowered. The sound is synthesized by the code, so there's no license to worry about when you send clips to creators. It's created at `assets/sfx/whoosh.wav` on the first render. Drop your own `whoosh.wav` there to replace it.

**Re-trims** made with "Adjust Clip Boundaries" in the app keep their punch-ins.

## New optional `.env` settings

```
PUNCH_IN_ZOOM=1.15     # 1 turns punch-ins off; 1.1 is subtler, 1.25 is stronger
PUNCH_IN_SFX=1         # 0 turns the whoosh off
SFX_VOLUME=0.35        # whoosh loudness, 0–1
```

## How this was tested

- **78 automated checks pass:** 54 from rounds 1–2 and 24 new ones. The new ones cover moment picking and spacing, AI moments taking priority, face-anchored zoom across camera cuts, the FFmpeg commands, the settings and the AI response format.
- **Real renders:**
  - The 1080p multi-camera test clip came out as a face crop with 2 punch-ins.
  - A 32-second section of your 720p video came out as the blurred layout with 3 punch-ins.
  - The frames in `punch_ins_preview.png` show each zoom switching on and off at the planned times.
- **Audio:** I compared the output with the original audio. The only differences are at the 3 punch-in times, and the voice level is unchanged (RMS 0.141 vs 0.140).
- **CLI on your 38-minute video:** 3 clips in 95 seconds, against 89 seconds without punch-ins.
- **The Streamlit app** loads with no errors in a headless test.

**Not tested live:** real Gemini and OpenAI responses. My environment can't reach them, so the AI picks were stubbed. If a model ever leaves out `punch_ins`, the sentence-start fallback still adds punch-ins.
