"""Round 61: a stream's chat replay, as (seconds into the VOD, message) pairs.

Reads:
- TwitchDownloaderCLI chat JSON (``TwitchDownloaderCLI chatdownload --id <vod> -o chat.json``,
  https://github.com/lay295/TwitchDownloader): comments[].content_offset_seconds and
  comments[].message.body.
- yt-dlp's YouTube live chat (``--write-subs --sub-langs live_chat``): one JSON object per
  line, replayChatItemAction.videoOffsetTimeMsec plus the message runs.
- A plain CSV or TSV: seconds, message (for anything else you can export).

download() fetches it for a Twitch or YouTube VOD URL when the tool is installed.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)
_TWITCH_VOD = re.compile(r"twitch\.tv/(?:videos/|\w+/v/)(\d+)")


@dataclass(frozen=True)
class Message:
    t: float  # seconds into the VOD
    text: str


def _twitch_downloader(data: dict) -> list[Message]:
    out: list[Message] = []
    for c in data.get("comments") or []:
        try:
            t = float(c["content_offset_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        msg = c.get("message") or {}
        body = msg.get("body")
        if body is None:
            body = "".join(f.get("text", "") for f in msg.get("fragments") or [])
        out.append(Message(t, str(body)))
    return out


def _youtube_runs(item: dict) -> str:
    runs = (item.get("message") or {}).get("runs") or []
    parts = []
    for r in runs:
        if "text" in r:
            parts.append(r["text"])
        elif "emoji" in r:
            label = (r["emoji"].get("shortcuts") or [""])[0]
            parts.append(label or ":emoji:")
    return "".join(parts)


def _youtube_live_chat(lines: list[str]) -> list[Message]:
    out: list[Message] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        replay = obj.get("replayChatItemAction") or {}
        try:
            t = float(replay.get("videoOffsetTimeMsec", obj.get("videoOffsetTimeMsec"))) / 1000.0
        except (TypeError, ValueError):
            continue
        for action in replay.get("actions") or []:
            item = (action.get("addChatItemAction") or {}).get("item") or {}
            renderer = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
            if renderer:
                out.append(Message(t, _youtube_runs(renderer)))
    return out


def _csv(path: Path) -> list[Message]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=",\t;")
    except csv.Error:
        return []
    out: list[Message] = []
    for row in csv.reader(text.splitlines(), dialect):
        if len(row) < 2:
            continue
        try:
            out.append(Message(float(row[0]), row[1]))
        except ValueError:
            continue  # a header row
    return out


def load(path: str | Path) -> list[Message]:
    """The chat in time order. Raises ValueError for a file in none of the known formats."""
    path = Path(path)
    raw = path.read_text(encoding="utf-8", errors="replace")
    messages: list[Message] = []
    stripped = raw.lstrip()
    if stripped.startswith("{") and '"comments"' in raw[:5000]:
        messages = _twitch_downloader(json.loads(raw))
    elif stripped.startswith("{"):
        messages = _youtube_live_chat(raw.splitlines())
    else:
        messages = _csv(path)
    if not messages:
        raise ValueError(f"No chat messages found in {path.name}: not TwitchDownloader JSON, yt-dlp live_chat, "
                         "or a seconds,message CSV.")
    return sorted(messages, key=lambda m: m.t)


def twitch_downloader() -> str | None:
    """TwitchDownloaderCLI: on PATH, or (Round 66) the one unzipped into the project folder."""
    found = shutil.which("TwitchDownloaderCLI") or shutil.which("TwitchDownloaderCLI.exe")
    if found:
        return found
    local = Path(__file__).resolve().parents[2] / "TwitchDownloaderCLI"
    return str(local) if local.is_file() and os.access(local, os.X_OK) else None


def download(url: str, out_dir: str | Path) -> Path | None:
    """The chat replay for a Twitch or YouTube VOD, or None (with a log line) when it can't be fetched."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if (m := _TWITCH_VOD.search(url)):
        exe = twitch_downloader()
        if not exe:
            logger.warning("Twitch chat needs TwitchDownloaderCLI on PATH or in the project folder "
                           "(https://github.com/lay295/TwitchDownloader/releases). Continuing without chat.")
            return None
        target = out_dir / f"chat_{m.group(1)}.json"
        subprocess.run([exe, "chatdownload", "--id", m.group(1), "-o", str(target), "--collision", "Overwrite"],
                       check=True)
        return target
    if "youtube.com" in url or "youtu.be" in url:
        try:
            import yt_dlp
        except ImportError:
            return None
        opts = {"skip_download": True, "writesubtitles": True, "subtitleslangs": ["live_chat"],
                "outtmpl": str(out_dir / "chat.%(ext)s"), "quiet": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        found = sorted(out_dir.glob("chat*.live_chat.json"))
        if not found:
            logger.warning("This YouTube video has no chat replay. Continuing without chat.")
        return found[0] if found else None
    logger.warning("No chat downloader for %s. Pass --chat <file> instead.", url)
    return None
