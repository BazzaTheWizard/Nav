"""Round 59: Narrate mode. Your script and your voiceover over gameplay footage you bought.

The flow (tools/narrate.py runs it):

  ingest    footage -> shots in a tagged library   (core.narrate.library)
  script    a topic -> a tagged script             (core.narrate.script)
  align     voiceover + script -> word timings     (core.narrate.align)
  assemble  timings + library -> one 16:9 video    (core.narrate.assemble)
  finish    core.pipeline.render_selected_clip: the same captions, title card, SFX and
            loudness as every other Nav clip (the voiceover is the "speaker").

Nothing here changes the podcast pipeline: every module is new and only tools/narrate.py
calls it. The heavy libraries (PySceneDetect, stable-ts, audio-separator, auto-editor) are
optional; each step falls back to plain FFmpeg or faster-whisper when one is missing.
"""
