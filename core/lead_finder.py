from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, Field
import requests
from dotenv import load_dotenv
from config import load_settings


logger = logging.getLogger(__name__)
API_BASE_URL = "https://www.googleapis.com/youtube/v3"
REQUEST_TIMEOUT_SECONDS = 10
MAX_TRENDING_CHANNELS = 15
LAST_TRENDING_CHANNELS_EVALUATED = 0
_VIDEO_DURATION_PATTERN = re.compile(
    r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$"
)

CURATED_NICHES = {
    "Business & Startups": [
        "bootstrapped SaaS founder podcast",
        "solo entrepreneur interview",
        "e-commerce brand builder breakdown",
        "angel investor interview",
    ],
    "AI & Tech Innovations": [
        "AI workflow tutorial podcast",
        "developer founder interview",
        "future of tech discussion",
    ],
    "Personal Finance & Real Estate": [
        "commercial real estate syndication podcast",
        "cash flow investing interview",
        "small business acquisition",
    ],
    "Health & Longevity": [
        "functional medicine interview",
        "strength training science podcast",
        "longevity protocol breakdown",
    ],
    "Mindset & Solo Founders": [
        "solo entrepreneur interview",
        "independent creator business podcast",
        "founder mindset interview",
    ],
    "Sports & Athlete Development": [
        "hockey coach breakdown",
        "lacrosse drills",
        "sports performance podcast",
        "youth hockey development",
        "athlete training and coaching",
    ],
}


class _NicheSuggestions(BaseModel):
    search_phrases: list[str] = Field(min_length=3, max_length=5)


def _load_api_key() -> str | None:
    env_file = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(env_file)
    return os.getenv("YOUTUBE_API_KEY")


def _duration_seconds(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = _VIDEO_DURATION_PATTERN.fullmatch(value)
    if not match:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def extract_email(text: str) -> str:
    match = re.search(
        r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+",
        text or "",
    )
    return match.group(0) if match else "Check Channel About"


def _recent_upload_metrics(
    channel_id: str,
    api_key: str,
) -> dict[str, Any] | None:
    try:
        response = requests.get(
            f"{API_BASE_URL}/search",
            params={
                "part": "snippet",
                "type": "video",
                "channelId": channel_id,
                "order": "date",
                "maxResults": 15,
                "key": api_key,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        search_data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Recent uploads lookup failed for channel %s: %s", channel_id, exc)
        return None

    items = search_data.get("items", []) if isinstance(search_data, dict) else []
    video_ids = [
        item["id"]["videoId"]
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("id"), dict)
        and isinstance(item["id"].get("videoId"), str)
    ]
    if not video_ids:
        return None

    videos: list[dict[str, Any]] = []
    for offset in range(0, len(video_ids), 50):
        try:
            response = requests.get(
                f"{API_BASE_URL}/videos",
                params={
                    "part": "snippet,contentDetails",
                    "id": ",".join(video_ids[offset : offset + 50]),
                    "key": api_key,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            videos_data = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning(
                "Recent upload details lookup failed for channel %s: %s",
                channel_id,
                exc,
            )
            return None
        response_items = (
            videos_data.get("items", []) if isinstance(videos_data, dict) else []
        )
        if isinstance(response_items, list):
            videos.extend(item for item in response_items if isinstance(item, dict))

    cutoff = datetime.now(timezone.utc) - timedelta(days=45)
    long_form_videos: list[dict[str, Any]] = []
    shorts_count = 0
    non_short_count = 0
    recent_long_form_count = 0
    for video in videos:
        duration = _duration_seconds(
            (video.get("contentDetails") or {}).get("duration")
        )
        snippet = video.get("snippet") or {}
        if duration is None:
            continue
        if duration <= 60:
            shorts_count += 1
        else:
            non_short_count += 1
            if duration >= 5 * 60:
                long_form_videos.append(video)
            published_at = snippet.get("publishedAt")
            if duration >= 5 * 60 and isinstance(published_at, str):
                try:
                    published = datetime.fromisoformat(
                        published_at.replace("Z", "+00:00")
                    )
                except ValueError:
                    logger.info(
                        "Invalid publishedAt for video %s",
                        video.get("id", "unknown"),
                    )
                else:
                    if published >= cutoff:
                        recent_long_form_count += 1

    latest_long_form = max(
        long_form_videos,
        key=lambda video: str((video.get("snippet") or {}).get("publishedAt", "")),
        default=None,
    )
    long_form_count = len(long_form_videos)
    return {
        "long_form_count": long_form_count,
        "recent_long_form_count": recent_long_form_count,
        "shorts_count": shorts_count,
        "short_to_long_ratio": shorts_count / max(long_form_count, 1),
        "shorts_dominate": shorts_count >= 3 and shorts_count > non_short_count,
        "latest_long_form": latest_long_form,
    }


def search_creators(
    keyword: str,
    min_subs: int = 0,
    max_subs: int = 150000,
) -> list[dict]:
    """Find YouTube creators below the subscriber cap using Data API v3."""
    if not keyword.strip():
        raise ValueError("keyword must not be empty.")
    if min_subs < 0 or max_subs < min_subs:
        raise ValueError("Subscriber bounds must be non-negative and ordered.")

    api_key = _load_api_key()
    if not api_key:
        logger.warning("YOUTUBE_API_KEY is not configured; creator search was skipped.")
        return []

    try:
        search_response = requests.get(
            f"{API_BASE_URL}/search",
            params={
                "part": "snippet",
                "type": "video",
                "q": keyword,
                "maxResults": 25,
                "key": api_key,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        search_response.raise_for_status()
        search_data = search_response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("YouTube video search failed: %s", exc)
        return []
    if not isinstance(search_data, dict):
        logger.warning("YouTube video search returned an invalid response.")
        return []

    search_items = search_data.get("items", [])
    if not isinstance(search_items, list):
        logger.warning("YouTube video search returned an invalid item list.")
        return []
    channel_candidates: dict[str, dict[str, Any]] = {}
    for item in search_items:
        if not isinstance(item, dict):
            continue
        snippet = item.get("snippet")
        item_id = item.get("id")
        if not isinstance(snippet, dict) or not isinstance(item_id, dict):
            continue
        channel_id = snippet.get("channelId")
        video_id = item_id.get("videoId")
        if not isinstance(channel_id, str) or not isinstance(video_id, str):
            continue
        channel_candidates.setdefault(
            channel_id,
            {
                "video_id": video_id,
                "video_title": str(snippet.get("title") or ""),
                "channel_title": str(snippet.get("channelTitle") or ""),
            },
        )
    channel_ids = list(channel_candidates)
    global LAST_TRENDING_CHANNELS_EVALUATED
    LAST_TRENDING_CHANNELS_EVALUATED = len(channel_ids)
    logger.info(
        "[DEBUG] Found %d unique channel IDs from search query '%s'",
        len(channel_ids),
        keyword,
    )
    if not channel_ids:
        return []

    leads: list[dict] = []
    for offset in range(0, len(channel_ids), 50):
        batch_ids = channel_ids[offset : offset + 50]
        try:
            channels_response = requests.get(
                f"{API_BASE_URL}/channels",
                params={
                    "part": "snippet,statistics",
                    "id": ",".join(batch_ids),
                    "key": api_key,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            channels_response.raise_for_status()
            channels_data: dict[str, Any] = channels_response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("YouTube channel lookup failed: %s", exc)
            channels_data = {}
        if not isinstance(channels_data, dict):
            logger.warning("YouTube channel lookup returned an invalid response.")
            channels_data = {}

        channel_items = channels_data.get("items", [])
        if not isinstance(channel_items, list):
            logger.warning("YouTube channel lookup returned an invalid item list.")
            channel_items = []
        channels_by_id: dict[str, dict[str, Any]] = {}
        for channel in channel_items:
            if not isinstance(channel, dict):
                continue
            channel_id = channel.get("id")
            if not isinstance(channel_id, str):
                continue
            channels_by_id[channel_id] = channel

        for channel_id in batch_ids:
            channel = channels_by_id.get(channel_id, {})
            statistics = channel.get("statistics") or {}
            subscriber_count: int | None = None
            try:
                if not statistics.get("hiddenSubscriberCount"):
                    subscriber_count = int(statistics["subscriberCount"])
            except (KeyError, TypeError, ValueError):
                logger.info("Subscriber count unavailable for channel %s", channel_id)
            if subscriber_count is not None and (
                subscriber_count < min_subs or subscriber_count > max_subs
            ):
                continue

            metrics = _recent_upload_metrics(channel_id, api_key)
            if metrics is not None and metrics["shorts_dominate"]:
                continue
            if metrics is None:
                metrics = {
                    "long_form_count": 0,
                    "recent_long_form_count": 0,
                    "shorts_count": 0,
                    "short_to_long_ratio": 0.0,
                    "shorts_dominate": False,
                    "latest_long_form": None,
                }
            latest_long_form = metrics["latest_long_form"]
            latest_video_snippet = (
                (latest_long_form.get("snippet") or {})
                if latest_long_form is not None
                else {}
            )
            latest_video_id = (
                latest_long_form.get("id")
                if latest_long_form is not None
                else channel_candidates[channel_id]["video_id"]
            )
            if not isinstance(latest_video_id, str):
                continue

            snippet = channel.get("snippet") or {}
            email = extract_email(str(snippet.get("description") or ""))
            if email == "Check Channel About":
                email = extract_email(str(latest_video_snippet.get("description") or ""))
            candidate = channel_candidates[channel_id]
            leads.append(
                {
                    "channel_title": str(
                        snippet.get("title") or candidate["channel_title"] or ""
                    ),
                    "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                    "subscriber_count": subscriber_count,
                    "contact_email": email,
                    "latest_video_title": str(
                        latest_video_snippet.get("title")
                        or candidate["video_title"]
                        or ""
                    ),
                    "latest_video_url": f"https://www.youtube.com/watch?v={latest_video_id}",
                    "duration_seconds": _duration_seconds(
                        (latest_long_form.get("contentDetails") or {}).get("duration")
                    ) if latest_long_form is not None else None,
                    **metrics,
                }
            )

    return leads


def get_trending_niches(category: str = "Business & Technology") -> list[str]:
    """Generate a small set of current, specific long-form creator search phrases."""
    try:
        settings = load_settings()
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured.")
        client = OpenAI(api_key=settings.openai_api_key)
        completion = client.beta.chat.completions.parse(
            model=settings.openai_model or "gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Suggest 5 specific breakout search phrases for finding "
                            "YouTube creators, coaches, interviews, tutorials, and "
                            "podcasts in the requested category. Include practical "
                            "searchable topics and varied formats, not only long-form "
                            "podcasts. Return concise phrases suitable for YouTube search."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Category: {category.strip()}",
                },
            ],
            response_format=_NicheSuggestions,
        )
        parsed = completion.choices[0].message.parsed
        phrases: list[str] = []
        seen: set[str] = set()
        for phrase in parsed.search_phrases if parsed is not None else []:
            normalized = phrase.strip()
            key = normalized.casefold()
            if normalized and key not in seen:
                seen.add(key)
                phrases.append(normalized)
        if phrases:
            return phrases[:5]
        logger.warning("OpenAI returned no usable trending niche suggestions.")
    except Exception as exc:
        logger.warning("Trending niche generation failed: %s", exc)

    fallback = CURATED_NICHES.get(category)
    if fallback is None:
        fallback = (
            CURATED_NICHES["Business & Startups"]
            + CURATED_NICHES["AI & Tech Innovations"]
        )
    return fallback[:5]


def find_trending_leads(
    category: str = "Business & Technology",
    min_subs: int = 0,
    max_subs: int = 150000,
) -> list[dict]:
    """Find recent high-trajectory channels discovered from generated niche queries."""
    global LAST_TRENDING_CHANNELS_EVALUATED
    LAST_TRENDING_CHANNELS_EVALUATED = 0

    if min_subs < 0 or max_subs < min_subs:
        raise ValueError("Subscriber bounds must be non-negative and ordered.")

    api_key = _load_api_key()
    if not api_key:
        logger.warning("YOUTUBE_API_KEY is not configured; trending lead search skipped.")
        return []

    niches = get_trending_niches(category)
    if not niches:
        return []

    published_after = (
        (datetime.now(timezone.utc) - timedelta(days=45))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    discovered_channels: dict[str, dict[str, str]] = {}
    searched_channel_ids: set[str] = set()
    for niche in niches[:5]:
        try:
            response = requests.get(
                f"{API_BASE_URL}/search",
                params={
                    "part": "snippet",
                    "type": "video",
                    "order": "date",
                    "publishedAfter": published_after,
                    "q": niche,
                    "maxResults": 25,
                    "key": api_key,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Trending video search failed for %r: %s", niche, exc)
            continue

        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            logger.warning("Trending video search returned invalid data for %r.", niche)
            continue
        for item in data.get("items", []):
            if not isinstance(item, dict):
                continue
            snippet = item.get("snippet") or {}
            channel_id = snippet.get("channelId")
            if not isinstance(channel_id, str):
                continue
            item_id = item.get("id")
            searched_channel_ids.add(channel_id)
            discovered_channels.setdefault(
                channel_id,
                {
                    "niche_keyword": niche,
                    "channel_title": str(snippet.get("channelTitle") or ""),
                    "video_id": str(
                        item_id.get("videoId")
                        if isinstance(item_id, dict)
                        else ""
                    ),
                    "video_title": str(snippet.get("title") or ""),
                },
            )
        query_channel_ids = {
            item["snippet"]["channelId"]
            for item in data.get("items", [])
            if isinstance(item, dict)
            and isinstance(item.get("snippet"), dict)
            and isinstance(item["snippet"].get("channelId"), str)
        }
        logger.info(
            "[DEBUG] Found %d unique channel IDs from search query '%s'",
            len(query_channel_ids),
            niche,
        )

    LAST_TRENDING_CHANNELS_EVALUATED = min(
        len(searched_channel_ids), MAX_TRENDING_CHANNELS
    )
    if not discovered_channels:
        return []

    evaluated_channel_ids = list(discovered_channels)[:MAX_TRENDING_CHANNELS]
    LAST_TRENDING_CHANNELS_EVALUATED = len(evaluated_channel_ids)
    channel_details: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(evaluated_channel_ids), 50):
        try:
            response = requests.get(
                f"{API_BASE_URL}/channels",
                params={
                    "part": "snippet,statistics",
                    "id": ",".join(evaluated_channel_ids[offset : offset + 50]),
                    "key": api_key,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Trending channel statistics lookup failed: %s", exc)
            continue
        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            continue
        for channel in data.get("items", []):
            if isinstance(channel, dict) and isinstance(channel.get("id"), str):
                channel_details[channel["id"]] = channel

    candidates: list[dict] = []
    for channel_id in evaluated_channel_ids:
        channel = channel_details.get(channel_id, {})
        statistics = channel.get("statistics") or {}
        subscriber_count: int | None = None
        if statistics.get("hiddenSubscriberCount"):
            logger.info("Subscriber count unavailable for channel %s", channel_id)
        else:
            try:
                subscriber_count = int(statistics["subscriberCount"])
            except (KeyError, TypeError, ValueError):
                logger.info("Subscriber count unavailable for channel %s", channel_id)
        if subscriber_count is not None and (
            subscriber_count < min_subs or subscriber_count > max_subs
        ):
            continue

        metrics = _recent_upload_metrics(channel_id, api_key)
        if metrics is not None and metrics["shorts_dominate"]:
            continue
        if metrics is None:
            metrics = {
                "long_form_count": 0,
                "recent_long_form_count": 0,
                "shorts_count": 0,
                "short_to_long_ratio": 0.0,
                "shorts_dominate": False,
                "latest_long_form": None,
            }
        latest_long_form = metrics["latest_long_form"]
        latest_video_snippet = (
            (latest_long_form.get("snippet") or {})
            if latest_long_form is not None
            else {}
        )
        discovered = discovered_channels[channel_id]
        latest_video_id = (
            latest_long_form.get("id")
            if latest_long_form is not None
            else discovered.get("video_id")
        )
        if not isinstance(latest_video_id, str) or not latest_video_id:
            continue

        channel_title = str(
            (channel.get("snippet") or {}).get("title")
            or discovered.get("channel_title")
            or ""
        )
        channel_snippet = channel.get("snippet") or {}
        email = extract_email(str(channel_snippet.get("description") or ""))
        if email == "Check Channel About":
            email = extract_email(str(latest_video_snippet.get("description") or ""))
        candidates.append(
            {
                "channel_title": channel_title,
                "niche_keyword": discovered["niche_keyword"],
                "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                "subscribers": subscriber_count,
                "contact_email": email,
                "latest_video_url": f"https://www.youtube.com/watch?v={latest_video_id}",
                "latest_video_title": str(
                    latest_video_snippet.get("title")
                    or discovered.get("video_title")
                    or ""
                ),
                "duration_seconds": _duration_seconds(
                    (latest_long_form.get("contentDetails") or {}).get("duration")
                ) if latest_long_form is not None else None,
                **metrics,
            }
        )

    return sorted(
        candidates,
        key=lambda lead: (
            lead["short_to_long_ratio"],
            -lead["long_form_count"],
        ),
    )[:5]
