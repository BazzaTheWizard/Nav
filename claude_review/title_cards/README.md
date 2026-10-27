# Title cards (replaces the old hook title)

See `preview/before_after.png` and the three sample clips in `preview/`.

## What was wrong
- The title was a subtitle style that draws a **separate box behind each line**. The boxes had different widths and overlapping corners, in a dull see-through grey.
- It sat at a **fixed height** (about 15% down), which in face-crop clips is right where the face is.

## What it does now
- **One clean card:** a white rounded box with dark text and a soft shadow, using Montserrat ExtraBold. Lines are split evenly (no single word left alone on a line). Emoji the font can't draw are removed.
- **It avoids the face.** For face-crop clips it checks about 12 frames from the seconds the title is on screen and puts the card in the highest spot that doesn't cover a face. Usually that's under the chin. If the person moves around (squats, walks), it picks the spot that covers the face least, and tries a smaller size if that helps.
- Blurred-background clips: the card sits just above the video. Stacked clips: it sits in the dark bar between the two panels.
- It fades in with a slight rise, then fades out after `HOOK_TITLE_SECONDS` (4 s by default). Captions are unchanged.
- If anything goes wrong with the card, the clip still renders with the old title, and the log says why.
- The log shows where each title went, for example: `Title card: under the face at y=570 ... covers a face in 0 of 12 sampled frames`.

**Cost:** nothing extra. It adds well under a second per clip.

## Options (`.env`, all optional)
- `HOOK_TITLE_STYLE=dark` gives a dark card with white text (the default is `light`).
- `HOOK_TITLE_SECONDS=4` sets how long the title shows; `0` turns it off.

## Files
- New: `core/title_card.py` and `assets/fonts/` (Montserrat font plus its free license, OFL).
- Changed: `core/video_renderer.py`, `core/pipeline.py`, `core/clipper.py`, `config.py`, `requirements.txt` (adds `pillow`, which Streamlit already installs).
- `app.py` isn't touched.

## How to apply (about 2 minutes)
From the project folder in Terminal:

1. Save a restore point:
   `git add -A && git commit -m "Before title cards"`
2. Copy the font into the project (a patch can't carry font files):
   `mkdir -p assets/fonts && cp claude_review/title_cards/assets/fonts/* assets/fonts/`
3. Apply the code changes:
   `patch -p1 < claude_review/title_cards/title_cards.patch`
   If a hunk fails, copy the files from `claude_review/title_cards/patched_files/` over yours instead. That's only safe if you haven't edited those files since Round 2.
4. Check it:
   `python claude_review/title_cards/tests/test_title_cards.py .` should end with "All checks passed".
5. Restart Streamlit, make a clip, and if you like it:
   `git add -A && git commit -m "Title cards"`

To undo before step 5: `git checkout -- . && git clean -fd assets core/title_card.py`

The `claude_review/title_cards/work/` folder was only used for testing; you can delete it.

## Antigravity prompt (only if you'd rather it apply the change)
> Copy `claude_review/title_cards/assets/fonts/` to `assets/fonts/` in the project root. Then apply `claude_review/title_cards/title_cards.patch` exactly as written, without changing anything else. If a hunk doesn't apply cleanly, make the same change by hand using the files in `claude_review/title_cards/patched_files/` as the reference. Then run `python claude_review/title_cards/tests/test_title_cards.py .` and show me the output.
