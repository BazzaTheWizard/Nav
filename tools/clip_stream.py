"""Round 61-62: Clip mode. A licensed streamer VOD -> short highlight clips.

  python tools/clip_stream.py "https://www.twitch.tv/videos/123456789" --campaign "Whop: <campaign name>"
  python tools/clip_stream.py "https://..." --chat chat.json --campaign "Vyro: <name>" --count 8
  python tools/clip_stream.py --video vod.mp4 --chat chat.json --campaign "..."   (a VOD on disk)

What it does, for a URL:
  1. fetches only the audio (a 2-hour VOD is ~100 MB this way, not gigabytes);
  2. gets the chat replay (Twitch: TwitchDownloaderCLI when installed; else pass --chat with the
     JSON from TwitchDownloader, or a CSV of "seconds,message"; YouTube: yt-dlp);
  3. transcribes the audio (cached in the output folder);
  4. turns chat spikes + loudness spikes into a heatmap (core.clip_mode.signals);
  5. the stream picker (core.clip_mode.picker) turns spikes into moments, asks the model what each
     one is and where its payoff lands, cuts 15-60 s around it, and judges each clip cold;
  6. downloads just the seconds each clip needs and renders it with Nav's normal renderer
     (captions, framing, SFX) in the "stream" layout: the facecam on top, the gameplay below
     (core.webinar, slides="stream"), captions under it.

Only clip streamers who run a clipping campaign: --campaign is required and saved with the
clips, so you always know which licence a clip was made under.

Output: storage/output/streams/<name>/clip_N.mp4, picks.json (the clips and the campaign),
moments.json (every moment considered, with scores) and heatmap.json.

Dry run (no model calls, no rendering): --plan prints the moments it would use.
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
# Stream highlights: short clips, creator captions, no LinkedIn copy or B2B data cards, no
# climax teaser (the clip is the climax), no mid-clip trimming (reactions have pauses).
for key, value in {"CLIP_LENGTH": "gaming", "LINKEDIN_POSTS": "0", "AUTO_GRAPHICS": "0", "VISUAL_OVERLAYS": "none",
                   "CAPTION_STYLE": "bold", "NAV_NO_CLIP_PAGES": "1", "ENABLE_CLIMAX_HOOK": "0",
                   "MIDCLIP_TRIM": "0"}.items():
    os.environ.setdefault(key, value)

from core import brief as B  # noqa: E402

OUT_ROOT = ROOT / "storage" / "output" / "streams"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "stream"


def transcript(audio: Path, out: Path) -> list:
    cache = out / "transcript.json"
    if cache.is_file():
        return B.load_words(cache)
    from config import load_settings
    from core.downloader import extract_audio_only
    from core.transcriber import transcribe_audio

    wav = out / "audio.wav"
    extract_audio_only(audio, wav)
    words = transcribe_audio(wav, load_settings())
    B.save_words(words, cache)
    wav.unlink(missing_ok=True)
    return words


def shifted(words: list, by: float) -> list:
    from core.transcriber import Word

    return [Word(text=w.text, start=round(max(0.0, w.start - by), 3), end=round(w.end - by, 3)) for w in words
            if w.end - by > 0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", default="", help="Twitch or YouTube VOD URL")
    ap.add_argument("--video", default="", help="a VOD already on disk (skips the downloads)")
    ap.add_argument("--chat", default="", help="chat replay file (skips the chat download)")
    ap.add_argument("--campaign", required=True, help='the clipping campaign that licenses this, e.g. "Whop: Name"')
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--name", default="")
    ap.add_argument("--streamer", default="", help="the streamer's name (for the model; found from the URL if empty)")
    ap.add_argument("--game", default="", help="the game (for the model; found from the URL if empty)")
    ap.add_argument("--layout", default="stream",
                    help="stream (default: facecam on top, gameplay below; falls back to blurred_background when "
                         "no facecam is found), or any Nav layout: blurred_background, smart_crop, webinar, auto")
    ap.add_argument("--no-audio-signal", action="store_true", help="rank by chat only")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--plan", action="store_true", help="find the moments and stop: no model, no rendering")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.url and not args.video:
        raise SystemExit("Give a VOD URL or --video.")

    from config import load_settings
    from core.clip_mode import chat as C
    from core.clip_mode import picker as P
    from core.clip_mode import signals as SG
    from core.clip_mode import vod as V
    from core.narrate.library import probe_duration
    from core.pipeline import render_selected_clip
    from core.webinar import plan_webinar

    name = args.name or slug(args.url.rsplit("/", 1)[-1] if args.url else Path(args.video).stem)
    out = OUT_ROOT / name
    out.mkdir(parents=True, exist_ok=True)
    meta = {"title": "", "uploader": "", "game": "", "duration": 0.0}
    if args.video:
        audio = Path(args.video)
    else:
        try:
            meta = V.info(args.url)
        except Exception as exc:  # noqa: BLE001 - metadata is a nicety
            logging.warning("Could not read the VOD's metadata: %s", exc)
        audio = V.audio_only(args.url, out)
    duration = meta["duration"] or probe_duration(audio)
    streamer = args.streamer or meta["uploader"]
    game = args.game or meta["game"]
    print(f"VOD: {meta['title'] or audio.name}  {duration / 60:.0f} min  {streamer} / {game or '?'}")

    chat_file = Path(args.chat) if args.chat else (C.download(args.url, out) if args.url else None)
    messages = C.load(chat_file) if chat_file else []
    print(f"Chat: {len(messages)} messages" if messages else "Chat: none (ranking by loudness and the words)")

    words = transcript(audio, out)
    heat = SG.stream_heatmap(None if args.no_audio_signal else audio, messages, duration, words)
    (out / "heatmap.json").write_text(json.dumps(heat))
    print(f"Spikes: {len([h for h in heat if h['value'] >= P.HOT])} hot buckets of {len(heat)}")

    report: dict = {}
    clips = P.pick_stream_clips(words, messages, heat, duration, args.count, streamer=streamer, game=game,
                                use_model=not args.plan, use_judge=not (args.no_judge or args.plan), report=report)
    P.save_report(report, out / "moments.json")
    for m in report.get("moments", []):
        judged = "" if m["judge"] is None else f"  judge {m['judge']}"
        print(f"  {m['start']:7.1f}-{m['end']:7.1f}s  {m['label']:<9} score {m['score']:>2}{judged}  {m['title']}")
    if args.plan:
        print(f"\n{len(clips)} clips planned (dry run). Remove --plan to render them.")
        return

    settings = load_settings()
    picks = []
    for n, clip in enumerate(clips, 1):
        if args.video:
            source, offset = Path(args.video), 0.0
        else:
            source, offset = V.section(args.url, clip.start_time, clip.end_time, out / f"section_{n}.mp4")
        local = clip.model_copy(update={"start_time": clip.start_time - offset, "end_time": clip.end_time - offset})
        layout, webinar = args.layout, None
        if args.layout == "stream":
            webinar = plan_webinar(source, local.start_time, local.end_time, "auto", slides="stream", divider="shadow")
            layout = "blurred_background" if webinar.found.startswith("no face") else "webinar"
            if layout != "webinar":
                webinar = None
                print(f"clip_{n}: no facecam found; using the blurred layout")
        made = render_selected_clip(source, local, shifted(words, offset), settings, out / f"clip_{n}.mp4",
                                    layout_mode=layout, webinar_layout=webinar, work_dir=out / "work",
                                    title_fade_in=False, climax_hook_on=False)
        picks.append({"file": str(made.path), "start": clip.start_time, "end": clip.end_time,
                      "title": clip.viral_title, "score": clip.virality_score,
                      "peak_retention_score": getattr(clip, "peak_retention_score", None)})
        print(f"clip_{n}: {clip.start_time:.0f}-{clip.end_time:.0f}s  {clip.viral_title}")
    (out / "picks.json").write_text(json.dumps({"campaign": args.campaign, "source": args.url or str(args.video),
                                                "streamer": streamer, "game": game, "clips": picks}, indent=2))
    print(f"\n{len(picks)} clips in {out}  (campaign: {args.campaign})")


if __name__ == "__main__":
    main()
