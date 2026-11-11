"""Round 53: find the person who owns marketing at a prospect's company.

The executive on the show is the face; the marketing or content lead has the budget and the
problem (no time to cut video). Most of your pitches so far went to info@ inboxes, support@
and contact forms. This finds a named marketing person and their likely email:

1. Where: the company's own team, about and leadership pages (never episode, blog or press
   pages: those name guests from other companies).
2. Who: people named next to a marketing role, from the page's structured data (JSON-LD
   Person / jobTitle) or its text ("Jane Doe, Head of Marketing", or a name line followed by a
   role line). Best role first: CMO, VP or Head of Marketing, Head of Content or Brand,
   Director, Manager, then a podcast producer.
3. Their email: one published on the site that matches their name ("verified"); otherwise the
   company's own pattern, learned from other addresses it publishes for people we can name
   (jeb@salesgravy.com for Jeb Blount means first@), applied to this person ("site_pattern":
   likely, not confirmed). A plain first.last@ guess is the last resort. Nothing is ever sent
   on a guess without you seeing it; no SMTP probing.

``find(website, ...)`` returns {} or {"name", "role", "email", "status", "confidence",
"pattern", "linkedin", "source", "page"}.
"""
from __future__ import annotations

import html as html_lib
import json
import re
import urllib.parse
from typing import Any, Callable, Iterable

ROLE_RANKS: tuple[tuple[re.Pattern[str], int], ...] = tuple((re.compile(p, re.I), w) for p, w in (
    (r"\b(?:CMO|Chief Marketing Officer|Chief Brand Officer|Chief Growth Officer|Chief Content Officer)\b", 100),
    (r"\b(?:VP|Vice President|SVP|EVP|Head)\b[^,|\n]{0,12}\b(?:of\s+)?(?:Marketing|Brand)\b", 95),
    (r"\b(?:VP|Vice President|Head)\b[^,|\n]{0,12}\b(?:of\s+)?(?:Content|Communications|Comms|Demand Gen(?:eration)?|"
     r"Growth Marketing|Product Marketing|Social|Podcasts?|Media)\b", 90),
    (r"\bDirector\b[^,|\n]{0,12}\b(?:of\s+)?(?:Marketing|Brand|Content|Communications|Demand Gen(?:eration)?|"
     r"Growth Marketing|Social Media|Digital Marketing|Podcasts?)\b", 85),
    (r"\b(?:Marketing|Brand|Content|Communications|Social Media|Digital Marketing|Demand Gen(?:eration)?|"
     r"Product Marketing|Growth Marketing)\s+(?:Director|Lead|Leader)\b", 85),
    (r"\b(?:Marketing|Content|Brand|Social Media|Communications|Digital Marketing|Video|Podcast)\s+"
     r"(?:Manager|Strategist|Specialist|Coordinator|Producer|Editor)\b", 70),
    (r"\b(?:Managing Editor|Content Lead|Head of Podcast|Podcast Producer|Executive Producer)\b", 65),
    (r"\bChief of Staff\b", 45),
))
TEAM_PATH = re.compile(r"/(?:team|our-team|the-team|meet-the-team|about|about-us|who-we-are|people|leadership|"
                       r"management|company|staff|our-people|our-story)(?:[/?#.]|$)", re.I)
NOT_TEAM_PATH = re.compile(r"/(?:podcasts?|episodes?|blog|news|press|events?|webinars?|resources|case-studies|"
                           r"customers|careers|jobs|posts?|articles?|insights|shows?)(?:/|$)", re.I)
GUESS_PATHS = ("/team", "/about", "/about-us", "/company", "/leadership", "/our-team")
NAME = re.compile(r"\b[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?(?: [A-Z]\.)? [A-Z][a-z]+(?:[-'][A-Z][a-z]+)?\b")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SEPARATORS = re.compile(r"\s*(?:,|\||–|—|-|·|:)\s+")
PATTERNS = ("first", "first.last", "firstlast", "flast", "f.last", "first_last", "firstl", "last", "first-last")


def role_rank(role: str) -> int:
    """How close a job title is to the buyer (0: not a marketing role)."""
    return max((weight for pattern, weight in ROLE_RANKS if pattern.search(role or "")), default=0)


def _plain(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr|td|section|article|span|a|strong|b|em)>", "\n", page)
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", page))


def _lines(page: str) -> list[str]:
    return [" ".join(line.split()) for line in _plain(page).splitlines() if line.strip()]


def _is_name(text: str) -> str:
    from core.linkedin_harvester import person_name

    text = " ".join((text or "").split())
    match = NAME.fullmatch(text)
    return person_name(text) if match else ""


def _foreign_company(role: str, domain: str) -> bool:
    """'VP Growth at Common Room' on Acme's site is a guest, not Acme's marketer."""
    match = re.search(r"\b(?:at|@)\s+([A-Z][\w&.-]*(?:\s+[A-Z][\w&.-]*){0,3})", role)
    if not match:
        return False
    stem = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    company = re.sub(r"[^a-z0-9]", "", match.group(1).lower())
    return bool(stem) and stem not in company and company not in stem


def _json_ld_people(page: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Person" in kinds and isinstance(node.get("name"), str):
                found.append((node["name"].strip(), str(node.get("jobTitle") or "").strip()))
            for value in node.values():
                if isinstance(value, (list, dict)):
                    walk(value)

    for block in re.findall(r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page):
        try:
            walk(json.loads(html_lib.unescape(block.strip())))
        except ValueError:
            continue
    return found


def people_on_page(page: str, domain: str = "") -> list[tuple[str, str]]:
    """(name, role) pairs with a marketing role on one page, in page order, no guests."""
    found: list[tuple[str, str]] = []

    def add(name: str, role: str) -> None:
        name, role = _is_name(name), " ".join(role.split()).strip(" ,|-–—·:")
        if name and role and len(role) <= 80 and role_rank(role) and not _foreign_company(role, domain) \
                and (name, role) not in found:
            found.append((name, role))

    for name, role in _json_ld_people(page):
        add(name, role)
    lines = _lines(page)
    for line in lines:
        if len(line) > 110:
            continue
        parts = SEPARATORS.split(line, maxsplit=1)
        if len(parts) == 2:
            if _is_name(parts[0]):
                add(parts[0], parts[1])
            elif _is_name(parts[1]):
                add(parts[1], parts[0])
    for first, second in zip(lines, lines[1:]):
        if len(first) <= 40 and len(second) <= 80:
            if _is_name(first) and role_rank(second):
                add(first, second)
            elif role_rank(first) and _is_name(second) and not _is_name(first):
                add(second, first)
    return found


def _parts(name: str) -> tuple[str, str]:
    words = [re.sub(r"[^a-z]", "", w.lower()) for w in name.split()]
    words = [w for w in words if w]
    return (words[0], words[-1]) if len(words) >= 2 else ("", "")


def apply_pattern(pattern: str, name: str, domain: str) -> str:
    first, last = _parts(name)
    if not first or not domain:
        return ""
    local = {"first": first, "first.last": f"{first}.{last}", "firstlast": first + last, "flast": first[0] + last,
             "f.last": f"{first[0]}.{last}", "first_last": f"{first}_{last}", "firstl": first + last[0], "last": last,
             "first-last": f"{first}-{last}"}.get(pattern, "")
    return f"{local}@{domain}" if local else ""


def infer_pattern(emails: Iterable[str], names: Iterable[str], domain: str) -> str:
    """The company's email pattern, from addresses it publishes for people we can name."""
    emails = [e.lower() for e in emails if e.lower().endswith("@" + domain.lower())]
    for name in names:
        for pattern in PATTERNS:
            guess = apply_pattern(pattern, name, domain)
            if guess and guess in emails:
                return pattern
    return ""


def _domain(url: str) -> str:
    host = urllib.parse.urlparse(url if "//" in url else f"https://{url}").netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _emails(page: str) -> list[str]:
    found = re.findall(r'mailto:([^"\'?>\s]+)', page) + EMAIL.findall(_plain(page))
    out: list[str] = []
    for mail in found:
        mail = urllib.parse.unquote(mail).strip().lower().rstrip(".")
        if EMAIL.fullmatch(mail) and not re.search(r"\.(png|jpg|jpeg|gif|webp|svg)$", mail) and mail not in out:
            out.append(mail)
    return out


def _linkedin_for(name: str, pages: Iterable[str]) -> str:
    first, last = _parts(name)
    for page in pages:
        for slug in re.findall(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([A-Za-z0-9_%.-]+)", page):
            low = slug.lower()
            if first and first in low and last in low:
                return f"https://www.linkedin.com/in/{slug.rstrip('/')}"
    return ""


def team_links(home: str, base_url: str) -> list[str]:
    base = urllib.parse.urlparse(base_url)
    links: list[str] = []
    for href in re.findall(r'href=["\']([^"\'#]+)["\']', home):
        full = urllib.parse.urljoin(base_url, href)
        part = urllib.parse.urlparse(full)
        if part.netloc.replace("www.", "") == base.netloc.replace("www.", "") and TEAM_PATH.search(part.path) \
                and not NOT_TEAM_PATH.search(part.path) and full not in links:
            links.append(full)
    return links


def find(website: str, fetch: Callable[[str], str | None] | None = None, bodies: dict[str, str] | None = None,
         known_emails: Iterable[str] = (), known_names: Iterable[str] = (), max_pages: int = 6,
         mx: Callable[[str], bool | None] | None = None) -> dict[str, Any]:
    """The best marketing contact on the company's own site, or {}.

    ``bodies``: pages already fetched ({url: html}, e.g. by the harvester); ``fetch``: how to get
    more (None: use only ``bodies``, so a harvest makes no extra requests). ``known_emails`` /
    ``known_names``: addresses and people we already know at the company (the host's own
    address teaches the pattern)."""
    if not website:
        return {}
    website = website if website.startswith("http") else f"https://{website}"
    domain = _domain(website)
    part = urllib.parse.urlparse(website)
    website = f"{part.scheme}://{part.netloc}/"  # the company's home, even when we were given a podcast page
    pages: dict[str, str] = {u: b for u, b in (bodies or {}).items() if b}
    if website.rstrip("/") in pages:
        pages[website] = pages.pop(website.rstrip("/"))
    if website not in pages and fetch:
        home = fetch(website)
        if home:
            pages[website] = home
    if fetch:
        home = pages.get(website) or next(iter(pages.values()), "")
        wanted = team_links(home, website) if home else []
        if not wanted:
            wanted = [urllib.parse.urljoin(website, p) for p in GUESS_PATHS]
        for url in wanted:
            if len(pages) >= max_pages:
                break
            if url not in pages:
                body = fetch(url)
                if body:
                    pages[url] = body
    candidates: list[tuple[int, int, str, str, str]] = []
    order = 0
    for url, body in pages.items():
        if NOT_TEAM_PATH.search(urllib.parse.urlparse(url).path):
            continue  # an episode or blog page names guests, not staff
        for name, role in people_on_page(body, domain):
            candidates.append((role_rank(role), -order, name, role, url))
            order += 1
    if not candidates:
        return {}
    rank, _, name, role, page_url = max(candidates)
    published = [e for body in pages.values() for e in _emails(body)] + [e.lower() for e in known_emails if e]
    first, last = _parts(name)
    mine = next((e for e in published if e.endswith("@" + domain) and first and
                 any(e == apply_pattern(p, name, domain) for p in PATTERNS)), "")
    pattern, status, confidence = "", "unknown", "none"
    if mine:
        email, status, confidence = mine, "verified", "verified_site"
    else:
        names = [n for n in known_names if n] + [n for body in pages.values() for n, _ in people_on_page(body, domain)]
        pattern = infer_pattern(published, names, domain)
        email = apply_pattern(pattern or "first.last", name, domain)
        status, confidence = "guess", ("site_pattern" if pattern else "domain_mx_pattern")
    if email and mx is not None and mx(domain) is False:
        status, confidence = "bad_domain", "bad_domain"
    return {"name": name, "role": role, "email": email, "status": status, "confidence": confidence,
            "pattern": pattern, "linkedin": _linkedin_for(name, pages.values()), "source": "team page",
            "page": page_url, "rank": rank}


def site_for(record: dict[str, Any], prospect: dict[str, Any] | None = None) -> str:
    """The company's own site for a pitch or prospect: its website when that's the company's
    own (not a podcast host or a social page), else the domain of a company email address."""
    from core.fit import NOT_A_COMPANY_SITE, WEBMAIL, _domain as fit_domain

    prospect = prospect or {}
    for url in (record.get("website"), prospect.get("website")):
        domain = fit_domain(str(url or ""))
        if domain and "." in domain and not NOT_A_COMPANY_SITE.search(domain) and not re.search(
                r"(?:redcircle|podcast\.show|transistor|captivate|megaphone|simplecast|buzzsprout|libsyn|podbean|"
                r"acast|omny|art19|podigee|spreaker|anchor)\.", domain):
            return f"https://{domain}"
    emails = [str(record.get("contact_email") or "")]
    emails += [str(c.get("email") or "") for c in [prospect.get("contact") or {}] + list(prospect.get("contacts") or [])
               if isinstance(c, dict)]
    for email in emails:
        if "@" in email and "://" not in email and not WEBMAIL.search(email):
            domain = email.rsplit("@", 1)[1].lower()
            if not re.search(r"(?:acast|libsyn|anchor|buzzsprout|podbean|megaphone|simplecast|redcircle)\.", domain):
                return f"https://{domain}"
    return ""


def lookup(record: dict[str, Any], prospect: dict[str, Any] | None = None) -> dict[str, Any]:
    """Look up the marketing contact live (the Outbox's 🔎 button): the company's own site,
    its team and about pages, the addresses we already know there."""
    from core import linkedin_harvester as LH

    prospect = prospect or {}
    site = site_for(record, prospect)
    if not site:
        return {}
    contacts = [c for c in [prospect.get("contact") or {}] + list(prospect.get("contacts") or []) if isinstance(c, dict)]
    emails = [str(record.get("contact_email") or "")] + [str(c.get("email") or "") for c in contacts]
    names = [str(record.get("contact_name") or "")] + [str(c.get("name") or "") for c in contacts]
    return find(site, fetch=lambda url: LH.fetch(url, timeout=10), known_emails=[e for e in emails if "@" in e],
                known_names=[n for n in names if n], mx=LH.mx_ok)


def backfill(prospects: list[dict[str, Any]], workers: int = 8) -> int:
    """Look up the marketing lead for prospects harvested before Round 53 (in place, live
    requests, several at a time). Returns how many got one."""
    from concurrent.futures import ThreadPoolExecutor

    todo = [p for p in prospects if not p.get("marketing_contact") and site_for({}, p)]

    def one(p: dict[str, Any]) -> dict[str, Any]:
        try:
            return lookup({}, p)
        except Exception:  # noqa: BLE001 - one bad site never stops the rest
            return {}

    with ThreadPoolExecutor(max(1, workers)) as pool:
        results = list(pool.map(one, todo))
    found = 0
    for p, result in zip(todo, results):
        p["marketing_contact"] = result or {"name": "", "looked_up": True}
        found += bool(result)
    return found
