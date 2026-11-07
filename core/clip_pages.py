"""Personal clip pages: https://clips.navstudiolab.com/<page>, one per pitch.

The files go to a Cloudflare R2 bucket; the Worker in cloudflare/clip-pages-worker.js
shows them. R2 bucket layout:
    <page>/page.json            who it's for, the video, each clip's start/end
    <page>/clip_01.mp4          the clip as rendered (what they download and post)
    <page>/poster_01.jpg        first frame shown before they press play
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
EVENT_TYPES = ("open", "play", "half", "end", "dl")
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
) -> None:
    client.upload_file(
        str(clip), settings.bucket, f"{name}/clip_{index:02d}.mp4",
        ExtraArgs={"ContentType": "video/mp4", "CacheControl": "no-cache"},
    )
    _upload_poster(client, settings, name, index, clip, poster_at)


def replace_poster(name: str, index: int, clip: Path, at: float | None) -> float:
    """Swap one clip's poster on its page (the frame at ``at`` s; None: picked again).
    Returns the time used."""
    settings, client = _require()
    _check_name(name)
    return _upload_poster(client, settings, name, int(index), Path(clip), at)


def publish_page(
    channel_title: str,
    video_title: str,
    clips: list[dict[str, Any]],
    kind: str = "video",
    slug: str = "",
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
        for index, clip in enumerate(clips, start=1):
            _upload_clip(client, settings, name, index, Path(clip["path"]))
        info = {
            "v": 1,
            "channel_title": channel_title,
            "video_title": video_title,
            "kind": "episode" if kind == "episode" else "video",
            "clips": [{"start": round(float(c["start"]), 2), "end": round(float(c["end"]), 2)} for c in clips],
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
    _upload_clip(client, settings, name, index, Path(clip), poster_at)
    if start is None or end is None:
        return
    info = json.loads(client.get_object(Bucket=settings.bucket, Key=f"{name}/page.json")["Body"].read())
    if 1 <= index <= len(info.get("clips", [])):
        info["clips"][index - 1] = {"start": round(float(start), 2), "end": round(float(end), 2)}
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

    files = [(number, kind, extension) for number in range(index + 1, expected_count + 1)
             for kind, extension in (("clip", "mp4"), ("poster", "jpg"))]
    info = read_page()
    clips = list(info.get("clips") or [])
    moving = info.get("moving")
    if not moving and len(clips) == expected_count:
        # 1. Copy the later clips aside (the originals are untouched until step 2).
        moving = {"index": index, "count": expected_count, "run": secrets.token_hex(4)}
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
        Delete={"Objects": [{"Key": f"{name}/clip_{expected_count:02d}.mp4"},
                            {"Key": f"{name}/poster_{expected_count:02d}.jpg"}], "Quiet": True},
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


def page_activity() -> dict[str, dict[str, Any]]:
    """What each page's visitor did: {"opens", "played", "half", "finished",
    "downloaded" (clip numbers), "last" (datetime)}. One listing for all pages."""
    settings, client = _require()
    activity: dict[str, dict[str, Any]] = {}
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
            page = activity.setdefault(parts[1], {
                "opens": 0, "played": set(), "half": set(), "finished": set(), "downloaded": set(), "last": None,
            })
            if fields[1] == "open":
                page["opens"] += 1
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
    if page["last"] is not None:
        delta = (now or datetime.now(timezone.utc)) - page["last"]
        hours = delta.total_seconds() / 3600
        parts.append("just now" if hours < 1 else f"{int(hours)} h ago" if hours < 48 else f"{int(hours // 24)} days ago")
    return " · ".join(parts)


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
