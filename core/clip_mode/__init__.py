"""Round 61: Clip mode. Highlights from licensed streamer VODs (Whop / Vyro clipping campaigns).

  chat.py     the stream's chat replay: download it and read it (Twitch or YouTube)
  signals.py  chat spikes + loudness spikes -> a "heatmap" in yt-dlp's format, so the existing
              core.heatmap_scorer ranks clips whose hook lands on a spike higher, exactly as it
              does with YouTube's "Most replayed" data
  tools/clip_stream.py runs it: VOD -> chat -> transcript -> heatmap -> picks -> renders.

Only clip streamers who run a clipping campaign (it is their licence to you).
"""
