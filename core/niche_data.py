"""Round 36: data for the non-market Bento cards (geopolitics, politics, strategy).

- COUNTRIES: the nations a speaker names (aliases: "the US", "America", "Britain"), with
  their map id (ISO 3166 numeric, as in the bundled Natural Earth map) and ISO3 code.
  countries_in(text) finds them, in the order named.
- World Bank (api.worldbank.org/v2, no key): country_stat(iso3, "gdp") -> the latest value.
- Polymarket (gamma-api.polymarket.com, public): polymarket_search(query) -> live odds of
  open markets, for a prediction_gauge_card picked by hand in the app.
- Wikipedia (REST API): wiki_summary(query) -> a one-line description of a law, event,
  person or private company, for a headline_card's dek line.
Every answer is cached as JSON in cache/niche_data/ (fresh for a day; used stale when a
source is down or NAV_OFFLINE=1). Nothing here is ever required: a card without it is
drawn from what the speaker said.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 10.0
TTL = 24 * 3600
logger = logging.getLogger(__name__)


class NicheDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class Country:
    name: str  # as the card shows it
    atlas: str  # ISO 3166 numeric: the bundled map's feature id
    iso3: str  # World Bank's code
    aliases: tuple[str, ...] = ()


def _c(name: str, atlas: str, iso3: str, *aliases: str) -> Country:
    return Country(name, atlas, iso3, aliases)


COUNTRIES: tuple[Country, ...] = (
    _c("United States", "840", "USA", "US", "U.S.", "USA", "America", "the States"),
    _c("China", "156", "CHN", "PRC", "Beijing", "mainland China"),
    _c("Russia", "643", "RUS", "Moscow", "Russian Federation"),
    _c("Ukraine", "804", "UKR", "Kyiv", "Kiev"),
    _c("Taiwan", "158", "TWN", "Taipei"),
    _c("Japan", "392", "JPN", "Tokyo"),
    _c("South Korea", "410", "KOR", "Korea", "Seoul"),
    _c("North Korea", "408", "PRK", "Pyongyang", "DPRK"),
    _c("India", "356", "IND", "New Delhi", "Delhi"),
    _c("Pakistan", "586", "PAK"),
    _c("United Kingdom", "826", "GBR", "UK", "U.K.", "Britain", "Great Britain", "England", "London"),
    _c("Germany", "276", "DEU", "Berlin"),
    _c("France", "250", "FRA", "Paris"),
    _c("Italy", "380", "ITA", "Rome"),
    _c("Spain", "724", "ESP", "Madrid"),
    _c("Netherlands", "528", "NLD", "Holland", "Dutch", "Amsterdam"),
    _c("Switzerland", "756", "CHE", "Swiss", "Zurich"),
    _c("Sweden", "752", "SWE"),
    _c("Norway", "578", "NOR"),
    _c("Poland", "616", "POL"),
    _c("Ireland", "372", "IRL", "Dublin"),
    _c("Greece", "300", "GRC"),
    _c("Turkey", "792", "TUR", "Türkiye", "Ankara"),
    _c("Israel", "376", "ISR", "Tel Aviv", "Jerusalem"),
    _c("Iran", "364", "IRN", "Tehran"),
    _c("Iraq", "368", "IRQ", "Baghdad"),
    _c("Saudi Arabia", "682", "SAU", "Saudi", "Saudis", "Riyadh"),
    _c("United Arab Emirates", "784", "ARE", "UAE", "Dubai", "Abu Dhabi", "Emirates"),
    _c("Qatar", "634", "QAT", "Doha"),
    _c("Egypt", "818", "EGY", "Cairo"),
    _c("Syria", "760", "SYR"),
    _c("Lebanon", "422", "LBN"),
    _c("Yemen", "887", "YEM"),
    _c("Afghanistan", "004", "AFG"),
    _c("Canada", "124", "CAN", "Ottawa", "Toronto"),
    _c("Mexico", "484", "MEX"),
    _c("Brazil", "076", "BRA"),
    _c("Argentina", "032", "ARG", "Buenos Aires"),
    _c("Chile", "152", "CHL"),
    _c("Colombia", "170", "COL"),
    _c("Venezuela", "862", "VEN"),
    _c("Peru", "604", "PER"),
    _c("Cuba", "192", "CUB"),
    _c("Nigeria", "566", "NGA"),
    _c("South Africa", "710", "ZAF"),
    _c("Kenya", "404", "KEN", "Nairobi"),
    _c("Ethiopia", "231", "ETH"),
    _c("Indonesia", "360", "IDN", "Jakarta"),
    _c("Vietnam", "704", "VNM", "Hanoi"),
    _c("Thailand", "764", "THA", "Bangkok"),
    _c("Malaysia", "458", "MYS"),
    _c("Philippines", "608", "PHL", "Manila"),
    _c("Australia", "036", "AUS", "Sydney", "Canberra"),
    _c("New Zealand", "554", "NZL"),
    _c("Kazakhstan", "398", "KAZ"),
    _c("Bangladesh", "050", "BGD"),
    _c("Finland", "246", "FIN"),
    _c("Denmark", "208", "DNK", "Greenland"),
    _c("Belgium", "056", "BEL", "Brussels"),
    _c("Austria", "040", "AUT", "Vienna"),
    _c("Portugal", "620", "PRT", "Lisbon"),
    _c("Hungary", "348", "HUN"),
    _c("Romania", "642", "ROU"),
    _c("Belarus", "112", "BLR"),
    _c("Georgia", "268", "GEO"),
    _c("Armenia", "051", "ARM"),
    _c("Azerbaijan", "031", "AZE"),
    _c("Libya", "434", "LBY"),
    _c("Algeria", "012", "DZA"),
    _c("Morocco", "504", "MAR"),
    _c("Sudan", "729", "SDN"),
    _c("Panama", "591", "PAN"),
)
# Words that are a country name AND an ordinary word or a US state: matched only capitalised.
_SENSITIVE = {"US", "UK", "Georgia", "Turkey", "Chile", "China", "Jordan", "Dutch", "Swiss", "Saudi", "Saudis"}
_BY_NAME = {c.name.lower(): c for c in COUNTRIES}


def _country_pattern(alias: str) -> re.Pattern[str]:
    body = re.escape(alias).replace(r"\ ", r"\s+")
    flags = 0 if alias in _SENSITIVE or alias.isupper() else re.IGNORECASE
    return re.compile(rf"(?<![\w.]){body}(?![\w])", flags)


_COUNTRY_PATTERNS = sorted(((_country_pattern(alias), country) for country in COUNTRIES
                            for alias in {country.name, *country.aliases}), key=lambda item: -len(item[0].pattern))


def country(name: str) -> Country | None:
    """A country from its name or an alias ("the US", "Britain"), or None."""
    found = countries_in(name)
    return found[0] if found else _BY_NAME.get(name.strip().lower())


def countries_in(text: str) -> list[Country]:
    """The countries named in ``text``, in the order first named (each once)."""
    first: dict[str, tuple[int, Country]] = {}
    taken: list[tuple[int, int]] = []
    for pattern, place in _COUNTRY_PATTERNS:
        for match in pattern.finditer(text or ""):
            if any(a < match.end() and match.start() < b for a, b in taken):
                continue  # "South Korea" already took "Korea"
            taken.append((match.start(), match.end()))
            if place.name not in first or match.start() < first[place.name][0]:
                first[place.name] = (match.start(), place)
    return [place for _at, place in sorted(first.values(), key=lambda item: item[0])]


GEO_WORDS = re.compile(
    r"\b(trade|tariffs?|sanctions?|exports?|imports?|supply chains?|war|invad\w*|border|alliance|treaty|"
    r"capital (?:flows?|flight)|reshoring|onshoring|chips?|semiconductors?|oil|gas|pipeline|ships?|shipping|"
    r"embargo|geopoliti\w*|conflict|tension|deal|manufactur\w*|factories|moved? (?:to|from|out of))\b", re.IGNORECASE)


# ---------------------------------------------------------------------------- cache
def enabled() -> bool:
    """Online look-ups on (NICHE_DATA=off turns them off; the test suite never fetches unless asked)."""
    default = "off" if os.getenv("NAV_NO_CLIP_PAGES") == "1" else "on"
    return (os.getenv("NICHE_DATA") or default).strip().lower() not in {"off", "0", "false", "no"}


def cache_dir() -> Path:
    if os.getenv("NAV_NICHE_CACHE"):
        return Path(os.environ["NAV_NICHE_CACHE"])
    if os.getenv("NAV_NO_CLIP_PAGES") == "1":  # tests never write to your real cache
        return Path(tempfile.gettempdir()) / f"nav-niche-{os.getpid()}"
    return PROJECT_ROOT / "cache" / "niche_data"


def _cached(name: str, fetch: Any, ttl: float = TTL) -> Any:
    path = cache_dir() / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', name)[:120]}.json"
    stale = None
    if path.is_file():
        try:
            saved = json.loads(path.read_text())
            if time.time() - saved.get("at", 0) < ttl or os.getenv("NAV_OFFLINE") == "1":
                return saved["data"]
            stale = saved["data"]
        except (OSError, ValueError, KeyError):
            pass
    if os.getenv("NAV_OFFLINE") == "1":
        raise NicheDataError(f"Offline and nothing cached for {name}.")
    try:
        data = fetch()
    except Exception as exc:  # noqa: BLE001 - a source that's down falls back to what was saved
        if stale is not None:
            return stale
        raise NicheDataError(f"{name}: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"at": time.time(), "data": data}))
    temporary.replace(path)
    return data


def _get_json(url: str) -> Any:
    import requests

    answer = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "NavStudios-graphics/1.0 (clip graphics)"})
    answer.raise_for_status()
    return answer.json()


# ---------------------------------------------------------------------------- World Bank
INDICATORS = {  # what a geo card can show next to a country, and how
    "gdp": ("NY.GDP.MKTP.CD", "GDP"),
    "population": ("SP.POP.TOTL", "Population"),
    "inflation": ("FP.CPI.TOTL.ZG", "Inflation"),
    "gdp_growth": ("NY.GDP.MKTP.KD.ZG", "GDP growth"),
    "debt": ("GC.DOD.TOTL.GD.ZS", "Gov. debt"),
    "exports": ("NE.EXP.GNFS.CD", "Exports"),
}


def country_stat(iso3: str, stat: str) -> tuple[float, int]:
    """(latest value, its year) of a World Bank indicator for a country."""
    code = INDICATORS[stat][0]

    def fetch() -> list[Any]:
        data = _get_json(f"https://api.worldbank.org/v2/country/{iso3}/indicator/{code}?format=json&mrnev=1")
        rows = data[1] if isinstance(data, list) and len(data) > 1 and data[1] else []
        found = [(float(row["value"]), int(row["date"])) for row in rows if row.get("value") is not None]
        if not found:
            raise NicheDataError(f"No {stat} for {iso3}.")
        return list(found[0])

    value, year = _cached(f"wb_{iso3}_{code}", fetch, ttl=7 * TTL)
    return float(value), int(year)


def fmt_stat(value: float, stat: str) -> str:
    """$27.4T, $1.9B, 331M, 3.2%."""
    if stat in {"inflation", "gdp_growth", "debt"}:
        return f"{value:.1f}%"
    prefix = "" if stat == "population" else "$"
    for size, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(value) >= size:
            return f"{prefix}{value / size:.1f}{unit}"
    return f"{prefix}{value:,.0f}"


# ---------------------------------------------------------------------------- Polymarket
@dataclass(frozen=True)
class Market:
    question: str
    yes: float  # probability 0-1 of the first outcome
    outcomes: tuple[str, str]
    volume: float
    slug: str


def _market(row: dict[str, Any]) -> Market | None:
    try:
        outcomes = json.loads(row["outcomes"]) if isinstance(row.get("outcomes"), str) else row.get("outcomes")
        prices = json.loads(row["outcomePrices"]) if isinstance(row.get("outcomePrices"), str) else row.get("outcomePrices")
        if not outcomes or not prices or len(outcomes) != 2:
            return None
        return Market(str(row.get("question") or ""), float(prices[0]), (str(outcomes[0]), str(outcomes[1])),
                      float(row.get("volume") or row.get("volumeNum") or 0), str(row.get("slug") or ""))
    except (KeyError, TypeError, ValueError):
        return None


def polymarket_search(query: str, limit: int = 5) -> list[Market]:
    """Open two-outcome Polymarket markets matching ``query``, biggest volume first."""

    def fetch() -> list[dict[str, Any]]:
        data = _get_json("https://gamma-api.polymarket.com/public-search?" + urllib.parse.urlencode(
            {"q": query, "limit_per_type": 10, "events_status": "active"}))
        rows: list[dict[str, Any]] = []
        for event in data.get("events") or []:
            rows.extend(m for m in event.get("markets") or [] if not m.get("closed"))
        return rows

    markets = [m for m in (_market(row) for row in _cached(f"pm_{query.lower()}", fetch, ttl=3600)) if m]
    return sorted(markets, key=lambda m: -m.volume)[:limit]


def fmt_volume(volume: float) -> str:
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if volume >= size:
            return f"${volume / size:.1f}{unit}"
    return f"${volume:,.0f}"


# ---------------------------------------------------------------------------- Wikipedia
def wiki_summary(query: str) -> dict[str, str]:
    """{"title", "description", "extract"} of the Wikipedia page that best matches
    ``query`` (a law, an event, a person, a private company)."""

    def fetch() -> dict[str, str]:
        found = _get_json("https://en.wikipedia.org/w/rest.php/v1/search/title?" + urllib.parse.urlencode(
            {"q": query, "limit": 1}))
        pages = found.get("pages") or []
        if not pages:
            raise NicheDataError(f"No Wikipedia page for {query}.")
        key = pages[0]["key"]
        page = _get_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(key)}")
        return {"title": str(page.get("title") or key), "description": str(page.get("description") or ""),
                "extract": str(page.get("extract") or "")}

    return _cached(f"wiki_{query.lower()}", fetch, ttl=30 * TTL)


def dek(summary: dict[str, str], limit: int = 58) -> str:
    """A tear-sheet's dek line from a Wikipedia summary: its short description, or the
    first sentence of the extract, cut at a word."""
    text = summary.get("description") or re.split(r"(?<=[.!?])\s", summary.get("extract") or "")[0]
    text = text.strip().rstrip(".")
    if len(text) <= limit:
        return text[:1].upper() + text[1:]
    cut = text[:limit - 1].rsplit(" ", 1)[0]
    return cut[:1].upper() + cut[1:] + "…"
