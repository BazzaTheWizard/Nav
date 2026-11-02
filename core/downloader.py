from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, TypeVar
from urllib.parse import urlparse

from config import Settings, load_settings

ProgressCallback = Callable[[float, str], None]
T = TypeVar("T")
logger = logging.getLogger(__name__)

# Source quality. YouTube's H.264 1080p stream is its lowest-quality 1080p (the
# downloads measured about 1.1-1.25 Mbps), and a 9:16 crop of it is only 608 px
# wide, enlarged 1.78x. So: the best VP9 stream up to 1440p (a 810 px wide crop,
# enlarged 1.33x), falling back to H.264. AV1 stays last (slow to decode on a
# MacBook; an earlier 4K AV1 pick was 587 MB for 14 minutes). Sorting never falls
# through to "best". SOURCE_MAX_HEIGHT in .env: 1080, 1440 (default) or 2160.
# "proto": regular downloads before YouTube's chunked HLS streams (616 "Premium"
# 1080p, 620 at 1440p). Round 12 picked those because they are MP4: one 50-minute
# video was 1.23 GB and took 7.5 minutes, in hundreds of small pieces.
VIDEO_FORMAT = "bv*+ba/b"
FFMPEG_TIMEOUT_SEC = 15 * 60  # audio extraction from a 90-min video takes about a minute
DEFAULT_SOURCE_MAX_HEIGHT = 1440
VIDEO_FORMAT_SORT = [f"res:{DEFAULT_SOURCE_MAX_HEIGHT}", "vcodec:vp9", "proto", "ext:mp4:m4a"]


def video_format_sort() -> list[str]:
    """VIDEO_FORMAT_SORT with SOURCE_MAX_HEIGHT from .env (720-2160) applied."""
    try:
        height = int(os.getenv("SOURCE_MAX_HEIGHT", "") or DEFAULT_SOURCE_MAX_HEIGHT)
    except ValueError:
        height = DEFAULT_SOURCE_MAX_HEIGHT
    height = min(2160, max(720, height))
    return [f"res:{height}", *VIDEO_FORMAT_SORT[1:]]

# YouTube refusing the worker for a while (bot check, rate limit): pause the worker
# instead of using up creators' attempts. A plain 403 is often just one video or an
# outdated yt-dlp, so it's reported separately (DownloadForbidden).
_YOUTUBE_BLOCKED = re.compile(r"not a bot|HTTP Error 429|Too Many Requests", re.IGNORECASE)
YOUTUBE_PAUSE_SEC = 30 * 60
UPDATE_HINT = "If it keeps happening, update yt-dlp: .venv/bin/pip install -U yt-dlp"


class DownloadForbidden(RuntimeError):
    """YouTube answered HTTP 403. Two in a row and the worker pauses (see the worker)."""


# --- Signed-in downloads (age-restricted videos, "confirm you're not a bot") -----------
# Every download first runs without cookies. Only when YouTube asks to sign in does it
# run once more with your YouTube session: the cookies.txt file in YTDLP_COOKIES_PATH,
# then the cookies of the browser in YTDLP_COOKIES_BROWSER (default chrome; "off" to
# turn it off). On a Mac, reading Chrome's or Brave's cookies asks once for your login
# keychain password ("Always Allow"); Safari needs Full Disk Access for the Terminal.
_SIGN_IN_NEEDED = re.compile(
    r"sign in to confirm|confirm your age|age[- ]restricted|inappropriate for some users|"
    r"not a bot|--cookies",
    re.IGNORECASE,
)
_AGE_GATE = re.compile(r"confirm your age|age[- ]restricted|inappropriate for some users", re.IGNORECASE)
COOKIE_BROWSERS = ("chrome", "brave", "safari", "firefox", "edge", "chromium", "opera", "vivaldi", "whale")
DEFAULT_COOKIE_BROWSER = "chrome"
SIGN_IN_HELP = (
    "Sign in to YouTube in Chrome (an account that is 18+), or set YTDLP_COOKIES_PATH in .env "
    "to a cookies.txt file. YTDLP_COOKIES_BROWSER picks another browser (brave, safari, firefox)."
)
# The background worker reads browser cookies only with YTDLP_COOKIES_IN_WORKER=1 in
# .env: a 24/7 downloader on your own account's cookies is the kind of use YouTube
# limits. A cookies.txt file (YTDLP_COOKIES_PATH) is used everywhere.
_browser_cookies_allowed = True


def allow_browser_cookies(allowed: bool) -> None:
    """Turn the browser-cookie fallback on or off for this process."""
    global _browser_cookies_allowed
    _browser_cookies_allowed = bool(allowed)


class SignInRequired(RuntimeError):
    """YouTube wants a signed-in session (an age-restricted video, or its bot check)
    and no cookies got past it. The message says what was tried and how to fix it."""

    def __init__(self, message: str, *, age_restricted: bool) -> None:
        super().__init__(message)
        self.age_restricted = age_restricted


def _browser_spec(value: str) -> tuple[str, ...] | None:
    """'chrome' or 'chrome:Profile 1' -> yt-dlp's cookiesfrombrowser tuple."""
    name, _, profile = value.strip().partition(":")
    name = name.strip().lower()
    if name not in COOKIE_BROWSERS:
        return None
    return (name, profile.strip()) if profile.strip() else (name,)


def cookie_sources() -> list[tuple[str, dict[str, object]]]:
    """(label, extra yt-dlp options) to try, in order, after YouTube asks to sign in."""
    load_settings()  # reads .env, in case nothing has yet
    sources: list[tuple[str, dict[str, object]]] = []
    path = os.getenv("YTDLP_COOKIES_PATH", "").strip()
    if path:
        cookie_file = Path(path).expanduser()
        if cookie_file.is_file():
            sources.append((f"the cookies file {cookie_file.name}", {"cookiefile": str(cookie_file)}))
        else:
            logger.warning("YTDLP_COOKIES_PATH is set, but %s doesn't exist.", cookie_file)
    browser = (os.getenv("YTDLP_COOKIES_BROWSER") or DEFAULT_COOKIE_BROWSER).strip()
    if _browser_cookies_allowed and browser.lower() not in {"off", "none", "no", "0", "false"}:
        spec = _browser_spec(browser)
        if spec is None:
            logger.warning("YTDLP_COOKIES_BROWSER=%s isn't one of %s.", browser, ", ".join(COOKIE_BROWSERS))
        else:
            sources.append((f"{spec[0].title()}'s YouTube sign-in", {"cookiesfrombrowser": spec}))
    return sources


def sign_in_summary() -> str:
    """One line for the app's System tab: which YouTube sign-in the fallback would use."""
    sources = [label for label, _options in cookie_sources()]
    path = os.getenv("YTDLP_COOKIES_PATH", "").strip()
    missing = f" (YTDLP_COOKIES_PATH {Path(path).expanduser().name} not found)" if path and not Path(path).expanduser().is_file() else ""
    worker = "on" if os.getenv("YTDLP_COOKIES_IN_WORKER", "").strip().lower() in {"1", "true", "yes", "on"} else "off"
    used = ", then ".join(sources) if sources else "none"
    return f"only when YouTube asks: {used}{missing}. Browser cookies in the worker: {worker}"


def _first_line(error: Exception) -> str:
    text = re.sub(r"^ERROR:\s*", "", str(error).strip())
    return text.splitlines()[0][:300] if text else type(error).__name__


def _with_sign_in(run: Callable[[dict[str, object]], T], options: dict[str, object], what: str) -> T:
    """``run(options)`` once; if YouTube asks to sign in, again with each cookie
    source in turn. Raises what _download_error maps the error to, or SignInRequired."""
    from yt_dlp import DownloadError

    try:
        return run(options)
    except DownloadError as exc:
        if not _SIGN_IN_NEEDED.search(str(exc)):
            raise _download_error(exc, what) from exc
        first = exc
    age_restricted = bool(_AGE_GATE.search(str(first)))
    reason = "the video is age-restricted" if age_restricted else "YouTube's bot check"
    tried: list[str] = []
    last: Exception = first
    for label, extra in cookie_sources():
        logger.info("YouTube asked to sign in (%s); trying again with %s.", reason, label)
        try:
            result = run({**options, **extra})
        except Exception as exc:  # DownloadError, or the cookies couldn't be read
            tried.append(f"{label}: {_first_line(exc)}")
            last = exc
            continue
        logger.info("Downloaded %s with %s.", what, label)
        return result
    detail = f" Tried: {'; '.join(tried)}." if tried else " No YouTube sign-in is set up."
    if not age_restricted:
        # Still the bot check: the worker pauses as before; the app shows how to sign in.
        from core.service_pause import ServicePause

        raise ServicePause(
            f"YouTube is blocking downloads for now (its \"not a bot\" check).{detail}",
            YOUTUBE_PAUSE_SEC, service="youtube", hint=SIGN_IN_HELP,
        ) from last
    raise SignInRequired(
        f"YouTube needs a signed-in account for {what} ({reason}): {_first_line(first).rstrip('.')}.{detail} {SIGN_IN_HELP}",
        age_restricted=age_restricted,
    ) from last


# What yt-dlp learned while downloading (title, heatmap, ...), so the worker doesn't
# ask YouTube a second time for the "Most replayed" heatmap. url -> info.
_DOWNLOAD_INFO: dict[str, dict[str, object]] = {}


def downloaded_video_info(url: str) -> dict[str, object] | None:
    """Title, channel, duration and heatmap from this URL's download (once), or None
    when the file came from the cache or the download didn't report them."""
    return _DOWNLOAD_INFO.pop(url, None)


def yt_dlp_version() -> str:
    try:
        from yt_dlp.version import __version__
    except ImportError:
        return "not installed"
    return str(__version__)


def _download_error(exc: Exception, what: str) -> Exception:
    """The exception to raise for a yt-dlp DownloadError."""
    text = str(exc)
    if _YOUTUBE_BLOCKED.search(text):
        from core.service_pause import ServicePause

        return ServicePause(
            "YouTube is blocking downloads for now (bot check or too many requests)",
            YOUTUBE_PAUSE_SEC, service="youtube", hint=UPDATE_HINT,
        )
    if "HTTP Error 403" in text:
        return DownloadForbidden(f"yt-dlp could not download {what}: {exc}")
    return RuntimeError(f"yt-dlp could not download {what}: {exc}")


def _audio_language(info: object) -> str | None:
    """The language of the audio track yt-dlp picked (a video can have dubbed tracks)."""
    if not isinstance(info, dict):
        return None
    for item in info.get("requested_formats") or []:
        if isinstance(item, dict) and item.get("acodec") not in (None, "none") and item.get("language"):
            return str(item["language"])
    return str(info["language"]) if info.get("language") else None


__all__ = [
    "fetch_video_info",
    "download_audio_only",
    "download_source_video",
    "extract_audio_only",
    "extract_audio_segment",
    "fetch_captions",
    "download_section",
]


def _yt_dlp_progress_hook(
    on_progress: ProgressCallback | None,
) -> Callable[[dict[str, object]], None]:
    def report_download_progress(data: dict[str, object]) -> None:
        if on_progress is None:
            return
        if data.get("status") == "finished":
            on_progress(1.0, "100% complete")
            return
        percent_text = str(data.get("_percent_str", ""))
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", percent_text)
        if match is None:
            return
        percent = min(max(float(match.group(1)), 0.0), 100.0)
        progress = percent / 100.0
        on_progress(
            progress,
            f"{int(progress * 100)}% complete",
        )

    return report_download_progress


def _download_url(
    url: str,
    settings: Settings,
    on_progress: ProgressCallback | None = None,
) -> Path:
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise RuntimeError(
            "URL downloads require yt-dlp. Install dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    # Reuse a finished download of the same YouTube video instead of fetching it
    # again (one 38-minute video was stored 5 times).
    youtube_id = _youtube_id(url)
    cached = settings.raw_dir / f"yt_{youtube_id}.mp4" if youtube_id else None
    if cached is not None and cached.is_file() and cached.stat().st_size > 0:
        if on_progress is not None:
            on_progress(1.0, "100% complete")
        return cached

    download_id = uuid.uuid4().hex
    output_template = str(settings.raw_dir / f"{download_id}.%(ext)s")
    homebrew_ffmpeg_dir = "/opt/homebrew/opt/ffmpeg-full/bin"
    ffmpeg_path = (
        homebrew_ffmpeg_dir
        if os.path.exists(f"{homebrew_ffmpeg_dir}/ffmpeg")
        else shutil.which("ffmpeg")
    )
    options = {
        "format": VIDEO_FORMAT,
        "format_sort": video_format_sort(),
        "outtmpl": output_template,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
        "quiet": True,
        "noprogress": True,  # the progress hook still runs; this only drops the bars in the log
        "no_warnings": True,
        "ffmpeg_location": ffmpeg_path,
        "progress_hooks": [_yt_dlp_progress_hook(on_progress)],
    }
    started = time.monotonic()

    def run(run_options: dict[str, object]) -> object:
        with YoutubeDL(run_options) as downloader:
            return downloader.extract_info(url, download=True)

    info = _with_sign_in(run, options, "the input URL")

    if isinstance(info, dict):
        _DOWNLOAD_INFO[url] = {
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "duration": info.get("duration"),
            "heatmap": info.get("heatmap") if isinstance(info.get("heatmap"), list) else None,
        }
    downloaded_files = list(settings.raw_dir.glob(f"{download_id}*.mp4"))
    if len(downloaded_files) != 1 or not downloaded_files[0].is_file():
        raise RuntimeError(
            "yt-dlp completed, but did not produce exactly one MP4 file in "
            f"{settings.raw_dir}."
        )
    logger.info(
        "%s",
        download_summary(
            (info or {}).get("format_id") if isinstance(info, dict) else None,
            downloaded_files[0].stat().st_size,
            time.monotonic() - started,
            _audio_language(info),
        ),
    )
    if cached is not None:
        downloaded_files[0].replace(cached)
        return cached
    return downloaded_files[0]


def download_summary(format_id: object, size_bytes: int, seconds: float, audio_language: str | None = None) -> str:
    """One log line per download, so slow or oversized downloads show up in the log."""
    megabytes = size_bytes / 1_000_000
    return (
        f"Downloaded format {format_id or '?'}: {megabytes:,.0f} MB in {seconds:,.0f} s "
        f"({megabytes / max(seconds, 0.001):.1f} MB/s)"
        + (f", audio language {audio_language}" if audio_language else "")
    )


def _youtube_id(url: str) -> str | None:
    from core.transcriber import youtube_video_id

    video_id = youtube_video_id(url)
    return video_id if video_id and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id) else None


def download_source_video(
    url: str,
    output_path: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> str:
    """Download one full source video for rendering and later trim adjustments."""
    if not url.startswith(("http://", "https://")) or not urlparse(url).netloc:
        raise ValueError("A valid http:// or https:// video URL is required.")
    downloaded_path = _download_url(url, load_settings(), on_progress=on_progress)
    if output_path is None:
        return str(downloaded_path.resolve())

    destination = Path(output_path).expanduser().resolve()
    if destination != downloaded_path.resolve():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(downloaded_path), str(destination))
    if not destination.is_file():
        raise RuntimeError(
            f"The downloaded source video is missing from the expected path: {destination}"
        )
    return str(destination)


def fetch_video_info(url: str) -> dict[str, object]:
    """Title, channel and YouTube "Most replayed" heatmap, without downloading.

    The heatmap is a list of {"start_time", "end_time", "value"} dicts, or None
    when YouTube has none (new or low-view videos).
    """
    from yt_dlp import YoutubeDL

    def run(options: dict[str, object]) -> object:
        with YoutubeDL(options) as downloader:
            return downloader.extract_info(url, download=False)

    info = _with_sign_in(
        run, {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}, "the video's details"
    )
    if not isinstance(info, dict):
        raise RuntimeError(f"Could not read video metadata for {url}")
    heatmap = info.get("heatmap")
    return {
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
        "heatmap": heatmap if isinstance(heatmap, list) else None,
    }


# --- ✂️ Quote grabber (Make clips) -----------------------------------------------------
CAPTION_LANGUAGES = ("en", "en-US", "en-GB", "en-orig")


def _caption_track(info: dict[str, object]) -> dict[str, object] | None:
    """The video's English captions as json3: the uploader's own first, else YouTube's
    automatic ones. None when there are none."""
    for field in ("subtitles", "automatic_captions"):
        tracks = info.get(field) if isinstance(info.get(field), dict) else {}
        languages = [lang for lang in CAPTION_LANGUAGES if lang in tracks] + sorted(
            lang for lang in tracks if str(lang).startswith("en") and lang not in CAPTION_LANGUAGES
        )
        for lang in languages:
            for track in tracks.get(lang) or []:
                if isinstance(track, dict) and track.get("ext") == "json3" and track.get("url"):
                    return track
    return None


def parse_json3_captions(text: str) -> list[tuple[float, float, str]]:
    """YouTube json3 captions -> [(start s, end s, text)], one per caption event."""
    import json

    lines: list[tuple[float, float, str]] = []
    for event in (json.loads(text) or {}).get("events") or []:
        words = "".join(str(seg.get("utf8", "")) for seg in event.get("segs") or [])
        words = " ".join(words.split())
        if not words:
            continue
        start = float(event.get("tStartMs", 0)) / 1000.0
        end = start + float(event.get("dDurationMs", 0)) / 1000.0
        lines.append((start, max(end, start + 0.5), words))
    return lines


def fetch_captions(url: str) -> tuple[dict[str, object], list[tuple[float, float, str]]]:
    """(title/channel/duration, caption lines) without downloading the video. The lines
    are empty when the video has no English captions."""
    from yt_dlp import YoutubeDL

    def run(options: dict[str, object]) -> tuple[object, str | None]:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
            track = _caption_track(info) if isinstance(info, dict) else None
            if track is None:
                return info, None
            return info, downloader.urlopen(str(track["url"])).read().decode("utf-8")

    info, text = _with_sign_in(
        run, {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}, "the video's captions"
    )
    if not isinstance(info, dict):
        raise RuntimeError(f"Could not read the video's details for {url}")
    details = {"title": info.get("title"), "channel": info.get("channel") or info.get("uploader"), "duration": info.get("duration")}
    return details, parse_json3_captions(text) if text else []


def download_section(url: str, start: float, end: float, output_path: str | Path) -> Path:
    """Download only ``start``-``end`` seconds of a video (cut at exactly those times),
    as MP4: a 30-second quote out of a 2-hour podcast, without the 2 hours."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import download_range_func

    if not 0 <= start < end:
        raise ValueError("The section must satisfy 0 <= start < end.")
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    download_id = uuid.uuid4().hex
    template = str(destination.parent / f"section_{download_id}.%(ext)s")
    options = {
        "format": VIDEO_FORMAT,
        "format_sort": video_format_sort(),
        "outtmpl": template,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "ffmpeg_location": shutil.which("ffmpeg"),
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
    }

    def run(run_options: dict[str, object]) -> object:
        with YoutubeDL(run_options) as downloader:
            return downloader.extract_info(url, download=True)

    _with_sign_in(run, options, "the quote")
    found = sorted(destination.parent.glob(f"section_{download_id}*"))
    if not found:
        raise RuntimeError("yt-dlp finished but saved no file for the quote.")
    for extra in found[1:]:
        extra.unlink(missing_ok=True)
    found[0].replace(destination)
    return destination


def _require_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RuntimeError(
            "FFmpeg was not found. Install it with `brew install ffmpeg` and "
            "ensure `ffmpeg` is on your PATH."
        )
    return executable


def download_audio_only(
    url: str,
    output_dir: str | Path = "storage/temp",
    on_progress: ProgressCallback | None = None,
) -> str:
    """Download audio only and return its absolute M4A or WAV path.

    An explicit ``.wav`` output path remains supported for existing callers.
    """
    if not url.startswith(("http://", "https://")) or not urlparse(url).netloc:
        raise ValueError("A valid http:// or https:// video URL is required.")

    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise RuntimeError(
            "URL downloads require yt-dlp. Install dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    requested_path = Path(output_dir).expanduser()
    wav_destination = (
        requested_path.resolve()
        if requested_path.suffix.lower() == ".wav"
        else None
    )
    destination_dir = (
        wav_destination.parent
        if wav_destination is not None
        else requested_path.resolve()
    )
    destination_dir.mkdir(parents=True, exist_ok=True)
    download_id = uuid.uuid4().hex
    output_template = str(destination_dir / f"{download_id}.%(ext)s")
    options = {
        "format": "ba[ext=m4a]/ba",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
            }
        ],
        "progress_hooks": [_yt_dlp_progress_hook(on_progress)],
    }
    temporary_files: list[Path] = []
    final_audio: Path | None = None
    def run(run_options: dict[str, object]) -> None:
        with YoutubeDL(run_options) as downloader:
            downloader.download([url])

    try:
        _with_sign_in(run, options, "audio from the URL")
        temporary_files = list(destination_dir.glob(f"{download_id}.*"))
        source_audio = next(
            (
                path
                for path in temporary_files
                if path.is_file() and path.suffix.lower() == ".m4a"
            ),
            None,
        )
        if source_audio is None:
            raise RuntimeError(
                "yt-dlp completed, but did not produce an M4A audio file in "
                f"{destination_dir}."
            )
        if wav_destination is None:
            final_audio = destination_dir / f"audio_{download_id}.m4a"
            source_audio.replace(final_audio)
        else:
            ffmpeg = _require_ffmpeg()
            try:
                subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(source_audio),
                        "-vn",
                        "-acodec",
                        "pcm_s16le",
                        "-ar",
                        "16000",
                        "-ac",
                        "1",
                        str(wav_destination),
                    ],
                    check=True,
                    timeout=FFMPEG_TIMEOUT_SEC,  # a stuck FFmpeg must not stall the worker
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or "").strip()
                raise RuntimeError(f"FFmpeg WAV conversion failed: {detail}") from exc
            final_audio = wav_destination
        if final_audio is None or not final_audio.is_file():
            raise RuntimeError(
                "Audio download completed, but the expected output file is missing."
            )
    finally:
        for temporary_file in set(temporary_files) | set(destination_dir.glob(f"{download_id}.*")):
            if temporary_file.is_file() or temporary_file.is_symlink():
                temporary_file.unlink(missing_ok=True)
        if wav_destination is not None and (
            final_audio is None or not final_audio.is_file()
        ):
            wav_destination.unlink(missing_ok=True)
    return str(final_audio)


def extract_audio_only(video_path: str | Path, output_path: str | Path) -> Path:
    """Extract a local video's audio to 16 kHz mono PCM WAV."""
    source = Path(video_path).expanduser().resolve(strict=True)
    destination = Path(output_path).expanduser().resolve()
    if destination.suffix.lower() != ".wav":
        raise ValueError("Audio output path must have a .wav extension.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _require_ffmpeg()
    try:
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(destination),
            ],
            check=True,
            timeout=FFMPEG_TIMEOUT_SEC,  # a stuck FFmpeg must not stall the worker
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg audio extraction failed: {detail}") from exc
    return destination


def extract_audio_segment(
    video_path: str | Path,
    start_time: float,
    end_time: float,
    output_path: str | Path,
) -> Path:
    """Extract one video's audio segment as 16 kHz mono PCM WAV."""
    if (
        not math.isfinite(start_time)
        or not math.isfinite(end_time)
        or start_time < 0
        or end_time <= start_time
    ):
        raise ValueError("Audio segment times must satisfy 0 <= start_time < end_time.")

    source = Path(video_path).expanduser().resolve(strict=True)
    destination = Path(output_path).expanduser().resolve()
    if destination.suffix.lower() != ".wav":
        raise ValueError("Clip audio output path must have a .wav extension.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _require_ffmpeg()
    try:
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(start_time),
                "-i",
                str(source),
                "-t",
                f"{end_time - start_time:.6f}",
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(destination),
            ],
            check=True,
            timeout=FFMPEG_TIMEOUT_SEC,  # a stuck FFmpeg must not stall the worker
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg clip audio extraction failed: {detail}") from exc
    return destination
