"""Round 61: Clip mode. Highlights from licensed streamer VODs (Whop / Vyro clipping campaigns).

  chat.py     the stream's chat replay: download it and read it (Twitch or YouTube)
  signals.py  chat spikes + loudness spikes -> a "heatmap" in yt-dlp's format, so the existing
              core.heatmap_scorer ranks clips whose hook lands on a spike higher, exactly as it
              does with YouTube's "Most replayed" data
  picker.py   spikes -> moments -> clip boundaries -> a judge (Round 62)
  vod.py      the VOD's audio and each clip's video section, retried and checked for completeness
  windows.py  Round 65, long VODs: the windows around the spikes, which are all that is transcribed,
              and the progress file that lets a stopped run carry on
  tools/clip_stream.py runs it: VOD audio -> chat -> loudness -> windows -> their words -> heatmap ->
  picks -> renders.

Only clip streamers who run a clipping campaign (it is their licence to you).
"""
