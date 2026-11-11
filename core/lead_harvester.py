from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from importlib import import_module
import json
import logging
import os
import re
import statistics
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from core.gemini_resilience import (
    gemini_thinking_config,
    generate_content_with_fallback,
)


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEEN_CHANNELS_PATH = PROJECT_ROOT / "storage" / "seen_channels.json"
# Why each channel was kept or rejected, and when. Channels rejected for a reason
# that can change (too few uploads, Shorts already working, ...) are checked again
# after RECHECK_DAYS; the numbers are also the data for tuning the cut-offs below.
CHANNEL_CHECKS_PATH = PROJECT_ROOT / "storage" / "channel_checks.json"
YOUTUBE_API_SERVICE = "youtube"
YOUTUBE_API_VERSION = "v3"
RECHECK_DAYS = 60

# --- Shorts gap ------------------------------------------------------------------
# What matters is whether Shorts are working for the channel *now*, not how many it
# has ever posted: 2,000 old Shorts and nothing recent is the best kind of lead.
SHORTS_MAX_SECONDS = 180  # YouTube Shorts can be up to 3 minutes (since Oct 2024)
SHORTS_LOOKBACK = 50  # most recent Shorts read per channel (one API call)
SHORTS_ACTIVE_PER_30_DAYS = 4  # about one a week: an established Shorts habit
SHORTS_FADING_RATIO = 1 / 3  # now posting a third or less of their earlier pace
SHORTS_LAPSED_MIN_TOTAL = 3  # fewer than this ever = "tried", not a lapsed habit
# "Clearly working" = their recent Shorts get at least as many views as their own
# long videos. On large channels the typical Shorts/long-form ratio is about 0.5,
# so 1.0 only flags channels where Shorts clearly carry their reach.
SHORTS_WORKING_VIEW_RATIO = 1.0
SHORTS_MIN_AGE_DAYS = 7  # newer videos are still collecting views
SHORTS_PERFORMANCE_SAMPLE = 10

# --- Who to look for (round 23, from the checks so far) --------------------------
# Subscriber range. Of 1,254 channels checked, 897 (72%) failed only on this: 469 under
# 1,000, then 250 at 1,000-5,000 and 79 at 150,000-500,000. 3,000-250,000 keeps the
# small-to-mid creators you pitch (sent: 7,250-79,100) and adds the near misses.
# LEAD_MIN_SUBSCRIBERS / LEAD_MAX_SUBSCRIBERS in .env change it.
DEFAULT_MIN_SUBSCRIBERS = 3_000
DEFAULT_MAX_SUBSCRIBERS = 250_000
# Channels turned down only for their size, whose saved count is inside the range now,
# are checked again at no search cost (channels.list), this many per search round.
RANGE_RECHECKS_PER_ROUND = 50
# Search: a lead needs 2+ long videos in the last 30 days, so search only for recent
# videos of 4+ minutes. Shorts and old uploads filled result slots before and led to
# channels that then failed "recent long videos" (182 of the checks). The two lengths
# take turns by query, so a round costs the same quota (100 units a query).
SEARCH_RECENT_DAYS = 45
SEARCH_DURATIONS = ("long", "medium")  # YouTube: long = over 20 min, medium = 4-20 min


# Round 25: which creators the worker looks for. "b2b" (the default): experts who sell
# a service (a booking link, "work with me", consulting, coaching...), not
# institutions, news, gaming, music or sports. "wide": every niche, as before.
# Set LEAD_PROFILE=wide in .env to go back.
LEAD_PROFILES = ("b2b", "wide")
B2B_MIN_SUBSCRIBERS = 1_000  # B2B podcasts are often small; the business behind them matters more
B2B_MAX_SUBSCRIBERS = 100_000


def lead_profile() -> str:
    """'b2b' (default) or 'wide', from LEAD_PROFILE in .env."""
    value = os.getenv("LEAD_PROFILE", "").strip().lower()
    return "wide" if value in {"wide", "all", "any", "off"} else "b2b"


def default_subscriber_range() -> tuple[int, int]:
    if lead_profile() == "b2b":
        return B2B_MIN_SUBSCRIBERS, B2B_MAX_SUBSCRIBERS
    return DEFAULT_MIN_SUBSCRIBERS, DEFAULT_MAX_SUBSCRIBERS


def subscriber_range() -> tuple[int, int]:
    """(min, max) subscribers for a lead: .env, else the lead profile's defaults."""
    default_low, default_high = default_subscriber_range()

    def number(name: str, default: int) -> int:
        try:
            return max(0, int(str(os.getenv(name, "")).replace(",", "").replace("_", "") or default))
        except ValueError:
            return default
    low = number("LEAD_MIN_SUBSCRIBERS", default_low)
    high = number("LEAD_MAX_SUBSCRIBERS", default_high)
    return (low, high) if low <= high else (default_low, default_high)


def _in_range_now(check: dict[str, Any]) -> bool:
    """A channel turned down only for its size, whose saved count fits the range now."""
    low, high = subscriber_range()
    count = check.get("subscribers")
    return check.get("result") == "subscribers_out_of_range" and isinstance(count, int) and low <= count <= high


# --- Target video ----------------------------------------------------------------
LONG_FORM_MIN_SECONDS = 300
LONG_FORM_MAX_SECONDS = 90 * 60
PREFERRED_MIN_SECONDS = 20 * 60  # best source length for 3 clips; never required
PREFERRED_MAX_SECONDS = 60 * 60

# Queue order: best opportunity first. Reachable creators (email found) jump ahead.
_SHORTS_PRIORITY = {
    "lapsed": 3,
    "fading": 3,
    "none": 2,
    "tried": 2,
    "occasional": 2,
    "unknown": 2,
    "active_weak": 1,
    "active_unknown": 1,
}
EMAIL_PRIORITY_BONUS = 2
PREFERRED_LENGTH_BONUS = 1
_DURATION_PATTERN = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")
_EMAIL_PATTERN = re.compile(
    r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
)
_NOT_AN_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
_CONTACT_WORDS = ("business", "inquir", "enquir", "contact", "sponsor", "collab", "booking", "partner")
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
    "B2B": (
        # Round 30: the core market first. Shows whose talk carries numbers and
        # mechanics (where the data graphics shine), not motivation or banter.
        "B2B SaaS founders and operators; venture capital and private equity investors "
        "(GPs, LPs, angels); consulting and advisory firms; finance, fintech and investing "
        "shows; CFOs, CROs, RevOps and go-to-market leaders; executive and operator interview "
        "podcasts on unit economics, pricing, fundraising, valuation, market structure, "
        "go-to-market and how institutions really work"
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


B2B_QUERY_HINT = (
    "Aim at the shows where operators and investors talk numbers: e.g. 'SaaS founder podcast ARR', "
    "'venture capital podcast fund strategy', 'go-to-market strategy podcast', 'B2B sales leadership "
    "interview', 'private equity deal breakdown', 'CFO podcast unit economics', 'fintech founder "
    "interview', 'consulting firm podcast'. Never self-help, motivation, mindset or lifestyle. "
)

# Round 30: how well a channel fits the core market, from its own words (channel
# description and recent video titles). Each core term counts once; so does each
# self-help or lightweight term. A channel with no core term and 2+ of those is off
# niche; otherwise the fit adds to its place in the queue.
CORE_TERMS = (
    "saas", "b2b", "arr", "mrr", "startup", "founder", "venture", "vc", "fund", "fundraising", "investor",
    "private equity", "m&a", "valuation", "cfo", "revenue", "go-to-market", "gtm", "sales", "pricing",
    "unit economics", "cac", "ltv", "churn", "enterprise", "consulting", "advisory", "fintech", "finance",
    "investing", "capital", "markets", "operator", "ceo", "growth", "margin", "earnings", "economy",
)
OFF_NICHE_TERMS = (
    "mindset", "motivation", "motivational", "manifest", "manifesting", "self-help", "self love", "self-love",
    "healing", "spiritual", "dating", "relationship advice", "morning routine", "life coach", "inspiration",
    "law of attraction", "affirmations", "wellness", "vlog", "reaction", "prank",
)
NICHE_FIT_MAX_BONUS = 3


def niche_fit(channel_description: str, video_titles: list[str]) -> int:
    """Core-market terms minus self-help/lightweight terms (each counted once) in the
    channel's description and recent titles."""
    text = " " + re.sub(r"\s+", " ", " ".join([channel_description, *video_titles]).lower()) + " "

    def hits(terms: tuple[str, ...]) -> int:
        return sum(1 for term in terms if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text))

    return hits(CORE_TERMS) - 2 * hits(OFF_NICHE_TERMS)


def off_niche(channel_description: str, video_titles: list[str]) -> bool:
    """No core-market term at all and 2+ self-help or lightweight ones."""
    text = " ".join([channel_description, *video_titles]).lower()
    core = any(re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text) for term in CORE_TERMS)
    light = sum(1 for term in OFF_NICHE_TERMS if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text))
    return not core and light >= 2


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
        + (B2B_QUERY_HINT if category == "B2B" else "")
        +
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


def _best_email(texts: list[str]) -> str | None:
    """The most likely business email in the texts (channel description first).

    Creators usually put "Business inquiries: ..." in their video descriptions,
    not the channel description, so both are searched. An address on a line that
    mentions business/contact/sponsors wins over a random one.
    """
    fallback: str | None = None
    for text in texts:
        for line in text.splitlines():
            for match in _EMAIL_PATTERN.finditer(line):
                email = match.group(0).rstrip(".-")
                if email.lower().endswith(_NOT_AN_EMAIL_SUFFIXES) or "." not in email.split("@")[-1]:
                    continue
                if any(word in line.casefold() for word in _CONTACT_WORDS):
                    return email
                fallback = fallback or email
    return fallback


def _channel_contacts(description: str, video_descriptions: list[str] | None = None) -> tuple[str | None, list[str]]:
    email = _best_email([description, *(video_descriptions or [])])
    social_links = list(dict.fromkeys(_URL_PATTERN.findall(description)))
    return email, social_links


# --- Round 25: does the host sell something? -----------------------------------------
_OFFER_HOSTS = (
    "calendly.com", "cal.com/", "tidycal.com", "savvycal.com", "acuityscheduling.com",
    "meetings.hubspot.com", "zcal.co", "youcanbook.me", "oncehub.com", "typeform.com",
    "skool.com", "kajabi.com", "mykajabi.com", "teachable.com", "thinkific.com", "stan.store",
    "podia.com", "systeme.io", "clickfunnels.com",
)
_OFFER_PHRASES = re.compile(
    r"\b(?:book (?:a|your|an) (?:(?:free|quick|short|discovery|strategy|intro|introductory|\d+[- ]?min(?:ute)?) ){0,2}"
    r"(?:call|consult\w*|session|meeting|demo)"
    r"|(?:strategy|discovery|sales) call|free consultation|work with (?:me|us)|hire (?:me|us)"
    r"|our services|done[- ]for[- ]you|mastermind|coaching program(?:me)?|1[:-]1 coaching"
    r"|one[- ]on[- ]one coaching|consulting (?:services|firm|agency)|we help \w+"
    r"|apply (?:now|here|to work)|free (?:audit|assessment)|speaking (?:inquiries|engagements)"
    r"|book (?:me|us) to speak|keynote speaker|fractional (?:cfo|cmo|coo|cto|executive))\b",
    re.IGNORECASE,
)
_INSTITUTION = re.compile(
    r"\b(?:university|college|school district|high school|public schools?|institute of|department of"
    r"|ministry of|government|county|city of|state of|public library|museum|hospital|health system"
    r"|church|parish|diocese|newsroom|news network|official channel|tv network|radio station"
    r"|federation|association of|society of|chamber of commerce|non-?profit|charity)\b",
    re.IGNORECASE,
)
_NEWS_TITLE = re.compile(r"\bnews\b", re.IGNORECASE)
# YouTube categories that aren't B2B experts: Film, Autos, Music, Pets, Sports, Gaming,
# Comedy, News & Politics, Nonprofits.
OFF_TOPIC_CATEGORY_IDS = frozenset({"1", "2", "10", "15", "17", "20", "23", "25", "29"})
OFF_TOPIC_MAX_SHARE = 0.5


def business_offer(description: str, video_descriptions: list[str] | None = None) -> list[str]:
    """Signs that the host sells a service, from their channel and recent video
    descriptions: booking links ('calendly.com') and phrases ('book a call',
    'work with me'). Empty when there are none."""
    found: list[str] = []
    for text in [description, *(video_descriptions or [])[:6]]:
        lower = str(text or "").lower()
        for host in _OFFER_HOSTS:
            label = host.rstrip("/")
            if host in lower and label not in found:
                found.append(label)
        for match in _OFFER_PHRASES.finditer(lower):
            phrase = " ".join(match.group(0).split())
            if phrase not in found:
                found.append(phrase)
    return found[:5]


def institution_match(title: str, description: str) -> str:
    """The word that marks a channel as an institution or news outlet ('university',
    'news'), or ''. Reads the title and the start of the description."""
    match = _INSTITUTION.search(f"{title}\n{str(description or '')[:300]}")
    if match:
        return match.group(0).lower()
    match = _NEWS_TITLE.search(title or "")
    return match.group(0).lower() if match else ""


def off_topic_share(videos: list[dict[str, Any]]) -> float:
    """Share of the videos in a non-B2B YouTube category (gaming, music, sports...)."""
    categories = [str((video.get("snippet") or {}).get("categoryId") or "") for video in videos]
    categories = [category for category in categories if category]
    if not categories:
        return 0.0
    return sum(category in OFF_TOPIC_CATEGORY_IDS for category in categories) / len(categories)


def linkedin_url(links: list[str]) -> str:
    """Their LinkedIn from the channel's links (a person's page before a company's)."""
    people = [link for link in links if "linkedin.com/in/" in link.lower()]
    companies = [link for link in links if "linkedin.com/company/" in link.lower()]
    return (people or companies or [""])[0].rstrip(".,")


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


def _read_channel_checks() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(CHANNEL_CHECKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): v for k, v in payload.items() if isinstance(v, dict)} if isinstance(payload, dict) else {}


def _write_channel_checks(checks: dict[str, dict[str, Any]]) -> None:
    _write_json_atomic(CHANNEL_CHECKS_PATH, checks)


def _recheckable_channel_ids(now: datetime | None = None) -> set[str]:
    """Channels rejected for a reason that can change, long enough ago to look again."""
    now = now or datetime.now(timezone.utc)
    ids: set[str] = set()
    for channel_id, check in _read_channel_checks().items():
        if not check.get("soft"):
            continue
        if _in_range_now(check):
            ids.add(channel_id)  # the range changed: look again now
            continue
        try:
            checked_at = datetime.fromisoformat(str(check.get("checked_at")))
        except ValueError:
            continue
        if now - checked_at >= timedelta(days=RECHECK_DAYS):
            ids.add(channel_id)
    return ids


def _read_seen_channel_ids() -> set[str]:
    """Channels not to look at again: checked recently, queued, pitched or skipped."""
    seen = _read_checked_channel_ids() - _recheckable_channel_ids()
    return seen | _read_contacted_channel_ids()


def _read_checked_channel_ids() -> set[str]:
    seen: set[str] = set()
    if SEEN_CHANNELS_PATH.exists():
        try:
            payload = json.loads(SEEN_CHANNELS_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                seen.update(str(x) for x in payload if isinstance(x, str))
        except Exception as exc:
            logger.warning("Could not read checked channel cache at %s: %s", SEEN_CHANNELS_PATH, exc)
    return seen


def _read_contacted_channel_ids() -> set[str]:
    """Creators already processed, pitched, skipped by you or waiting in the queue."""
    seen: set[str] = set()
    queue_path = PROJECT_ROOT / "storage" / "lead_queue.json"
    if queue_path.exists():
        try:
            payload = json.loads(queue_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                seen.update(str(item["channel_id"]) for item in payload if isinstance(item, dict) and item.get("channel_id"))
        except Exception as exc:
            logger.warning("Could not read the lead queue at %s: %s", queue_path, exc)

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


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise RuntimeError(f"Could not save {path}: {exc}") from exc


def _write_seen_channel_ids(channel_ids: set[str]) -> None:
    _write_json_atomic(SEEN_CHANNELS_PATH, sorted(channel_ids))


def _search_candidates(
    youtube: Any,
    query: str,
    duration: str = "any",
) -> list[dict[str, str]]:
    """Up to 50 recent videos for ``query`` (``duration``: YouTube's "long", "medium"
    or "any"), one candidate per video."""
    published_after = (datetime.now(timezone.utc) - timedelta(days=SEARCH_RECENT_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    response = _execute_youtube_request(
        youtube.search().list(
            part="snippet",
            type="video",
            q=query,
            maxResults=50,
            order="relevance",
            # A ranking hint, not a filter: non-English channels are rejected later
            # anyway, so this just wastes fewer of the 100-unit search results.
            relevanceLanguage="en",
            videoDuration=duration,
            publishedAfter=published_after,
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


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_not_found(error: Exception) -> bool:
    return getattr(getattr(error, "resp", None), "status", None) == 404


def _playlist_items(playlist_id: str, max_results: int) -> tuple[list[tuple[str, datetime | None]], int | None]:
    """(video id, publish time) for the newest items, plus the playlist's total size."""
    response = _execute_youtube_request(
        _youtube_client().playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=max_results,
        )
    )
    items: list[tuple[str, datetime | None]] = []
    for item in response.get("items", []):
        details = item.get("contentDetails") or {}
        video_id = details.get("videoId")
        if isinstance(video_id, str):
            items.append((video_id, _parse_time(details.get("videoPublishedAt"))))
    total = (response.get("pageInfo") or {}).get("totalResults")
    return items, total if isinstance(total, int) else None


@dataclass
class _ChannelUploads:
    long_ids: list[str]  # newest uploads that aren't Shorts (may still need a length check)
    long_needs_length_filter: bool  # True when they came from the all-uploads list
    shorts: list[tuple[str, datetime | None]] | None  # None = couldn't check
    shorts_total: int | None


def _channel_uploads(channel_id: str, uploads_playlist: str) -> _ChannelUploads:
    """Recent long videos and recent Shorts, one API call each (1 unit each).

    YouTube keeps per-channel lists of only long videos ("UULF" + id) and only
    Shorts ("UUSH" + id). They are unofficial, so every step falls back safely:
    no long-video list -> use all uploads and filter by length; no Shorts list
    (404) -> the channel has no Shorts; any other error -> "couldn't check",
    which never causes a skip.
    """
    suffix = channel_id[2:] if channel_id.startswith("UC") else ""
    long_items: list[tuple[str, datetime | None]] | None = None
    if suffix:
        try:
            long_items, _total = _playlist_items(f"UULF{suffix}", 10)
        except YouTubeQuotaExceededError:
            raise
        except Exception as exc:
            if not _is_not_found(exc):
                logger.info("Long-video list unavailable for %s: %s", channel_id, exc)
    needs_filter = long_items is None
    if long_items is None:
        long_items, _total = _playlist_items(uploads_playlist, 10)

    shorts: list[tuple[str, datetime | None]] | None = None
    shorts_total: int | None = None
    if suffix:
        try:
            shorts, shorts_total = _playlist_items(f"UUSH{suffix}", SHORTS_LOOKBACK)
        except YouTubeQuotaExceededError:
            raise
        except Exception as exc:
            if _is_not_found(exc):
                shorts, shorts_total = [], 0
            else:
                logger.info("Shorts list unavailable for %s: %s", channel_id, exc)
    return _ChannelUploads([video_id for video_id, _ in long_items], needs_filter, shorts, shorts_total)


def _settled_short_ids(shorts: list[tuple[str, datetime | None]] | None, now: datetime) -> list[str]:
    """Newest Shorts that are at least a week old (their views have mostly settled)."""
    dated = sorted(
        ((now - published).total_seconds() / 86400, video_id)
        for video_id, published in shorts or []
        if published is not None
    )
    return [video_id for age, video_id in dated if age >= SHORTS_MIN_AGE_DAYS][:SHORTS_PERFORMANCE_SAMPLE]


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _ago(days: float) -> str:
    if days < 60:
        return f"{int(days)} days ago"
    if days < 730:
        return f"{int(days // 30)} months ago"
    return f"{days / 365:.1f} years ago"


def shorts_profile(
    shorts: list[tuple[str, datetime | None]] | None,
    shorts_total: int | None,
    now: datetime,
    short_views: dict[str, int] | None = None,
    long_view_counts: list[int] | None = None,
) -> dict[str, Any]:
    """Classify how the channel uses Shorts *now*.

    Statuses: none, tried, lapsed (stopped), fading (slowing down), occasional,
    active_weak, active_unknown, active_working (the only one that is skipped),
    unknown (couldn't check; never skipped).
    """
    if shorts is None:
        return {"status": "unknown", "summary": "Shorts: couldn't check"}
    dated = sorted(
        ((now - published).total_seconds() / 86400, video_id)
        for video_id, published in shorts
        if published is not None
    )
    total = max(shorts_total or 0, len(shorts))
    if not dated:
        return {"status": "none", "total": 0, "summary": "No Shorts yet"}

    ages = [age for age, _ in dated]
    recent = sum(age <= 30 for age in ages)
    last_days = ages[0]
    # Earlier pace (per 30 days) over days 31-120. If the 50 newest Shorts don't
    # reach back that far, use the part they cover.
    covered = min(120.0, ages[-1]) if len(shorts) >= SHORTS_LOOKBACK else 120.0
    prior_span = covered - 30
    prior_rate = (
        sum(30 < age <= 120 for age in ages) / (prior_span / 30) if prior_span >= 15 else None
    )
    profile: dict[str, Any] = {
        "total": total,
        "last_short_days": round(last_days, 1),
        "recent_30_days": recent,
        "earlier_per_30_days": round(prior_rate, 1) if prior_rate is not None else None,
    }

    if recent == 0:
        if total >= SHORTS_LAPSED_MIN_TOTAL:
            profile.update(status="lapsed", summary=f"Stopped Shorts: last one {_ago(last_days)} ({total:,} total)")
        else:
            profile.update(status="tried", summary=f"Tried Shorts ({total}), last one {_ago(last_days)}")
        return profile
    if (
        prior_rate is not None
        and prior_rate >= SHORTS_ACTIVE_PER_30_DAYS
        and recent <= prior_rate * SHORTS_FADING_RATIO
    ):
        profile.update(
            status="fading",
            summary=f"Shorts slowing down: {recent} in the last 30 days, about {prior_rate:.0f}/month before",
        )
        return profile
    if recent < SHORTS_ACTIVE_PER_30_DAYS:
        profile.update(status="occasional", summary=f"Occasional Shorts: {recent} in the last 30 days")
        return profile

    # Active: are their Shorts clearly working? Compare settled Shorts (a week or
    # older) with the views of their own recent long videos.
    settled = _settled_short_ids(shorts, now)
    views = [short_views[v] for v in settled if short_views and v in short_views]
    short_median = _median([float(v) for v in views]) if len(views) >= 3 else None
    long_median = _median([float(v) for v in long_view_counts or []])
    if short_median is None or not long_median:
        profile.update(status="active_unknown", summary=f"Active Shorts: {recent} in the last 30 days (too new to judge views)")
        return profile
    ratio = short_median / long_median
    profile.update(view_ratio=round(ratio, 2), shorts_median_views=int(short_median), long_median_views=int(long_median))
    if ratio >= SHORTS_WORKING_VIEW_RATIO:
        profile.update(
            status="active_working",
            summary=f"Shorts already working: {recent} in 30 days, {ratio:.1f}x their long-video views",
        )
    else:
        profile.update(
            status="active_weak",
            summary=f"Active but weak Shorts: {recent} in 30 days, {ratio:.0%} of their long-video views",
        )
    return profile


def needs_short_views(profile_without_views: dict[str, Any]) -> bool:
    return profile_without_views.get("status") == "active_unknown"


def choose_target_video(
    videos: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Newest 20-60 min upload; otherwise the newest 12-90 min; otherwise 5-90 min.

    Nothing is rejected for length beyond what was accepted before; this only
    picks the best of the channel's recent videos.
    """
    def duration(video: dict[str, Any]) -> int | None:
        return _duration_seconds((video.get("contentDetails") or {}).get("duration"))

    for low, high in (
        (PREFERRED_MIN_SECONDS, PREFERRED_MAX_SECONDS),
        (12 * 60, LONG_FORM_MAX_SECONDS),
        (LONG_FORM_MIN_SECONDS + 1, LONG_FORM_MAX_SECONDS),
    ):
        for video in videos:  # newest first
            seconds = duration(video)
            if seconds is not None and low <= seconds <= high:
                return video
    return None


def lead_priority(shorts_status: str, has_email: bool, preferred_length: bool) -> int:
    return (
        _SHORTS_PRIORITY.get(shorts_status, 2)
        + (EMAIL_PRIORITY_BONUS if has_email else 0)
        + (PREFERRED_LENGTH_BONUS if preferred_length else 0)
    )


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
    b2b = lead_profile() == "b2b"
    checks = _read_channel_checks()
    now = datetime.now(timezone.utc)
    low_subscribers, high_subscribers = subscriber_range()
    funnel: Counter[str] = Counter()
    contacted = _read_contacted_channel_ids()
    rechecks = [
        {"channel_id": channel_id, "video_id": "", "channel_title": "", "video_title": ""}
        for channel_id, check in checks.items() if _in_range_now(check) and channel_id not in contacted
    ][:RANGE_RECHECKS_PER_ROUND]

    def record(channel_id: str, result: str, soft: bool = True, **details: Any) -> None:
        checks[channel_id] = {"checked_at": now.isoformat(), "result": result, "soft": soft, **details}
        funnel[result] += 1

    def views(video: dict[str, Any]) -> int | None:
        try:
            return int((video.get("statistics") or {})["viewCount"])
        except (KeyError, TypeError, ValueError):
            return None

    def age_days(video: dict[str, Any]) -> float | None:
        published = _parse_time((video.get("snippet") or {}).get("publishedAt"))
        return (now - published).total_seconds() / 86400 if published else None

    with ThreadPoolExecutor(max_workers=5) as executor:
        for query_offset in range(0, len(queries), 5):
            query_batch = queries[query_offset : query_offset + 5]
            search_futures = {
                executor.submit(
                    _search_candidates,
                    _youtube_client(),
                    query.strip(),
                    SEARCH_DURATIONS[(query_offset + position) % len(SEARCH_DURATIONS)],
                ): query
                for position, query in enumerate(query_batch)
            }
            candidates: dict[str, dict[str, str]] = {}
            if query_offset == 0:  # turned down for size earlier, inside the range now: no search needed
                for candidate in rechecks:
                    candidates.setdefault(candidate["channel_id"], candidate)
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

            eligible_channels: dict[str, tuple[dict[str, str], dict[str, Any], int | None, str]] = {}
            for channel_id, candidate in candidates.items():
                channel = channel_metadata.get(channel_id)
                if channel is None:  # deleted or hidden: recorded, so a size re-check isn't retried each round
                    record(channel_id, "channel_not_found")
                    continue
                subscriber_count = _channel_subscriber_count(channel)
                if subscriber_count == -1 or (
                    subscriber_count is not None
                    and not (low_subscribers <= subscriber_count <= high_subscribers)
                ):
                    record(channel_id, "subscribers_out_of_range", subscribers=subscriber_count)
                    continue
                uploads_playlist = (
                    (channel.get("contentDetails") or {})
                    .get("relatedPlaylists", {})
                    .get("uploads")
                )
                if not isinstance(uploads_playlist, str):
                    record(channel_id, "no_uploads_list")
                    continue
                eligible_channels[channel_id] = (candidate, channel, subscriber_count, uploads_playlist)

            upload_futures = {
                executor.submit(_channel_uploads, channel_id, info[3]): channel_id
                for channel_id, info in eligible_channels.items()
            }
            uploads_by_channel: dict[str, _ChannelUploads] = {}
            for future in as_completed(upload_futures):
                channel_id = upload_futures[future]
                try:
                    uploads_by_channel[channel_id] = future.result()
                except YouTubeQuotaExceededError:
                    raise
                except Exception:
                    logger.exception("Could not load recent uploads for channel %s", channel_id)

            # Shorts posting pattern first (no extra calls); view counts are only
            # fetched for channels that post Shorts actively, where they decide.
            profiles = {
                channel_id: shorts_profile(uploads.shorts, uploads.shorts_total, now)
                for channel_id, uploads in uploads_by_channel.items()
            }
            needed_ids = list(
                dict.fromkeys(
                    [video_id for uploads in uploads_by_channel.values() for video_id in uploads.long_ids]
                    + [
                        video_id
                        for channel_id, uploads in uploads_by_channel.items()
                        if needs_short_views(profiles[channel_id])
                        for video_id in _settled_short_ids(uploads.shorts, now)
                    ]
                )
            )
            videos_by_id: dict[str, dict[str, Any]] = {}
            for offset in range(0, len(needed_ids), 50):
                video_batch = needed_ids[offset : offset + 50]
                response = _execute_youtube_request(
                    _youtube_client().videos().list(
                        part="snippet,contentDetails,statistics",
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

            for channel_id, (candidate, channel, subscriber_count, _uploads_playlist) in eligible_channels.items():
                uploads = uploads_by_channel.get(channel_id)
                if uploads is None:
                    record(channel_id, "check_failed")
                    continue
                recent_videos = [videos_by_id[v] for v in uploads.long_ids if v in videos_by_id]
                if uploads.long_needs_length_filter:
                    recent_videos = [
                        video
                        for video in recent_videos
                        if (_duration_seconds((video.get("contentDetails") or {}).get("duration")) or 0)
                        > SHORTS_MAX_SECONDS
                    ]
                if not recent_videos:
                    record(channel_id, "no_recent_long_videos")
                    continue

                if not _is_english_channel(channel, recent_videos):
                    record(channel_id, "not_english", soft=False)
                    continue

                recent_durations = [
                    _duration_seconds((video.get("contentDetails") or {}).get("duration"))
                    for video in recent_videos
                ]

                # Active long-form production: at least 2 long videos (>5 min) in the last 30 days
                long_form_durations: list[int] = []
                recent_30_day_long_form_count = 0
                for video, duration in zip(recent_videos, recent_durations):
                    if duration is not None and duration > LONG_FORM_MIN_SECONDS:
                        long_form_durations.append(duration)
                        age = age_days(video)
                        if age is not None and age <= 30:
                            recent_30_day_long_form_count += 1
                if recent_30_day_long_form_count < 2:
                    record(channel_id, "too_few_recent_long_videos", recent_long_videos=recent_30_day_long_form_count)
                    continue

                # Avoid raw stream dumps: average long-form duration must not exceed 90 minutes
                if not long_form_durations or sum(long_form_durations) / len(long_form_durations) > LONG_FORM_MAX_SECONDS:
                    record(channel_id, "mostly_very_long_videos")
                    continue

                snippet = channel.get("snippet") or {}
                channel_description = str(snippet.get("description") or "")
                video_descriptions = [str((video.get("snippet") or {}).get("description") or "") for video in recent_videos]
                offer = business_offer(channel_description, video_descriptions)
                if b2b:
                    institution = institution_match(str(snippet.get("title") or candidate["channel_title"]), channel_description)
                    if institution:
                        record(channel_id, "institution_or_news", soft=False, matched=institution)
                        continue
                    if off_topic_share(recent_videos) > OFF_TOPIC_MAX_SHARE:
                        record(channel_id, "off_topic_category")
                        continue
                    titles = [str((video.get("snippet") or {}).get("title") or "") for video in recent_videos]
                    if off_niche(channel_description, titles):
                        record(channel_id, "off_niche")  # soft: self-help or lightweight, not the core market
                        continue
                    if not offer:
                        record(channel_id, "no_business_offer")  # soft: they may add a booking link later
                        continue

                profile = profiles.get(channel_id) or {"status": "unknown", "summary": "Shorts: couldn't check"}
                if needs_short_views(profile):
                    short_views = {
                        video_id: count
                        for video_id in _settled_short_ids(uploads.shorts, now)
                        if video_id in videos_by_id and (count := views(videos_by_id[video_id])) is not None
                    }
                    settled_long = [
                        count
                        for video in recent_videos
                        if (age := age_days(video)) is not None
                        and age >= SHORTS_MIN_AGE_DAYS
                        and (count := views(video)) is not None
                    ]
                    if len(settled_long) < 2:
                        settled_long = [count for video in recent_videos if (count := views(video)) is not None]
                    profile = shorts_profile(uploads.shorts, uploads.shorts_total, now, short_views, settled_long)
                if profile["status"] == "active_working":
                    record(channel_id, "shorts_already_working", shorts=profile)
                    continue

                target_video = choose_target_video(recent_videos)
                target_id = target_video.get("id") if target_video else None
                if target_video is None or not isinstance(target_id, str):
                    record(channel_id, "no_suitable_video")
                    continue

                channel_title = str(snippet.get("title") or candidate["channel_title"])
                email, social_links = _channel_contacts(channel_description, video_descriptions)
                channel_about_url = f"https://www.youtube.com/channel/{channel_id}/about"
                target_snippet = target_video.get("snippet") or {}
                target_seconds = _duration_seconds((target_video.get("contentDetails") or {}).get("duration"))
                preferred_length = (
                    target_seconds is not None
                    and PREFERRED_MIN_SECONDS <= target_seconds <= PREFERRED_MAX_SECONDS
                )
                priority = lead_priority(str(profile["status"]), bool(email), preferred_length)
                if b2b:  # the core market (SaaS, VC, consulting, finance) goes first
                    fit = niche_fit(channel_description,
                                    [str((video.get("snippet") or {}).get("title") or "") for video in recent_videos])
                    priority += max(0, min(NICHE_FIT_MAX_BONUS, fit // 2))
                qualified.append(
                    {
                        "channel_id": channel_id,
                        "channel_title": channel_title,
                        "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                        "subscriber_count": subscriber_count,
                        "latest_video_url": f"https://www.youtube.com/watch?v={target_id}",
                        "latest_video_title": str(target_snippet.get("title") or ""),
                        "duration_seconds": target_seconds,
                        "contact_email": email,
                        "contact_info": email or channel_about_url,
                        "channel_about_url": channel_about_url,
                        "social_links": social_links,
                        "shorts": profile,
                        "shorts_summary": str(profile["summary"]),
                        "priority": priority,
                        "business_offer": offer,
                        "linkedin_url": linkedin_url(social_links),
                        # Round 53: buyer fit (core.fit) reads what the channel says it does
                        "description": channel_description[:600],
                    }
                )
                record(channel_id, "qualified", shorts=profile, priority=priority, has_email=bool(email))

            _write_channel_checks(checks)
            # Every creator that qualified in this batch is kept (they are already
            # marked as checked); only stop *starting* new batches once there are enough.
            if len(qualified) >= max_results:
                break
    if funnel:
        logger.info(
            "Search funnel: %d channel(s) checked (%d re-checked for the new size range): %s",
            sum(funnel.values()), len(rechecks),
            ", ".join(f"{count} {result.replace('_', ' ')}" for result, count in funnel.most_common()),
        )
    return qualified
