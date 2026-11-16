"""Round 62: getting just enough of a 2-hour VOD.

A stream is long and most of it is never used, so Clip mode fetches it in two parts:
- audio_only(): the audio track (about 100 MB for 2 hours instead of 2-4 GB of video). The
  transcript and the loudness signal need nothing more.
- section(): the video for one clip, a few seconds around it, via yt-dlp's download ranges
  (Twitch and YouTube VODs are served as HLS, so fetching a range is quick). Each clip is
  then rendered from its own small file, with the clip's times shifted to that file.

Both go through yt-dlp, as core.downloader does, with the same format preferences.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PAD = 3.0  # seconds fetched before and after a clip, so the renderer has room to breathe


def _ydl(opts: dict):
    import yt_dlp

    base = {"quiet": True, "no_warnings": True, "noprogress": True, "retries": 5, "fragment_retries": 10}
    return yt_dlp.YoutubeDL({**base, **opts})


def audio_only(url: str, out_dir: Path) -> Path:
    """The VOD's audio track as out_dir/audio.<ext>, fetched once."""
    out_dir.mkdir(parents=True, exist_ok=True)
    found = sorted(p for p in out_dir.glob("audio.*") if p.suffix.lower() in {".m4a", ".mp4", ".aac", ".mp3", ".webm", ".opus"})
    if found:
        return found[0]
    with _ydl({"format": "Audio_Only/bestaudio/best", "outtmpl": str(out_dir / "audio.%(ext)s")}) as ydl:
        ydl.download([url])
    found = sorted(p for p in out_dir.glob("audio.*") if not p.suffix.endswith(".part"))
    if not found:
        raise RuntimeError(f"yt-dlp made no audio file for {url}")
    return found[0]


def section(url: str, start: float, end: float, out: Path, pad: float = PAD) -> tuple[Path, float]:
    """The video from ``start - pad`` to ``end + pad``. Returns (file, the file's offset in the VOD)."""
    offset = max(0.0, start - pad)
    if out.is_file():
        return out, offset
    import yt_dlp

    out.parent.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "outtmpl": str(out),
        "download_ranges": yt_dlp.utils.download_range_func(None, [(offset, end + pad)]),
        "force_keyframes_at_cuts": True,
        "merge_output_format": "mp4",
    }
    with _ydl(opts) as ydl:
        ydl.download([url])
    if not out.is_file():
        raise RuntimeError(f"yt-dlp made no file for {start:.0f}-{end:.0f} s of {url}")
    return out, offset


def info(url: str) -> dict:
    """Title, uploader, duration and (for Twitch) the game, without downloading."""
    with _ydl({"skip_download": True}) as ydl:
        data = ydl.extract_info(url, download=False) or {}
    title = str(data.get("title") or "")
    game = ""
    for key in ("game", "category", "categories"):
        value = data.get(key)
        if isinstance(value, list) and value:
            game = str(value[0])
            break
        if isinstance(value, str) and value:
            game = value
            break
    return {"title": title, "uploader": str(data.get("uploader") or data.get("channel") or ""),
            "duration": float(data.get("duration") or 0.0), "game": game}
