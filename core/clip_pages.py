"""Personal clip pages: https://clips.navstudiolab.com/<page>, one per pitch.

The files go to a Cloudflare R2 bucket; the Worker in cloudflare/clip-pages-worker.js
shows them. R2 bucket layout:
    <page>/page.json            who it's for, the video, each clip's start/end
    <page>/clip_01.mp4          the clip as rendered (what they download and post)
    <page>/poster_01.jpg        first frame shown before they press play
    <page>/captions_01.srt      the captions file (Round 46)
    <page>/cover_01.jpg         Round 49: the full-size cover image (the frame at 0.5 s, title on it)
    _events/<page>/<ms>_<type>_<clip>_<id>    one empty file per open/play/download

Settings in .env (see claude_review/round16/README.md):
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, CLIP_PAGES_URL
Check them with:  .venv/bin/python -m core.clip_pages --check
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import ssl
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

PAGE_NAME = re.compile(r"[a-z0-9-]{6,80}")  # same rule as the Worker
CLIP_FILE = re.compile(r"clip_(0[1-9])\.mp4")
EVENT_TYPES = ("open", "play", "half", "end", "dl", "copy", "offer", "srt", "cover", "social", "call")
# Round 45: copied a post, opened the offer; Round 49: downloaded the captions file / the cover image;
# Round 50: copied the Shorts & Reels caption, pressed "Request a call"
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes (l/1, o/0)


@dataclass(frozen=True)
class PageSettings:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    base_url: str


def page_settings() -> PageSettings | None:
    """None until all five settings are in .env (then the email links to the page).
    NAV_NO_CLIP_PAGES=1 (set by tests/run_all.py) keeps tests away from your real bucket."""
    if os.getenv("NAV_NO_CLIP_PAGES"):
        return None
    try:
        from dotenv import load_dotenv

        from config import PROJECT_ROOT

        load_dotenv(PROJECT_ROOT / ".env")
    except Exception:
        pass
    values = {
        name: os.getenv(name, "").strip()
        for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "CLIP_PAGES_URL")
    }
    if not all(values.values()):
        return None
    return PageSettings(
        values["R2_ACCOUNT_ID"], values["R2_ACCESS_KEY_ID"], values["R2_SECRET_ACCESS_KEY"],
        values["R2_BUCKET"], values["CLIP_PAGES_URL"].rstrip("/"),
    )


def configured() -> bool:
    return page_settings() is not None


def _client(settings: PageSettings) -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise RuntimeError("Clip pages need boto3: run  .venv/bin/pip install boto3") from exc

    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        region_name="auto",
        # R2 doesn't take every newer default checksum; send them only when required.
        config=Config(request_checksum_calculation="when_required", response_checksum_validation="when_required",
                      retries={"max_attempts": 5, "mode": "standard"}),
    )


def _require() -> tuple[PageSettings, Any]:
    settings = page_settings()
    if settings is None:
        raise RuntimeError("Clip pages aren't set up (R2 settings missing in .env).")
    return settings, _client(settings)


def make_page_name(channel_title: str) -> str:
    """'Leighton Grey - Grey Matter 🇨🇦' -> 'leighton-grey-grey-matter-k7m2qx'.
    The random end makes the address impossible to guess."""
    ascii_title = unicodedata.normalize("NFKD", channel_title).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    words = words[:40].rstrip("-") or "clips"
    return f"{words}-{''.join(secrets.choice(_ALPHABET) for _ in range(6))}"


def page_url(name: str, settings: PageSettings | None = None) -> str:
    settings = settings or page_settings()
    if settings is None:
        raise RuntimeError("Clip pages aren't set up.")
    return f"{settings.base_url}/{name}"


def _check_name(name: str) -> str:
    if not PAGE_NAME.fullmatch(name or ""):
        raise ValueError(f"Not a clip page name: {name!r}")
    return name


def make_poster(clip: Path, output: Path, at: float | None = None) -> float:
    """The poster still, 540 px wide: the frame at ``at`` seconds, or (None) the best
    moment of the first seconds, eyes open (core.poster). Returns the time used."""
    from core.poster import best_poster_time, poster_jpeg

    seconds = best_poster_time(clip) if at is None else float(at)
    try:
        output.write_bytes(poster_jpeg(clip, seconds))
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise RuntimeError(f"Could not make a poster image: {exc}") from exc
    return seconds


COVER_AT = 0.5  # Round 49: the hook frame, with the topic header already up (it shows from frame 0)
COVER_WIDTH = 1080


def covers_on() -> bool:
    """Round 49: cover images on (CLIP_COVERS=0 turns them off; tests/run_all.py does for the
    suites written before them, which check the exact files a page uploads)."""
    return os.getenv("CLIP_COVERS", "1").strip().lower() not in {"0", "false", "no", "off"}


def make_cover(clip: Path, output: Path, at: float = COVER_AT) -> Path:
    """The cover image clients upload as the post's thumbnail (LinkedIn, Instagram, Meta):
    the clip's frame at ``at`` seconds, full size (1080x1920), as a high-quality JPEG."""
    from core.poster import poster_jpeg

    try:
        output.write_bytes(poster_jpeg(clip, at, width=COVER_WIDTH, quality=2))
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise RuntimeError(f"Could not make the cover image: {exc}") from exc
    return output


def cover_path(clip: Path) -> Path:
    """Where a clip's cover image is kept on this Mac: <clip>_cover.jpg next to it."""
    clip = Path(clip)
    return clip.with_name(f"{clip.stem}_cover.jpg")


def _upload_cover(client: Any, settings: PageSettings, name: str, index: int, clip: Path) -> bool:
    """Make the clip's cover (next to the clip, so the app can offer it too) and upload it as
    cover_NN.jpg. False when it can't be made (the page then has no cover button)."""
    try:
        cover = make_cover(clip, cover_path(clip))
    except RuntimeError:
        logger.warning("No cover image for %s.", clip, exc_info=True)
        return False
    client.upload_file(str(cover), settings.bucket, f"{name}/cover_{index:02d}.jpg",
                       ExtraArgs={"ContentType": "image/jpeg", "CacheControl": "no-cache"})
    return True


def _upload_poster(client: Any, settings: PageSettings, name: str, index: int, clip: Path, at: float | None) -> float:
    with tempfile.TemporaryDirectory() as temporary:
        poster = Path(temporary) / "poster.jpg"
        seconds = make_poster(clip, poster, at)
        client.upload_file(
            str(poster), settings.bucket, f"{name}/poster_{index:02d}.jpg",
            ExtraArgs={"ContentType": "image/jpeg", "CacheControl": "no-cache"},
        )
    return seconds


def _upload_clip(
    client: Any, settings: PageSettings, name: str, index: int, clip: Path, poster_at: float | None = None
) -> dict[str, bool]:
    """Upload a clip and its extras. Returns the page.json flags for them ({"srt", "cover"})."""
    client.upload_file(
        str(clip), settings.bucket, f"{name}/clip_{index:02d}.mp4",
        ExtraArgs={"ContentType": "video/mp4", "CacheControl": "no-cache"},
    )
    _upload_poster(client, settings, name, index, clip, poster_at)
    flags: dict[str, bool] = {}
    srt = Path(clip).with_suffix(".srt")  # Round 46: the captions file (core.srt_export)
    if srt.is_file():
        client.upload_file(str(srt), settings.bucket, f"{name}/captions_{index:02d}.srt",
                           ExtraArgs={"ContentType": "application/x-subrip; charset=utf-8", "CacheControl": "no-cache"})
        flags["srt"] = True
    if covers_on() and _upload_cover(client, settings, name, index, Path(clip)):  # Round 49
        flags["cover"] = True
    return flags


def _social_for(clip: dict[str, Any]) -> dict[str, Any] | None:
    """Round 50: the clip's Shorts & Reels caption (core.social_post), if written."""
    if isinstance(clip.get("social"), dict):
        return clip["social"]
    try:
        from core.social_post import read_for_clip

        return read_for_clip(Path(clip["path"])) if clip.get("path") else None
    except (KeyError, TypeError):
        return None


def _clip_entry(clip: dict[str, Any], flags: dict[str, bool]) -> dict[str, Any]:
    """A clip's page.json entry: its times, its LinkedIn post and Shorts & Reels caption, and
    which extras it has."""
    return {"start": round(float(clip["start"]), 2), "end": round(float(clip["end"]), 2),
            **({"post": post} if (post := _post_for(clip)) else {}),
            **({"social": social} if (social := _social_for(clip)) else {}), **(flags or {})}


def replace_poster(name: str, index: int, clip: Path, at: float | None) -> float:
    """Swap one clip's poster on its page (the frame at ``at`` s; None: picked again).
    Returns the time used."""
    settings, client = _require()
    _check_name(name)
    return _upload_poster(client, settings, name, int(index), Path(clip), at)


def _post_for(clip: dict[str, Any]) -> str:
    """Round 45: the clip's ready-to-post LinkedIn caption (core.linkedin_post), if written."""
    if clip.get("post"):
        return str(clip["post"])[:3000]
    try:
        from core.linkedin_post import post_path

        path = post_path(Path(clip["path"]))
        return path.read_text(encoding="utf-8")[:3000] if path.is_file() else ""
    except (OSError, KeyError, TypeError):
        return ""


def publish_page(
    channel_title: str,
    video_title: str,
    clips: list[dict[str, Any]],
    kind: str = "video",
    slug: str = "",
    theme: str = "",
) -> tuple[str, str]:
    """Upload a pitch's clips and make its page. ``clips``: [{"path", "start", "end"}]
    in order. Returns (page name, page address). The page appears only once every
    file is up (page.json goes last). ``slug``: words for the address instead of the
    channel name (round 26, Make clips); a random end is always added, so two pages
    never share an address."""
    if not 1 <= len(clips) <= 9:
        raise ValueError("A clip page needs 1 to 9 clips.")
    settings, client = _require()
    name = make_page_name(slug.strip() or channel_title)
    try:
        flags = [_upload_clip(client, settings, name, index, Path(clip["path"]))
                 for index, clip in enumerate(clips, start=1)]
        info = {
            "v": 1,
            "channel_title": channel_title,
            "video_title": video_title,
            "kind": "episode" if kind == "episode" else "video",
            "clips": [_clip_entry(c, f) for c, f in zip(clips, flags)],
            **({"theme": " ".join(theme.split())[:80]} if theme.strip() else {}),  # Round 46: their theme this month
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        client.put_object(
            Bucket=settings.bucket, Key=f"{name}/page.json", Body=json.dumps(info).encode(),
            ContentType="application/json", CacheControl="no-cache",
        )
    except Exception:
        _delete_prefixes(client, settings, [f"{name}/"])
        raise
    return name, page_url(name, settings)


# --- Pages published from 🎬 Make clips (round 26) -----------------------------------
# The worker's pages are kept on their pitch records; these are kept here, so the app
# can list them with what each visitor did (the same page_activity() as the worker's).
MANUAL_PAGES_PATH = Path(__file__).resolve().parents[1] / "storage" / "manual_pages.json"


def manual_pages() -> list[dict[str, Any]]:
    """Pages you published from Make clips, newest first."""
    try:
        pages = json.loads(MANUAL_PAGES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    pages = [page for page in pages if isinstance(page, dict) and page.get("page_name")]
    return sorted(pages, key=lambda page: str(page.get("created_at")), reverse=True)


def record_manual_page(name: str, url: str, channel_title: str, video_title: str, clip_count: int) -> dict[str, Any]:
    """Save a page published from Make clips (storage/manual_pages.json)."""
    entry = {
        "page_name": name,
        "page_url": url,
        "channel_title": channel_title,
        "video_title": video_title,
        "clips": clip_count,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    pages = [page for page in manual_pages() if page.get("page_name") != name]
    MANUAL_PAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = MANUAL_PAGES_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps([entry, *pages], indent=2), encoding="utf-8")
    temporary.replace(MANUAL_PAGES_PATH)
    return entry


def update_manual_page(name: str, **fields: Any) -> None:
    """Change a saved Make-clips page's record (e.g. its clip count after a removal)."""
    pages = manual_pages()
    for page in pages:
        if page.get("page_name") == name:
            page.update(fields)
    MANUAL_PAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = MANUAL_PAGES_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(pages, indent=2), encoding="utf-8")
    temporary.replace(MANUAL_PAGES_PATH)


def replace_clip(
    name: str,
    file_name: str,
    clip: Path,
    start: float | None = None,
    end: float | None = None,
    poster_at: float | None = None,
) -> None:
    """After a trim: same address, new clip (and poster, and times on the page).
    ``poster_at``: the poster frame you picked (None: picked automatically)."""
    match = CLIP_FILE.fullmatch(file_name)
    if match is None:
        raise ValueError(f"Unexpected clip file name: {file_name!r}")
    index = int(match.group(1))
    settings, client = _require()
    _check_name(name)
    flags = _upload_clip(client, settings, name, index, Path(clip), poster_at)
    info = json.loads(client.get_object(Bucket=settings.bucket, Key=f"{name}/page.json")["Body"].read())
    if 1 <= index <= len(info.get("clips", [])):
        # Round 49: the entry keeps its post and gets the new clip's extras. It used to be
        # replaced by the times alone, so a trimmed clip lost its LinkedIn post, its captions
        # file link and (now) its cover on the client's page.
        entry = dict(info["clips"][index - 1])
        old_times = {"start": entry.get("start", 0.0), "end": entry.get("end", 0.0)}
        entry.update(_clip_entry({"path": str(clip), **old_times,
                                  **({"start": start, "end": end} if start is not None and end is not None else {})},
                                 flags))
        for extra in ("srt", "cover"):
            if not (flags or {}).get(extra):
                entry.pop(extra, None)
        info["clips"][index - 1] = entry
        client.put_object(
            Bucket=settings.bucket, Key=f"{name}/page.json", Body=json.dumps(info).encode(),
            ContentType="application/json", CacheControl="no-cache",
        )


def remove_clip(name: str, index: int, expected_count: int) -> int:
    """Take clip ``index`` (from 1) off a page: the later clips move up one number
    (clip_03 -> clip_02, posters too, copied inside the bucket), page.json lists one
    clip fewer, and the last number's files are deleted. Returns the new count.

    Safe to run again after a failure (``expected_count``: how many clips the pitch
    has now). The later clips are first copied aside, and page.json notes the move
    before any original is overwritten, so a retry always copies from the right files
    and never removes a second clip."""
    settings, client = _require()
    _check_name(name)
    if not 1 <= index <= expected_count:
        raise ValueError(f"No clip {index} on a page with {expected_count} clips.")
    bucket = settings.bucket

    def read_page() -> dict[str, Any]:
        return json.loads(client.get_object(Bucket=bucket, Key=f"{name}/page.json")["Body"].read())

    def write_page(info: dict[str, Any]) -> None:
        client.put_object(
            Bucket=bucket, Key=f"{name}/page.json", Body=json.dumps(info).encode(),
            ContentType="application/json", CacheControl="no-cache",
        )

    info = read_page()
    clips = list(info.get("clips") or [])

    def files_for(entries: list[Any]) -> list[tuple[int, str, str]]:
        """The later clips' files: the clip and poster always; the captions file and cover
        when the page lists them (Round 49: they used to stay behind under the old number,
        so the clip that moved up showed the removed clip's captions)."""
        out = []
        for number in range(index + 1, expected_count + 1):
            entry = entries[number - 1] if number - 1 < len(entries) and isinstance(entries[number - 1], dict) else {}
            out += [(number, "clip", "mp4"), (number, "poster", "jpg")]
            if entry.get("srt"):
                out.append((number, "captions", "srt"))
            if entry.get("cover"):
                out.append((number, "cover", "jpg"))
        return out

    moving = info.get("moving")
    if not moving and len(clips) == expected_count:
        # 1. Copy the later clips aside (the originals are untouched until step 2).
        files = files_for(clips)
        moving = {"index": index, "count": expected_count, "run": secrets.token_hex(4),
                  "files": [list(f) for f in files]}
        for number, kind, extension in files:
            client.copy_object(
                Bucket=bucket, Key=f"{name}/_moving/{moving['run']}/{kind}_{number:02d}.{extension}",
                CopySource={"Bucket": bucket, "Key": f"{name}/{kind}_{number:02d}.{extension}"},
            )
        info["moving"] = moving
        write_page(info)
    if moving:
        if (moving.get("index"), moving.get("count")) != (index, expected_count):
            raise RuntimeError("Another clip removal on this page didn't finish. Refresh the app and try again.")
        files = ([(int(n), str(k), str(e)) for n, k, e in moving["files"]] if moving.get("files")
                 else [(n, k, e) for n in range(index + 1, expected_count + 1) for k, e in (("clip", "mp4"), ("poster", "jpg"))])
        # 2. Put them under their new numbers (repeatable: always from the copies).
        for number, kind, extension in files:
            client.copy_object(
                Bucket=bucket, Key=f"{name}/{kind}_{number - 1:02d}.{extension}",
                CopySource={"Bucket": bucket, "Key": f"{name}/_moving/{moving['run']}/{kind}_{number:02d}.{extension}"},
            )
        info.pop("moving", None)
        info["clips"] = clips[: index - 1] + clips[index:]
        write_page(info)
    elif len(clips) != expected_count - 1:  # not an earlier attempt that got this far either
        raise RuntimeError(f"The page has {len(clips)} clips, not {expected_count}. Refresh the app and try again.")
    # 3. The last number's files (now copies) and the set-aside copies go.
    client.delete_objects(
        Bucket=bucket,
        Delete={"Objects": [{"Key": f"{name}/{kind}_{expected_count:02d}.{extension}"}
                            for kind, extension in (("clip", "mp4"), ("poster", "jpg"), ("captions", "srt"), ("cover", "jpg"))],
                "Quiet": True},
    )
    _delete_prefixes(client, settings, [f"{name}/_moving/"])
    return expected_count - 1


def _delete_prefixes(client: Any, settings: PageSettings, prefixes: Iterable[str]) -> int:
    """Delete in rounds (list what's left, delete it) rather than paging through a
    listing while deleting from it, which can skip files."""
    removed = 0
    for prefix in prefixes:
        for _ in range(1000):  # 1,000 files per round; a page has a few dozen
            listing = client.list_objects_v2(Bucket=settings.bucket, Prefix=prefix)
            keys = [{"Key": item["Key"]} for item in listing.get("Contents", [])]
            if not keys:
                break
            client.delete_objects(Bucket=settings.bucket, Delete={"Objects": keys, "Quiet": True})
            removed += len(keys)
    return removed


def delete_page(name: str) -> int:
    """Remove a page, its files and its play records (when a pitch is passed, or old)."""
    settings, client = _require()
    _check_name(name)
    return _delete_prefixes(client, settings, [f"{name}/", f"_events/{name}/"])


OWNER_PATH = "_nav_owner"  # Round 51: the Worker marks the caller's network as yours


def register_own_network(timeout: float = 8.0) -> str:
    """Round 51: tell the Worker this Mac's network is yours, so your own views, plays, downloads
    and copies are never counted (the app does this by itself when it starts). Returns the
    network's short hash ('' when it couldn't)."""
    settings = page_settings()
    if settings is None:
        return ""
    request = urllib.request.Request(f"{settings.base_url}/{OWNER_PATH}?app=1",
                                     headers={"user-agent": "nav-studios-app"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_https_context()) as response:
            return str(json.loads(response.read(2000) or b"{}").get("network") or "")
    except Exception as exc:  # noqa: BLE001 - views still work; yours may count until the next try
        logger.info("Couldn't mark this network as yours on the clip pages: %s", exc)
        return ""


def owner_link() -> str:
    """The address to open once on your phone (mobile data) to mark it as yours."""
    settings = page_settings()
    return f"{settings.base_url}/{OWNER_PATH}" if settings else ""


def _owner_networks(client: Any, settings: PageSettings) -> set[str]:
    """The networks marked as yours (_me/<hash> in the bucket)."""
    owners: set[str] = set()
    token = None
    while True:
        kwargs = {"Bucket": settings.bucket, "Prefix": "_me/"}
        if token:
            kwargs["ContinuationToken"] = token
        listing = client.list_objects_v2(**kwargs)
        owners.update(item["Key"].split("/", 1)[1] for item in listing.get("Contents", []) if "/" in item["Key"])
        if not listing.get("IsTruncated"):
            return owners
        token = listing.get("NextContinuationToken")


def page_activity(since: datetime | None = None) -> dict[str, dict[str, Any]]:
    """What each page's visitor did: {"opens", "played", "half", "finished",
    "downloaded" (clip numbers), "last" (datetime)}. One listing for all pages.
    Round 51: events from your own networks are left out, and so are events before ``since``."""
    settings, client = _require()
    activity: dict[str, dict[str, Any]] = {}
    try:
        owners = _owner_networks(client, settings)
    except Exception:  # noqa: BLE001 - an older bucket or a listing hiccup: count everything
        owners = set()
    token = None
    while True:
        kwargs = {"Bucket": settings.bucket, "Prefix": "_events/"}
        if token:
            kwargs["ContinuationToken"] = token
        listing = client.list_objects_v2(**kwargs)
        for item in listing.get("Contents", []):
            parts = item["Key"].split("/")
            if len(parts) != 3:
                continue
            fields = parts[2].split("_")
            if len(fields) != 4 or fields[1] not in EVENT_TYPES:
                continue
            try:
                when = datetime.fromtimestamp(int(fields[0]) / 1000, tz=timezone.utc)
                clip = int(fields[2])
            except ValueError:
                continue
            if fields[3][:8] in owners or (since is not None and when < since):
                continue  # Round 51: you, or before you started counting
            page = activity.setdefault(parts[1], {
                "opens": 0, "played": set(), "half": set(), "finished": set(), "downloaded": set(), "last": None,
                "copied": set(), "offer": 0, "opened_at": [], "offer_at": [], "assets": set(),
            })
            if fields[1] == "open":
                page["opens"] += 1
                page["opened_at"].append(when)  # Round 49: when each view happened (Sent's page views)
            elif fields[1] == "offer":
                page["offer"] += 1
                page["offer_at"].append(when)
            elif fields[1] in {"srt", "cover", "social"}:  # Round 49/50: took a file or the Reels caption
                page["assets"].add((fields[1], clip))
            elif fields[1] == "call":  # Round 50: opened an email asking for a call
                page["call"] = page.get("call", 0) + 1
            elif fields[1] == "copy":
                page["copied"].add(clip)
            else:
                {"play": page["played"], "half": page["half"], "end": page["finished"], "dl": page["downloaded"]}[fields[1]].add(clip)
            if page["last"] is None or when > page["last"]:
                page["last"] = when
        if not listing.get("IsTruncated"):
            break
        token = listing.get("NextContinuationToken")
    return activity


def describe_activity(page: dict[str, Any] | None, now: datetime | None = None) -> str:
    """'▶ Played Shorts 1 and 3 (watched half of 1) · ⬇ downloaded 2 · 3 h ago'."""
    if not page or (not page["opens"] and not page["played"] and not page["downloaded"]):
        return "Not opened yet"

    def numbers(values: set[int]) -> str:
        items = [str(v) for v in sorted(values)]
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]

    parts = []
    if page["played"]:
        played = f"▶ Played Short{'s' if len(page['played']) > 1 else ''} {numbers(page['played'])}"
        watched = page["finished"] or page["half"]
        if page["finished"]:
            played += f" (watched {numbers(page['finished'])} to the end)"
        elif watched:
            played += f" (half of {numbers(watched)})"
        parts.append(played)
    else:
        times = "" if page["opens"] <= 1 else f" {page['opens']}×"
        parts.append(f"👀 Opened{times}, no plays yet")
    if page["downloaded"]:
        parts.append(f"⬇ downloaded {numbers(page['downloaded'])}")
    if page.get("copied"):
        parts.append(f"📋 copied the LinkedIn post for {numbers(page['copied'])}")
    if page.get("offer"):
        parts.append("💼 opened the offer")
    if page.get("call"):
        parts.append("📞 started a call request")
    if page["last"] is not None:
        delta = (now or datetime.now(timezone.utc)) - page["last"]
        hours = delta.total_seconds() / 3600
        parts.append("just now" if hours < 1 else f"{int(hours)} h ago" if hours < 48 else f"{int(hours // 24)} days ago")
    return " · ".join(parts)


# ---------- Round 49: page views (the 📈 panel at the top of 📨 Sent) ----------
VIEWS_SEEN_PATH = Path(__file__).resolve().parents[1] / "storage" / "page_views_seen.json"


def views_seen_at() -> datetime | None:
    """When you last marked the page views as seen (None: never)."""
    try:
        stamp = json.loads(VIEWS_SEEN_PATH.read_text(encoding="utf-8")).get("seen_at")
        return datetime.fromisoformat(stamp) if stamp else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _views_state() -> dict[str, Any]:
    try:
        data = json.loads(VIEWS_SEEN_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_views_state(state: dict[str, Any]) -> None:
    VIEWS_SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = VIEWS_SEEN_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(VIEWS_SEEN_PATH)


def mark_views_seen(now: datetime | None = None) -> None:
    _write_views_state({**_views_state(), "seen_at": (now or datetime.now(timezone.utc)).isoformat()})


def views_count_from() -> datetime | None:
    """Round 51: page views before this time are left out (None: all of them)."""
    try:
        stamp = _views_state().get("count_from")
        return datetime.fromisoformat(stamp) if stamp else None
    except (ValueError, TypeError):
        return None


def start_counting_now(now: datetime | None = None) -> None:
    """Round 51: count page views from now on (earlier ones may include your own visits from
    before your network was marked as yours)."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    _write_views_state({**_views_state(), "count_from": stamp, "seen_at": stamp})


def views_feed(activity: dict[str, dict[str, Any]], pages: Iterable[dict[str, Any]],
               seen_at: datetime | None = None) -> list[dict[str, Any]]:
    """One row per page you sent ({"page_name", "title", "url", "source"}), with what its
    visitor did, most recent activity first: views (page loads by them, not you: the app
    opens pages with ?me=1), first/last view, plays, what they took, the offer, and "new"
    (viewed since ``seen_at``)."""
    rows: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for page in pages:
        name = str(page.get("page_name") or "")
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        act = activity.get(name) or {}
        opened = sorted(act.get("opened_at") or [])
        took = []
        if act.get("downloaded"):
            took.append(f"video {', '.join(str(n) for n in sorted(act['downloaded']))}")
        if act.get("copied"):
            took.append(f"post {', '.join(str(n) for n in sorted(act['copied']))}")
        for kind, label in (("social", "reels caption"), ("srt", "captions"), ("cover", "cover")):
            numbers = sorted(n for k, n in act.get("assets") or () if k == kind)
            if numbers:
                took.append(f"{label} {', '.join(str(n) for n in numbers)}")
        last = act.get("last")
        rows.append({
            "page_name": name, "title": str(page.get("title") or name), "url": str(page.get("url") or ""),
            "source": str(page.get("source") or ""), "sent_at": str(page.get("sent_at") or ""),
            "views": int(act.get("opens") or 0), "first_view": opened[0] if opened else None,
            "last_view": opened[-1] if opened else None, "played": sorted(act.get("played") or ()),
            "finished": sorted(act.get("finished") or ()), "took": took, "offer": int(act.get("offer") or 0),
            "call": int(act.get("call") or 0),
            "last": last, "new": bool(last and (seen_at is None or last > seen_at)),
        })
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda row: (row["last"] or epoch, row["sent_at"]), reverse=True)
    return rows


def ago(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return "—"
    seconds = ((now or datetime.now(timezone.utc)) - when).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"


# ---------- Setup check: .venv/bin/python -m core.clip_pages --check ----------
def _https_context() -> ssl.SSLContext:
    """The certificate list the rest of the pipeline uses (certifi). Python from
    python.org doesn't use the Mac's own list, so plain urllib fails with
    CERTIFICATE_VERIFY_FAILED on every https address."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def check_setup(out: Any = print) -> bool:
    settings = page_settings()
    if settings is None:
        missing = [n for n in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "CLIP_PAGES_URL")
                   if not os.getenv(n, "").strip()]
        out(f"✗ Missing in .env: {', '.join(missing)}")
        return False
    out(f"Bucket {settings.bucket}, pages at {settings.base_url}")
    client = _client(settings)
    try:
        client.list_objects_v2(Bucket=settings.bucket, MaxKeys=1)
        out("✓ The R2 key can read the bucket")
    except Exception as exc:
        out(f"✗ Can't read the bucket with this key: {exc}")
        return False
    with tempfile.TemporaryDirectory() as temporary:
        clip = Path(temporary) / "clip.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x202020:s=1080x1920:r=30:d=3", "-f", "lavfi",
             "-i", "sine=d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
             "-movflags", "+faststart", str(clip)],
            check=True, capture_output=True, timeout=120,
        )
        try:
            name, url = publish_page("Setup check", "A test video", [{"path": clip, "start": 0, "end": 3}])
        except Exception as exc:
            out(f"✗ Upload failed: {exc}")
            return False
        out("✓ Uploaded a test page")
        ok = True
        try:
            ok &= _check_url(url, out, "The page opens", expect_text="Setup check")
            ok &= _check_url(f"{url}/clip_01.mp4", out, "Video seeking works (range request)", range_header="bytes=0-99", expect_status=206)
            beacon = urllib.request.Request(f"{settings.base_url}/e", method="POST",
                                            data=json.dumps({"s": name, "t": "play", "c": 1}).encode(),
                                            headers={"content-type": "text/plain", "user-agent": "nav-setup-check"})
            try:
                urllib.request.urlopen(beacon, timeout=20, context=_https_context()).read()
                seen = page_activity().get(name, {})
                ok &= _report(out, 1 in seen.get("played", set()), "Plays are recorded",
                              "the Worker didn't save the play (is the R2 binding named CLIPS?)")
            except Exception as exc:
                ok &= _report(out, False, "Plays are recorded", f"{settings.base_url}/e: {exc}")
        finally:
            delete_page(name)
            out("✓ Test page deleted")
    out("All set: new pitches will link to their clip page." if ok else "Something above needs fixing (see the README).")
    return bool(ok)


def _report(out: Any, ok: bool, label: str, detail: str = "") -> bool:
    out(f"{'✓' if ok else '✗'} {label}" + (f": {detail}" if detail and not ok else ""))
    return ok


def _check_url(url: str, out: Any, label: str, *, expect_text: str | None = None, range_header: str | None = None,
               expect_status: int = 200) -> bool:
    request = urllib.request.Request(url, headers={"user-agent": "nav-setup-check", **({"range": range_header} if range_header else {})})
    try:
        with urllib.request.urlopen(request, timeout=30, context=_https_context()) as response:
            body = response.read(200_000)
            status = response.status
    except urllib.error.HTTPError as exc:
        return _report(out, False, label, f"{url} answered {exc.code}")
    except Exception as exc:
        return _report(out, False, label, f"{url}: {exc}")
    if status != expect_status:
        return _report(out, False, label, f"{url} answered {status}, expected {expect_status}")
    if expect_text and expect_text.encode() not in body:
        return _report(out, False, label, f"{url} isn't the clip page (is the Worker on this address?)")
    return _report(out, True, label)


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(0 if check_setup() else 1)
    print("Usage: .venv/bin/python -m core.clip_pages --check")
