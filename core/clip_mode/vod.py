"""Round 62: getting just enough of a 2-hour VOD.

A stream is long and most of it is never used, so Clip mode fetches it in two parts:
- audio_only(): the audio track (about 75 MB an hour instead of 1-2 GB of video). The
  transcript and the loudness signal need nothing more.
- section(): the video for one clip, a few seconds around it, via yt-dlp's download ranges
  (Twitch and YouTube VODs are served as HLS, so fetching a range is quick). Each clip is
  then rendered from its own small file, with the clip's times shifted to that file.

Both go through yt-dlp, as core.downloader does, with the same format preferences.

Round 65 (7-hour VODs): a long download is a few thousand HLS fragments, and yt-dlp's default
is to skip a fragment it cannot get, which leaves a file that plays but is short, with every
later second in the wrong place. So fragments are never skipped, a failed download is tried
again with growing waits (yt-dlp carries on from the fragments it has), and a file is only
used once its length matches the VOD's (complete()).
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

PAD = 3.0  # seconds fetched before and after a clip, so the renderer has room to breathe
RETRY_WAITS = (5.0, 20.0, 60.0)  # seconds before the 2nd, 3rd and 4th try
SHORT_SECONDS = 15.0  # a file this much shorter than the VOD (or 1 %, if that is more) is incomplete
SHORT_SHARE = 0.01
AUDIO_SUFFIXES = {".m4a", ".mp4", ".aac", ".mp3", ".webm", ".opus", ".ogg", ".wav", ".flac"}


def _ydl(opts: dict):
    import yt_dlp

    base = {"quiet": True, "no_warnings": True, "noprogress": True, "retries": 10, "fragment_retries": 10,
            "skip_unavailable_fragments": False, "continuedl": True,
            "retry_sleep_functions": {"http": lambda n: min(30.0, 2.0 ** n), "fragment": lambda n: min(30.0, 2.0 ** n)}}
    return yt_dlp.YoutubeDL({**base, **opts})


def probe_seconds(path: Path) -> float:
    """The file's length, or 0.0 when FFmpeg cannot read it (half-written, not media)."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                              "default=nw=1:nk=1", str(path)], capture_output=True, text=True, timeout=120)
        return max(0.0, float(out.stdout.strip()))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def complete(path: Path, expected: float = 0.0) -> bool:
    """Is the file whole? It must be readable, and when the VOD's length is known, as long as
    it (15 s or 1 % short is allowed: containers and Twitch's own figure differ a little)."""
    actual = probe_seconds(path)
    if actual <= 0.0:
        return False
    return expected <= 0.0 or actual >= expected - max(SHORT_SECONDS, SHORT_SHARE * expected)


def _audio_files(out_dir: Path) -> list[Path]:
    return sorted(p for p in out_dir.glob("audio.*") if p.suffix.lower() in AUDIO_SUFFIXES)


def _fetch_audio(url: str, out_dir: Path) -> None:
    with _ydl({"format": "Audio_Only/bestaudio/best", "outtmpl": str(out_dir / "audio.%(ext)s"),
               "concurrent_fragment_downloads": 4}) as ydl:
        ydl.download([url])


def _with_retries(fetch: Callable[[], Path | None], what: str, sleep: Callable[[float], None]) -> Path:
    """``fetch()`` until it returns a file: again after 5, 20 and 60 s, then an error."""
    problem = "nothing was downloaded"
    for attempt, wait in enumerate((0.0, *RETRY_WAITS), 1):
        if wait:
            logger.warning("%s: %s. Trying again in %.0f s (try %d of %d).", what, problem, wait, attempt,
                           len(RETRY_WAITS) + 1)
            sleep(wait)
        try:
            found = fetch()
        except Exception as exc:  # noqa: BLE001 - network errors, yt-dlp's DownloadError
            problem = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__
            continue
        if found is not None:
            return found
        problem = "the file came out incomplete"
    raise RuntimeError(f"{what} failed after {len(RETRY_WAITS) + 1} tries: {problem}")


def audio_only(url: str, out_dir: Path, expected: float = 0.0, sleep: Callable[[float], None] = time.sleep) -> Path:
    """The VOD's audio track as out_dir/audio.<ext>, fetched once. ``expected``: the VOD's
    length in seconds when known; a file shorter than that is thrown away and fetched again."""
    out_dir.mkdir(parents=True, exist_ok=True)
    found = _audio_files(out_dir)
    if found and complete(found[0], expected):
        return found[0]
    for stale in found:
        logger.warning("%s is incomplete (%.0f s of %.0f s): fetching it again.", stale.name, probe_seconds(stale), expected)
        stale.unlink()

    def fetch() -> Path | None:
        _fetch_audio(url, out_dir)
        made = _audio_files(out_dir)
        if made and complete(made[0], expected):
            return made[0]
        for short in made:  # whole but short: a fragment went missing. Start it again.
            short.unlink()
        return None

    return _with_retries(fetch, f"Downloading the audio of {url}", sleep)


def _fetch_section(url: str, start: float, end: float, out: Path) -> None:
    import yt_dlp

    opts = {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "outtmpl": str(out),
        "download_ranges": yt_dlp.utils.download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
        "merge_output_format": "mp4",
    }
    with _ydl(opts) as ydl:
        ydl.download([url])


def section(url: str, start: float, end: float, out: Path, pad: float = PAD,
            sleep: Callable[[float], None] = time.sleep) -> tuple[Path, float]:
    """The video from ``start - pad`` to ``end + pad``. Returns (file, the file's offset in the VOD)."""
    offset = max(0.0, start - pad)
    need = end - offset - 1.0  # the clip itself must be in it (the pad after it may be cut by the VOD's end)
    if out.is_file():
        if complete(out, need + SHORT_SECONDS):
            return out, offset
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)

    def fetch() -> Path | None:
        _fetch_section(url, offset, end + pad, out)
        if out.is_file() and complete(out, need + SHORT_SECONDS):
            return out
        out.unlink(missing_ok=True)
        return None

    return _with_retries(fetch, f"Downloading {start:.0f}-{end:.0f} s of {url}", sleep), offset


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
