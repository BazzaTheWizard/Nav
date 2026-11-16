"""Round 61-62: Clip mode. A licensed streamer VOD -> short highlight clips.

  python tools/clip_stream.py "https://www.twitch.tv/videos/123456789" --campaign "Whop: <campaign name>"
  python tools/clip_stream.py "https://..." --chat chat.json --campaign "Vyro: <name>" --count 8
  python tools/clip_stream.py --video vod.mp4 --chat chat.json --campaign "..."   (a VOD on disk)

What it does, for a URL:
  1. fetches only the audio (about 75 MB an hour, not gigabytes), again if the file comes out short;
  2. gets the chat replay (Twitch: TwitchDownloaderCLI when installed; else pass --chat with the
     JSON from TwitchDownloader, or a CSV of "seconds,message"; YouTube: yt-dlp);
  3. measures the loudness of the whole stream (seconds, even for 7 hours);
  4. turns chat spikes + loudness spikes into a heatmap (core.clip_mode.signals) and chooses
     windows around the raised parts: at most 30 windows or 45 minutes of audio, dead air left out
     (core.clip_mode.windows). With no chat, loudness alone chooses wider windows;
  5. transcribes ONLY those windows (Round 65: a 7-hour stream is 25,200 s of audio, the windows
     2,700 s at most), with the words' times on the stream's clock;
  6. the stream picker (core.clip_mode.picker) turns spikes into moments, asks the model what each
     one is and where its payoff lands, cuts 15-60 s around it, and judges each clip cold;
  7. downloads just the seconds each clip needs and renders it with Nav's normal renderer
     (captions, framing, SFX) in the "stream" layout: the facecam on top, the gameplay below
     (core.webinar, slides="stream"), captions under it.

--full-transcript transcribes the whole stream instead, as Rounds 61-63 did (a moment that only
the words give away, with no loudness or chat spike, is only found this way).

A stopped run carries on where it was: run the same command again. Every step's result is kept in
the output folder (the audio, the chat, loudness.npy, windows.json, each window's words under
windows/, clips.json, each finished clip) and progress.json says which steps are done.

Only clip streamers who run a clipping campaign: --campaign is required and saved with the
clips, so you always know which licence a clip was made under.

Output: storage/output/streams/<name>/clip_N.mp4, picks.json (the clips and the campaign),
moments.json (every moment considered, with scores), heatmap.json and windows.json.

Dry run (no model calls, no rendering): --plan prints the moments it would use.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
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


def _clock(seconds: float) -> str:
    seconds = int(round(max(0.0, seconds)))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _took(seconds: float) -> str:
    return f"{seconds:.0f} s" if seconds < 90 else f"{seconds / 60:.1f} min"


def _key(*parts: object) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def audio_key(audio: Path) -> str:
    return f"{audio.name}:{audio.stat().st_size}"


def stream_loudness(audio: Path, out: Path, progress, say=print):
    """signals.loudness() for the whole stream, kept in loudness.npy for the next run."""
    import numpy as np

    from core.clip_mode import signals as SG

    cache, key = out / "loudness.npy", audio_key(audio)
    if progress.done("loudness", key) and cache.is_file():
        say("  kept from the last run")
        return np.load(cache)
    progress.start("loudness", key)
    lufs = SG.loudness(audio)
    with open(out / "loudness.tmp", "wb") as f:
        np.save(f, lufs)
    os.replace(out / "loudness.tmp", cache)
    say(f"  {len(lufs)} five-second buckets in {_took(progress.finish('loudness', key, buckets=int(len(lufs))))}")
    return lufs


def analyse(audio: Path, messages: list, duration: float, out: Path, *, full_transcript: bool = False,
            use_audio: bool = True, transcribe=None, progress=None, say=print, stage=None) -> tuple[list, list, list | None]:
    """The stream's words and heatmap: (words, heatmap, windows).

    The default (Round 65): loudness + chat first, windows around what they raise, and only the
    windows transcribed (windows is the list of core.clip_mode.windows.Window). With
    ``full_transcript``, or when a whole transcript.json is already in ``out``, or when there is
    neither chat nor loudness to choose windows from: the whole stream is transcribed and
    windows is None, exactly as before.

    ``transcribe(wav) -> words``: the transcriber (default: core.transcriber.transcribe_audio)."""
    import numpy as np

    from core.clip_mode import signals as SG
    from core.clip_mode import windows as W

    progress = progress or W.Progress(out / "progress.json")
    stage = stage or (lambda title: say(title))
    n = max(1, int(np.ceil(duration / SG.BUCKET)))

    def fit(values) -> "np.ndarray":
        return np.zeros(n) if values is None or not len(values) else np.pad(values, (0, max(0, n - len(values))))[:n]

    stage(f"Loudness of the whole stream ({_clock(duration)}; about "
          f"{_took(max(1.0, duration / 3600 * W.LOUDNESS_SECONDS_PER_HOUR))})" if use_audio else "Loudness: off (--no-audio-signal)")
    lufs = stream_loudness(audio, out, progress, say) if use_audio else None
    audio_v = SG.audio_values(lufs) if lufs is not None else None
    chat_v = SG.chat_values(messages, duration) if messages else None

    whole = out / "transcript.json"
    if not full_transcript and whole.is_file():
        say("A whole transcript from an earlier run is in the folder: using it.")
        full_transcript = True
    if not full_transcript and chat_v is None and audio_v is None:
        say("No chat and no loudness to choose windows from: transcribing the whole stream.")
        full_transcript = True
    if full_transcript:
        stage(f"Transcribing the whole stream ({_clock(duration)} of audio)" if not whole.is_file()
              else "Transcript: kept from the last run")
        words = transcript(audio, out)
        speech = SG.speech_values(words, duration) if words else None
        return words, SG.heatmap(SG.fuse(chat_v, audio_v, speech)), None

    stage("Choosing what to transcribe")
    report: dict = {}
    wins = W.choose(fit(SG.fuse(chat_v, audio_v)), duration, lufs, messages, report=report)
    (out / "windows.json").write_text(json.dumps({"duration": duration, **report, "windows": [w.to_dict() for w in wins]},
                                                 indent=1))
    progress.update("windows", done=True, key=audio_key(audio), **report)
    if not wins:
        say("  Nothing in the chat or the loudness stands out, so there is nothing to transcribe. "
            "--full-transcript reads the whole stream instead.")
        return [], SG.heatmap(np.zeros(n)), wins
    seconds = report["audio_seconds"]
    say(f"  {len(wins)} windows, {seconds / 60:.1f} min of audio: {100 * report['share_of_stream']:.0f}% of the stream"
        + (f" ({report['dead_air_seconds']:.0f} s of dead air left out)" if report["dead_air_seconds"] else "")
        + (f"; {report['runs_left_out']} weaker spots left out by the {W.MAX_AUDIO / 60:.0f}-minute / "
           f"{W.MAX_WINDOWS}-window cap" if report["runs_left_out"] else "")
        + ("" if messages else f"; no chat, so loudness alone and wider windows (±{report['pad']:.0f} s)"))

    guess = "the first window shows the pace"
    if transcribe is None:
        from config import load_settings
        from core.transcriber import transcribe_audio

        settings = load_settings()
        transcribe = lambda wav: transcribe_audio(wav, settings)  # noqa: E731
        if os.getenv("GROQ_API_KEY", "").strip():
            guess = f"about {_took(seconds / 60 * W.GROQ_SECONDS_PER_MINUTE)} with Groq"
    pieces = [p for w in wins for p in w.pieces]
    key = _key(audio_key(audio), pieces)
    stage(f"Transcribing {len(pieces)} windows ({_clock(seconds)} of audio instead of {_clock(duration)}; {guess})")
    progress.start("transcript", key, pieces=len(pieces), audio_seconds=seconds, pieces_done=0)
    words = W.transcribe_windows(audio, wins, out / "windows", transcribe, say=say,
                                 on_piece=lambda done, total: progress.update("transcript", pieces_done=done))
    B.save_words(words, out / "transcript_windows.json")
    say(f"  {len(words)} words in {_took(progress.finish('transcript', key, words=len(words)))}")

    speech = SG.speech_values(words, duration, covered=W.covered(wins, n)) if words else None
    fused = fit(SG.fuse(chat_v, audio_v, speech))
    fused[~W.allowed(wins, n)] = 0.0  # a moment is only seeded where the transcript reaches around it
    return words, SG.heatmap(fused), wins


def chat_replay(url: str, out: Path, progress) -> Path | None:
    """The VOD's chat replay, downloaded once. A download that fails (a sub-only or deleted VOD,
    the downloader crashing) is not the end of the run: the clips are then ranked by loudness and
    words, and the next run tries the chat again."""
    from core.clip_mode import chat as C

    kept = [p for p in sorted(out.glob("chat*.json")) if p.stat().st_size > 0]
    if progress.done("chat", url):
        return kept[0] if kept else None
    progress.start("chat", url)
    try:
        found = C.download(url, out)
    except Exception as exc:  # noqa: BLE001 - chat is one signal of three
        logging.warning("The chat replay could not be downloaded (%s). Continuing without chat.",
                        str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__)
        return None
    progress.finish("chat", url, file=found.name if found else "")
    return found


def _dump_clip(clip) -> dict:
    return {**clip.model_dump(), "peak_retention_score": getattr(clip, "peak_retention_score", None)}


def _load_clip(data: dict):
    from core.hook_selector import ClipResult

    data = dict(data)
    peak = data.pop("peak_retention_score", None)
    return ClipResult(**data).model_copy(update={"peak_retention_score": peak})


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
    ap.add_argument("--full-transcript", action="store_true",
                    help="transcribe the whole stream (slow for long VODs) instead of only the windows around "
                         "chat and loudness spikes")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--plan", action="store_true", help="find the moments and stop: no model, no rendering")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.url and not args.video:
        raise SystemExit("Give a VOD URL or --video.")

    from config import load_settings
    from core.clip_mode import chat as C
    from core.clip_mode import picker as P
    from core.clip_mode import vod as V
    from core.clip_mode import windows as W
    from core.narrate.library import probe_duration
    from core.pipeline import render_selected_clip
    from core.transcriber import TranscriptionLimitReached
    from core.webinar import plan_webinar

    name = args.name or slug(args.url.rsplit("/", 1)[-1] if args.url else Path(args.video).stem)
    out = OUT_ROOT / name
    out.mkdir(parents=True, exist_ok=True)
    progress = W.Progress(out / "progress.json")
    began = time.time()
    stages = [0]
    whole = args.full_transcript or (out / "transcript.json").is_file()  # one step fewer: no windows to choose
    total_stages = (4 if whole else 5) + (0 if args.plan else 1) + (0 if args.video else 1)

    def stage(title: str) -> None:
        stages[0] += 1
        print(f"\n[{stages[0]}/{total_stages}] {title}", flush=True)

    meta = {"title": "", "uploader": "", "game": "", "duration": 0.0}
    if args.video:
        audio = Path(args.video)
    else:
        try:
            meta = V.info(args.url)
        except Exception as exc:  # noqa: BLE001 - metadata is a nicety
            logging.warning("Could not read the VOD's metadata: %s", exc)
        size = f"about {meta['duration'] / 3600 * W.AUDIO_MB_PER_HOUR:.0f} MB" if meta["duration"] else "size unknown"
        stage(f"Audio of the VOD ({_clock(meta['duration'])}, {size})")
        progress.start("audio", args.url)
        audio = V.audio_only(args.url, out, meta["duration"])
        print(f"  {audio.name}, {audio.stat().st_size / 1e6:.0f} MB, complete, in "
              f"{_took(progress.finish('audio', args.url, file=audio.name))}")
    duration = meta["duration"] or probe_duration(audio)
    streamer = args.streamer or meta["uploader"]
    game = args.game or meta["game"]
    print(f"VOD: {meta['title'] or audio.name}  {duration / 60:.0f} min  {streamer} / {game or '?'}")

    stage("Chat replay")
    chat_file = Path(args.chat) if args.chat else chat_replay(args.url, out, progress) if args.url else None
    messages = C.load(chat_file) if chat_file else []
    print(f"  {len(messages)} messages" if messages else "  none (ranking by loudness and the words)")

    try:
        words, heat, wins = analyse(audio, messages, duration, out, full_transcript=args.full_transcript,
                                    use_audio=not args.no_audio_signal, progress=progress, stage=stage)
    except TranscriptionLimitReached as exc:
        done = progress.info("transcript")
        raise SystemExit(f"\n{exc}\n{done.get('pieces_done', 0)} of {done.get('pieces', '?')} windows are transcribed and "
                         f"saved. Run the same command again in about {round(exc.retry_after / 60)} min: it carries on "
                         "from there.")
    (out / "heatmap.json").write_text(json.dumps(heat))
    print(f"Spikes: {len([h for h in heat if h['value'] >= P.HOT])} hot buckets of {len(heat)}")

    stage("Picking the moments" + (" (dry run: no model)" if args.plan else ""))
    pick_key = _key(heat, len(words), args.count, args.plan, args.no_judge, streamer, game)
    if progress.done("pick", pick_key) and (out / "clips.json").is_file() and (out / "moments.json").is_file():
        print("  kept from the last run")
        clips = [_load_clip(c) for c in json.loads((out / "clips.json").read_text())]
        report = json.loads((out / "moments.json").read_text())
    else:
        progress.start("pick", pick_key)
        report = {}
        clips = P.pick_stream_clips(words, messages, heat, duration, args.count, streamer=streamer, game=game,
                                    use_model=not args.plan, use_judge=not (args.no_judge or args.plan), report=report)
        P.save_report(report, out / "moments.json")
        (out / "clips.json").write_text(json.dumps([_dump_clip(c) for c in clips], indent=1))
        progress.finish("pick", pick_key, clips=len(clips))
    for m in report.get("moments", []):
        judged = "" if m["judge"] is None else f"  judge {m['judge']}"
        print(f"  {m['start']:7.1f}-{m['end']:7.1f}s  {m['label']:<9} score {m['score']:>2}{judged}  {m['title']}")
    if args.plan:
        print(f"\n{len(clips)} clips planned (dry run) in {_took(time.time() - began)}. Remove --plan to render them.")
        return

    stage(f"Rendering {len(clips)} clips")
    settings = load_settings()
    picks = []
    render_began, rendered = time.time(), 0
    for n, clip in enumerate(clips, 1):
        target = out / f"clip_{n}.mp4"
        clip_key = _key(round(clip.start_time, 3), round(clip.end_time, 3), args.layout, args.url or args.video)
        made_path = target
        if progress.done(f"clip_{n}", clip_key) and V.complete(target):
            print(f"clip_{n}: kept from the last run")
        else:
            progress.start(f"clip_{n}", clip_key)
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
            made = render_selected_clip(source, local, shifted(words, offset), settings, target,
                                        layout_mode=layout, webinar_layout=webinar, work_dir=out / "work",
                                        title_fade_in=False, climax_hook_on=False)
            made_path = Path(made.path)
            progress.finish(f"clip_{n}", clip_key, file=str(made_path))
            rendered += 1
            pace = (time.time() - render_began) / rendered
            print(f"clip_{n}: {clip.start_time:.0f}-{clip.end_time:.0f}s  {clip.viral_title}"
                  + (f"  ·  {len(clips) - n} to go, about {_took(pace * (len(clips) - n))}" if n < len(clips) else ""))
        picks.append({"file": str(made_path), "start": clip.start_time, "end": clip.end_time,
                      "title": clip.viral_title, "score": clip.virality_score,
                      "peak_retention_score": getattr(clip, "peak_retention_score", None)})
    (out / "picks.json").write_text(json.dumps({"campaign": args.campaign, "source": args.url or str(args.video),
                                                "streamer": streamer, "game": game, "clips": picks}, indent=2))
    print(f"\n{len(picks)} clips in {out}  (campaign: {args.campaign})  ·  {_took(time.time() - began)} in all")


if __name__ == "__main__":
    main()
