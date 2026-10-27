from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib import import_module
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from dotenv import load_dotenv

from core.gemini_resilience import (
    gemini_thinking_config,
    generate_content_with_fallback,
)


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEEN_CHANNELS_PATH = PROJECT_ROOT / "storage" / "seen_channels.json"
YOUTUBE_API_SERVICE = "youtube"
YOUTUBE_API_VERSION = "v3"
YOUTUBE_QUOTA_WAIT_SECONDS = 15 * 60
_quota_wait_lock = Lock()
_quota_wait_deadline = 0.0
_DURATION_PATTERN = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")
_EMAIL_PATTERN = re.compile(
    r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
)
_URL_PATTERN = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)


class YouTubeQuotaExceededError(RuntimeError):
    """Raised after waiting for a YouTube quota limit to cool down."""


_CATEGORY_GUIDANCE = {
    "All": (
        "sports coaching and athletic drills; high-ticket business and consulting; "
        "real estate; health, biomechanics, and fitness; niche education"
    ),
    "Sports & Training": (
        "sports coaching, athletic drills, game analysis, strength training, "
        "biomechanics, and athlete development"
    ),
    "Business & Finance": (
        "high-ticket business, consulting, entrepreneurship, investing, and finance"
    ),
    "Real Estate": (
        "residential and commercial real estate, investing, property management, "
        "and agent education"
    ),
    "Health & Biomechanics": (
        "health education, biomechanics, physical therapy, fitness, and performance"
    ),
    "Niche Education": (
        "specialist educators, professional training, and practical niche tutorials"
    ),
}


def _load_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env")


def _youtube_client() -> Any:
    _load_env()
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("YOUTUBE_API_KEY is not configured in the project .env file.")
    from googleapiclient.discovery import build

    return build(
        YOUTUBE_API_SERVICE,
        YOUTUBE_API_VERSION,
        developerKey=api_key,
        cache_discovery=False,
    )


def _gemini_client() -> tuple[Any, Any]:
    _load_env()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured in the project .env file.")
    try:
        genai = import_module("google.genai")
        types = import_module("google.genai.types")
    except ImportError as exc:
        raise RuntimeError(
            "Gemini query generation requires google-genai. "
            "Install it with `python -m pip install google-genai`."
        ) from exc
    return genai.Client(api_key=api_key), types


def _parse_query_response(text: str, count: int) -> list[str]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError("Gemini returned invalid JSON for search phrases.") from exc

    if isinstance(payload, dict):
        phrases = payload.get("search_phrases")
    else:
        phrases = payload
    if not isinstance(phrases, list):
        raise ValueError("Gemini's response must contain a search_phrases list.")
    unique_phrases: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        if not isinstance(phrase, str) or not phrase.strip():
            continue
        normalized = phrase.strip()
        if normalized.casefold() not in seen:
            unique_phrases.append(normalized)
            seen.add(normalized.casefold())
        if len(unique_phrases) == count:
            break
    if not unique_phrases:
        raise ValueError("Gemini did not generate any usable search phrases.")
    return unique_phrases


def generate_search_queries(category: str = "All", count: int = 10) -> list[str]:
    """Generate targeted YouTube search phrases for long-form creator discovery."""
    if count < 1:
        raise ValueError("count must be at least 1.")
    category_guidance = _CATEGORY_GUIDANCE.get(
        category,
        category.strip() if category.strip() else _CATEGORY_GUIDANCE["All"],
    )
    client, types = _gemini_client()
    prompt = (
        f"Create {count} distinct YouTube search phrases across these niches: "
        f"{category_guidance}.\n"
        "Target creators and channels publishing long-form conversation, podcasting, education, "
        "interview, commentary, or deep-dive analysis videos that under-index on Shorts.\n"
        "Incorporate high-yield keywords like 'podcast', 'deep dive', 'interview', 'commentary', "
        "and 'breakdown' (e.g., '[Niche] podcast interview', '[Topic] deep dive breakdown', "
        "'[Topic] masterclass analysis'). Keep phrases concise and specific. "
        'Return only JSON in the form {"search_phrases": ["phrase", ...]}.'
    )
    response = generate_content_with_fallback(
        client.models.generate_content,
        model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            thinking_config=gemini_thinking_config(types),
        ),
    )
    if not response.text:
        raise RuntimeError("Gemini returned an empty search phrase response.")
    return _parse_query_response(response.text, count)


def _duration_seconds(duration: object) -> int | None:
    if not isinstance(duration, str):
        return None
    match = _DURATION_PATTERN.fullmatch(duration)
    if not match:
        return None
    hours, minutes, seconds = (int(value or 0) for value in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _channel_contacts(description: str) -> tuple[str | None, list[str]]:
    email_match = _EMAIL_PATTERN.search(description)
    social_links = list(dict.fromkeys(_URL_PATTERN.findall(description)))
    return (email_match.group(0) if email_match else None), social_links


def _is_quota_error(error: Exception) -> bool:
    status = getattr(getattr(error, "resp", None), "status", None)
    if status == 429:
        return True
    content = getattr(error, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    if not isinstance(content, str):
        content = str(content)
    reason = str(getattr(error, "reason", ""))
    details = f"{reason} {content}".casefold()
    return any(
        marker in details
        for marker in (
            "quotaexceeded",
            "dailylimitexceeded",
            "userratelimitexceeded",
            "quota exceeded",
            "daily limit exceeded",
            "user rate limit exceeded",
        )
    )


def _execute_youtube_request(request: Any) -> dict[str, Any]:
    try:
        return request.execute()
    except Exception as exc:
        if not _is_quota_error(exc):
            raise

        try:
            from autonomous_worker import set_youtube_quota_exhausted

            set_youtube_quota_exhausted(
                True,
                "YouTube API quota limit reached; waiting 15 minutes before retrying.",
            )
        except Exception:
            pass

        logger.warning(
            "YouTube API quota limit reached; background harvester paused."
        )
        raise YouTubeQuotaExceededError(
            "YouTube API quota limit reached; waiting 15 minutes before retrying."
        ) from exc


def _read_seen_channel_ids() -> set[str]:
    seen: set[str] = set()
    if SEEN_CHANNELS_PATH.exists():
        try:
            payload = json.loads(SEEN_CHANNELS_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                seen.update(str(x) for x in payload if isinstance(x, str))
        except Exception as exc:
            logger.warning("Could not read checked channel cache at %s: %s", SEEN_CHANNELS_PATH, exc)

    processed_path = PROJECT_ROOT / "storage" / "processed_creators.json"
    if processed_path.exists():
        try:
            payload = json.loads(processed_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                for cid in payload.get("processed_channel_ids", []):
                    if isinstance(cid, str):
                        seen.add(cid)
                records = payload.get("records") or payload.get("creators", [])
                if isinstance(records, list):
                    for record in records:
                        if isinstance(record, dict) and record.get("channel_id"):
                            seen.add(str(record["channel_id"]))
        except Exception as exc:
            logger.warning("Could not read processed creators at %s: %s", processed_path, exc)

    skipped_path = PROJECT_ROOT / "storage" / "skipped_leads.json"
    if skipped_path.exists():
        try:
            payload = json.loads(skipped_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict) and item.get("channel_id"):
                        seen.add(str(item["channel_id"]))
        except Exception as exc:
            logger.warning("Could not read skipped leads at %s: %s", skipped_path, exc)

    pending_path = PROJECT_ROOT / "storage" / "pending_outreach.json"
    if pending_path.exists():
        try:
            payload = json.loads(pending_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict) and item.get("channel_id"):
                        seen.add(str(item["channel_id"]))
        except Exception as exc:
            logger.warning("Could not read pending outreach at %s: %s", pending_path, exc)

    return seen


def _write_seen_channel_ids(channel_ids: set[str]) -> None:
    SEEN_CHANNELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=SEEN_CHANNELS_PATH.parent,
            prefix=f".{SEEN_CHANNELS_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            json.dump(sorted(channel_ids), file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, SEEN_CHANNELS_PATH)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Could not save checked channel cache at {SEEN_CHANNELS_PATH}: {exc}"
        ) from exc


def _search_candidates(
    youtube: Any,
    query: str,
) -> list[dict[str, str]]:
    response = _execute_youtube_request(
        youtube.search().list(
            part="snippet",
            type="video",
            q=query,
            maxResults=50,
            order="relevance",
        )
    )
    search_items = response.get("items", [])
    candidates: list[dict[str, str]] = []
    seen_video_ids: set[str] = set()
    for item in search_items:
        item_id = item.get("id") or {}
        snippet = item.get("snippet") or {}
        channel_id = snippet.get("channelId")
        video_id = item_id.get("videoId")
        if (
            not isinstance(channel_id, str)
            or not isinstance(video_id, str)
            or video_id in seen_video_ids
        ):
            continue
        seen_video_ids.add(video_id)
        candidates.append(
            {
                "channel_id": channel_id,
                "video_id": video_id,
                "channel_title": str(snippet.get("channelTitle") or ""),
                "video_title": str(snippet.get("title") or ""),
            }
        )
    return candidates


def _recent_upload_ids(uploads_playlist: str) -> list[str]:
    youtube = _youtube_client()
    response = _execute_youtube_request(
        youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist,
            maxResults=10,
        )
    )
    video_ids: list[str] = []
    for item in response.get("items", []):
        video_id = (item.get("contentDetails") or {}).get("videoId")
        if isinstance(video_id, str):
            video_ids.append(video_id)
    return video_ids


_NON_LATIN_SCRIPT_PATTERN = re.compile(
    r"[\u0400-\u04FF\u0600-\u06FF\u0900-\u097F\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF\u0E00-\u0E7F\u0590-\u05FF]"
)


def _is_english_channel(
    channel: dict[str, Any],
    sample_videos: list[dict[str, Any]],
) -> bool:
    snippet = channel.get("snippet") or {}
    channel_lang = snippet.get("defaultLanguage")
    if isinstance(channel_lang, str) and channel_lang.strip():
        if not channel_lang.strip().lower().startswith("en"):
            return False

    non_en_count = 0
    en_count = 0
    for video in sample_videos:
        v_snippet = video.get("snippet") or {}
        v_lang = v_snippet.get("defaultLanguage") or v_snippet.get("defaultAudioLanguage")
        if isinstance(v_lang, str) and v_lang.strip():
            if v_lang.strip().lower().startswith("en"):
                en_count += 1
            else:
                non_en_count += 1
    if non_en_count > 0 and en_count == 0:
        return False

    channel_title = str(snippet.get("title") or "")
    if len(_NON_LATIN_SCRIPT_PATTERN.findall(channel_title)) >= 3:
        return False

    non_latin_video_count = 0
    for video in sample_videos:
        v_snippet = video.get("snippet") or {}
        title = str(v_snippet.get("title") or "")
        if len(_NON_LATIN_SCRIPT_PATTERN.findall(title)) >= 3:
            non_latin_video_count += 1
    if sample_videos and (non_latin_video_count / len(sample_videos)) > 0.2:
        return False

    return True


def _channel_subscriber_count(channel: dict[str, Any]) -> int | None:
    statistics = channel.get("statistics") or {}
    if statistics.get("hiddenSubscriberCount"):
        return None
    subscriber_value = statistics.get("subscriberCount")
    if subscriber_value is None:
        return None
    try:
        return int(subscriber_value)
    except (TypeError, ValueError):
        logger.warning("Invalid subscriber count for channel %s", channel.get("id"))
        return -1


def discover_channels(
    queries: list[str],
    max_results: int = 10,
) -> list[dict[str, Any]]:
    """Find qualified YouTube channels and one eligible long-form video per lead."""
    if not queries or any(not query.strip() for query in queries):
        raise ValueError("queries must contain at least one non-empty search phrase.")
    if not 1 <= max_results <= 50:
        raise ValueError("max_results must be between 1 and 50.")

    qualified: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        for query_offset in range(0, len(queries), 5):
            query_batch = queries[query_offset : query_offset + 5]
            search_futures = {
                executor.submit(
                    _search_candidates,
                    _youtube_client(),
                    query.strip(),
                ): query
                for query in query_batch
            }
            candidates: dict[str, dict[str, str]] = {}
            for future in as_completed(search_futures):
                query = search_futures[future]
                try:
                    query_candidates = future.result()
                except YouTubeQuotaExceededError:
                    raise
                except Exception:
                    logger.exception("YouTube search failed for query %r", query)
                    continue
                for candidate in query_candidates:
                    candidates.setdefault(candidate["channel_id"], candidate)
            seen_channel_ids = _read_seen_channel_ids()
            candidates = {
                channel_id: candidate
                for channel_id, candidate in candidates.items()
                if channel_id not in seen_channel_ids
            }
            if not candidates:
                continue

            channel_ids = list(candidates)
            channel_metadata: dict[str, dict[str, Any]] = {}
            for offset in range(0, len(channel_ids), 50):
                channel_batch = channel_ids[offset : offset + 50]
                response = _execute_youtube_request(
                    _youtube_client().channels().list(
                        part="snippet,statistics,contentDetails",
                        id=",".join(channel_batch),
                        maxResults=50,
                    )
                )
                seen_channel_ids.update(channel_batch)
                _write_seen_channel_ids(seen_channel_ids)
                for channel in response.get("items", []):
                    channel_id = channel.get("id")
                    if isinstance(channel_id, str):
                        channel_metadata[channel_id] = channel

            eligible_channels: dict[str, tuple[dict[str, str], dict[str, Any], int | None]] = {}
            playlist_to_channel: dict[str, str] = {}
            for channel_id, candidate in candidates.items():
                channel = channel_metadata.get(channel_id)
                if channel is None:
                    continue
                subscriber_count = _channel_subscriber_count(channel)
                if subscriber_count == -1 or (
                    subscriber_count is not None
                    and not (5_000 <= subscriber_count <= 150_000)
                ):
                    continue
                uploads_playlist = (
                    (channel.get("contentDetails") or {})
                    .get("relatedPlaylists", {})
                    .get("uploads")
                )
                if isinstance(uploads_playlist, str):
                    eligible_channels[channel_id] = (
                        candidate,
                        channel,
                        subscriber_count,
                    )
                    playlist_to_channel[uploads_playlist] = channel_id

            playlist_futures = {
                executor.submit(_recent_upload_ids, playlist_id): playlist_id
                for playlist_id in playlist_to_channel
            }
            recent_ids_by_channel: dict[str, list[str]] = {}
            for future in as_completed(playlist_futures):
                playlist_id = playlist_futures[future]
                channel_id = playlist_to_channel[playlist_id]
                try:
                    recent_ids_by_channel[channel_id] = future.result()
                except YouTubeQuotaExceededError:
                    raise
                except Exception:
                    logger.exception(
                        "Could not load recent uploads for channel %s",
                        channel_id,
                    )

            all_recent_ids = list(
                dict.fromkeys(
                    video_id
                    for video_ids in recent_ids_by_channel.values()
                    for video_id in video_ids[:10]
                )
            )
            videos_by_id: dict[str, dict[str, Any]] = {}
            for offset in range(0, len(all_recent_ids), 50):
                video_batch = all_recent_ids[offset : offset + 50]
                response = _execute_youtube_request(
                    _youtube_client().videos().list(
                        part="snippet,contentDetails",
                        id=",".join(video_batch),
                        maxResults=50,
                    )
                )
                videos_by_id.update(
                    {
                        video["id"]: video
                        for video in response.get("items", [])
                        if isinstance(video.get("id"), str)
                    }
                )

            for channel_id, (candidate, channel, subscriber_count) in eligible_channels.items():
                recent_video_ids = recent_ids_by_channel.get(channel_id, [])[:10]
                recent_videos = [
                    videos_by_id[video_id]
                    for video_id in recent_video_ids
                    if video_id in videos_by_id
                ]
                if not recent_videos:
                    continue

                if not _is_english_channel(channel, recent_videos):
                    continue

                recent_durations = [
                    _duration_seconds((video.get("contentDetails") or {}).get("duration"))
                    for video in recent_videos
                ]

                # Under-index on Shorts: no more than 3 Shorts (<=60s) in the last 10 uploads
                shorts_count = sum(
                    1 for duration in recent_durations if duration is not None and duration <= 60
                )
                if shorts_count > 3:
                    continue

                # Active long-form production: at least 2 long-form videos (>5 min) in last 30 days
                cutoff_30_days = datetime.now(timezone.utc) - timedelta(days=30)
                long_form_durations: list[int] = []
                recent_30_day_long_form_count = 0
                for video, duration in zip(recent_videos, recent_durations):
                    if duration is not None and duration > 300:
                        long_form_durations.append(duration)
                        published_at_str = (video.get("snippet") or {}).get("publishedAt")
                        if isinstance(published_at_str, str):
                            try:
                                published_at = datetime.fromisoformat(
                                    published_at_str.replace("Z", "+00:00")
                                )
                                if published_at >= cutoff_30_days:
                                    recent_30_day_long_form_count += 1
                            except Exception:
                                pass

                if recent_30_day_long_form_count < 2:
                    continue

                # Avoid raw stream dumps: average long-form duration must not exceed 90 minutes
                if not long_form_durations:
                    continue
                avg_long_form_duration = sum(long_form_durations) / len(long_form_durations)
                if avg_long_form_duration > 90 * 60:
                    continue

                target_video = next(
                    (
                        video
                        for video, duration in zip(recent_videos, recent_durations)
                        if duration is not None and 300 < duration <= 90 * 60
                    ),
                    None,
                )
                if target_video is None:
                    continue

                snippet = channel.get("snippet") or {}
                channel_title = str(
                    snippet.get("title") or candidate["channel_title"]
                )
                email, social_links = _channel_contacts(
                    str(snippet.get("description") or "")
                )
                channel_about_url = (
                    f"https://www.youtube.com/channel/{channel_id}/about"
                )
                target_id = target_video.get("id")
                if not isinstance(target_id, str):
                    continue
                target_snippet = target_video.get("snippet") or {}
                qualified.append(
                    {
                        "channel_id": channel_id,
                        "channel_title": channel_title,
                        "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                        "subscriber_count": subscriber_count,
                        "latest_video_url": f"https://www.youtube.com/watch?v={target_id}",
                        "latest_video_title": str(target_snippet.get("title") or ""),
                        "duration_seconds": _duration_seconds(
                            (target_video.get("contentDetails") or {}).get("duration")
                        ),
                        "contact_email": email,
                        "contact_info": email or channel_about_url,
                        "channel_about_url": channel_about_url,
                        "social_links": social_links,
                    }
                )
                if len(qualified) >= max_results:
                    return qualified[:max_results]
    return qualified
