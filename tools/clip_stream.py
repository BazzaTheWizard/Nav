"""Round 61: Clip mode. A licensed streamer VOD -> short highlight clips.

  python tools/clip_stream.py "https://www.twitch.tv/videos/123456789" --campaign "Whop: <campaign name>"
  python tools/clip_stream.py --video vod.mp4 --chat chat.json --campaign "Vyro: <name>" --count 8

What it does:
  1. downloads the VOD (yt-dlp, as Nav already does) unless --video is given;
  2. gets the chat replay (TwitchDownloaderCLI for Twitch, yt-dlp for YouTube) unless --chat is given;
  3. transcribes it (cached in the output folder, like tools/brief_clips.py);
  4. turns chat spikes + loudness spikes into a heatmap (core.clip_mode.signals);
  5. lets Nav's normal picker choose the moments, with that heatmap boosting hooks on a spike;
  6. renders each pick with Nav's normal renderer (captions, framing, SFX), 15-60 s long.

Only clip streamers who run a clipping campaign: --campaign is required and saved with the
clips, so you always know which licence a clip was made under.

Output: storage/output/streams/<name>/clip_N.mp4, picks.json and heatmap.json.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Stream highlights: short clips, creator captions, no LinkedIn copy or B2B data cards.
for key, value in {"CLIP_LENGTH": "gaming", "LINKEDIN_POSTS": "0", "AUTO_GRAPHICS": "0", "VISUAL_OVERLAYS": "none",
                   "CAPTION_STYLE": "bold", "NAV_NO_CLIP_PAGES": "1"}.items():
    os.environ.setdefault(key, value)

from core import brief as B  # noqa: E402

OUT_ROOT = ROOT / "storage" / "output" / "streams"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "stream"


def transcript(video: Path, out: Path) -> list:
    cache = out / "transcript.json"
    if cache.is_file():
        return B.load_words(cache)
    from config import load_settings
    from core.downloader import extract_audio_only
    from core.transcriber import transcribe_audio

    wav = out / "audio.wav"
    extract_audio_only(video, wav)
    words = transcribe_audio(wav, load_settings())
    B.save_words(words, cache)
    wav.unlink(missing_ok=True)
    return words


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", default="", help="Twitch or YouTube VOD URL")
    ap.add_argument("--video", default="", help="a VOD already on disk (skips the download)")
    ap.add_argument("--chat", default="", help="chat replay file (skips the chat download)")
    ap.add_argument("--campaign", required=True, help='the clipping campaign that licenses this, e.g. "Whop: Name"')
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--name", default="")
    ap.add_argument("--streamer", default="", help="the streamer's name (for titles)")
    ap.add_argument("--layout", default="auto", help="auto, webinar (facecam over content), smart_crop, ...")
    ap.add_argument("--no-audio-signal", action="store_true", help="rank by chat only")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.url and not args.video:
        raise SystemExit("Give a VOD URL or --video.")

    from config import load_settings
    from core.clip_mode import chat as C
    from core.clip_mode import signals as SG
    from core.narrate.library import probe_duration
    from core.pipeline import pick_clips_with_agent, render_selected_clip

    name = args.name or slug(args.url.rsplit("/", 1)[-1] if args.url else Path(args.video).stem)
    out = OUT_ROOT / name
    out.mkdir(parents=True, exist_ok=True)
    if args.video:
        video = Path(args.video)
    else:
        from core.downloader import download_source_video

        video = out / "source.mp4"
        if not video.is_file():
            video = Path(download_source_video(args.url, str(video)))
    duration = probe_duration(video)

    chat_file = Path(args.chat) if args.chat else (C.download(args.url, out) if args.url else None)
    messages = C.load(chat_file) if chat_file else []
    print(f"Chat: {len(messages)} messages" if messages else "Chat: none (ranking by loudness and the transcript)")

    words = transcript(video, out)
    heat = SG.stream_heatmap(None if args.no_audio_signal else video, messages, duration)
    (out / "heatmap.json").write_text(json.dumps(heat))
    hot = [h for h in heat if h["value"] >= 0.65]
    print(f"Spikes: {len(hot)} hot buckets of {len(heat)}")

    settings = load_settings()
    clips, _email, _subject = pick_clips_with_agent(words, duration, args.count, args.streamer or "the streamer",
                                                    name, heatmap=heat)
    picks = []
    for n, clip in enumerate(clips, 1):
        made = render_selected_clip(video, clip, words, settings, out / f"clip_{n}.mp4", layout_mode=args.layout,
                                    work_dir=out / "work")
        picks.append({"file": str(made.path), "start": clip.start_time, "end": clip.end_time,
                      "title": clip.viral_title, "score": clip.virality_score,
                      "peak_retention_score": getattr(clip, "peak_retention_score", None)})
        print(f"clip_{n}: {clip.start_time:.0f}-{clip.end_time:.0f}s  {clip.viral_title}")
    (out / "picks.json").write_text(json.dumps({"campaign": args.campaign, "source": args.url or str(video),
                                                "clips": picks}, indent=2))
    print(f"\n{len(picks)} clips in {out}  (campaign: {args.campaign})")


if __name__ == "__main__":
    main()
