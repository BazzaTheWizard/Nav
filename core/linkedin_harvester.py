"""Round 46: the B2B prospect harvester (LinkedIn Video Gap). Round 47: many sources, a
persistent cursor, a funnel report, email-confidence badges (see the end of this docstring).

Who we look for: B2B companies, executive podcasts and founder-led brands that publish
long-form episodes (YouTube / podcast RSS), treat LinkedIn as a channel, and aren't (yet)
posting native vertical clips there.

Where the data comes from (all public, all their own publishing):
1. Apple Podcasts search (itunes.apple.com/search, no key) across B2B queries and genres.
2. The show's RSS feed: website, owner name and email (the publisher's contact), author,
   episode cadence, recent titles, YouTube and LinkedIn links in the show notes.
3. Their website (home plus about / team / contact pages): published emails, LinkedIn
   company and personal links, a marketing / content lead on a team page, the YouTube channel.
4. YouTube (the existing API key): the latest long episode to cut the sample clips from, and
   whether they post vertical video there (the Shorts gap).

What it does NOT do: read LinkedIn. There is no public API for someone else's LinkedIn
activity and scraping it breaks LinkedIn's terms (and gets accounts banned). The score uses
what we can verify (a LinkedIn page they link themselves, cadence, the YouTube vertical gap),
plus one manual check in the app: open their LinkedIn and mark "posts native video?" yes / rarely / no.

Emails: "verified" = published on their own site or feed AND the domain accepts mail (an MX
record, checked over DNS-over-HTTPS). "published" = published, MX unknown. "guess" = a name
pattern (first.last@domain) at a domain with MX: shown, never queued as the contact. No SMTP
probing (it gets the sending IP flagged).

Round 47:
- Why it found 0: python.org's Python on the Mac ships without trusted HTTPS certificates, and
  fetch() hid the error, so every request came back empty. fetch() now uses certifi (like
  clip_pages and face_tracker) and says so loudly once. Also: feeds over 2 MB were cut mid-XML
  and dropped (now read to 3 MB and repaired at the last whole episode), and websites given
  without https:// were skipped.
- Discovery: core.b2b_sources (Apple search over a niche x format matrix, Apple's business and
  tech charts, gpodder, your seed pages, YC B2B companies, YouTube search, Podcast Index).
- State: storage/b2b_harvest_state.json: each source's cursor, every show looked at and why it
  was kept or dropped, a backlog. Each run starts where the last stopped.
- Funnel: raw_fetched -> deduplicated -> enriched -> active_b2b -> saved, printed every run.
- Emails: every contact gets a confidence badge (EMAIL_CONFIDENCE). Nothing is dropped for its
  email; shared inboxes are queueable, name-pattern guesses only with allow_guess.
- YouTube without quota: the channel's public feed (Shorts show as /shorts/ links), and
  @handle guesses from the show's name, confirmed by the channel's own title.
- Signals: the podcast host (paid vs free), webinar / video tools on their site (Wistia,
  Vidyard, ON24, Goldcast...), a podcast agency named on the site or feed.

CLI: python -m core.linkedin_harvester harvest [--limit 15] [--niche fintech] [--source apple_search,yc]
         [--query "..."] [--queue-min 70] [--minutes 6]
     python -m core.linkedin_harvester list | queue <id>... [--allow-guess] | check <id> yes|rare|no
     python -m core.linkedin_harvester sources | funnel
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import re
import ssl
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.entities import name2codepoint
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROSPECTS_PATH = PROJECT_ROOT / "storage" / "b2b_prospects.json"
USER_AGENT = "Mozilla/5.0 (compatible; ProspectResearch/1.0)"

DEFAULT_QUERIES = (
    "B2B SaaS podcast", "SaaS founder interviews", "venture capital podcast", "private equity podcast",
    "fintech podcast", "B2B marketing podcast", "sales leadership podcast", "CFO podcast",
    "legal tech podcast", "startup law podcast", "consulting podcast", "revenue operations podcast",
    "commercial real estate investing podcast", "wealth management podcast", "supply chain leadership podcast",
    "cybersecurity leadership podcast", "HR tech podcast", "AI for business podcast", "CEO interviews business",
    "go-to-market strategy podcast",
)
B2B_GENRES = {"business", "technology", "investing", "management", "marketing", "entrepreneurship", "careers",
              "tech news", "non-profit"}
B2B_WORDS = re.compile(
    r"\b(b2b|saas|enterprise|founders?|ceos?|cfos?|cmos?|venture|vc|private equity|fintech|finance|investing|investors?|"
    r"capital|startups?|go-to-market|gtm|sales|revenue|marketing|growth|leadership|executives?|consulting|legal|law|"
    r"compliance|real estate|wealth|accounting|operations|supply chain|cybersecurity|software|ai|data|hr|"
    r"procurement|m&a|banking|insurance)\b", re.I)
OFF_NICHE = re.compile(r"\b(gaming|anime|true crime|comedy|sports|nba|nfl|football|music|horoscope|astrology|"
                       r"dating|celebrity|gossip|fiction|kids)\b", re.I)
SHARED = None  # filled from core.outreach.SHARED_INBOXES
MARKETING_ROLE = re.compile(r"\b(?:Head|VP|Vice President|Director|Chief|Manager|Lead)\b[^.\n|]{0,30}?\b"
                            r"(?:Marketing|Content|Brand|Communications|Growth|Social|Demand Gen(?:eration)?|Podcast)\b"
                            r"|\bCMO\b|\bPodcast (?:Producer|Host)\b|\bContent Lead\b", re.I)
NAME = r"[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?(?: [A-Z]\.)? [A-Z][a-z]+(?:[-'][A-Z][a-z]+)?"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LINKEDIN = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(company|in|showcase)/([A-Za-z0-9_%.-]+)/?", re.I)
YOUTUBE = re.compile(r"https?://(?:www\.|m\.)?youtube\.com/(?:(channel)/(UC[A-Za-z0-9_-]{22})|@([A-Za-z0-9._-]+)|"
                     r"watch\?v=([A-Za-z0-9_-]{11}))|https?://youtu\.be/([A-Za-z0-9_-]{11})", re.I)
THEME_STOP = set("""the a an and or of to in on for with from by at is are was be your you our we i my how why what when who
this that these those it its vs versus episode ep podcast show part live special interview interviews conversation
talk talks chat q&a qa guest feat ft series season new top best more less about into out up do does can will just
get got go make makes making use using way ways thing things lessons story stories tips guide here there 1 2 3 4 5
6 7 8 9 10 one two three""".split())

_http_get: Callable[[str, dict[str, str] | None], str | None] | None = None  # tests replace this


# ------------------------------------------------------------------------------ fetching
_SSL_CONTEXT: ssl.SSLContext | None = None
FETCH_STATS: Counter[str] = Counter()  # ok / failed / ssl (this process)
_SSL_WARNED = False


def _ssl_context() -> ssl.SSLContext:
    """certifi's certificates: python.org's Python on macOS has none until "Install
    Certificates.command" is run, and every HTTPS request then fails (Round 47's 0-found bug)."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        try:
            import certifi

            _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # noqa: BLE001
            _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def fetch(url: str, headers: dict[str, str] | None = None, timeout: float = 12.0,
          max_bytes: int = 2_000_000) -> str | None:
    """GET a page as text (None on any failure; certificate failures are logged as a warning)."""
    global _SSL_WARNED
    if _http_get is not None:
        return _http_get(url, headers)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
            body = response.read(max_bytes).decode(response.headers.get_content_charset() or "utf-8", "replace")
        FETCH_STATS["ok"] += 1
        return body
    except Exception as exc:  # noqa: BLE001
        FETCH_STATS["failed"] += 1
        if "CERTIFICATE_VERIFY_FAILED" in str(exc) or isinstance(getattr(exc, "reason", None), ssl.SSLError):
            FETCH_STATS["ssl"] += 1
            if not _SSL_WARNED:
                _SSL_WARNED = True
                logger.warning("HTTPS certificate check failed (%s). Run: .venv/bin/pip install -U certifi", exc)
        logger.debug("Fetch failed for %s: %s", url, exc)
        return None


def search_podcasts(term: str, limit: int = 25) -> list[dict[str, Any]]:
    """Apple Podcasts search: shows with an RSS feed, in a B2B genre."""
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"media": "podcast", "entity": "podcast", "term": term, "limit": max(1, min(limit, 200)), "country": "US"})
    raw = fetch(url)
    try:
        results = json.loads(raw or "{}").get("results") or []
    except ValueError:
        return []
    out = []
    for item in results:
        genres = {str(g).lower() for g in item.get("genres") or []}
        if item.get("feedUrl") and genres & B2B_GENRES:
            out.append({"show": item.get("collectionName") or "", "author": item.get("artistName") or "",
                        "feed_url": item["feedUrl"], "apple_url": item.get("collectionViewUrl") or "",
                        "genres": sorted(genres), "episodes_total": item.get("trackCount")})
    return out


# ------------------------------------------------------------------------------ RSS
@dataclass
class Feed:
    title: str = ""
    website: str = ""
    author: str = ""
    owner_name: str = ""
    owner_email: str = ""
    description: str = ""
    episodes: list[dict[str, Any]] = field(default_factory=list)  # {"title", "date", "link"}
    links: list[str] = field(default_factory=list)  # every URL in the show notes


def _text(node: ET.Element | None) -> str:
    return html.unescape((node.text or "").strip()) if node is not None and node.text else ""


_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
FEED_MAX_BYTES = 3_000_000  # enough for the 40 newest episodes of even the longest feeds


def repair_xml(text: str) -> str:
    """A feed that won't parse, made parseable: cut at its last whole episode (a feed read
    only partly, or truncated by its host), HTML entities (&nbsp;) turned into characters,
    stray '&' escaped, control characters dropped."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if not re.search(r"</rss>\s*$", text[-4000:]):
        cut = text.rfind("</item>")
        if cut != -1:
            text = text[:cut + len("</item>")] + "</channel></rss>"

    def entity(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in _XML_ENTITIES:
            return match.group(0)
        return chr(name2codepoint[name]) if name in name2codepoint else "&amp;" + name + ";"

    text = re.sub(r"&([A-Za-z][A-Za-z0-9]*);", entity, text)
    return re.sub(r"&(?!(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);)", "&amp;", text)


def normalize_site(url: str) -> str:
    """'acme.com' or '//acme.com' -> 'https://acme.com'; '' for anything that isn't a site."""
    url = (url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")) and re.fullmatch(r"(?:www\.)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?:/\S*)?", url):
        url = "https://" + url
    return url if url.startswith(("http://", "https://")) else ""


def parse_feed(xml_text: str) -> Feed:
    ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
    if not xml_text:
        return Feed()
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        try:
            root = ET.fromstring(repair_xml(xml_text if isinstance(xml_text, str) else xml_text.decode("utf-8", "replace")).encode("utf-8"))
        except ET.ParseError:
            return Feed()
    channel = root.find("channel")
    if channel is None:
        return Feed()
    feed = Feed(title=_text(channel.find("title")), website=normalize_site(_text(channel.find("link"))),
                author=_text(channel.find("itunes:author", ns)), description=_text(channel.find("description")))
    owner = channel.find("itunes:owner", ns)
    if owner is not None:
        feed.owner_name = _text(owner.find("itunes:name", ns))
        feed.owner_email = _text(owner.find("itunes:email", ns)).lower()
    texts = [feed.description]
    for item in channel.findall("item")[:40]:
        date = None
        try:
            date = parsedate_to_datetime(_text(item.find("pubDate")))
            date = date if date.tzinfo else date.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
        feed.episodes.append({"title": _text(item.find("title")), "date": date.isoformat() if date else "",
                              "link": _text(item.find("link"))})
        texts.append(_text(item.find("description")))
    feed.links = sorted(set(re.findall(r"https?://[^\s\"'<>)]+", " ".join(texts))))
    return feed


def recent(episodes: list[dict[str, Any]], days: int, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    out = []
    for episode in episodes:
        try:
            when = datetime.fromisoformat(str(episode.get("date")))
        except ValueError:
            continue
        if now - when <= timedelta(days=days):
            out.append(episode)
    return out


# ------------------------------------------------------------------------------ website
@dataclass
class Site:
    emails: list[str] = field(default_factory=list)
    linkedin_company: str = ""
    linkedin_people: list[str] = field(default_factory=list)
    youtube: list[str] = field(default_factory=list)
    people: list[tuple[str, str]] = field(default_factory=list)  # (name, role) from a team page
    pages: list[str] = field(default_factory=list)
    # Round 47
    feeds: list[str] = field(default_factory=list)  # RSS feeds it links or advertises
    apple_ids: list[str] = field(default_factory=list)  # Apple Podcasts shows it links
    video_stack: list[str] = field(default_factory=list)  # Wistia, Vidyard, ON24... on the site
    agency: str = ""  # a podcast agency named on the site
    contact_form: str = ""  # a contact page with a form on it
    bodies: dict[str, str] = field(default_factory=dict)  # Round 53: each page's HTML (core.marketing_contact)


# Round 47: what a company's site and feed say about its budget for video.
VIDEO_TOOLS = {
    "Wistia": r"wistia\.(?:com|net)", "Vidyard": r"vidyard\.com", "Vimeo": r"player\.vimeo\.com|vimeo\.com/(?:event|showcase)",
    "Brightcove": r"brightcove", "ON24": r"on24\.com", "Goldcast": r"goldcast\.io", "Livestorm": r"livestorm\.co",
    "Sequel": r"sequel\.io", "Zoom Events": r"events\.zoom\.us|zoom\.us/webinar", "Bizzabo": r"bizzabo\.com",
    "Cvent": r"cvent\.com", "RingCentral Events": r"hopin\.com|events\.ringcentral\.com", "Hubilo": r"hubilo\.com",
    "Airmeet": r"airmeet\.com", "Riverside": r"riverside\.fm", "StreamYard": r"streamyard\.com",
    "BigMarker": r"bigmarker\.com", "Demio": r"demio\.com",
}
_VIDEO_TOOLS = {name: re.compile(pattern, re.I) for name, pattern in VIDEO_TOOLS.items()}
AGENCIES = {
    "Sweet Fish Media": r"sweet ?fish", "Lower Street": r"lower ?street", "Content Allies": r"content ?allies",
    "Fame": r"\bfame\.so\b|produced by fame\b", "Casted": r"\bcasted\.us\b|powered by casted", "Quill": r"quillpodcasting|\bquill podcast",
    "ThePod.fm": r"thepod\.fm", "Resonate Recordings": r"resonate ?recordings", "Channel V Media": r"channel ?v ?media",
    "Pacific Content": r"pacific ?content", "JAR Audio": r"jar ?audio", "Grow The Show": r"grow ?the ?show",
}
_AGENCIES = {name: re.compile(pattern, re.I) for name, pattern in AGENCIES.items()}
# Podcast hosts, by feed domain: (name, paid). A paid host means the show is a budget line.
HOSTS = {
    "transistor.fm": ("Transistor", True), "simplecast.com": ("Simplecast", True), "captivate.fm": ("Captivate", True),
    "buzzsprout.com": ("Buzzsprout", True), "libsyn.com": ("Libsyn", True), "podbean.com": ("Podbean", True),
    "megaphone.fm": ("Megaphone", True), "omnycontent.com": ("Omny Studio", True), "omny.fm": ("Omny Studio", True),
    "art19.com": ("ART19", True), "acast.com": ("Acast", True), "redcircle.com": ("RedCircle", True),
    "castos.com": ("Castos", True), "bcast.fm": ("Fame / bcast.fm", True), "cohostpodcasting.com": ("CoHost", True),
    "rss.com": ("RSS.com", True), "casted.us": ("Casted", True), "blubrry.com": ("Blubrry", True),
    "audioboom.com": ("Audioboom", True), "spreaker.com": ("Spreaker", True), "fireside.fm": ("Fireside", True),
    "pinecast.com": ("Pinecast", True), "podigee.io": ("Podigee", True), "hubhopper.com": ("Hubhopper", True),
    "anchor.fm": ("Spotify for Creators (free)", False), "spotify.com": ("Spotify for Creators (free)", False),
    "soundcloud.com": ("SoundCloud", False), "substack.com": ("Substack", False),
}


# Shows made by media companies and networks: in-house producers, not a founding client.
MEDIA_NETWORKS = re.compile(
    r"(?:^|\.)(?:voxmedia|npr|americanpublicmedia|iheartmedia|iheart|wondery|audacy|siriusxm|stitcher|pushkin|"
    r"nytimes|wsj|dowjones|bloomberg|cnbc|cnn|bbc|ft|economist|fool|ramseysolutions|foxnews|nbcuni|nbcuniversal|"
    r"cbs|abc|washpost|theatlantic|barstoolsports|dailywire|businessinsider|insider|forbes|axios|politico|"
    r"theringer|spotify|amazon|audible|lemonadamedia|kaleidoscope|cadence13|podcastone|creatorsagency|"
    r"morningbrew|hubspot|gimletmedia|marketplace)\.(?:com|org|net|co|co\.uk|io|fm)$", re.I)


def media_network(*emails_or_domains: str) -> str:
    """The media company behind a show ('' when none of the addresses or domains is one)."""
    for value in emails_or_domains:
        domain = str(value or "").split("@")[-1].lower().strip()
        if domain and MEDIA_NETWORKS.search(domain):
            return domain
    return ""


def host_platform(feed_url: str) -> dict[str, Any]:
    """{"name", "paid"} for the feed's host, {} when it's self-hosted or unknown."""
    host = domain_of(feed_url) if feed_url else ""
    for suffix, (name, paid) in HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return {"name": name, "paid": paid}
    return {}


def video_tools(text: str) -> list[str]:
    return [name for name, pattern in _VIDEO_TOOLS.items() if pattern.search(text or "")]


def agency_named(text: str) -> str:
    return next((name for name, pattern in _AGENCIES.items() if pattern.search(text or "")), "")


def _plain(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr|section|article)>", "\n", page)
    return html.unescape(re.sub(r"<[^>]+>", " ", page))


def scan_website(url: str, max_pages: int = 5) -> Site:
    """Published emails, LinkedIn and YouTube links, and marketing/content people."""
    site = Site()
    if not url.startswith("http"):
        return site
    home = fetch(url)
    if not home:
        return site
    base = urllib.parse.urlparse(url)
    pages = [(url, home)]
    for href in re.findall(r'href=["\']([^"\'#]+)["\']', home):
        full = urllib.parse.urljoin(url, href)
        part = urllib.parse.urlparse(full)
        if part.netloc == base.netloc and re.search(r"/(about|team|people|leadership|contact|company|who-we-are|"
                                                    r"podcast|press|media|our-story|founders)",
                                                    part.path, re.I) and full not in [p for p, _ in pages]:
            body = fetch(full)
            if body:
                pages.append((full, body))
            if len(pages) >= max_pages:
                break
    from core import b2b_sources

    for page_url, page in pages:
        site.pages.append(page_url)
        site.bodies[page_url] = page
        shows = b2b_sources.links_on_page(page, page_url)
        alternates = [urllib.parse.urljoin(page_url, h) for h in re.findall(
            r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)', page, re.I)]
        for feed_link in [f for f in alternates if not re.search(r"/comments/|/wp-json/", f)] + shows["feeds"]:
            if feed_link not in site.feeds:
                site.feeds.append(feed_link)
        site.apple_ids += [i for i in shows["apple_ids"] if i not in site.apple_ids]
        for tool in video_tools(page):
            if tool not in site.video_stack:
                site.video_stack.append(tool)
        site.agency = site.agency or agency_named(_plain(page))
        if not site.contact_form and re.search(r"/contact", page_url, re.I) and re.search(r"<form\b", page, re.I):
            site.contact_form = page_url
        mails = re.findall(r'mailto:([^"\'?>\s]+)', page) + EMAIL.findall(_plain(page))
        for mail in mails:
            mail = urllib.parse.unquote(mail).strip().lower().rstrip(".")
            if EMAIL.fullmatch(mail) and not re.search(r"\.(png|jpg|jpeg|gif|webp|svg)$|sentry|wixpress|example\.", mail) \
                    and mail not in site.emails:
                site.emails.append(mail)
        for kind, slug in LINKEDIN.findall(page):
            link = f"https://www.linkedin.com/{kind.lower()}/{slug}"
            if kind.lower() in {"company", "showcase"} and not site.linkedin_company:
                site.linkedin_company = link
            elif kind.lower() == "in" and link not in site.linkedin_people:
                site.linkedin_people.append(link)
        for match in YOUTUBE.finditer(page):
            if match.group(0) not in site.youtube:
                site.youtube.append(match.group(0))
        text = _plain(page)
        for line in [l.strip() for l in text.splitlines() if l.strip()]:
            role = MARKETING_ROLE.search(line)
            if not role:
                continue
            names = [n for n in re.findall(NAME, line) if person_name(n)]
            if names and len(line) <= 140:
                person = (names[0], line[role.start():role.end()].strip())
                if person not in site.people:
                    site.people.append(person)
        # A name on one line and the role on the next ("Jane Doe\nHead of Marketing").
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for previous, line in zip(lines, lines[1:]):
            role = MARKETING_ROLE.fullmatch(line.strip(" ,|-")) or (MARKETING_ROLE.match(line) if len(line) <= 60 else None)
            if role and re.fullmatch(NAME, previous) and person_name(previous):
                person = (previous, line.strip(" ,|-"))
                if person not in site.people:
                    site.people.append(person)
    return site


# ------------------------------------------------------------------------------ email checks
_MX: dict[str, bool | None] = {}


def mx_ok(domain: str) -> bool | None:
    """True when the domain has an MX record (it accepts mail), False when it has none,
    None when it couldn't be checked. DNS-over-HTTPS (Cloudflare), so no extra package."""
    domain = domain.lower().strip(".")
    if domain in _MX:
        return _MX[domain]
    raw = fetch("https://cloudflare-dns.com/dns-query?" + urllib.parse.urlencode({"name": domain, "type": "MX"}),
                {"accept": "application/dns-json"})
    result: bool | None = None
    try:
        answer = json.loads(raw) if raw else None
        if isinstance(answer, dict) and "Status" in answer:
            result = answer.get("Status") == 0 and any(a.get("type") == 15 for a in answer.get("Answer") or [])
    except ValueError:
        result = None
    _MX[domain] = result
    return result


# Round 47: show and team inboxes common at B2B companies, on top of core.outreach's list.
B2B_SHARED = frozenset({"podcast", "podcasts", "thepodcast", "show", "studio", "partnerships", "partners", "partner",
                        "sponsor", "sponsors", "sponsorship", "sponsorships", "booking", "bookings", "pr", "events",
                        "content", "comms", "communications", "brand", "growth", "business", "bd", "biz", "hey",
                        "connect", "questions", "feedback", "listen", "ask"})


def is_shared(email: str) -> bool:
    from core.outreach import is_shared_inbox

    local = str(email or "").strip().lower().split("@", 1)[0]
    return is_shared_inbox(email) or local in B2B_SHARED


def email_status(email: str, published: bool) -> str:
    """"verified", "published", "guess", "bad_domain" or "unknown"."""
    if not EMAIL.fullmatch(email or ""):
        return "unknown"
    ok = mx_ok(email.split("@", 1)[1])
    if ok is False:
        return "bad_domain"
    if published:
        return "verified" if ok else "published"
    return "guess" if ok else "unknown"


def pattern_guess(name: str, domain: str) -> str:
    """first.last@domain (the most common corporate pattern); '' without a full name."""
    parts = [re.sub(r"[^a-z]", "", p.lower()) for p in name.split()]
    parts = [p for p in parts if p]
    return f"{parts[0]}.{parts[-1]}@{domain}" if len(parts) >= 2 and domain else ""


def domain_of(url: str) -> str:
    host = urllib.parse.urlparse(url if "//" in url else f"https://{url}").netloc.lower()
    host = host.split(":")[0]
    return host[4:] if host.startswith("www.") else host


# Hosting and webmail domains: not the company's own (whole domains only: "acme.com" isn't "me.com").
_HOSTING = re.compile(r"(?:^|\.)(?:anchor\.fm|spotify\.com|buzzsprout\.com|libsyn\.com|podbean\.com|simplecast\.com|"
                      r"transistor\.fm|captivate\.fm|megaphone\.fm|apple\.com|soundcloud\.com|substack\.com|"
                      r"squarespace\.com|wixsite\.com|linktr\.ee|youtube\.com|gmail\.com|googlemail\.com|outlook\.com|"
                      r"hotmail\.com|yahoo\.com|icloud\.com|me\.com|proton\.me|protonmail\.com|aol\.com)$", re.I)


# ------------------------------------------------------------------------------ contacts
# Round 47: how sure we are of an email, shown as a badge and used for queueing.
EMAIL_CONFIDENCE = {
    "verified_rss": "✅ verified: published in their podcast feed, domain takes mail",
    "verified_site": "✅ verified: published on their website, domain takes mail",
    "shared_inbox": "📮 shared inbox (podcast@, hello@...): published, read by a team",
    "published_unchecked": "📄 published, mail server not confirmed",
    "domain_mx_pattern": "❔ pattern guess (first.last@) at a domain that takes mail: may bounce",
    "site_pattern": "🧩 their company's own email pattern (learned from addresses on their site): likely, not confirmed",
    "bad_domain": "⛔ domain takes no mail",
    "none": "– no email",
}


@dataclass
class Contact:
    name: str = ""
    role: str = ""
    email: str = ""
    status: str = "unknown"  # verified / published / guess / bad_domain / unknown
    source: str = ""
    linkedin: str = ""
    shared: bool = False
    confidence: str = ""  # Round 47: a key of EMAIL_CONFIDENCE (filled by contacts_for)
    alternates: list[str] = field(default_factory=list)  # other pattern guesses (first@, flast@)

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "alternates": list(self.alternates)}


def confidence_of(contact: Contact | dict[str, Any]) -> str:
    get = (lambda k: getattr(contact, k, "")) if isinstance(contact, Contact) else (lambda k: (contact or {}).get(k, ""))
    status = get("status")
    if not get("email") or status == "unknown":
        return "none"
    if status in {"verified", "published"} and get("shared"):
        return "shared_inbox"
    if status == "verified":
        return "verified_rss" if "feed" in str(get("source")) else "verified_site"
    return {"published": "published_unchecked", "guess": "domain_mx_pattern", "bad_domain": "bad_domain"}.get(status, "none")


_NOT_A_PERSON = re.compile(r"\b(?:media|podcasts?|show|inc|llc|ltd|group|network|studios?|partners|capital|company|"
                           r"team|ventures|labs|productions?|radio|news|agency|university|institute|foundation)\b", re.I)


# Capitalised words that look like a "First Last" name on a team page but aren't one.
_NOT_NAME_WORDS = set("""marketing sales product content brand growth customer success future futures our meet the team
head chief director manager lead senior vice president founder founders partner partners operations people talent
engineering design data finance legal support community social media communications demand revenue strategy
global north south east west new about contact careers press news events podcast podcasts webinar blog home learn
read more view all latest open source platform solutions services company industry enterprise digital creative
studio studios group capital ventures labs agency network radio show episode series""".split())


def person_name(text: str) -> str:
    """'Dan Ortiz' from 'Dan Ortiz' or 'Hosted by Dan Ortiz'; '' for a company, a heading or a list."""
    text = " ".join((text or "").split())
    if not text or _NOT_A_PERSON.search(text) or re.search(r"\b(?:and|&)\b|,", text):
        return ""
    match = re.search(NAME, text)
    if not match or len(text) > 40:
        return ""
    words = [w.lower().strip(".") for w in match.group(0).split()]
    return "" if any(w in _NOT_NAME_WORDS for w in words) else match.group(0)


def pattern_alternates(name: str, domain: str) -> list[str]:
    parts = [re.sub(r"[^a-z]", "", p.lower()) for p in name.split()]
    parts = [p for p in parts if p]
    if len(parts) < 2 or not domain:
        return []
    return [f"{parts[0]}@{domain}", f"{parts[0][0]}{parts[-1]}@{domain}"]


def _person_link(name: str, links: list[str]) -> str:
    parts = [re.sub(r"[^a-z]", "", p.lower()) for p in name.split()]
    for link in links:
        slug = link.rstrip("/").rsplit("/", 1)[-1].lower()
        if parts and all(p and p in slug for p in parts[:1] + parts[-1:]):
            return link
    return ""


def contacts_for(feed: Feed, site: Site, domain: str) -> list[Contact]:
    """Every decision-maker we can name, best first: a marketing / content lead (their own
    published email, else a pattern guess), then the show owner, then other published emails."""
    found: list[Contact] = []
    people_links = site.linkedin_people + [l for l in feed.links if "/in/" in l]
    for name, role in site.people:
        mine = next((e for e in site.emails if pattern_guess(name, e.split("@")[1]).split("@")[0] in e
                     or e.split("@")[0] == name.split()[0].lower()), "")
        email = mine or pattern_guess(name, domain)
        found.append(Contact(name, role, email, email_status(email, bool(mine)) if email else "unknown",
                             "website team page", _person_link(name, people_links)))
    if feed.owner_email:
        owner = Contact(feed.owner_name or feed.author, "Show owner / host", feed.owner_email,
                        email_status(feed.owner_email, True), "podcast feed (itunes:owner)",
                        _person_link(feed.owner_name or feed.author, people_links), is_shared(feed.owner_email))
        if not _HOSTING.search(feed.owner_email.split("@")[1]) or not found:
            found.append(owner)
    for email in site.emails:
        if any(c.email == email for c in found):
            continue
        found.append(Contact("", "Published on their website", email, email_status(email, True), "website",
                             "", is_shared(email)))
    # Round 47: the host by name at the company's own domain (first.last@), when nothing of
    # theirs is published: a guess, kept as a backup and badged as one.
    host = person_name(feed.owner_name) or person_name(feed.author)
    if host and domain and not _HOSTING.search(domain) and not any(c.name == host and not c.shared and c.email
                                                                   for c in found):
        guess = pattern_guess(host, domain)
        status = email_status(guess, False) if guess else "unknown"
        if status == "guess":
            found.append(Contact(host, "Host", guess, status, "name pattern at their domain",
                                 _person_link(host, people_links), alternates=pattern_alternates(host, domain)))
    for contact in found:
        contact.confidence = confidence_of(contact)
    rank = {"verified": 0, "published": 1, "guess": 3, "unknown": 4, "bad_domain": 5}
    return sorted(found, key=lambda c: (rank.get(c.status, 4) + (1.5 if c.shared else 0) - (0.5 if c.role and
                                        MARKETING_ROLE.search(c.role) else 0), not c.name))


def queueable(contact: Contact | dict[str, Any] | None, allow_guess: bool = False) -> bool:
    """A contact the worker may email: verified or published (shared inboxes included); a
    name-pattern guess only with ``allow_guess`` (it may bounce, which hurts your Gmail)."""
    status = contact.status if isinstance(contact, Contact) else (contact or {}).get("status")
    return status in {"verified", "published"} or (allow_guess and status == "guess")


# ------------------------------------------------------------------------------ calendar angle
def calendar_angle(titles: list[str], ask: Callable[[str, str], str | None] | None = None) -> dict[str, Any]:
    """This month's themes from their recent episode titles: {"theme", "themes", "recent_titles"}.
    The AI may name them, but a theme only stands if its words are in the titles."""
    titles = [" ".join(t.split()) for t in titles if t and t.strip()][:8]
    angle: dict[str, Any] = {"theme": "", "themes": [], "recent_titles": titles}
    if not titles:
        return angle
    words_in = set(re.findall(r"[a-z0-9&]+", " ".join(titles).lower()))
    themes: list[str] = []
    if ask is not None:
        try:
            raw = ask("Recent episode titles:\n" + "\n".join(f"- {t}" for t in titles),
                      "Name the 1-3 themes these podcast episodes keep coming back to this month, each 1-4 words, "
                      "using words from the titles, lowercase except names and acronyms. "
                      'Return JSON: {"themes": ["...", "..."]}')
            from core.agent_brain import _parse_json_response

            for theme in (_parse_json_response(raw).get("themes") or [])[:3] if raw else []:
                tokens = re.findall(r"[a-z0-9&]+", str(theme).lower())
                if tokens and all(t in words_in or t in THEME_STOP for t in tokens) and len(tokens) <= 4:
                    themes.append(" ".join(str(theme).split()))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Theme AI failed: %s", exc)
    if not themes:
        grams: Counter[str] = Counter()
        for title in titles:
            tokens = [t for t in re.findall(r"[A-Za-z0-9&][A-Za-z0-9&'-]*", title)]
            seen: set[str] = set()
            lower = [t.lower() for t in tokens]
            for n in (2, 1):
                for i in range(len(tokens) - n + 1):
                    chunk = lower[i:i + n]
                    if any(c in THEME_STOP or c.isdigit() or len(c) < 2 for c in chunk):
                        continue
                    gram = " ".join(tokens[i:i + n]) if any(t.isupper() and len(t) > 1 for t in tokens[i:i + n]) \
                        else " ".join(chunk)
                    if gram.lower() not in seen:
                        seen.add(gram.lower())
                        grams[gram] += 2 if n == 2 else 1
        common = [g for g, count in grams.most_common(10) if count >= 2]
        themes = []
        for gram in common:
            if not any(gram.lower() in t.lower() or t.lower() in gram.lower() for t in themes):
                themes.append(gram)
            if len(themes) == 3:
                break
    from core.outreach import fix_acronyms

    themes = [fix_acronyms(t) for t in themes]
    angle.update(theme=themes[0] if themes else "", themes=themes)
    return angle


# ------------------------------------------------------------------------------ YouTube
def youtube_target(urls: list[str], ask_api: Any = None) -> dict[str, Any]:
    """The latest long episode on their YouTube channel, and their vertical-video habit there:
    {"channel_id", "channel_title", "subscriber_count", "latest_video_url", "latest_video_title",
    "duration_seconds", "shorts_30d", "long_30d", "titles"}; {} when there's no channel or no key."""
    channel_id, handle, video_id = "", "", ""
    for url in urls:
        match = YOUTUBE.search(url)
        if match:
            channel_id = channel_id or (match.group(2) or "")
            handle = handle or (match.group(3) or "")
            video_id = video_id or (match.group(4) or match.group(5) or "")
    if not (channel_id or handle or video_id):
        return {}
    try:
        from core import lead_harvester as LH

        client = ask_api or LH._youtube_client()
        run = LH._execute_youtube_request
        if not channel_id and handle:
            found = run(client.channels().list(part="id", forHandle=handle)).get("items") or []
            channel_id = found[0]["id"] if found else ""
        if not channel_id and video_id:
            found = run(client.videos().list(part="snippet", id=video_id)).get("items") or []
            channel_id = found[0]["snippet"]["channelId"] if found else ""
        if not channel_id:
            return {}
        channel = (run(client.channels().list(part="snippet,statistics,contentDetails", id=channel_id)).get("items") or [{}])[0]
        uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
        items = run(client.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=30)).get("items") or []
        ids = [i["contentDetails"]["videoId"] for i in items]
        videos = run(client.videos().list(part="snippet,contentDetails", id=",".join(ids))).get("items") if ids else []
    except Exception as exc:  # noqa: BLE001 - no key, quota, private channel
        logger.info("YouTube API lookup failed (%s): trying the channel's public feed.", exc)
        return public_youtube(urls)
    now = datetime.now(timezone.utc)
    shorts = long_recent = 0
    latest: dict[str, Any] | None = None
    titles: list[str] = []
    for video in videos or []:
        seconds = LH._duration_seconds(video["contentDetails"].get("duration")) or 0
        published = datetime.fromisoformat(video["snippet"]["publishedAt"].replace("Z", "+00:00"))
        fresh = now - published <= timedelta(days=30)
        if seconds <= LH.SHORTS_MAX_SECONDS:
            shorts += fresh
            continue
        long_recent += fresh
        titles.append(video["snippet"]["title"])
        # Round 51: an upload whose audio isn't English (or is a dub of something that isn't)
        # is never the one to cut: the worker would skip it as auto-dubbed anyway.
        audio = str(video["snippet"].get("defaultAudioLanguage") or "").lower()
        if audio and not audio.startswith("en"):
            continue
        if seconds >= 240 and latest is None and now - published <= timedelta(days=60):
            latest = {"latest_video_url": f"https://www.youtube.com/watch?v={video['id']}",
                      "latest_video_title": video["snippet"]["title"], "duration_seconds": seconds}
    stats = channel.get("statistics") or {}
    return {"channel_id": channel_id, "channel_title": (channel.get("snippet") or {}).get("title", ""),
            "subscriber_count": int(stats.get("subscriberCount") or 0) if str(stats.get("subscriberCount", "")).isdigit() else None,
            "shorts_30d": shorts, "long_30d": long_recent, "titles": titles[:8], **(latest or {})}


def _channel_from_page(url: str) -> tuple[str, str]:
    """(channel id, channel title) from a youtube.com/@handle, /channel or watch page, key-free."""
    page = fetch(url, max_bytes=1_500_000)
    if not page:
        return "", ""
    match = (re.search(r'"externalId":"(UC[A-Za-z0-9_-]{22})"', page)
             or re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"', page)
             or re.search(r'"channelId":"(UC[A-Za-z0-9_-]{22})"', page))
    title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    return (match.group(1) if match else "", html.unescape(title.group(1)) if title else "")


def public_youtube(urls: list[str], now: datetime | None = None) -> dict[str, Any]:
    """youtube_target without the API (no key, or the quota is used up): the channel's public
    feed has its 15 newest uploads, and Shorts show there as /shorts/ links. No durations or
    subscriber counts this way."""
    channel_id, handle, video_id = "", "", ""
    for url in urls:
        match = YOUTUBE.search(url)
        if match:
            channel_id = channel_id or (match.group(2) or "")
            handle = handle or (match.group(3) or "")
            video_id = video_id or (match.group(4) or match.group(5) or "")
    if not channel_id and handle:
        channel_id = _channel_from_page(f"https://www.youtube.com/@{handle}")[0]
    if not channel_id and video_id:
        channel_id = _channel_from_page(f"https://www.youtube.com/watch?v={video_id}")[0]
    if not channel_id:
        return {}
    raw = fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    try:
        root = ET.fromstring(raw.encode("utf-8")) if raw else None
    except ET.ParseError:
        root = None
    if root is None:
        return {}
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    now = now or datetime.now(timezone.utc)
    shorts = long_recent = 0
    latest: dict[str, Any] | None = None
    titles: list[str] = []
    for entry in root.findall("a:entry", ns):
        link = (entry.find("a:link", ns).get("href") if entry.find("a:link", ns) is not None else "") or ""
        vid = _text(entry.find("yt:videoId", ns))
        title = _text(entry.find("a:title", ns))
        try:
            published = datetime.fromisoformat(_text(entry.find("a:published", ns)).replace("Z", "+00:00"))
        except ValueError:
            continue
        fresh = now - published <= timedelta(days=30)
        if "/shorts/" in link:
            shorts += fresh
            continue
        long_recent += fresh
        titles.append(title)
        if latest is None and vid and now - published <= timedelta(days=60):
            latest = {"latest_video_url": f"https://www.youtube.com/watch?v={vid}", "latest_video_title": title,
                      "duration_seconds": None}
    return {"channel_id": channel_id, "channel_title": _text(root.find("a:title", ns)), "subscriber_count": None,
            "shorts_30d": shorts, "long_30d": long_recent, "titles": titles[:8], "youtube_via": "public feed",
            **(latest or {})}


_TITLE_STOP = {"the", "podcast", "show", "a", "with", "official", "channel", "pod", "and", "&"}


def _norm_title(text: str) -> str:
    return " ".join(w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _TITLE_STOP)


def titles_match(channel_title: str, show: str) -> bool:
    a, b = _norm_title(channel_title), _norm_title(show)
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.72 or (len(a) >= 4 and (a in b or b in a))


def handle_guesses(show: str, domain: str = "") -> list[str]:
    words = re.findall(r"[a-z0-9]+", (show or "").lower())
    core = [w for w in words if w not in _TITLE_STOP]
    out = ["".join(words), "".join(core), "".join(core) + "podcast", domain.split(".")[0] if domain else ""]
    return [h for h in dict.fromkeys(out) if 3 <= len(h) <= 30][:4]


def guess_youtube(show: str, domain: str = "") -> list[str]:
    """The channel URL of an @handle made from the show's name (or its domain) that exists
    and whose title matches the show, key-free; [] when none does."""
    for handle in handle_guesses(show, domain):
        channel_id, title = _channel_from_page(f"https://www.youtube.com/@{handle}")
        if channel_id and (titles_match(title, show) or _norm_title(title) == handle):
            return [f"https://www.youtube.com/channel/{channel_id}"]
    return []


def channel_about(channel_id: str, url: str = "") -> dict[str, Any]:
    """A YouTube channel's title, description and the links in it (API, else its About page)."""
    out: dict[str, Any] = {"title": "", "description": "", "links": []}
    if channel_id:
        try:
            from core import lead_harvester as LH

            item = (LH._execute_youtube_request(LH._youtube_client().channels().list(
                part="snippet", id=channel_id)).get("items") or [{}])[0]
            out["title"] = (item.get("snippet") or {}).get("title", "")
            out["description"] = (item.get("snippet") or {}).get("description", "")
        except Exception as exc:  # noqa: BLE001
            logger.debug("channels.list failed: %s", exc)
    if not out["description"]:
        page = fetch((f"https://www.youtube.com/channel/{channel_id}" if channel_id else url.rstrip("/")) + "/about",
                     max_bytes=2_000_000) or ""
        description = re.search(r'<meta name="description" content="([^"]*)"', page)
        out["description"] = html.unescape(description.group(1)) if description else ""
        title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
        out["title"] = out["title"] or (html.unescape(title.group(1)) if title else "")
        out["links"] = [urllib.parse.unquote(q) for q in re.findall(r"[?&]q=(https?%3A%2F%2F[^&\"\\]+)", page)]
    out["links"] = list(dict.fromkeys(out["links"] + re.findall(r"https?://[^\s\"'<>)]+", out["description"])))
    return out


# ------------------------------------------------------------------------------ scoring
CHECK_POINTS = {"no": 15, "rare": 8, "unknown": 0, "yes": -20}


def score(p: dict[str, Any]) -> tuple[int, list[str]]:
    """The LinkedIn Video Gap score (0-100) and why."""
    points, why = 0, []
    text = " ".join([p.get("show", ""), p.get("description", ""), " ".join(p.get("genres") or []),
                     " ".join((p.get("calendar_angle") or {}).get("recent_titles") or [])])
    hits = len(set(m.lower() for m in B2B_WORDS.findall(text)))
    fit = min(25, hits * 5)
    if OFF_NICHE.search(p.get("show", "")):
        fit = 0
        why.append("off-niche show name")
    points += fit
    why.append(f"B2B fit {fit}/25 ({hits} signal{'s' if hits != 1 else ''})")
    if p.get("linkedin_company") or p.get("linkedin_people"):
        points += 20
        why.append("+20 links its own LinkedIn (LinkedIn is a channel for them)")
    cadence = int(p.get("episodes_30d") or 0)
    if cadence >= 2:
        points += 15
        why.append(f"+15 {cadence} long episodes in 30 days")
    elif cadence == 1:
        points += 8
        why.append("+8 one long episode in 30 days")
    if p.get("latest_video_url"):
        points += 10
        why.append("+10 a recent YouTube episode to cut the samples from")
        shorts = p.get("shorts_30d")
        if shorts == 0:
            points += 15
            why.append("+15 no vertical video on YouTube in 30 days (the gap)")
        elif isinstance(shorts, int) and shorts <= 3:
            points += 7
            why.append(f"+7 only {shorts} vertical video(s) in 30 days")
        elif isinstance(shorts, int):
            why.append(f"+0 already posting vertical ({shorts} in 30 days)")
    contact = p.get("contact") or {}
    status = contact.get("status")
    if status == "verified" and not contact.get("shared"):
        bonus = 15 if contact.get("name") else 10
    elif status in {"verified", "published"}:
        bonus = 6 if contact.get("shared") else 10
    elif status == "guess":
        bonus = 4
    else:
        bonus = 0
    points += bonus
    why.append(f"+{bonus} contact: {status or 'none'}" + (" (shared inbox)" if contact.get("shared") else ""))
    host = p.get("host_platform") or {}
    if host.get("paid"):
        points += 5
        why.append(f"+5 pays for podcast hosting ({host.get('name')})")
    if p.get("video_stack"):
        points += 5
        why.append(f"+5 records video for marketing ({', '.join(p['video_stack'][:3])})")
    if p.get("agency"):
        why.append(f"+0 produced by {p['agency']}: the budget is there, but ask whether clips are in their package")
    if p.get("media_network"):
        points -= 25
        why.append(f"-25 a media-company show ({p['media_network']}): in-house producers")
    rank = p.get("chart_rank")
    if isinstance(rank, int) and rank <= 100:
        points -= 10
        why.append(f"-10 #{rank} on Apple's {p.get('discovered_via', 'chart').split(': ')[-1]} chart: a big show")
    subs = p.get("subscriber_count")
    if isinstance(subs, int) and subs > 1_000_000:
        points -= 15
        why.append("-15 over 1M YouTube subscribers: likely an in-house team")
    check = p.get("linkedin_native_video") or "unknown"
    if CHECK_POINTS.get(check):
        points += CHECK_POINTS[check]
        why.append(f"{CHECK_POINTS[check]:+d} posts native video on LinkedIn: {check}")
    return max(0, min(100, points)), why


def tier(points: int) -> str:
    return "hot" if points >= 70 else "warm" if points >= 50 else "cool"


# ------------------------------------------------------------------------------ prospects
def prospect_id(feed_url: str = "", channel_id: str = "") -> str:
    return channel_id or "rss-" + hashlib.sha1(feed_url.encode()).hexdigest()[:12]


INACTIVE_DAYS = 120  # no new episode in this long: the show has stopped
DROP_REASONS = {
    "duplicate": "already found, queued, or looked at recently", "off_niche": "off-niche show name",
    "fetch_failed": "feed / site didn't load (retried in a week)", "no_feed": "feed unreadable (retried in a week)",
    "no_show": "company without a podcast or YouTube channel", "inactive": f"no new episode in {INACTIVE_DAYS} days",
    "not_b2b": "no B2B signal in its name, description or titles", "contacted": "already pitched or passed",
    "error": "an error while reading it (retried in a week)",
}
NOT_LOADED = {"fetch_failed", "no_feed", "no_show", "error"}


def enrich(seed: dict[str, Any], ask: Callable[[str, str], str | None] | None = None,
           youtube: Callable[[list[str]], dict[str, Any]] | None = None,
           about: Callable[[str, str], dict[str, Any]] | None = None) -> tuple[dict[str, Any] | None, str]:
    """One seed from any source (a feed, a YouTube channel or a company site), enriched and
    scored: (prospect, "saved") or (None, the reason it was dropped: a DROP_REASONS key)."""
    from core import b2b_sources

    feed_url = str(seed.get("feed_url") or "")
    seed_site = normalize_site(str(seed.get("website") or ""))
    yt_urls = [f"https://www.youtube.com/channel/{seed['channel_id']}"] if seed.get("channel_id") else []
    yt_urls += [str(seed["youtube_url"])] if seed.get("youtube_url") else []
    extra_text, extra_emails, show_name = str(seed.get("description") or ""), [], str(seed.get("show") or "")
    site: Site | None = None
    if yt_urls and not feed_url:  # a YouTube channel first: its description has the website
        info = (about or channel_about)(str(seed.get("channel_id") or ""), yt_urls[0])
        extra_text += " " + str(info.get("description") or "")
        show_name = show_name or str(info.get("title") or "")
        extra_emails = sorted({e.lower().rstrip(".") for e in EMAIL.findall(str(info.get("description") or ""))})
        seed_site = seed_site or next((normalize_site(l) for l in info.get("links") or []
                                       if not _HOSTING.search(domain_of(l)) and "linkedin.com" not in l
                                       and not YOUTUBE.search(l)), "")
    if not feed_url and not yt_urls:  # a company first: does it have a show?
        if not seed_site:
            return None, "no_show"
        site = scan_website(seed_site)
        if not site.pages:
            return None, "fetch_failed"
        feed_url = site.feeds[0] if site.feeds else ""
        if not feed_url and site.apple_ids:
            looked = b2b_sources.apple_lookup(site.apple_ids[:1], str(seed.get("source") or ""))
            feed_url = looked[0]["feed_url"] if looked else ""
        yt_urls = [u for u in site.youtube if "/channel/" in u or "/@" in u][:2]
        if not feed_url and not yt_urls:
            return None, "no_show"
    feed = Feed()
    if feed_url:
        raw = fetch(feed_url, max_bytes=FEED_MAX_BYTES)
        feed = parse_feed(raw or "")
        if not feed.title and not feed.episodes and not yt_urls:
            return None, "fetch_failed" if raw is None else "no_feed"
    website = feed.website if feed.website.startswith("http") and not _HOSTING.search(domain_of(feed.website)) else ""
    website = website or (seed_site if seed_site and not _HOSTING.search(domain_of(seed_site)) else "")
    if site is None or not site.pages or domain_of(site.pages[0]) != domain_of(website):
        site = scan_website(website) if website else Site()
    domain = domain_of(website) if website else ""
    yt_lookup = youtube or youtube_target
    yt = yt_lookup(site.youtube + [l for l in feed.links if "youtu" in l] + yt_urls)
    if not yt:
        guessed = guess_youtube(show_name or feed.title, domain)
        yt = yt_lookup(guessed) if guessed else {}
    now = datetime.now(timezone.utc)
    dates = [datetime.fromisoformat(e["date"]) for e in feed.episodes if e.get("date")]
    if dates and now - max(dates) > timedelta(days=INACTIVE_DAYS) and not yt.get("long_30d"):
        return None, "inactive"
    if not feed.episodes and yt and not yt.get("latest_video_url") and not yt.get("long_30d"):
        return None, "inactive"
    if not feed.episodes and not yt:
        return None, "no_show"
    last_30 = recent(feed.episodes, 30)
    titles = [e["title"] for e in recent(feed.episodes, 45)] or (yt.get("titles") or [])
    show_name = show_name or feed.title or str(yt.get("channel_title") or "")
    fit_text = " ".join([show_name, feed.description, extra_text, " ".join(seed.get("genres") or []), " ".join(titles)])
    if OFF_NICHE.search(show_name) or not B2B_WORDS.search(fit_text):
        return None, "not_b2b"
    contacts = contacts_for(feed, site, domain)
    for email in extra_emails:
        if EMAIL.fullmatch(email) and not any(c.email == email for c in contacts):
            contact = Contact("", "On their YouTube channel", email, email_status(email, True),
                              "YouTube channel description", "", is_shared(email))
            contact.confidence = confidence_of(contact)
            contacts.append(contact)
    linkedin_company = site.linkedin_company or next((l for l in feed.links if LINKEDIN.match(l) and "/company/" in l), "")
    # Round 53: the company's marketing or content lead, from the pages already read (no extra requests)
    from core import marketing_contact

    try:
        marketer = marketing_contact.find(website, bodies=site.bodies, known_emails=site.emails + [c.email for c in contacts],
                                          known_names=[c.name for c in contacts if c.name]) if website else {}
    except Exception:  # noqa: BLE001 - a page we can't parse never costs the prospect
        logger.debug("Marketing contact lookup failed for %s", website, exc_info=True)
        marketer = {}
    all_text = " ".join([feed.description, " ".join(feed.links)])
    channel_id = str(yt.get("channel_id") or "")
    prospect: dict[str, Any] = {
        "id": prospect_id(feed_url, channel_id) if (feed_url or channel_id) else "web-" + hashlib.sha1(domain.encode()).hexdigest()[:12],
        "show": show_name, "author": seed.get("author") or feed.author,
        "genres": seed.get("genres") or [], "description": (feed.description or extra_text.strip())[:600],
        "feed_url": feed_url, "apple_url": seed.get("apple_url", ""), "website": website, "domain": domain,
        "linkedin_company": linkedin_company,
        "linkedin_people": sorted(set(site.linkedin_people + [l for l in feed.links if "linkedin.com/in/" in l]))[:5],
        "episodes_30d": max(len(last_30), int(yt.get("long_30d") or 0)),
        "contact": contacts[0].as_dict() if contacts else {}, "contacts": [c.as_dict() for c in contacts[:6]],
        "email_confidence": contacts[0].confidence if contacts else "none",
        "calendar_angle": calendar_angle(titles, ask),
        "linkedin_native_video": "unknown", "status": "new",
        **({"marketing_contact": marketer} if marketer else {}),
        "harvested_at": now.isoformat(),
        # Round 47: where it was found and what its stack says about budget
        "sources": [str(seed.get("source") or "apple_search")], "discovered_via": str(seed.get("query") or ""),
        "host_platform": host_platform(feed_url), "video_stack": site.video_stack,
        "agency": site.agency or agency_named(all_text), "contact_form": site.contact_form,
        "media_network": media_network(feed.owner_email, domain, *[c.email for c in contacts[:3]]),
        **({"chart_rank": seed["chart_rank"]} if isinstance(seed.get("chart_rank"), int) else {}),
        **{k: v for k, v in yt.items() if k != "titles"},
    }
    prospect["score"], prospect["reasons"] = score(prospect)
    prospect["tier"] = tier(prospect["score"])
    return prospect, "saved"


def build_prospect(show: dict[str, Any], ask: Callable[[str, str], str | None] | None = None,
                   youtube: Callable[[list[str]], dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """One show, fully enriched and scored (None when it's dropped: see enrich)."""
    return enrich(show, ask, youtube)[0]


def load_prospects(path: Path | None = None) -> list[dict[str, Any]]:
    try:
        data = json.loads((path or PROSPECTS_PATH).read_text(encoding="utf-8"))
        return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_prospects(prospects: list[dict[str, Any]], path: Path | None = None) -> None:
    target = path or PROSPECTS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=target.parent, suffix=".tmp", encoding="utf-8") as tmp:
        json.dump(prospects, tmp, indent=2)
    Path(tmp.name).replace(target)


def _already_contacted() -> tuple[set[str], set[str]]:
    """(channel ids, email domains) already queued, pitched or passed."""
    ids: set[str] = set()
    domains: set[str] = set()
    try:
        from core.lead_harvester import _read_contacted_channel_ids

        ids = _read_contacted_channel_ids()
    except Exception:  # noqa: BLE001
        pass
    try:
        pending = json.loads((PROJECT_ROOT / "storage" / "pending_outreach.json").read_text(encoding="utf-8"))
        for record in pending if isinstance(pending, list) else []:
            ids.add(str(record.get("channel_id") or ""))
            mail = str(record.get("contact_email") or "")
            if "@" in mail and not _HOSTING.search(mail.split("@")[1]):
                domains.add(mail.split("@")[1].lower())
    except (OSError, ValueError):
        pass
    return ids, domains


LAST_REPORT: dict[str, Any] = {}
WORKERS = 8


def _known_keys(prospects: list[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for p in prospects:
        if p.get("feed_url"):
            keys.add("feed:" + str(p["feed_url"]).strip().lower().rstrip("/"))
        if p.get("channel_id"):
            keys.add("yt:" + str(p["channel_id"]))
        if p.get("domain"):
            keys.add("web:" + str(p["domain"]))
    return keys


def _interleave(seeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin by source, so a run's first enrichments cover every source."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for seed in seeds:
        groups.setdefault(str(seed.get("source") or "?"), []).append(seed)
    out: list[dict[str, Any]] = []
    while any(groups.values()):
        for items in groups.values():
            if items:
                out.append(items.pop(0))
    return out


def harvest(queries: list[str] | None = None, limit: int = 15, per_query: int = 100,
            ask: Callable[[str, str], str | None] | None = None,
            youtube: Callable[[list[str]], dict[str, Any]] | None = None,
            path: Path | None = None, *, niche: str | None = None, sources: str | list[str] | None = None,
            minutes: float = 6.0, report: dict[str, Any] | None = None,
            about: Callable[[str, str], dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Find, enrich and score up to ``limit`` new B2B shows; saved to storage/b2b_prospects.json
    (best first). ``queries``: just these Apple searches (the Round 46 way); otherwise the
    sources (core.b2b_sources) take turns, each from where its last run stopped. Shows already
    found, looked at, queued, pitched or passed are skipped. The funnel is in ``report`` and
    LAST_REPORT (format_report prints it)."""
    from core import b2b_sources as S

    state_path = S.state_path_for(path)
    state = S.load_state(state_path)
    existing = load_prospects(path)
    known = _known_keys(existing)
    chosen = ["apple_search"] if queries else S.parse_sources(sources)
    if niche:
        S.keyword_matrix(niche)  # an unknown niche fails here, before anything runs
    rep: dict[str, Any] = {"raw_fetched": 0, "deduplicated": 0, "enriched": 0, "active_b2b": 0, "saved": 0,
                           "long_video": 0, "with_email": 0, "hot": 0, "confidence": Counter(), "dropped": Counter(),
                           "by_source": {}, "backlog_in": 0, "backlog_left": 0, "niche": niche or "all",
                           "sources": chosen, "started": datetime.now(timezone.utc).isoformat()}
    FETCH_STATS.clear()
    pool: list[dict[str, Any]] = []
    pool_keys: set[str] = set()

    def source_counts(name: str) -> dict[str, int]:
        return rep["by_source"].setdefault(name, {"found": 0, "new": 0, "saved": 0})

    def take(seed: dict[str, Any]) -> None:
        rep["raw_fetched"] += 1
        counts = source_counts(str(seed.get("source") or "apple_search"))
        counts["found"] += 1
        key = S.seed_key(seed)
        if key in pool_keys or key in known or S.is_seen(state, key):
            rep["dropped"]["duplicate"] += 1
            return
        if OFF_NICHE.search(str(seed.get("show") or "")):
            rep["dropped"]["off_niche"] += 1
            S.mark_seen(state, key, "off_niche")
            return
        pool_keys.add(key)
        pool.append(seed)
        counts["new"] += 1

    if queries:
        for query in queries:
            for show in search_podcasts(query, per_query):
                take({**show, "source": "apple_search", "query": query})
    else:
        backlog = [b for b in state.get("backlog") or [] if isinstance(b, dict)]
        state["backlog"] = []
        rep["backlog_in"] = len(backlog)
        for seed in backlog:
            take(seed)
        steps = {name: S.steps_for(name) for name in chosen}
        target = max(limit * 6, 30)
        while len(pool) < target and any(v > 0 for v in steps.values()):
            for name in chosen:
                if steps[name] <= 0:
                    continue
                steps[name] -= 1
                try:
                    seeds = S.SOURCE_FNS[name](state, niche)
                except Exception as exc:  # noqa: BLE001 - one source failing never stops the others
                    logger.warning("Source %s failed: %s", name, exc)
                    seeds, steps[name] = [], 0
                if not seeds and name in {"seed_pages", "podcastindex", "youtube", "yc"}:
                    steps[name] = 0  # nothing (more) there this run
                for seed in seeds:
                    take(seed)
    rep["deduplicated"] = len(pool)
    contacted_ids, contacted_domains = _already_contacted()
    found: list[dict[str, Any]] = []
    found_ids: set[str] = set()
    seed_of: dict[str, dict[str, Any]] = {}
    todo = _interleave(pool)
    deadline = time.monotonic() + minutes * 60
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        running: dict[Any, dict[str, Any]] = {}
        while todo or running:
            room = min(WORKERS, max(2, (limit - len(found)) * 2))  # about twice what's still needed
            while todo and len(running) < room and len(found) < limit and time.monotonic() < deadline:
                seed = todo.pop(0)
                running[executor.submit(_safe_enrich, seed, ask, youtube, about)] = seed
            if not running:
                break
            done, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for future in done:
                seed = running.pop(future)
                prospect, why = future.result()
                key = S.seed_key(seed)
                if why in NOT_LOADED:
                    rep["dropped"][why] += 1
                    S.mark_seen(state, key, why)
                    continue
                rep["enriched"] += 1
                if prospect is not None and (prospect["id"] in contacted_ids or prospect.get("domain") in contacted_domains):
                    prospect, why = None, "contacted"
                if prospect is not None and (prospect["id"] in found_ids or prospect["id"] in {p.get("id") for p in existing}):
                    prospect, why = None, "duplicate"
                if prospect is None:
                    rep["dropped"][why] += 1
                    S.mark_seen(state, key, why)
                    continue
                rep["active_b2b"] += 1
                found.append(prospect)
                seed_of[prospect["id"]] = seed
                found_ids.add(prospect["id"])
                S.mark_seen(state, key, "saved")
                for other in _known_keys([prospect]):
                    S.mark_seen(state, other, "saved")
                source_counts(prospect["sources"][0])["saved"] += 1
    if len(found) > limit:  # the last few in flight: keep the best, the rest wait for the next run
        found.sort(key=lambda p: -int(p.get("score") or 0))
        for extra in found[limit:]:
            for key in {S.seed_key(seed_of[extra["id"]])} | _known_keys([extra]):
                state["seen"].pop(key, None)
            todo.insert(0, seed_of[extra["id"]])
            rep["by_source"][extra["sources"][0]]["saved"] -= 1
        found = found[:limit]
    if not queries:
        state["backlog"] = todo + state.get("backlog", [])
    rep["backlog_left"] = len(todo)
    merged = sorted(existing + found, key=lambda p: -int(p.get("score") or 0))
    save_prospects(merged, path)
    rep.update(saved=len(found), long_video=sum(bool(p.get("latest_video_url")) for p in found),
               with_email=sum(bool((p.get("contact") or {}).get("email")) for p in found),
               hot=sum(p["tier"] == "hot" for p in found),
               confidence=Counter(p.get("email_confidence") or "none" for p in found),
               fetch_failed=FETCH_STATS["failed"], ssl_errors=FETCH_STATS["ssl"],
               finished=datetime.now(timezone.utc).isoformat())
    state["runs"].append({k: (dict(v) if isinstance(v, Counter) else v) for k, v in rep.items()})
    S.save_state(state, state_path)
    LAST_REPORT.clear()
    LAST_REPORT.update(rep)
    if report is not None:
        report.update(rep)
    logger.info("Harvested %d new B2B prospect(s) (%d hot).", len(found), rep["hot"])
    return found


def format_report(rep: dict[str, Any] | None = None) -> str:
    """The funnel of a harvest, for the terminal and the app."""
    rep = rep if rep is not None else LAST_REPORT
    if not rep:
        return "No harvest yet."
    chain = " -> ".join(f"{stage} {rep.get(stage, 0)}" for stage in
                        ("raw_fetched", "deduplicated", "enriched", "active_b2b", "saved"))
    lines = [f"[{chain}]"]
    conf = rep.get("confidence") or {}
    lines.append(f"  saved {rep.get('saved', 0)} ({rep.get('hot', 0)} hot): {rep.get('long_video', 0)} with a recent "
                 f"long video to cut, {rep.get('with_email', 0)} with an email"
                 + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(conf.items(), key=lambda kv: -kv[1])) + ")" if conf else ""))
    dropped = rep.get("dropped") or {}
    if dropped:
        lines.append("  dropped: " + ", ".join(f"{k} {v}" for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])))
    by_source = rep.get("by_source") or {}
    if by_source:
        lines.append("  by source: " + ", ".join(f"{k} {v['found']} found / {v['new']} new / {v['saved']} saved"
                                                 for k, v in by_source.items()))
    if rep.get("backlog_in") or rep.get("backlog_left"):
        lines.append(f"  backlog: {rep.get('backlog_in', 0)} carried in, {rep.get('backlog_left', 0)} left for the next run")
    if rep.get("ssl_errors"):
        lines.append(f"  ⚠️ {rep['ssl_errors']} HTTPS certificate failure(s): run .venv/bin/pip install -U certifi")
    return "\n".join(lines)


def _safe_enrich(seed: dict[str, Any], ask: Any, youtube: Any, about: Any = None) -> tuple[dict[str, Any] | None, str]:
    try:
        return enrich(seed, ask, youtube, about)
    except Exception as exc:  # noqa: BLE001 - one bad site never stops a harvest
        logger.warning("Couldn't enrich %s: %s", seed.get("show") or seed.get("feed_url") or seed.get("website"), exc)
        return None, "error"


def _safe_build(show: dict[str, Any], ask: Any, youtube: Any) -> dict[str, Any] | None:
    return _safe_enrich(show, ask, youtube)[0]


def set_check(prospect_id_: str, answer: str, path: Path | None = None) -> dict[str, Any]:
    """Record the manual LinkedIn check ("yes" / "rare" / "no" / "unknown") and re-score."""
    if answer not in CHECK_POINTS:
        raise ValueError("answer must be yes, rare, no or unknown")
    prospects = load_prospects(path)
    for p in prospects:
        if p.get("id") == prospect_id_:
            p["linkedin_native_video"] = answer
            p["score"], p["reasons"] = score(p)
            p["tier"] = tier(p["score"])
            save_prospects(sorted(prospects, key=lambda x: -int(x.get("score") or 0)), path)
            return p
    raise KeyError(prospect_id_)


def to_lead(p: dict[str, Any], allow_guess: bool = False) -> dict[str, Any]:
    """The worker's lead for a prospect (needs a YouTube episode to cut). The email is the
    best contact we may send to (Round 47: a published shared inbox when the named lead's
    address is only a guess); the greeting keeps the named lead."""
    best = p.get("contact") or {}
    contacts = [c for c in p.get("contacts") or [] if isinstance(c, dict)] or [best]
    chosen = next((c for c in contacts if c.get("email") and queueable(c, allow_guess)), {})
    contact = dict(chosen or best)
    if not contact.get("name") and best.get("name") and contact.get("email", "").split("@")[-1] == best.get("email", "").split("@")[-1]:
        contact.update(name=best["name"], role=best.get("role", ""), linkedin=best.get("linkedin", ""))
    email = contact.get("email", "") if chosen else ""
    channel_id = p.get("channel_id") or ""
    return {
        "channel_id": channel_id, "channel_title": p.get("channel_title") or p.get("show", ""),
        "channel_url": f"https://www.youtube.com/channel/{channel_id}", "subscriber_count": p.get("subscriber_count"),
        "latest_video_url": p.get("latest_video_url", ""), "latest_video_title": p.get("latest_video_title", ""),
        "duration_seconds": p.get("duration_seconds"), "contact_email": email or None,
        "contact_info": email or p.get("website") or p.get("apple_url") or "",
        "contact_name": contact.get("name", ""), "contact_role": contact.get("role", ""),
        "linkedin_url": contact.get("linkedin") or p.get("linkedin_company") or "",
        "business_offer": ["B2B show (LinkedIn Video Gap)"], "priority": 4 + int(p.get("score") or 0) // 15,
        "calendar_angle": p.get("calendar_angle") or {}, "source": "b2b_harvester", "prospect_id": p.get("id"),
        "contact_confidence": confidence_of(contact) if chosen else "none", "website": p.get("website", ""),
        "shorts_summary": f"{p.get('shorts_30d', '?')} vertical video(s) on YouTube in 30 days",
        # Round 53: buyer fit reads these; the Outbox offers the marketing contact as a second email
        **{key: p[key] for key in ("marketing_contact", "description", "linkedin_company") if p.get(key)},
    }


def queue(ids: list[str], path: Path | None = None, enqueue: Callable[[list[dict[str, Any]]], int] | None = None,
          allow_guess: bool = False) -> int:
    """Put prospects into the worker's queue (only those with a YouTube episode to cut)."""
    prospects = load_prospects(path)
    chosen = [p for p in prospects if p.get("id") in ids and p.get("latest_video_url") and p.get("channel_id")]
    if enqueue is None:
        from autonomous_worker import _enqueue_leads as enqueue
    added = enqueue([to_lead(p, allow_guess) for p in chosen])
    for p in chosen:
        p["status"] = "queued"
    save_prospects(prospects, path)
    return added


# --------------------------------------------------------- Round 51: pass, clear, direct pitch
HIDDEN = {"passed", "cleared"}  # never shown again; kept on file so a harvest never brings them back


def has_upload(p: dict[str, Any]) -> bool:
    """A recent YouTube episode the worker can cut sample clips from."""
    return bool(p.get("latest_video_url") and p.get("channel_id"))


def visible(prospects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [p for p in prospects if p.get("status") not in HIDDEN]


def pass_prospects(ids: list[str], reason: str = "passed", path: Path | None = None) -> int:
    """Pass on prospects (reason "passed" or "cleared"): they leave the prospects list for good.
    They stay in the file with that status, so no later harvest adds them again."""
    wanted = set(ids)
    prospects = load_prospects(path)
    now = datetime.now(timezone.utc).isoformat()
    changed = 0
    for p in prospects:
        if p.get("id") in wanted and p.get("status") not in HIDDEN:
            p["status"], p["passed_at"] = ("cleared" if reason == "cleared" else "passed"), now
            changed += 1
    if changed:
        save_prospects(prospects, path)
    return changed


def direct_pitch_record(p: dict[str, Any], allow_guess: bool = False) -> dict[str, Any]:
    """The Outbox record for a direct pitch: the intro email (core.outreach.build_intro_email),
    its subject, and everything the Outbox shows to identify and reach them."""
    from core.outreach import (SITE_URL, build_intro_email, build_intro_subject, clean_first_name,
                               greeting_name_of)

    lead = to_lead(p, allow_guess)
    first = clean_first_name(str(lead.get("contact_name") or "").split(" ")[0])
    angle = p.get("calendar_angle") if isinstance(p.get("calendar_angle"), dict) else {}
    show = str(p.get("show") or p.get("channel_title") or "")
    body = build_intro_email(show, first, str(p.get("latest_video_title") or ""), str(angle.get("theme") or ""))
    email = str(lead.get("contact_email") or "")
    return {
        "record_id": f"intro-{p.get('id')}", "kind": "intro", "channel_id": "", "channel_title": show,
        "contact_email": email or str(p.get("contact_form") or p.get("website") or ""),
        "subject": build_intro_subject(show), "full_email_body": body, "greeting_name": greeting_name_of(body),
        "video_title": "", "site_url": SITE_URL, "prospect_id": p.get("id"),
        "contact_name": lead.get("contact_name", ""), "contact_role": lead.get("contact_role", ""),
        "contact_confidence": lead.get("contact_confidence", "none"), "website": p.get("website", ""),
        "linkedin_url": lead.get("linkedin_url", ""), "business_offer": lead.get("business_offer"),
        "pitch": {"format": "episode", "business": True, **({"theme": angle["theme"]} if angle.get("theme") else {})},
    }


def direct_pitch(ids: list[str], path: Path | None = None, add: Callable[[dict[str, Any]], bool] | None = None,
                 allow_guess: bool = False) -> int:
    """Round 51: prospects with no upload to cut go straight to the Outbox as a direct pitch
    (an email only; the worker does nothing for them). Returns how many were added."""
    prospects = load_prospects(path)
    if add is None:
        from autonomous_worker import add_direct_pitch as add
    added = 0
    for p in prospects:
        if p.get("id") in ids and p.get("status") not in HIDDEN | {"direct", "queued", "partner"}:
            if add(direct_pitch_record(p, allow_guess)):
                added += 1
            p["status"] = "direct"
    save_prospects(prospects, path)
    return added


# --------------------------------------------------------- Round 54: partner pitch
def partner_pitch_record(p: dict[str, Any], allow_guess: bool = False) -> dict[str, Any]:
    """The Outbox record for an agency (core.fit grade "partner"): a direct pitch with the
    white-label email (core.outreach.build_partner_email) and its own follow-ups and yes-reply."""
    from core.outreach import PARTNER_SUBJECT, build_partner_email, greeting_name_of

    record = direct_pitch_record(p, allow_guess)
    first = str(record.get("contact_name") or "").split(" ")[0]
    body = build_partner_email(str(p.get("show") or p.get("channel_title") or ""), first)
    return {**record, "record_id": f"partner-{p.get('id')}", "partner": True, "subject": PARTNER_SUBJECT,
            "full_email_body": body, "greeting_name": greeting_name_of(body)}


def partner_pitch(ids: list[str], path: Path | None = None, add: Callable[[dict[str, Any]], bool] | None = None,
                  allow_guess: bool = False) -> int:
    """Round 54: agencies go to the Outbox with the partner pitch (an email only). Returns how many were added."""
    prospects = load_prospects(path)
    if add is None:
        from autonomous_worker import add_direct_pitch as add
    added = 0
    for p in prospects:
        if p.get("id") in ids and p.get("status") not in HIDDEN | {"direct", "queued", "partner"}:
            if add(partner_pitch_record(p, allow_guess)):
                added += 1
            p["status"] = "partner"
    save_prospects(prospects, path)
    return added


# ------------------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    from core import b2b_sources as S

    parser = argparse.ArgumentParser(prog="python -m core.linkedin_harvester",
                                     description="Find B2B shows with a LinkedIn Video Gap.")
    sub = parser.add_subparsers(dest="command", required=True)
    h = sub.add_parser("harvest", help="find, enrich and score new shows")
    h.add_argument("--query", action="append", help="just this Apple search phrase (repeatable)")
    h.add_argument("--limit", type=int, default=15)
    h.add_argument("--niche", choices=S.niche_names(), help="one vertical (default: all of them, in turn)")
    h.add_argument("--source", default="all", help=f"comma list of: {', '.join(S.SOURCES)} (default: all)")
    h.add_argument("--minutes", type=float, default=6.0, help="time budget for enriching (default 6)")
    h.add_argument("--queue-min", type=int, default=0, help="queue prospects at or above this score (0: none)")
    sub.add_parser("list", help="show saved prospects, best first")
    q = sub.add_parser("queue", help="queue prospects for the worker")
    q.add_argument("ids", nargs="+")
    q.add_argument("--allow-guess", action="store_true", help="may email a first.last@ pattern guess (can bounce)")
    c = sub.add_parser("check", help="record the manual LinkedIn check")
    c.add_argument("id")
    c.add_argument("answer", choices=sorted(CHECK_POINTS))
    sub.add_parser("sources", help="the sources, niches and where each cursor is")
    sub.add_parser("funnel", help="the funnel of the last runs")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if args.command == "harvest":
        try:
            from core.graphics_director import ask_ai
        except Exception:  # noqa: BLE001 - themes then come from the titles alone
            ask_ai = None
        try:
            found = harvest(args.query, args.limit, ask=ask_ai, niche=args.niche,
                            sources=None if args.query else args.source, minutes=args.minutes)
        except ValueError as exc:
            parser.error(str(exc))
        for p in found:
            contact = p.get("contact") or {}
            print(f"{p['score']:3d} {p['tier']:4s}  {p['show'][:44]:44s}  {(contact.get('email') or '-')[:32]:32s} "
                  f"{p.get('email_confidence', '-'):19s} {p['sources'][0]:12s} {p['id']}")
        print(format_report())
        print(f"Found {len(found)} new prospect(s), {sum(p['tier'] == 'hot' for p in found)} hot.")
        if args.queue_min:
            print(f"Queued {queue([p['id'] for p in found if p['score'] >= args.queue_min])} lead(s).")
    elif args.command == "list":
        for p in load_prospects():
            contact = p.get("contact") or {}
            print(f"{p.get('score', 0):3d} {p.get('tier', ''):4s} {p.get('status', ''):7s} {p.get('show', '')[:44]:44s} "
                  f"{contact.get('email', '-'):34s} {p.get('email_confidence') or contact.get('status', '-'):19s} {p.get('id')}")
    elif args.command == "queue":
        print(f"Queued {queue(args.ids, allow_guess=args.allow_guess)} lead(s).")
    elif args.command == "check":
        p = set_check(args.id, args.answer)
        print(f"{p['show']}: score {p['score']} ({p['tier']})")
    elif args.command == "sources":
        state = S.load_state()
        print("Sources:", ", ".join(S.SOURCES))
        print("Niches:", ", ".join(S.niche_names()))
        print(f"Seed pages ({S.SEED_PAGES_PATH.relative_to(S.PROJECT_ROOT)}): {len(S.seed_pages())}")
        print(f"Podcast Index key: {'set' if all(S.podcastindex_keys()) else 'not set (optional)'}")
        print(f"Shows looked at: {len(state['seen'])}, backlog: {len(state['backlog'])}")
        for name, value in sorted(state["cursors"].items()):
            print(f"  cursor {name}: {value}")
    elif args.command == "funnel":
        for run in S.load_state()["runs"][-5:]:
            print(run.get("started", "")[:16], run.get("niche"), "/", ",".join(run.get("sources") or []))
            print(format_report(run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
