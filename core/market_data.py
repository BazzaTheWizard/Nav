"""Round 35: real market and macro data for a clip, when the speaker names it but doesn't
say the numbers.

"NVIDIA's stock just ran", "the 2008 crash", "inflation got out of hand", "rates went from
zero to five", "Apple's revenue": moments() finds these in a clip (an entity from
core.entity_enricher next to a market word, with fewer than two numbers said, so the
transcript alone can't draw it), series() fetches the real history and market_card()
turns it into a `market_line` Bento card (TradingView Lightweight Charts) with its source.

Sources (every answer cached as JSON in cache/market_data/, so a re-render is offline):
  - prices: Yahoo Finance via yfinance (stocks, ETFs like SPY/QQQ, crypto, ^TNX, futures);
  - macro: FRED's public CSV (CPI -> inflation year over year, fed funds, unemployment);
  - fundamentals: SEC EDGAR company facts (annual revenue from 10-K filings). SEC asks
    every caller to identify itself: set SEC_USER_AGENT in .env ("Nav Studios you@domain").
MARKET_DATA=off in .env turns all of it off; NAV_OFFLINE=1 uses the cache only.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from core import entity_enricher as E

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TTL = 12 * 3600  # a cached answer is fresh for 12 hours (older ones are still used when offline)
TIMEOUT = 12.0
CARD_SECONDS = 5.4  # in at the mention, built by 1.2 s, held still 3.8 s, out in 0.4 s
logger = logging.getLogger(__name__)


class MarketDataError(RuntimeError):
    pass


@dataclass
class Series:
    points: list[tuple[str, float]]  # (ISO date, value), oldest first
    unit: str  # "$", "%", "$B" (revenue in billions), "" (an index level)
    source: str  # shown on the card: "Yahoo Finance", "FRED (St. Louis Fed)", "SEC EDGAR (10-K filings)"
    what: str  # "SHARE PRICE", "INFLATION (CPI, YEAR OVER YEAR)", ...
    cadence: str  # "daily", "weekly", "monthly" or "annual" closes


@dataclass
class Moment:
    key: str  # core.entity_enricher key
    kind: str  # "price", "macro" or "revenue"
    start: float  # source seconds: where the mention starts
    end: float  # ...and its sentence ends
    since: date
    until: date
    said: str  # the sentence


def enabled() -> bool:
    default = "off" if os.getenv("NAV_NO_CLIP_PAGES") == "1" else "on"  # the test suite never fetches unless asked
    return (os.getenv("MARKET_DATA") or default).strip().lower() not in {"off", "0", "false", "no"}


def cache_dir() -> Path:
    if os.getenv("NAV_MARKET_CACHE"):
        return Path(os.environ["NAV_MARKET_CACHE"])
    if os.getenv("NAV_NO_CLIP_PAGES") == "1":  # tests never write to your real cache
        import tempfile

        return Path(tempfile.gettempdir()) / f"nav-market-{os.getpid()}"
    return PROJECT_ROOT / "cache" / "market_data"


def _cached(name: str, fetch: Any, ttl: float = TTL) -> Any:
    """A JSON answer from the cache when fresh, else ``fetch()`` (saved); a stale copy when
    fetching fails or NAV_OFFLINE=1."""
    path = cache_dir() / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', name)}.json"
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
        raise MarketDataError(f"Offline and nothing cached for {name}.")
    try:
        data = fetch()
    except Exception as exc:  # noqa: BLE001 - any source failing falls back to what was saved
        if stale is not None:
            logger.info("Using cached %s (%s)", name, exc)
            return stale
        raise MarketDataError(f"{name}: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"at": time.time(), "data": data}))
    temporary.replace(path)
    return data


# ---------------------------------------------------------------------------- sources
def _interval(since: date, until: date) -> tuple[str, str]:
    days = (until - since).days
    return ("1d", "daily") if days <= 370 else ("1wk", "weekly") if days <= 6 * 366 else ("1mo", "monthly")


def history(symbol: str, since: date, until: date) -> list[tuple[str, float]]:
    """Closing prices (dividend/split adjusted) from Yahoo Finance, oldest first."""
    interval, _cadence = _interval(since, until)

    def fetch() -> list[list[Any]]:
        import yfinance

        frame = yfinance.Ticker(symbol).history(start=since.isoformat(), end=(until + timedelta(days=1)).isoformat(),
                                                interval=interval, auto_adjust=True)
        if frame is None or frame.empty:
            raise MarketDataError(f"No prices for {symbol}.")
        return [[index.date().isoformat(), round(float(close), 6)] for index, close in frame["Close"].items()
                if close == close]  # NaN-free

    rows = _cached(f"yf_{symbol}_{since}_{until}_{interval}", fetch)
    return [(str(d), float(v)) for d, v in rows]


def fred(series_id: str) -> list[tuple[str, float]]:
    """A FRED series (public CSV, no key), oldest first."""

    def fetch() -> list[list[Any]]:
        import requests

        answer = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}", timeout=TIMEOUT * 2,
                              headers={"User-Agent": "NavStudios-graphics/1.0"})
        answer.raise_for_status()
        rows = []
        for row in csv.reader(io.StringIO(answer.text)):
            try:
                rows.append([row[0], float(row[1])])
            except (IndexError, ValueError):
                continue
        if not rows:
            raise MarketDataError(f"FRED sent nothing for {series_id}.")
        return rows

    return [(str(d), float(v)) for d, v in _cached(f"fred_{series_id}", fetch, ttl=24 * 3600)]


def _sec_headers() -> dict[str, str]:
    agent = os.getenv("SEC_USER_AGENT", "").strip()
    if not agent:
        raise MarketDataError("SEC filings need SEC_USER_AGENT in .env (\"Nav Studios you@yourdomain.com\").")
    return {"User-Agent": agent, "Accept-Encoding": "gzip, deflate"}


def cik_for(ticker: str) -> str:
    """The SEC's 10-digit company number for a ticker."""

    def fetch() -> dict[str, str]:
        import requests

        answer = requests.get("https://www.sec.gov/files/company_tickers.json", timeout=TIMEOUT, headers=_sec_headers())
        answer.raise_for_status()
        return {row["ticker"].upper(): f"{int(row['cik_str']):010d}" for row in answer.json().values()}

    table = _cached("sec_tickers", fetch, ttl=30 * 24 * 3600)
    cik = table.get(ticker.upper().replace("-", "."))  # BRK-B is BRK.B at the SEC
    if not cik:
        raise MarketDataError(f"{ticker} isn't an SEC filer.")
    return cik


REVENUE_CONCEPTS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                    "RevenueFromContractWithCustomerIncludingAssessedTax")


def sec_revenue(ticker: str) -> list[tuple[str, float]]:
    """Annual revenue (USD) from the company's 10-K filings, oldest first (one per fiscal year)."""
    cik = cik_for(ticker)

    def fetch() -> dict[str, Any]:
        import requests

        answer = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", timeout=TIMEOUT,
                              headers=_sec_headers())
        answer.raise_for_status()
        facts = answer.json().get("facts", {}).get("us-gaap", {})
        return {name: facts[name]["units"].get("USD", []) for name in REVENUE_CONCEPTS if name in facts}

    return annual_values(_cached(f"sec_{cik}", fetch, ttl=7 * 24 * 3600))


def annual_values(facts: dict[str, list[dict[str, Any]]]) -> list[tuple[str, float]]:
    """Full fiscal years from 10-K facts (a ~365-day span), the concept with the newest year."""
    best: list[tuple[str, float]] = []
    for name in REVENUE_CONCEPTS:
        years: dict[str, float] = {}
        for fact in facts.get(name, []):
            if fact.get("form") != "10-K" or not fact.get("start") or not fact.get("end"):
                continue
            span = (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days
            if 350 <= span <= 380:
                years[fact["end"]] = float(fact["val"])  # a later filing of the same year wins
        found = sorted(years.items())
        if found and (not best or found[-1][0] > best[-1][0]):
            best = found
    return best[-10:]


# ---------------------------------------------------------------------------- moments
PRICE_WORDS = re.compile(
    r"\b(stock|stocks|shares?|share price|price|prices|ran|run|run-?up|rall(?:y|ied)|soar(?:ed|ing)?|surg(?:e|ed)|"
    r"crash(?:ed|es)?|tank(?:ed)?|plung(?:e|ed)|dropp?(?:ed)?|fell|doubled|tripled|ten-?x|all-time high|market cap|"
    r"bubble|went up|went down|is up|is down|trading|valuation|returns?|performance|bull|bear)\b", re.IGNORECASE)
REVENUE_WORDS = re.compile(r"\b(revenue|revenues|sales|top line|top-line)\b", re.IGNORECASE)
ERAS = [  # (words, from, to): a named era sets the window
    (re.compile(r"\bdot-?com\b", re.I), date(1997, 1, 1), date(2003, 12, 31)),
    (re.compile(r"\b(financial crisis|great recession|2008 crash|the crash of 2008)\b", re.I), date(2007, 1, 1), date(2010, 6, 30)),
    (re.compile(r"\b(covid|pandemic|lockdowns?)\b", re.I), date(2019, 10, 1), date(2021, 12, 31)),
]
YEAR = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")


def window(text: str, today: date) -> tuple[date, date]:
    """The period a sentence is about: a named era, the years it says ("since 2020", "in
    2008", "from 2015 to 2021"), "this year", "the last decade"; five years otherwise."""
    for pattern, since, until in ERAS:
        if pattern.search(text):
            return since, min(until, today)
    years = sorted({int(y) for y in YEAR.findall(text) if int(y) <= today.year})
    lowered = text.lower()
    if years:
        if len(years) == 1 and not re.search(r"\b(since|from)\s+" + str(years[0]), lowered):
            year = years[0]  # "in 2008": the year with a year either side
            return date(year - 1, 1, 1), min(date(year + 1, 12, 31), today)
        return date(years[0], 1, 1), min(date(years[-1], 12, 31), today) if len(years) > 1 else today
    if "this year" in lowered or "ytd" in lowered:
        return date(today.year, 1, 1), today
    if "last year" in lowered or "past year" in lowered or "12 months" in lowered:
        return today - timedelta(days=365), today
    if "decade" in lowered or "ten years" in lowered or "10 years" in lowered:
        return today - timedelta(days=3653), today
    return today - timedelta(days=5 * 365 + 1), today


def _sentences(words: list[Any]) -> list[tuple[float, float, str]]:
    spans, current = [], []
    for word in words:
        current.append(word)
        if str(word.text).rstrip().endswith((".", "?", "!")) or len(current) >= 40:
            spans.append((float(current[0].start), float(current[-1].end), " ".join(str(w.text) for w in current)))
            current = []
    if current:
        spans.append((float(current[0].start), float(current[-1].end), " ".join(str(w.text) for w in current)))
    return spans


def moments(words: list[Any], clip_start: float, clip_end: float, today: date | None = None) -> list[Moment]:
    """Where the speaker names a market or a company's numbers without saying enough of
    them to draw (fewer than two numbers in the sentence), in the order said."""
    from core.card_checks import spoken_numbers

    today = today or date.today()
    found: list[Moment] = []
    inside = [w for w in words if clip_start - 0.05 <= float(w.start) < clip_end]
    for start, end, text in _sentences(inside):
        said = [w for w in inside if start - 0.01 <= float(w.start) <= end + 0.01]
        numbers = [n for n in spoken_numbers(said)  # a year ("since 2020") isn't a data point
                   if not (n.scale is None and n.value == int(n.value) and 1950 <= n.value <= 2049)]
        if len(numbers) >= 2:
            continue  # the speaker says the numbers: the transcript's own chart draws them
        since, until = window(text, today)
        for key in E.detect(text):
            entity = E.ENTITIES[key]
            if entity.kind == "macro" and (entity.fred or entity.ticker):
                kind = "macro"
            elif entity.ticker and entity.kind in {"company", "crypto", "index"} and PRICE_WORDS.search(text):
                kind = "price"
            elif entity.ticker and entity.kind == "company" and REVENUE_WORDS.search(text) \
                    and not re.search(r"[-^=]", entity.ticker):
                kind = "revenue"
                since, until = today - timedelta(days=10 * 366), today
            else:
                continue
            mention = next((float(w.start) for w in said if E.detect(str(w.text)) == [key]), start)
            found.append(Moment(key, kind, mention, end, since, until, text))
            break  # one series per sentence: its first-named entity
    return found


# ---------------------------------------------------------------------------- series
def _yoy(points: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Monthly index levels to year-over-year % change (CPI to inflation)."""
    return [(points[k][0], round((points[k][1] / points[k - 12][1] - 1) * 100, 2)) for k in range(12, len(points))]


def series(moment: Moment) -> Series:
    """The real data for a moment (raises MarketDataError when no source answers)."""
    entity = E.ENTITIES[moment.key]
    lo, hi = moment.since.isoformat(), moment.until.isoformat()
    if moment.kind == "revenue":
        rows = sec_revenue(entity.ticker)
        return Series([(d, v / 1e9) for d, v in rows], "$B", "SEC EDGAR (10-K filings)", "ANNUAL REVENUE", "annual")
    if moment.kind == "macro" and entity.fred:
        raw = fred(entity.fred)
        if entity.fred == "CPIAUCSL":
            pad = (moment.since - timedelta(days=400)).isoformat()
            rows = [p for p in _yoy([p for p in raw if p[0] >= pad]) if lo <= p[0] <= hi]
            return Series(rows, "%", "FRED (St. Louis Fed), CPI", "INFLATION · YEAR OVER YEAR", "monthly")
        rows = [p for p in raw if lo <= p[0] <= hi]
        return Series(rows, "%", "FRED (St. Louis Fed)", entity.name.upper(), "monthly")
    rows = history(entity.ticker, moment.since, moment.until)
    _interval_name, cadence = _interval(moment.since, moment.until)
    if entity.ticker.startswith("^TNX"):
        return Series(rows, "%", "Yahoo Finance", "10-YEAR TREASURY YIELD", cadence)
    unit = "" if entity.ticker.startswith("^") else "$"
    what = "PRICE" if entity.kind in {"crypto", "index"} else "SHARE PRICE"
    return Series(rows, unit, "Yahoo Finance", what, cadence)


def fmt(value: float, unit: str) -> str:
    """A value as the card shows it: $213, $1,240, $0.82, 4.9%, $130.5B, 5,812."""
    if unit == "%":
        return f"{value:.1f}%"
    if unit == "$B":
        return f"${value:,.0f}B" if value >= 100 else f"${value:,.1f}B"
    prefix = "$" if unit == "$" else ""
    if abs(value) >= 1000:
        return f"{prefix}{value:,.0f}"
    return f"{prefix}{value:,.0f}" if abs(value) >= 100 else f"{prefix}{value:,.2f}" if abs(value) < 10 else f"{prefix}{value:,.1f}"


def change(first: float, last: float, unit: str) -> str:
    """The header's change pill: % for prices and revenue, points for rates."""
    if unit == "%":
        diff = last - first
        return f"{'+' if diff >= 0 else '−'}{abs(diff):.1f} pts"
    if first <= 0:
        return ""
    pct = (last / first - 1) * 100
    body = f"{abs(pct):,.0f}%" if abs(pct) >= 10 else f"{abs(pct):.1f}%"
    return f"{'+' if pct >= 0 else '−'}{body}"


def _label(iso: str, cadence: str) -> str:
    day = date.fromisoformat(iso[:10])
    return f"FY{day.year}" if cadence == "annual" else day.strftime("%b %Y")


def market_card(moment: Moment, clip_end: float, data: Series | None = None) -> Any:
    """A `market_line` card for the moment (None when there isn't enough real data): in at
    the mention, the series in its own file, its source in the footer."""
    from core.edl import Card, CardRow

    data = data or series(moment)
    points = [(d, v) for d, v in data.points if v == v]
    if len(points) < (4 if data.cadence == "annual" else 10):
        return None
    entity = E.ENTITIES[moment.key]
    first, last = points[0], points[-1]
    delta = change(first[1], last[1], data.unit)
    folder = cache_dir() / "cards"
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(json.dumps([moment.key, moment.kind, points]).encode()).hexdigest()[:12]
    path = folder / f"{moment.key}_{digest}.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "value"])
        writer.writerows(points)
    since_year = date.fromisoformat(first[0][:10]).year
    name = entity.name.split(" (")[0][:18]
    headline = (f"{name} revenue" if moment.kind == "revenue" else f"{name} since {since_year}")[:30]
    ticker = entity.ticker.lstrip("^") if entity.ticker and moment.kind != "macro" else ""
    eyebrow = (f"{ticker} · {data.what}" if ticker else data.what)[:30]
    rows = [CardRow(_label(first[0], data.cadence), fmt(first[1], data.unit)),
            CardRow(_label(last[0], data.cadence), fmt(last[1], data.unit), highlight=not delta)]
    if delta:
        rows.append(CardRow("Change", delta, highlight=True))
    start = round(moment.start - 0.1, 3)
    end = round(min(start + CARD_SECONDS, clip_end), 3)
    footnote = f"{_label(first[0], data.cadence)} – {_label(last[0], data.cadence)}, {data.cadence}"
    return Card("market_line", start, end, eyebrow=eyebrow, headline=headline, rows=rows, source=data.source,
                data_path=str(path), footnote=footnote[:58], entity=moment.key)


def market_cards(words: list[Any], clip_start: float, clip_end: float, today: date | None = None) -> list[tuple[Any, str]]:
    """(card, why) for each market moment in the clip whose real data could be fetched."""
    if not enabled():
        return []
    out = []
    for moment in moments(words, clip_start, clip_end, today):
        try:
            card = market_card(moment, clip_end)
        except MarketDataError as exc:
            logger.info("No market data for %s: %s", moment.key, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - a data source is never a reason for a clip to fail
            logger.warning("Market data failed for %s: %s", moment.key, exc)
            continue
        if card is not None:
            entity = E.ENTITIES[moment.key]
            out.append((card, f"{entity.name} named without its numbers: real {moment.kind} data "
                              f"({card.source}, {card.footnote})"))
    return out

