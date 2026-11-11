"""Round 35: companies, funds and markets named in a clip, with their logo and colour.

detect(text) finds the entities a speaker names (Apple, NVDA, Stripe, Sequoia, "inflation",
"the S&P 500"...) from a curated list (ENTITIES: name, domain, ticker, kind). enrich(key)
fetches the logo once and caches it in assets/logos/ (Brandfetch, then logo.dev, then a
local initials badge), with the brand colour taken from the logo itself (Pillow colour
quantization, lifted until it reads on the dark card). The renderer only ever reads the
cache (cached(key)), so renders stay offline and instant.

Logo services need a free key in .env: BRANDFETCH_CLIENT_ID (brandfetch.com/developers)
and/or LOGO_DEV_TOKEN (logo.dev). Without either, the card shows an initials badge in the
brand's known colour. NAV_OFFLINE=1 never touches the network.
"""
from __future__ import annotations

import base64
import colorsys
import io
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CARD_BG = "#090C12"  # core.cards.CARD_BG: the surface the colour must read on
MIN_CONTRAST = 4.5
TIMEOUT = 6.0
logger = logging.getLogger(__name__)
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Entity:
    key: str
    name: str
    kind: str  # company, private, fund, crypto, index, macro
    domain: str = ""
    ticker: str = ""  # Yahoo Finance symbol (BRK-B, BTC-USD, ^TNX)
    color: str = ""  # the brand's known colour: used when no logo can be read
    aliases: tuple[str, ...] = ()  # extra names; a name in CAPS or "Apple"-like words match case-sensitively
    fred: str = ""  # a FRED series id for macro entities (CPIAUCSL)


def _e(key, name, kind, domain="", ticker="", color="", aliases=(), fred=""):
    return Entity(key, name, kind, domain, ticker, color, tuple(aliases), fred)


# Words that are also ordinary English: matched only capitalised, as a name.
CASE_SENSITIVE = {"Apple", "Meta", "Block", "Square", "Oracle", "Visa", "Target", "Ford", "Benchmark", "Index Ventures",
                  "Accel", "Arm", "Snowflake", "Zoom", "Uber", "Ramp", "Chime", "Plaid", "Gold", "Oil", "Strategy",
                  "Robinhood", "Coinbase", "Datadog", "Figma", "Canva", "Brex", "Revolut", "Klarna", "Stripe", "Dow",
                  "Patagonia", "Coke", "Nike", "Chipotle"}
ENTITIES: dict[str, Entity] = {e.key: e for e in [
    # public tech and finance
    _e("apple", "Apple", "company", "apple.com", "AAPL", "#A2AAAD"),
    _e("microsoft", "Microsoft", "company", "microsoft.com", "MSFT", "#00A4EF"),
    _e("alphabet", "Google", "company", "google.com", "GOOGL", "#4285F4", ("Alphabet",)),
    _e("amazon", "Amazon", "company", "amazon.com", "AMZN", "#FF9900", ("AWS",)),
    _e("meta", "Meta", "company", "meta.com", "META", "#0866FF", ("Facebook", "Instagram")),
    _e("tesla", "Tesla", "company", "tesla.com", "TSLA", "#E31937"),
    _e("nvidia", "NVIDIA", "company", "nvidia.com", "NVDA", "#76B900", ("Nvidia",)),
    _e("netflix", "Netflix", "company", "netflix.com", "NFLX", "#E50914"),
    _e("salesforce", "Salesforce", "company", "salesforce.com", "CRM", "#00A1E0"),
    _e("adobe", "Adobe", "company", "adobe.com", "ADBE", "#FF0000"),
    _e("oracle", "Oracle", "company", "oracle.com", "ORCL", "#F80000"),
    _e("intel", "Intel", "company", "intel.com", "INTC", "#0071C5"),
    _e("amd", "AMD", "company", "amd.com", "AMD", "#ED1C24"),
    _e("broadcom", "Broadcom", "company", "broadcom.com", "AVGO", "#CC092F"),
    _e("tsmc", "TSMC", "company", "tsmc.com", "TSM", "#E60012", ("Taiwan Semiconductor",)),
    _e("asml", "ASML", "company", "asml.com", "ASML", "#0F238C"),
    _e("arm", "Arm", "company", "arm.com", "ARM", "#0091BD"),
    _e("palantir", "Palantir", "company", "palantir.com", "PLTR", "#9FA6AD"),
    _e("shopify", "Shopify", "company", "shopify.com", "SHOP", "#95BF47"),
    _e("uber", "Uber", "company", "uber.com", "UBER", "#A0A0A0"),
    _e("airbnb", "Airbnb", "company", "airbnb.com", "ABNB", "#FF5A5F"),
    _e("coinbase", "Coinbase", "company", "coinbase.com", "COIN", "#0052FF"),
    _e("robinhood", "Robinhood", "company", "robinhood.com", "HOOD", "#CCFF00"),
    _e("snowflake", "Snowflake", "company", "snowflake.com", "SNOW", "#29B5E8"),
    _e("datadog", "Datadog", "company", "datadoghq.com", "DDOG", "#9B5DE5"),
    _e("crowdstrike", "CrowdStrike", "company", "crowdstrike.com", "CRWD", "#FC0000"),
    _e("servicenow", "ServiceNow", "company", "servicenow.com", "NOW", "#62D84E"),
    _e("block", "Block", "company", "block.xyz", "XYZ", "#3E4348", ("Square",)),
    _e("paypal", "PayPal", "company", "paypal.com", "PYPL", "#0070E0"),
    _e("visa", "Visa", "company", "visa.com", "V", "#1A1F71"),
    _e("mastercard", "Mastercard", "company", "mastercard.com", "MA", "#EB001B"),
    _e("jpmorgan", "JPMorgan", "company", "jpmorganchase.com", "JPM", "#6E5B4B", ("JP Morgan", "J.P. Morgan", "Chase")),
    _e("goldman", "Goldman Sachs", "company", "goldmansachs.com", "GS", "#7399C6", ("Goldman",)),
    _e("morgan-stanley", "Morgan Stanley", "company", "morganstanley.com", "MS", "#0D6AA8"),
    _e("blackrock", "BlackRock", "company", "blackrock.com", "BLK", "#E6E6E6"),
    _e("berkshire", "Berkshire Hathaway", "company", "berkshirehathaway.com", "BRK-B", "#4A5A8A", ("Berkshire",)),
    _e("walmart", "Walmart", "company", "walmart.com", "WMT", "#0071CE"),
    _e("costco", "Costco", "company", "costco.com", "COST", "#E31837"),
    _e("disney", "Disney", "company", "disney.com", "DIS", "#113CCF"),
    _e("boeing", "Boeing", "company", "boeing.com", "BA", "#1D439C"),
    _e("spotify", "Spotify", "company", "spotify.com", "SPOT", "#1DB954"),
    _e("zoom", "Zoom", "company", "zoom.us", "ZM", "#0B5CFF"),
    _e("microstrategy", "Strategy", "company", "strategy.com", "MSTR", "#F7931A", ("MicroStrategy",)),
    _e("gamestop", "GameStop", "company", "gamestop.com", "GME", "#E22027"),
    _e("reddit", "Reddit", "company", "reddit.com", "RDDT", "#FF4500"),
    _e("rivian", "Rivian", "company", "rivian.com", "RIVN", "#F4B62D"),
    _e("ford", "Ford", "company", "ford.com", "F", "#1C5BAA"),
    _e("ibm", "IBM", "company", "ibm.com", "IBM", "#1F70C1"),
    _e("snap", "Snap", "company", "snap.com", "SNAP", "#FFFC00", ("Snapchat",)),
    _e("pinterest", "Pinterest", "company", "pinterest.com", "PINS", "#E60023"),
    _e("doordash", "DoorDash", "company", "doordash.com", "DASH", "#FF3008"),
    _e("hubspot", "HubSpot", "company", "hubspot.com", "HUBS", "#FF7A59"),
    _e("atlassian", "Atlassian", "company", "atlassian.com", "TEAM", "#0052CC"),
    _e("twilio", "Twilio", "company", "twilio.com", "TWLO", "#F22F46"),
    _e("cloudflare", "Cloudflare", "company", "cloudflare.com", "NET", "#F38020"),
    _e("mongodb", "MongoDB", "company", "mongodb.com", "MDB", "#00ED64"),
    # private companies
    _e("openai", "OpenAI", "private", "openai.com", color="#10A37F", aliases=("ChatGPT",)),
    # Round 41: consumer brands (brand_proof pills)
    _e("nike", "Nike", "company", "nike.com", "NKE", "#111111"),
    _e("oakberry", "Oakberry", "private", "oakberry.com", color="#5B2A86", aliases=("OAKBERRY",)),
    _e("starbucks", "Starbucks", "company", "starbucks.com", "SBUX", "#00704A"),
    _e("cocacola", "Coca-Cola", "company", "coca-cola.com", "KO", "#F40009", ("Coke",)),
    _e("adidas", "Adidas", "company", "adidas.com", color="#000000"),
    _e("lululemon", "Lululemon", "company", "lululemon.com", "LULU", "#D22030"),
    _e("chipotle", "Chipotle", "company", "chipotle.com", "CMG", "#A81612"),
    _e("mcdonalds", "McDonald's", "company", "mcdonalds.com", "MCD", "#FFC72C", ("McDonalds",)),
    _e("patagonia", "Patagonia", "private", "patagonia.com", color="#1E2A44"),
    _e("redbull", "Red Bull", "private", "redbull.com", color="#DB0A40"),
    _e("anthropic", "Anthropic", "private", "anthropic.com", color="#D97757"),
    _e("stripe", "Stripe", "private", "stripe.com", color="#635BFF"),
    _e("spacex", "SpaceX", "private", "spacex.com", color="#A7B1BC"),
    _e("databricks", "Databricks", "private", "databricks.com", color="#FF3621"),
    _e("plaid", "Plaid", "private", "plaid.com", color="#A0A7B4"),
    _e("ramp", "Ramp", "private", "ramp.com", color="#E4F222"),
    _e("brex", "Brex", "private", "brex.com", color="#F46A35"),
    _e("figma", "Figma", "private", "figma.com", color="#A259FF"),
    _e("canva", "Canva", "private", "canva.com", color="#00C4CC"),
    _e("revolut", "Revolut", "private", "revolut.com", color="#7F84F6"),
    _e("klarna", "Klarna", "private", "klarna.com", color="#FFB3C7"),
    _e("chime", "Chime", "private", "chime.com", color="#1EC677"),
    _e("xai", "xAI", "private", "x.ai", color="#BFC5CC"),
    # venture and investment firms
    _e("sequoia", "Sequoia Capital", "fund", "sequoiacap.com", color="#2DA44E", aliases=("Sequoia",)),
    _e("a16z", "Andreessen Horowitz", "fund", "a16z.com", color="#F5A623", aliases=("a16z", "Andreessen")),
    _e("benchmark", "Benchmark", "fund", "benchmark.com", color="#9AA5B1"),
    _e("accel", "Accel", "fund", "accel.com", color="#3D7EFF"),
    _e("kleiner", "Kleiner Perkins", "fund", "kleinerperkins.com", color="#5AA9E6", aliases=("Kleiner",)),
    _e("founders-fund", "Founders Fund", "fund", "foundersfund.com", color="#C8CDD3"),
    _e("ycombinator", "Y Combinator", "fund", "ycombinator.com", color="#FF6600", aliases=("YC",)),
    _e("tiger-global", "Tiger Global", "fund", "tigerglobal.com", color="#F7A400"),
    _e("softbank", "SoftBank", "fund", "softbank.jp", "SFTBY", "#B7BCC2", ("Vision Fund",)),
    _e("lightspeed", "Lightspeed", "fund", "lsvp.com", color="#F04E23", aliases=("Lightspeed Venture Partners",)),
    _e("general-catalyst", "General Catalyst", "fund", "generalcatalyst.com", color="#4F7CFF"),
    _e("greylock", "Greylock", "fund", "greylock.com", color="#9BA3AE"),
    _e("index-ventures", "Index Ventures", "fund", "indexventures.com", color="#4A90E2"),
    _e("insight", "Insight Partners", "fund", "insightpartners.com", color="#0A7CFF"),
    _e("thrive", "Thrive Capital", "fund", "thrivecap.com", color="#C9CED4"),
    _e("coatue", "Coatue", "fund", "coatue.com", color="#3FA9F5"),
    _e("khosla", "Khosla Ventures", "fund", "khoslaventures.com", color="#E8463A", aliases=("Khosla",)),
    _e("bridgewater", "Bridgewater", "fund", "bridgewater.com", color="#5B8DEF"),
    _e("citadel", "Citadel", "fund", "citadel.com", color="#4E7BD8"),
    # crypto and markets
    _e("bitcoin", "Bitcoin", "crypto", "bitcoin.org", "BTC-USD", "#F7931A", ("BTC",)),
    _e("ethereum", "Ethereum", "crypto", "ethereum.org", "ETH-USD", "#8A92B2", ("ETH", "ether")),
    _e("solana", "Solana", "crypto", "solana.com", "SOL-USD", "#9945FF"),
    _e("sp500", "S&P 500", "index", ticker="SPY", color="#5B8DEF",
       aliases=("S&P", "S and P", "S&P 500", "SPY", "the market", "stock market")),
    _e("nasdaq", "Nasdaq 100", "index", "nasdaq.com", "QQQ", "#0996C7", ("Nasdaq", "QQQ")),
    _e("dow", "Dow Jones", "index", ticker="^DJI", color="#5B8DEF", aliases=("Dow", "Dow Jones")),
    _e("gold", "Gold", "index", ticker="GC=F", color="#D4AF37", aliases=("gold price", "price of gold")),
    _e("oil", "Oil", "index", ticker="CL=F", color="#8C8C8C", aliases=("oil price", "price of oil", "crude")),
    _e("inflation", "US inflation (CPI)", "macro", color="#F59E0B", aliases=("inflation", "CPI", "consumer prices"),
       fred="CPIAUCSL"),
    _e("rates", "10-year Treasury yield", "macro", ticker="^TNX", color="#60A5FA",
       aliases=("interest rates", "10-year", "ten-year", "treasury yield", "treasury yields", "bond yields")),
    _e("fed-funds", "Fed funds rate", "macro", color="#60A5FA", aliases=("fed funds", "federal funds rate", "rate hikes",
                                                                         "rate cuts"),
       fred="FEDFUNDS"),
    _e("unemployment", "US unemployment rate", "macro", color="#A78BFA", aliases=("unemployment", "jobless rate"),
       fred="UNRATE"),
]}


def _pattern(alias: str) -> re.Pattern[str]:
    body = re.escape(alias).replace(r"\ ", r"\s+")
    sensitive = alias in CASE_SENSITIVE or (alias.isupper() and len(alias) <= 5)
    return re.compile(rf"(?<![\w$&]){body}(?![\w&])", 0 if sensitive else re.IGNORECASE)


_PATTERNS: list[tuple[re.Pattern[str], str]] = []
for _entity in ENTITIES.values():
    for _alias in {_entity.name, *_entity.aliases} | ({_entity.ticker} if _entity.ticker.isalpha() and len(_entity.ticker) >= 3 else set()):
        _PATTERNS.append((_pattern(_alias), _entity.key))
_PATTERNS.sort(key=lambda item: -len(item[0].pattern))  # "S&P 500" before "S&P"
_TICKER = re.compile(r"\$([A-Z]{1,5})\b")


def detect(text: str) -> list[str]:
    """The entity keys named in ``text``, in the order first named (each once)."""
    found: dict[str, int] = {}
    for pattern, key in _PATTERNS:
        match = pattern.search(text or "")
        if match and (key not in found or match.start() < found[key]):
            found[key] = match.start()
    by_ticker = {e.ticker: e.key for e in ENTITIES.values() if e.ticker}
    for match in _TICKER.finditer(text or ""):  # "$NVDA"
        key = by_ticker.get(match.group(1))
        if key and key not in found:
            found[key] = match.start()
    return sorted(found, key=found.get)


def resolve(name_or_ticker: str) -> Entity | None:
    """An entity from its key, name, alias or ticker."""
    text = (name_or_ticker or "").strip()
    if text.lower() in ENTITIES:
        return ENTITIES[text.lower()]
    for entity in ENTITIES.values():
        if text.upper() == entity.ticker or text.lower() == entity.name.lower():
            return entity
    keys = detect(text)
    return ENTITIES[keys[0]] if keys else None


# ---------------------------------------------------------------------------- logos
@dataclass(frozen=True)
class Enriched:
    key: str
    name: str
    color: str  # readable on the card (CARD_BG), or "" to keep the client's accent
    logo_path: str
    logo_kind: str  # "brandfetch", "logo.dev", "initials" or ""
    logo_uri: str  # a data: URI for the card page ("" when there's no logo)


def logo_dir() -> Path:
    if os.getenv("NAV_LOGO_DIR"):
        return Path(os.environ["NAV_LOGO_DIR"])
    if os.getenv("NAV_NO_CLIP_PAGES") == "1":  # tests never write to your real logo cache
        import tempfile

        return Path(tempfile.gettempdir()) / f"nav-logos-{os.getpid()}"
    return PROJECT_ROOT / "assets" / "logos"


def _index_path() -> Path:
    return logo_dir() / "index.json"


def _read_index() -> dict[str, dict]:
    try:
        return json.loads(_index_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_index(index: dict[str, dict]) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(index, indent=1, sort_keys=True))
    temporary.replace(path)


def _download(url: str) -> bytes | None:
    """The image at ``url`` (None on any failure or a non-image answer)."""
    import requests

    try:
        answer = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "NavStudios-graphics/1.0"})
    except requests.RequestException as exc:
        logger.info("Logo fetch failed for %s: %s", url.split("?")[0], exc)
        return None
    kind = answer.headers.get("content-type", "")
    if answer.status_code != 200 or not answer.content or not kind.startswith("image/"):
        return None
    return answer.content


def _sources(domain: str) -> list[tuple[str, str]]:
    found = []
    if os.getenv("BRANDFETCH_CLIENT_ID"):
        found.append(("brandfetch", f"https://cdn.brandfetch.io/{domain}/w/256/h/256/fallback/404/icon.png"
                                    f"?c={os.environ['BRANDFETCH_CLIENT_ID']}"))
    if os.getenv("LOGO_DEV_TOKEN"):
        found.append(("logo.dev", f"https://img.logo.dev/{domain}?token={os.environ['LOGO_DEV_TOKEN']}&size=256&format=png"))
    return found


def _to_png(raw: bytes) -> bytes | None:
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError):
        return None
    if image.width < 16 or image.height < 16:
        return None
    out = io.BytesIO()
    image.convert("RGBA").save(out, "PNG")
    return out.getvalue()


def initials_badge(entity: Entity, color: str) -> bytes:
    """A local stand-in logo: the name's initials on the brand colour (256 px PNG)."""
    from PIL import Image, ImageDraw, ImageFont

    size = 256
    image = Image.new("RGBA", (size, size), color)
    words = [w for w in re.findall(r"[A-Za-z0-9]+", entity.name) if w]
    letters = (words[0][:2] if len(words) == 1 else words[0][0] + words[1][0]).upper() if words else "?"
    try:
        font = ImageFont.truetype(str(PROJECT_ROOT / "assets" / "fonts" / "Inter-Variable.ttf"), 112)
        try:
            font.set_variation_by_axes([800])
        except Exception:  # noqa: BLE001 - not every Pillow build reads variable fonts
            pass
    except OSError:
        font = ImageFont.load_default()
    ink = "#0A0D13" if _luminance(color) > 0.35 else "#FFFFFF"
    draw = ImageDraw.Draw(image)
    draw.text((size / 2, size / 2 + 4), letters, fill=ink, font=font, anchor="mm")
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


# ---------------------------------------------------------------------------- colour
def _luminance(color: str) -> float:
    r, g, b = (int(color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(first: str, second: str) -> float:
    a, b = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def readable(color: str, background: str = CARD_BG, target: float = MIN_CONTRAST) -> str:
    """``color`` with its hue and saturation kept, lightened until it reads on the card."""
    r, g, b = (int(color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    for _ in range(60):
        out = "#{:02X}{:02X}{:02X}".format(*(round(c * 255) for c in colorsys.hls_to_rgb(h, l, s)))
        if contrast(out, background) >= target:
            return out
        l = min(1.0, l + 0.02)
    return "#F5F6F8"


def dominant_color(png: bytes) -> str | None:
    """The logo's brand colour: the most common clearly-coloured pixel cluster (Pillow's
    median-cut quantization over its opaque pixels, ignoring white, black and greys).
    None for a black-and-white logo (Apple, Uber): the card keeps the client's accent."""
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGBA")
    image.thumbnail((96, 96))
    pixels = [p[:3] for p in image.getdata() if p[3] >= 160]
    coloured = []
    for r, g, b in pixels:
        _h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        if 0.12 <= l <= 0.9 and s >= 0.28:
            coloured.append((r, g, b))
    if len(coloured) < max(12, 0.04 * len(pixels)):
        return None
    strip = Image.new("RGB", (len(coloured), 1))
    strip.putdata(coloured)
    palette = strip.quantize(colors=4, method=Image.Quantize.MEDIANCUT)
    counts = sorted(palette.getcolors() or [], reverse=True)
    colours = palette.getpalette()[:12]
    if not counts:
        return None
    index = counts[0][1]
    r, g, b = colours[3 * index:3 * index + 3]
    return f"#{r:02X}{g:02X}{b:02X}"


# ---------------------------------------------------------------------------- enrich
def enrich(key: str, offline: bool | None = None) -> Enriched | None:
    """The entity's logo and colour, fetched once and cached in assets/logos/ (the next
    call, and every render, reads the cache). Never raises: no logo service reachable
    or configured gives a local initials badge."""
    entity = ENTITIES.get(key) or resolve(key)
    if entity is None:
        return None
    offline = os.getenv("NAV_OFFLINE") == "1" if offline is None else offline
    with _LOCK:
        index = _read_index()
        entry = index.get(entity.key)
        fresh = entry and (not entity.domain or Path(logo_dir() / entry.get("file", "")).is_file())
        if fresh and (entry.get("kind") != "initials" or offline or not _sources(entity.domain)):
            return cached(entity.key)  # cached (an initials badge is retried once a logo service is set up)
        png, kind = None, ""
        if entity.domain and not offline:
            for name, url in _sources(entity.domain):
                raw = _download(url)
                png = _to_png(raw) if raw else None
                if png:
                    kind = name
                    break
        color = dominant_color(png) if png else None
        base = color or entity.color or "#5B6474"
        file = ""
        if entity.domain:  # a market or macro series (inflation, rates) has a colour but no logo
            if png is None:
                png, kind = initials_badge(entity, readable(base)), "initials"
            file = f"{re.sub(r'[^a-z0-9.-]+', '-', entity.domain.lower())}{'-initials' if kind == 'initials' else ''}.png"
            logo_dir().mkdir(parents=True, exist_ok=True)
            (logo_dir() / file).write_bytes(png)
        index[entity.key] = {"file": file, "kind": kind, "color": readable(base) if (color or entity.color) else "",
                             "raw_color": color or entity.color, "domain": entity.domain, "ticker": entity.ticker}
        _write_index(index)
    return cached(entity.key)


def cached(key: str) -> Enriched | None:
    """What's in the cache for ``key`` (no network): what the renderer uses."""
    entity = ENTITIES.get(key) or resolve(key)
    if entity is None:
        return None
    entry = _read_index().get(entity.key)
    if not entry:
        return None
    path = logo_dir() / entry.get("file", "")
    uri = ""
    if entry.get("file") and path.is_file():
        uri = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
    return Enriched(entity.key, entity.name, entry.get("color", ""), str(path) if uri else "", entry.get("kind", "") if uri else "",
                    uri)


def card_entity(card_text: str) -> str:
    """The one entity a card is about (named in its own text), or "" when it names none or
    several (a card comparing Apple and Google keeps the client's accent)."""
    keys = [k for k in detect(card_text) if ENTITIES[k].kind not in {"macro", "index"}]
    return keys[0] if len(keys) == 1 else ""
