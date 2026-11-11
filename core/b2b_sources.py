"""Round 47: where the B2B harvester finds shows, and what it remembers between runs.

Every source returns "seeds": small dicts the harvester enriches into prospects
(core.linkedin_harvester.build_prospect). A seed has a ``source`` and at least one of
``feed_url`` (a podcast RSS feed), ``channel_id`` / ``youtube_url`` (a YouTube channel)
or ``website`` (a company site that may have a show).

Sources (all public; keys only where noted):
- apple_search: Apple Podcasts search over the niche x format keyword matrix (~180 phrases).
- apple_charts: Apple's top-200 chart in each business / tech genre (looked up for feeds).
- gpodder: gpodder.net's public tag directories (business, marketing, investing...).
- seed_pages: pages you list in storage/b2b_seed_pages.txt ("best B2B podcasts" lists,
  agency rosters, VC portfolios, conference agendas): every Apple Podcasts, RSS and YouTube
  link on them.
- yc: Y Combinator's active B2B companies (the public yc-oss mirror), checked for a show.
- youtube: YouTube Data API search for long B2B videos (100 quota units per phrase, so one
  phrase per run by default: B2B_YOUTUBE_SEARCHES in .env).
- podcastindex: Podcast Index search (free key: PODCASTINDEX_API_KEY / PODCASTINDEX_API_SECRET).

Not sources, on purpose: LinkedIn (no public API; scraping breaks its terms and gets accounts
banned), Podchaser / Rephonic / Listen Notes (paid or keyed APIs; add a key-based connector here
if you subscribe), Substack (its search endpoint returns nothing to scripts), and web searches
like "inurl:webinar" (they need a search API). Hosting platforms, webinar tools (Wistia,
Vidyard, ON24, Goldcast...) and podcast agencies are read as signals on each prospect instead.

The state (storage/b2b_harvest_state.json): a cursor per source and niche, so each run starts
where the last one stopped; every show already looked at and why it was kept or dropped (so
it isn't fetched again for months); a backlog of shows found but not yet enriched; the funnel
of the last runs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = PROJECT_ROOT / "storage" / "b2b_harvest_state.json"
SEED_PAGES_PATH = PROJECT_ROOT / "storage" / "b2b_seed_pages.txt"

# ------------------------------------------------------------------------------ niches
NICHES: dict[str, tuple[str, ...]] = {
    "saas": ("B2B SaaS", "SaaS founders", "SaaS growth"),
    "revops": ("revenue operations", "RevOps", "sales operations"),
    "b2b_marketing": ("B2B marketing", "demand generation", "product marketing", "content marketing B2B"),
    "sales": ("enterprise sales", "sales leadership", "B2B sales"),
    "finance": ("CFO", "finance leaders", "FP&A"),
    "fintech": ("fintech", "payments industry", "banking innovation"),
    "private_equity": ("private equity", "M&A", "dealmakers"),
    "venture": ("venture capital", "startup investors", "VC"),
    "supply_chain": ("supply chain", "logistics leadership", "procurement"),
    "cybersecurity": ("cybersecurity leadership", "CISO"),
    "healthtech": ("healthtech", "digital health leaders"),
    "legaltech": ("legal tech", "law firm leadership", "startup law"),
    "hr": ("HR tech", "people leaders", "talent acquisition"),
    "ai": ("AI for business", "enterprise AI", "AI infrastructure"),
    "devtools": ("developer tools", "DevOps leadership", "CTO"),
    "real_estate": ("commercial real estate", "proptech"),
    "manufacturing": ("manufacturing leadership", "industrial B2B"),
    "consulting": ("management consulting", "consulting firm"),
    "agency": ("agency growth", "agency owners"),
    "leadership": ("CEO interviews", "executive leadership", "founder stories"),
    "wealth": ("wealth management", "financial advisors"),
    "insurance": ("insurtech", "insurance leaders"),
    "customer_success": ("customer success", "customer experience leaders"),
    "data": ("data leaders", "analytics leadership"),
}
FORMATS = ("podcast", "interviews", "show")
# Apple chart genres: Business, Entrepreneurship, Management, Marketing, Investing, Careers,
# Business News, Technology.
CHART_GENRES = {"business": 1321, "entrepreneurship": 1493, "management": 1491, "marketing": 1492,
                "investing": 1412, "careers": 1410, "business_news": 1490, "technology": 1318}
NICHE_GENRES = {
    "finance": ("investing", "business"), "fintech": ("investing", "technology"), "private_equity": ("investing",),
    "venture": ("investing", "entrepreneurship"), "wealth": ("investing",), "b2b_marketing": ("marketing",),
    "sales": ("marketing", "business"), "revops": ("marketing", "business"), "leadership": ("management", "careers"),
    "consulting": ("management",), "hr": ("management", "careers"), "saas": ("technology", "entrepreneurship"),
    "ai": ("technology",), "devtools": ("technology",), "cybersecurity": ("technology",), "data": ("technology",),
}
GPODDER_TAGS = ("business", "marketing", "investing", "technology", "management", "entrepreneurship", "finance",
                "leadership", "sales", "startup")
# The order they take turns in. Apple's charts last: they are mostly big media shows.
SOURCES = ("apple_search", "seed_pages", "youtube", "yc", "podcastindex", "gpodder", "apple_charts")
# Cursor steps per source per run (one step = one query, one chart, one page, ...).
DEFAULT_STEPS = {"apple_search": 10, "apple_charts": 1, "seed_pages": 2, "gpodder": 1, "yc": 1, "youtube": 1,
                 "podcastindex": 3}
YC_BATCH = 30
YC_URL = "https://yc-oss.github.io/api/industries/b2b.json"
SEEN_DAYS = 120  # a show looked at stays skipped this long
RETRY_DAYS = 7  # ... unless it failed to load: then it's tried again after a week
RETRY_REASONS = {"fetch_failed", "no_feed", "error"}
BACKLOG_MAX = 500

APPLE_ID = re.compile(r"(?:podcasts|itunes)\.apple\.com/[^\s\"'<>]*?/id(\d{6,12})", re.I)
FEED_LINK = re.compile(r"https?://[^\s\"'<>]+?(?:\.rss|\.xml|/feed/?|/rss/?|/podcast\.xml)(?=[\s\"'<>?#]|$)", re.I)
FEED_HOSTS = re.compile(r"https?://(?:feeds\.|anchor\.fm/s/|rss\.|[^/\s\"'<>]*\.(?:transistor\.fm|simplecast\.com|"
                        r"captivate\.fm|buzzsprout\.com|libsyn\.com|podbean\.com|megaphone\.fm|omny\.fm|art19\.com|"
                        r"acast\.com|redcircle\.com|castos\.com|bcast\.fm|rss\.com)/)[^\s\"'<>]+", re.I)
YT_CHANNEL = re.compile(r"https?://(?:www\.)?youtube\.com/(?:channel/UC[A-Za-z0-9_-]{22}|@[A-Za-z0-9._-]{3,})", re.I)


def niche_names() -> list[str]:
    return sorted(NICHES)


def keyword_matrix(niche: str | None = None) -> list[str]:
    """Every search phrase for a niche (all niches when None), interleaved so that the first
    phrases of a run cover many niches rather than one."""
    chosen = [niche] if niche else sorted(NICHES)
    unknown = [n for n in chosen if n not in NICHES]
    if unknown:
        raise ValueError(f"unknown niche {unknown[0]!r}: choose from {', '.join(sorted(NICHES))}")
    columns = [[f"{term} {fmt}" for term in NICHES[n] for fmt in FORMATS] for n in chosen]
    out: list[str] = []
    for row in range(max(len(c) for c in columns)):
        out.extend(c[row] for c in columns if row < len(c))
    return out


# ------------------------------------------------------------------------------ state
def _now() -> datetime:
    return datetime.now(timezone.utc)


def state_path_for(prospects_path: Path | None) -> Path:
    return prospects_path.with_name("b2b_harvest_state.json") if prospects_path else STATE_PATH


def load_state(path: Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads((path or STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key, empty in (("cursors", {}), ("seen", {}), ("backlog", []), ("pages_done", {}), ("runs", [])):
        if not isinstance(data.get(key), type(empty)):
            data[key] = empty
    return data


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    target = path or STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    state["backlog"] = state.get("backlog", [])[:BACKLOG_MAX]
    state["runs"] = state.get("runs", [])[-20:]
    with tempfile.NamedTemporaryFile("w", delete=False, dir=target.parent, suffix=".tmp", encoding="utf-8") as tmp:
        json.dump(state, tmp, indent=1)
    Path(tmp.name).replace(target)


def seed_key(seed: dict[str, Any]) -> str:
    """One key per show: its feed, else its YouTube channel, else its website's domain."""
    if seed.get("feed_url"):
        return "feed:" + str(seed["feed_url"]).strip().lower().rstrip("/")
    if seed.get("channel_id"):
        return "yt:" + str(seed["channel_id"])
    if seed.get("youtube_url"):
        return "yt:" + str(seed["youtube_url"]).lower().rstrip("/")
    if seed.get("website"):
        host = urllib.parse.urlparse(str(seed["website"]) if "//" in str(seed["website"]) else "https://" + str(seed["website"])).netloc
        return "web:" + host.lower().removeprefix("www.")
    return "seed:" + hashlib.sha1(json.dumps(seed, sort_keys=True).encode()).hexdigest()[:12]


def is_seen(state: dict[str, Any], key: str, now: datetime | None = None) -> bool:
    record = state["seen"].get(key)
    if not isinstance(record, dict):
        return False
    try:
        when = datetime.fromisoformat(str(record.get("at")))
    except ValueError:
        return False
    days = RETRY_DAYS if record.get("why") in RETRY_REASONS else SEEN_DAYS
    return (now or _now()) - when < timedelta(days=days)


def mark_seen(state: dict[str, Any], key: str, why: str) -> None:
    state["seen"][key] = {"at": _now().isoformat(), "why": why}


def _cursor(state: dict[str, Any], name: str) -> int:
    value = state["cursors"].get(name, 0)
    return value if isinstance(value, int) and value >= 0 else 0


# ------------------------------------------------------------------------------ helpers
def _fetch(url: str, headers: dict[str, str] | None = None, max_bytes: int = 2_000_000) -> str | None:
    from core import linkedin_harvester as LH

    return LH.fetch(url, headers, max_bytes=max_bytes)


def _json(url: str, headers: dict[str, str] | None = None, max_bytes: int = 4_000_000) -> Any:
    raw = _fetch(url, headers, max_bytes=max_bytes)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def apple_lookup(ids: list[str], source: str) -> list[dict[str, Any]]:
    """Apple Podcasts ids -> seeds with their RSS feeds (100 per request)."""
    out: list[dict[str, Any]] = []
    unique = list(dict.fromkeys(str(i) for i in ids if str(i).isdigit()))
    for start in range(0, len(unique), 100):
        data = _json("https://itunes.apple.com/lookup?" + urllib.parse.urlencode(
            {"id": ",".join(unique[start:start + 100]), "entity": "podcast", "country": "US"})) or {}
        for item in data.get("results") or [] if isinstance(data, dict) else []:
            if item.get("feedUrl") and (item.get("kind") == "podcast" or item.get("wrapperType") == "track"):
                out.append(_apple_seed(item, source))
    return out


def _apple_seed(item: dict[str, Any], source: str) -> dict[str, Any]:
    return {"source": source, "show": item.get("collectionName") or "", "author": item.get("artistName") or "",
            "feed_url": item["feedUrl"], "apple_url": item.get("collectionViewUrl") or "",
            "genres": sorted({str(g).lower() for g in item.get("genres") or []}),
            "episodes_total": item.get("trackCount")}


def links_on_page(page: str, base: str = "") -> dict[str, list[str]]:
    """The Apple Podcasts ids, RSS feeds and YouTube channels a page links to."""
    page = page.replace("\\/", "/")
    feeds = sorted({m.group(0).rstrip(".,)") for m in FEED_HOSTS.finditer(page)}
                   | {m.group(0).rstrip(".,)") for m in FEED_LINK.finditer(page)
                      if not re.search(r"/(?:comments|wp-json)/|youtube\.com|\.(?:css|js)\b", m.group(0))})
    channels = sorted({m.group(0).rstrip("/") for m in YT_CHANNEL.finditer(page)})
    return {"apple_ids": list(dict.fromkeys(APPLE_ID.findall(page))), "feeds": feeds, "youtube": channels}


# ------------------------------------------------------------------------------ the sources
SourceFn = Callable[[dict[str, Any], str | None], list[dict[str, Any]]]


def source_apple_search(state: dict[str, Any], niche: str | None) -> list[dict[str, Any]]:
    from core import linkedin_harvester as LH

    phrases = keyword_matrix(niche)
    name = f"apple_search:{niche or 'all'}"
    index = _cursor(state, name)
    state["cursors"][name] = index + 1
    phrase = phrases[index % len(phrases)]
    seeds = LH.search_podcasts(phrase, 200)
    for seed in seeds:
        seed.update(source="apple_search", query=phrase)
    return seeds


def source_apple_charts(state: dict[str, Any], niche: str | None) -> list[dict[str, Any]]:
    genres = list(NICHE_GENRES.get(niche or "", ())) or list(CHART_GENRES)
    name = f"apple_charts:{niche or 'all'}"
    index = _cursor(state, name)
    state["cursors"][name] = index + 1
    genre = genres[index % len(genres)]
    data = _json(f"https://itunes.apple.com/us/rss/toppodcasts/limit=200/genre={CHART_GENRES[genre]}/json") or {}
    entries = ((data.get("feed") or {}).get("entry") or []) if isinstance(data, dict) else []
    ids = [((e.get("id") or {}).get("attributes") or {}).get("im:id", "") for e in entries if isinstance(e, dict)]
    rank = {str(i): n + 1 for n, i in enumerate(ids)}
    seeds = apple_lookup(ids, "apple_charts")
    for seed in seeds:
        seed["query"] = f"Apple top 200: {genre.replace('_', ' ')}"
        apple_id = re.search(r"/id(\d+)", seed.get("apple_url") or "")
        if apple_id and apple_id.group(1) in rank:
            seed["chart_rank"] = rank[apple_id.group(1)]
    return seeds


def source_gpodder(state: dict[str, Any], niche: str | None) -> list[dict[str, Any]]:
    tags = [t for t in GPODDER_TAGS if niche and t in niche] or list(GPODDER_TAGS)
    name = f"gpodder:{niche or 'all'}"
    index = _cursor(state, name)
    state["cursors"][name] = index + 1
    tag = tags[index % len(tags)]
    data = _json(f"https://gpodder.net/api/2/tag/{tag}/100.json") or []
    return [{"source": "gpodder", "show": d.get("title") or "", "author": d.get("author") or "",
             "feed_url": d["url"], "website": d.get("website") or "", "genres": [tag], "query": f"gpodder tag {tag}"}
            for d in data if isinstance(d, dict) and str(d.get("url", "")).startswith("http")]


def seed_pages(path: Path | None = None) -> list[str]:
    try:
        lines = (path or SEED_PAGES_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [l.strip() for l in lines if l.strip().startswith("http")]


def source_seed_pages(state: dict[str, Any], niche: str | None, path: Path | None = None) -> list[dict[str, Any]]:
    """The next listed page not read in the last 30 days."""
    for url in seed_pages(path):
        done = state["pages_done"].get(url)
        try:
            if done and _now() - datetime.fromisoformat(done) < timedelta(days=30):
                continue
        except ValueError:
            pass
        state["pages_done"][url] = _now().isoformat()
        page = _fetch(url, max_bytes=4_000_000)
        if not page:
            return []
        found = links_on_page(page, url)
        seeds = apple_lookup(found["apple_ids"], "seed_pages")
        seeds += [{"source": "seed_pages", "show": "", "feed_url": f} for f in found["feeds"]]
        seeds += [{"source": "seed_pages", "show": "", "youtube_url": y} for y in found["youtube"]]
        for seed in seeds:
            seed["query"] = f"listed on {urllib.parse.urlparse(url).netloc}"
        return seeds
    return []


def source_yc(state: dict[str, Any], niche: str | None) -> list[dict[str, Any]]:
    """The next batch of active YC B2B companies (10-2,000 people) with a website."""
    data = _json(YC_URL, max_bytes=12_000_000) or []
    companies = [c for c in data if isinstance(c, dict) and c.get("status") == "Active" and c.get("website")
                 and 10 <= int(c.get("team_size") or 0) <= 2000]
    if niche:
        words = {w.lower() for term in NICHES[niche] for w in term.split() if len(w) > 2}
        companies = [c for c in companies if words & set(re.findall(r"[a-z&]+", " ".join(
            [c.get("subindustry") or "", " ".join(c.get("tags") or []), c.get("one_liner") or ""]).lower()))]
    companies.sort(key=lambda c: int(c.get("id") or 0))
    if not companies:
        return []
    name = f"yc:{niche or 'all'}"
    index = _cursor(state, name)
    state["cursors"][name] = index + YC_BATCH
    batch = [companies[(index + k) % len(companies)] for k in range(min(YC_BATCH, len(companies)))]
    return [{"source": "yc", "show": c.get("name") or "", "author": c.get("name") or "", "website": c["website"],
             "description": c.get("one_liner") or "", "genres": ["business"], "query": f"YC {c.get('batch', '')}".strip()}
            for c in batch]


def youtube_searches_per_run() -> int:
    try:
        return max(0, int(os.getenv("B2B_YOUTUBE_SEARCHES", "1")))
    except ValueError:
        return 1


def source_youtube(state: dict[str, Any], niche: str | None, client: Any = None) -> list[dict[str, Any]]:
    """Long videos (over 20 minutes, last 45 days) for the next phrase: one seed per channel."""
    try:
        from autonomous_worker import is_youtube_quota_exhausted

        if client is None and is_youtube_quota_exhausted():
            return []
    except Exception:  # noqa: BLE001
        pass
    phrases = keyword_matrix(niche)
    name = f"youtube:{niche or 'all'}"
    index = _cursor(state, name)
    state["cursors"][name] = index + 1
    phrase = phrases[index % len(phrases)].replace(" show", " interview")
    try:
        from core import lead_harvester as LH

        client = client or LH._youtube_client()
        response = LH._execute_youtube_request(client.search().list(
            part="snippet", q=phrase, type="video", videoDuration="long", maxResults=50, relevanceLanguage="en",
            regionCode="US", publishedAfter=(_now() - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    except Exception as exc:  # noqa: BLE001 - no key, quota: the other sources still run
        logger.info("YouTube search skipped: %s", exc)
        return []
    seeds: dict[str, dict[str, Any]] = {}
    for item in response.get("items") or []:
        snippet = item.get("snippet") or {}
        channel = snippet.get("channelId")
        if channel and channel not in seeds:
            seeds[channel] = {"source": "youtube", "show": snippet.get("channelTitle") or "", "channel_id": channel,
                              "author": snippet.get("channelTitle") or "", "query": phrase, "genres": ["business"]}
    return list(seeds.values())


def podcastindex_keys() -> tuple[str, str]:
    return os.getenv("PODCASTINDEX_API_KEY", "").strip(), os.getenv("PODCASTINDEX_API_SECRET", "").strip()


def source_podcastindex(state: dict[str, Any], niche: str | None) -> list[dict[str, Any]]:
    key, secret = podcastindex_keys()
    if not (key and secret):
        return []
    phrases = keyword_matrix(niche)
    name = f"podcastindex:{niche or 'all'}"
    index = _cursor(state, name)
    state["cursors"][name] = index + 1
    phrase = phrases[index % len(phrases)]
    stamp = str(int(time.time()))
    headers = {"X-Auth-Key": key, "X-Auth-Date": stamp,
               "Authorization": hashlib.sha1((key + secret + stamp).encode()).hexdigest()}
    data = _json("https://api.podcastindex.org/api/1.0/search/byterm?" + urllib.parse.urlencode(
        {"q": phrase, "max": 100}), headers) or {}
    cutoff = time.time() - 45 * 86400
    out = []
    for feed in data.get("feeds") or [] if isinstance(data, dict) else []:
        categories = " ".join((feed.get("categories") or {}).values()).lower()
        updated = feed.get("newestItemPubdate") or feed.get("lastUpdateTime") or 0
        if feed.get("url") and (not updated or updated >= cutoff) and re.search(
                r"business|technology|investing|management|marketing|entrepreneur|careers", categories):
            out.append({"source": "podcastindex", "show": feed.get("title") or "", "author": feed.get("author") or "",
                        "feed_url": feed["url"], "website": feed.get("link") or "", "query": phrase,
                        "genres": sorted(set(re.findall(r"[a-z]+", categories)))})
    return out


SOURCE_FNS: dict[str, SourceFn] = {
    "apple_search": source_apple_search, "apple_charts": source_apple_charts, "seed_pages": source_seed_pages,
    "gpodder": source_gpodder, "yc": source_yc, "youtube": source_youtube, "podcastindex": source_podcastindex,
}


def steps_for(source: str) -> int:
    if source == "youtube":
        return youtube_searches_per_run()
    return DEFAULT_STEPS.get(source, 1)


def parse_sources(text: str | list[str] | None) -> list[str]:
    """"all" / None -> every source; else a comma list of source names."""
    if not text:
        return list(SOURCES)
    items = text if isinstance(text, list) else re.split(r"[,\s]+", text)
    chosen = [s.strip().lower() for s in items if s and s.strip()]
    if not chosen or "all" in chosen:
        return list(SOURCES)
    unknown = [s for s in chosen if s not in SOURCES]
    if unknown:
        raise ValueError(f"unknown source {unknown[0]!r}: choose from {', '.join(SOURCES)} or all")
    return chosen
