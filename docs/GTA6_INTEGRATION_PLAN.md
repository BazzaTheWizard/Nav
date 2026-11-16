# Nav Studio: GTA 6 integration plan

Drop this file into the Nav repo as `docs/GTA6_INTEGRATION_PLAN.md` and point Claude in VS Code at it
("implement Phase 1 of docs/GTA6_INTEGRATION_PLAN.md"). Each item names the Nav file it plugs into.

Rule for every phase: put the new code in `core/narrate/` or `core/clip_mode/`, keep it behind a setting,
and add a `tests/test_round59.py`-style test, so the existing podcast pipeline never breaks.

---

## Verdict on the repos you found

| Repo | License | Verdict | Why |
|---|---|---|---|
| Breakthrough/PySceneDetect | BSD-3 | **Use (Phase 1)** | Splits Fiverr footage into shots. `AdaptiveDetector` handles fast camera motion, which gameplay is full of. |
| WyattBlue/auto-editor | Unlicense | **Use (Phase 1)** | Its `--edit motion` method drops static footage (pause menus, loading screens, idle standing). Nav's `midclip` already handles audio dead air, so only the motion mode is new. |
| mifi/lossless-cut | GPL-2.0 | Skip in code; optional manual tool | Nav already drives FFmpeg directly. Handy as a desktop app for hand-trimming a Fiverr delivery, but it adds nothing to the automated pipeline. |
| webadderallorg/Recordly | AGPL-3.0 | Skip | A manual screen-recording editor for product demos. No CLI or batch mode, and you aren't recording a screen. |
| Anil-matcha/AI-Youtube-Shorts-Generator | MIT | Borrow three ideas, not the code | Nav already does more than it does (judge pass, replay heatmap, teaser, cards). The three ideas are listed under Phase 3. |
| cporter202/automate-faceless-content | none | Skip | A promo README for a paid tool (ViralWave), with no code. |

## Other repos worth adding

| Repo | License | Where it goes | What it fixes |
|---|---|---|---|
| jianfch/stable-ts | MIT | `core/narrate/align.py` | **Forced alignment.** You already have the script text, so align it to your voiceover instead of transcribing. Captions match the script word for word, with no Whisper typos. |
| m-bain/whisperX | BSD-2 | Fallback for `align.py` | Word timestamps with forced phoneme alignment, built on faster-whisper (already in Nav). |
| facebookresearch/demucs, or nomadkaraoke/python-audio-separator | MIT | `core/narrate/ingest.py` | **Strips music out of bought footage**, keeping engine, gunfire and ambient sound. Any radio a recorder left on is a Content ID claim waiting to happen. |
| hexgrad/kokoro | Apache-2.0 | `core/narrate/voice.py` (optional) | Fast local TTS for draft or preview voiceovers. |
| resemble-ai/chatterbox | MIT | `core/narrate/voice.py` (optional) | Clones your own voice from a few seconds of audio, for days you can't record. It adds an audio watermark. |
| mlfoundations/open_clip (or SigLIP) | MIT | `core/narrate/library.py` | Local frame embeddings, so the clip matcher can search footage by meaning ("sunset beach drive") with no API cost. Gemini (already in Nav via `google-genai`) writes the readable tags. |
| lay295/TwitchDownloader (CLI) | MIT | `core/clip_mode/twitch.py` | Downloads VODs and the **chat replay** for licensed streamer campaigns. |
| PetterKraabol/Twitch-Chat-Downloader (`pip install tcd`) | MIT | `core/clip_mode/twitch.py` | Python-native chat download, if you don't want the C# CLI. |
| gitroomhq/postiz-app | AGPL-3.0 | Separate service, called over its REST API | Schedules posts to TikTok, Shorts, Reels and X from one queue. Run it as its own Docker app and call its API. **Don't copy its code into Nav**, because of the AGPL. |
| Edualnog/medusa-clip | AGPL-3.0 | Study only | An open-source gameplay-highlight clipper. Read it for ideas, but don't copy the code (AGPL). |

**Skipped on purpose:** Ultralytics YOLO is AGPL, which is risky inside a paid service. Use OpenCV optical flow and saliency instead (Phase 2). Nav already ships OpenCV.

---

## Phase 1: Narrate mode core (build before Nov 19)

**Status: built in Round 59** (`core/narrate/`, `tools/narrate.py`, `tests/test_round59.py`).
What changed from the plan below: the static-footage filter is done in OpenCV inside
`library.py` (auto-editor's motion idea, no extra install); shots are stored as
(file, start, end) instead of cut copies; music stripping is not in yet (ask Fiverr
recorders to turn the radio off, and use `--no-game-audio` when a file has music);
the app.py tab is not in yet, so use the command line in `tools/narrate.py`.

1. **`core/narrate/ingest.py`**
   - `audio-separator` / demucs: split each Fiverr file's audio into music and everything else, and keep everything else.
   - `auto-editor --edit motion:threshold=0.02` (via subprocess): mark static spans.
   - PySceneDetect `AdaptiveDetector`: split into shots, then drop shots that are static or shorter than 1.2 s.
   - Output: `storage/library/<clip_id>.mp4`, plus 3 thumbnail frames per shot.
2. **`core/narrate/library.py`**: SQLite table `shots(id, path, dur, tags_json, embedding, quality, used_count, last_used)`.
   - Gemini tags each shot from its 3 frames: `location, time_of_day, weather, activity, vehicle, vibe, hud_visible, quality 1-10`.
   - open_clip embeds the middle frame, so text search works.
3. **`core/narrate/script.py`**: script format, parser and generator.
   ```
   HOOK: You've been doing this wrong since day one.
   [driving, city, night] Most players never...
   [TEXT: "SAVE $40K"] [ZOOM] Here's the trick...
   ```
   The generator prompt comes from `agent_brain.py`'s style, retuned to 110-150 words, hook in the first line, one fact per line, and a source URL for each fact.
4. **`core/narrate/align.py`**: stable-ts `model.align(audio, script_text)` produces `list[Word]` in Nav's existing `core.transcriber.Word` shape. Fall back to `transcribe_clip_audio` if alignment fails.
5. **`core/narrate/assemble.py`**
   - For each script line, its duration comes from the aligned words. Pick a shot with matching tags and the lowest `used_count`, avoiding any shot used in the last 3 renders.
   - Cut and concat with FFmpeg, and pre-mix the separated game sound at about -18 dB under the voiceover.
   - Writes `assembled.mp4` plus `markers.json` (TEXT, ZOOM and SFX cues on the timeline).
6. **Finish with the existing pipeline**: call `core.pipeline.render_selected_clip(assembled.mp4, ClipResult(0, dur), words, ...)` with `layout_mode="blurred_background"` for now, `AUTO_GRAPHICS=0`, `linkedin_posts=False`. Captions, title card, loudnorm and the whoosh/tick SFX come for free.
7. **`app.py`**: new "GTA Narrate" tab. Generate or paste a script → teleprompter → upload voiceover → render → batch of 5.

## Phase 2: Quality upgrades

1. **`gameplay` layout** in `core/video_renderer.py` (add it to `LAYOUT_MODES`):
   - Crop path from OpenCV optical flow plus a saliency map, smoothed with the springs `core/camera_crop.py` already has. The crop follows the car or character instead of the frame's center.
   - Keep the minimap (bottom-left in GTA) out of the caption area, or blur it.
2. **Markers to graphics**: map `[TEXT]` onto the existing `headline_card` / `stat_pill` templates in `core/edl.py`, `[ZOOM]` onto `punch_in_filter`, and `[SFX]` onto `audio_polish.cue_track`.
3. **Variety guard**: refuse a render if more than 40% of its shots were used in the last 10 videos, and rotate caption styles and title-card palettes (`core/palettes.py`) between formats.

## Phase 3: Clip mode (licensed streamer campaigns)

1. **`core/clip_mode/twitch.py`**: TwitchDownloaderCLI or `tcd` pulls the VOD plus chat JSON. Only for campaigns you've joined.
2. **Chat-velocity peaks**: messages per 10 s against a rolling baseline, plus spikes in emote and "CLIP IT" counts, output as the same `Peak` objects `core/heatmap_scorer.spikes()` already uses for YouTube's replay heatmap. That makes `boost()` and `score_clips()` work on streams unchanged.
3. **Audio-energy peaks**: Nav already computes loudness (`camera_energy`, `video_renderer._measure_loudness`), so use it as a second peak source. A peak where chat and audio overlap counts double.
4. **Gaming prompt** for `hook_selector` / `agent_brain`: hype, rage, funny, clutch, reaction. Add a `CLIP_LENGTH=gaming` mode in `core/clip_length.py` (15-45 s).
5. **Ideas from AI-Youtube-Shorts-Generator**:
   - Classify the content type first, then use a ranking prompt tuned to that type.
   - Split VODs over 30 minutes into overlapping chunks so moments near boundaries aren't missed.
   - Collapse candidates that overlap by more than 50%, keeping the higher score.
6. **Layout**: the existing `webinar` layout (webcam on top, content below) is the facecam-over-gameplay reel.

## Phase 4: Publishing

1. Run Postiz with Docker, connect TikTok, YouTube, Instagram and X, and call its REST API from a new `core/publish.py`. Write per-platform captions and hashtags, and stagger the posting times.
   - Self-hosted Postiz needs your own developer apps on each platform, and approval (Meta, YouTube, TikTok) can take weeks. Start that now, or use Postiz Cloud at first.
2. Log every post (shot IDs, format, platform, time) to SQLite, so you can later rank formats by views.

---

## Install additions (`requirements.txt`)
```
scenedetect[opencv]>=0.7
auto-editor
stable-ts
audio-separator            # or: demucs
open_clip_torch
# optional
kokoro
chatterbox-tts
tcd
```
Keep `opencv-python-headless<5`, as `requirements.txt` already notes. Installing scenedetect's `[opencv]` extra can pull in a second OpenCV package, so check `pip list | grep opencv` afterwards and keep only one.

## Licensing quick rules
- MIT, BSD, Apache and Unlicense: fine to import and ship inside Nav.
- GPL and AGPL (lossless-cut, Postiz, Medusa Clip, Recordly, YOLO): run them as separate programs or services, or just study them. Don't paste their code into Nav, which you sell as a service.
