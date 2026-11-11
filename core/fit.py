"""Round 53: buyer fit. Is there a B2B company behind this show that would pay for clips?

The LinkedIn Video Gap score (core.linkedin_harvester.score) asks "do they need clips?". This
asks the other half: "is there a buyer?". Your first 194 pitches went largely to sports, fitness,
school and personal-finance channels, and to info@ inboxes and contact forms: people who
can't or won't pay. Fit looks at four things:

1. A company behind it: its own website or email domain (not gmail), a business offer, a
   company LinkedIn page.
2. Someone to reach: a named person, best a marketing or content lead, over a shared inbox
   (info@, support@) or a contact form.
3. A B2B audience: SaaS, sales, revenue, finance and similar topics, not sports, health,
   schools, news or consumer money tips.
4. Not a seller of the same service: marketing, video and podcast agencies are partners
   (they could resell you), not clients.

It works on every shape the app keeps: a B2B prospect (core.linkedin_harvester), a worker
lead (core.lead_harvester) and an Outbox / Sent record. ``buyer_fit`` returns
{"points": 0-100, "grade": "strong" | "good" | "weak" | "skip" | "partner", "label", "reasons"}.

The worker (FIT_GATE, on by default) skips "skip" leads it found by itself, so no render time
goes to channels that can't buy; a lead you queue or force always goes through.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable

WEBMAIL = re.compile(r"@(?:gmail|googlemail|yahoo|ymail|hotmail|outlook|live|msn|aol|icloud|me|mac|proton|protonmail|"
                     r"gmx|zoho|mail|yandex)\.[a-z.]+$", re.I)
NOT_A_COMPANY_SITE = re.compile(r"(?:^|\.)(?:youtube\.com|youtu\.be|linktr\.ee|beacons\.ai|instagram\.com|tiktok\.com|"
                                r"facebook\.com|twitter\.com|x\.com|linkedin\.com|patreon\.com|anchor\.fm|spotify\.com|"
                                r"apple\.com|podcasts\.apple\.com|substack\.com|buzzsprout\.com|libsyn\.com|podbean\.com|"
                                r"amazon\.com|amzn\.to|bit\.ly|gumroad\.com|discord\.gg|discord\.com|t\.me|"
                                r"wixsite\.com|squarespace\.com|calendly\.com|pod\.link)$", re.I)
SHARED_LOCAL = re.compile(r"^(?:info|support|hello|hi|hey|contact|team|admin|office|enquir(?:y|ies)|enquries|inquir(?:y|ies)|"
                          r"help|mail|general|sales|media|press|pr|podcast|podcasts|show|booking|bookings|business|"
                          r"partnerships?|sponsors?(?:hip)?s?|collab|collabs|network|marketing|social|content|studio|"
                          r"feedback|questions|listen|ask|editor|news|noreply|no-reply|customerservice|service|careers)"
                          r"(?:[+._-].*)?$", re.I)
MARKETING_ROLE = re.compile(r"\b(?:CMO|chief marketing|marketing|content|brand|communications|demand gen|growth|"
                            r"social media|podcast producer|head of podcast)\b", re.I)
B2B_TOPICS = re.compile(
    r"\b(b2b|saas|enterprise|go-to-market|gtm|revenue|sales (?:team|leaders?|leadership|enablement|process)|"
    r"sales|selling|salespeople|sales training|consultants?|pipeline|demand gen(?:eration)?|marketing leaders?|cmos?|cfos?|ceos?|cros?|founders?|executives?|"
    r"leadership|venture|vc|private equity|fintech|banking|insurance|accounting|procurement|supply chain|"
    r"operations|consulting|legal tech|law firm|compliance|cybersecurity|hr tech|recruiting|talent|"
    r"commercial real estate|commercial property|wealth management|advisors?|investment management|m&a|startups?|software|"
    r"product management|customer success|revops|account executives?|agency owners?|ai for business|data)\b", re.I)
OFF_NICHE = re.compile(
    r"\b(nfl|nba|mlb|nhl|ncaa|football|basketball|baseball|softball|soccer|hockey|tigers|steelers|lions|"
    r"sports?|athletes?|coach(?:es|ing)? (?:certification|podcast)|strength coach|fitness|workout|bodybuilding|"
    r"cscs|physical therap|orthop(?:a)?edic|medical center|hospital|nursing|patients?|"
    r"academy|academies|school|classroom|homeschool|students?|parenting|church|ministry|bible|sermon|"
    r"news|10tv|radio|the fan|tv station|broadcast|gaming|gameplay|anime|music|comedy|vlog|travel vlog|"
    r"recipes?|cooking|beauty|makeup|fashion|motivation(?:al)?|manifest|astrology|fan channel|funny|celebrity|"
    r"entertainment|emmy|hollywood|true crime|rugby|cricket|golf|nascar)\b",
    re.I)
CONSUMER_MONEY = re.compile(
    r"\b(stock market|stocks? to buy|day trad(?:e|ing)|trading|options trading|forex|crypto|bitcoin|dividends?|"
    r"personal finance|side hustles?|passive income|get rich|millionaires?|financial freedom|retire early|"
    r"fire movement|budgeting|debt free|frugal|flip(?:ping)?|wholesal(?:e|ing) houses|money tips)\b", re.I)
SELLS_THE_SAME = re.compile(
    r"\b(marketing agency|growth agency|digital agency|creative agency|content agency|social media agency|"
    r"video production|video agency|production company|podcast production|podcast agency|content studio|"
    r"video editing services?|clipping|repurpos\w*|ghostwrit\w*|personal brand(?:ing)? agency|lead gen(?:eration)? agency|"
    r"agency)\b", re.I)
SELLS_SPECIFIC = re.compile(SELLS_THE_SAME.pattern.replace("|agency)", ")"), re.I)
SELLS_DOMAIN = re.compile(r"(?:marketing|agency|media|studios?|productions?|creative|clips?|editing|podcasting)", re.I)
MEDIA_DOMAINS = re.compile(r"(?:^|\.)(?:nytimes|wsj|dowjones|bloomberg|cnbc|forbes|cnn|nbcuniversal|nbc|abc|cbs|foxnews|"
                           r"washingtonpost|theatlantic|economist|ft|reuters|npr|bbc|hbr|ted|iheart(?:media)?|siriusxm|wondery|"
                           r"spotify|audacy|cumulus|[a-z0-9-]*media)\.[a-z.]+$", re.I)
EXEC_HOST = re.compile(r"\b(ceo|founder|co-founder|cofounder|president|managing partner|cro|cmo|cfo|coo|vp|"
                       r"vice president|chief)\b", re.I)

GRADES = {
    "strong": "🟢 Strong fit", "good": "🟡 Good fit", "weak": "🟠 Weak fit", "skip": "⚪ Not a buyer",
    "partner": "🤝 Partner, not a client",
}
GRADE_ORDER = {"strong": 0, "good": 1, "partner": 2, "weak": 3, "skip": 4}


def fit_gate_on() -> bool:
    """FIT_GATE=0 in .env lets every discovered lead through again (tests/run_all.py pins it
    off for the suites written before Round 53)."""
    return os.getenv("FIT_GATE", "1").strip().lower() not in {"0", "false", "no", "off"}


def _domain(url_or_email: str) -> str:
    text = str(url_or_email or "").strip().lower()
    if "@" in text and "://" not in text:
        return text.rsplit("@", 1)[1].strip(".")
    text = re.sub(r"^[a-z]+://", "", text).split("/", 1)[0].split(":", 1)[0]
    return text[4:] if text.startswith("www.") else text


def _contacts(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Every contact we know of, best first (a prospect's list, a pitch's own fields, the
    marketing contact)."""
    found: list[dict[str, Any]] = []
    if isinstance(record.get("contact"), dict):
        found.append(record["contact"])
    found += [c for c in record.get("contacts") or [] if isinstance(c, dict)]
    email = str(record.get("contact_email") or "")
    if email or record.get("contact_name"):
        found.append({"email": email if "@" in email and "://" not in email else "",
                      "name": record.get("contact_name") or record.get("greeting_name") or "",
                      "role": record.get("contact_role") or ""})
    marketing = record.get("marketing_contact")
    if isinstance(marketing, dict) and (marketing.get("name") or marketing.get("email")):
        found.append(marketing)
    return found


def _text(record: dict[str, Any]) -> str:
    angle = record.get("calendar_angle") if isinstance(record.get("calendar_angle"), dict) else {}
    pitch = record.get("pitch") if isinstance(record.get("pitch"), dict) else {}
    parts: list[Any] = [record.get("show"), record.get("channel_title"), record.get("description"),
                        record.get("latest_video_title"), record.get("video_title"), " ".join(record.get("genres") or []),
                        " ".join(angle.get("recent_titles") or []), pitch.get("topic"), pitch.get("theme")]
    offer = record.get("business_offer")
    if isinstance(offer, list):
        parts += [o for o in offer if "LinkedIn Video Gap" not in str(o)]
    return " ".join(str(p) for p in parts if p)


def company_site(record: dict[str, Any]) -> str:
    """The company's own website, if we know one ('' for a channel with only social links)."""
    candidates: list[Any] = [record.get("website"), record.get("domain")]
    candidates += [link for link in record.get("social_links") or []]
    for url in candidates:
        domain = _domain(str(url or ""))
        if domain and "." in domain and not NOT_A_COMPANY_SITE.search(domain):
            return domain
    for contact in _contacts(record):
        email = str(contact.get("email") or "")
        if "@" in email and not WEBMAIL.search(email):
            return _domain(email)
    return ""


def _days_since(value: Any, now: datetime) -> float | None:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when).total_seconds() / 86400


def buyer_fit(record: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """How likely there's a paying B2B buyer behind this prospect, lead or pitch."""
    now = now or datetime.now(timezone.utc)
    title = " ".join(str(record.get(k) or "") for k in ("show", "channel_title"))
    text = _text(record)
    points, why = 50, []

    # 1. A company behind it.
    site = company_site(record)
    emails = [str(c.get("email") or "") for c in _contacts(record) if "@" in str(c.get("email") or "")]
    webmail_only = bool(emails) and all(WEBMAIL.search(e) for e in emails)
    if site:
        points += 12
        why.append(f"+12 a company website or email domain ({site})")
    elif webmail_only:
        points -= 12
        why.append("-12 only a personal email (gmail etc.): no company behind it")
    else:
        points -= 4
        why.append("-4 no company website found yet")
    media = MEDIA_DOMAINS.search(site) if site else None
    offer = [o for o in record.get("business_offer") or [] if "LinkedIn Video Gap" not in str(o)]
    if offer or record.get("source") == "b2b_harvester" or record.get("prospect_id") or record.get("feed_url"):
        points += 6
        why.append("+6 sells a product or service (a business offer, or found as a B2B company)")
    if record.get("linkedin_company") or "linkedin.com/company/" in str(record.get("linkedin_url") or ""):
        points += 5
        why.append("+5 has a company LinkedIn page")

    # 2. Someone to reach.
    contacts = _contacts(record)
    marketer = next((c for c in contacts if c.get("name") and MARKETING_ROLE.search(str(c.get("role") or ""))), None)
    named = next((c for c in contacts if c.get("name") and c.get("email")
                  and not SHARED_LOCAL.match(str(c.get("email")).split("@")[0])), None)
    shared = [e for e in emails if SHARED_LOCAL.match(e.split("@")[0])]
    if marketer:
        points += 15
        why.append(f"+15 a named marketing contact ({marketer.get('name')}, {marketer.get('role')})")
    elif named:
        points += 8
        why.append(f"+8 a named person to write to ({named.get('name') or named.get('email')})")
    elif emails and len(shared) == len(emails):
        points -= 6
        why.append("-6 only a shared inbox (info@, support@...): often unread")
    elif not emails:
        points -= 8
        why.append("-8 no email yet (contact form or About page only)")
    if EXEC_HOST.search(" ".join(str(c.get("role") or "") for c in contacts) + " " + str(record.get("description") or "")):
        points += 4
        why.append("+4 an executive or founder on the show")

    # 3. A B2B audience.
    topics = {m.lower() for m in B2B_TOPICS.findall(text)}
    if len(topics) >= 3:
        points += 14
        why.append(f"+14 B2B topics ({', '.join(sorted(topics)[:4])})")
    elif len(topics) == 2:
        points += 9
        why.append(f"+9 B2B topics ({', '.join(sorted(topics))})")
    elif topics:
        points += 4
        why.append(f"+4 one B2B topic ({next(iter(topics))})")
    else:
        points -= 6
        why.append("-6 no B2B topic in its name, description or episodes")
    off = (OFF_NICHE.search(title) or OFF_NICHE.search(text)) if len(topics) < 2 else None
    if off:
        points -= 35
        why.append(f"-35 off-niche ({off.group(0).lower()}): not a B2B buyer")
    money = CONSUMER_MONEY.search(text)
    if money and len(topics) < 3:
        points -= 15
        why.append(f"-15 consumer money content ({money.group(0).lower()}): sells views, not B2B services")

    # Size and timing.
    subs = record.get("subscriber_count")
    if isinstance(subs, int) and subs > 3_000_000:
        points -= 30
        why.append(f"-30 {subs:,} subscribers: a media empire with its own team")
    elif isinstance(subs, int) and subs > 500_000:
        points -= 12
        why.append(f"-12 {subs:,} subscribers: a big media brand, likely an in-house team")
    if record.get("media_network") or media:
        points -= 15
        why.append(f"-15 a media company ({record.get('media_network') or site}): in-house producers")
    age = None
    for key in ("latest_episode_at", "latest_video_published_at", "published_at"):
        age = _days_since(record.get(key), now) if record.get(key) else age
    if age is not None:
        if age <= 7:
            points += 6
            why.append("+6 a new episode this week: pitch while it's fresh")
        elif age > 45:
            points -= 6
            why.append(f"-6 latest episode is {int(age)} days old")

    points = max(0, min(100, points))
    sells_same = (SELLS_THE_SAME.search(title) or SELLS_DOMAIN.search(site.rsplit(".", 1)[0])
                  or SELLS_SPECIFIC.search(str(record.get("description") or "")))
    if off:
        grade = "skip"
    elif sells_same and not re.search(r"\bagency owners?\b", title, re.I):
        grade = "partner"
        why.append(f"🤝 sells marketing or video ({sells_same.group(0).lower()}): pitch them as a partner who "
                   "resells your clips, not as a client")
    elif points >= 72 and len(topics) >= 2:
        grade = "strong"
    elif points >= 55 and topics:
        grade = "good"
    else:  # no B2B topic at all: weak at best. Skip needs real evidence (above) or a very low score.
        grade = "weak" if points >= 30 else "skip"
    return {"points": points, "grade": grade, "label": GRADES[grade], "reasons": why}


def priority(prospect: dict[str, Any], fit: dict[str, Any] | None = None) -> int:
    """One number to sort B2B prospects by: buyer fit (is there a buyer?) weighs 60%, the
    LinkedIn Video Gap score (do they need clips?) 40%. Partners and skips sort last."""
    fit = fit or buyer_fit(prospect)
    value = round(0.6 * int(fit["points"]) + 0.4 * int(prospect.get("score") or 0))
    return value - (40 if fit["grade"] in {"skip", "partner"} else 0)


def rank_key(record: dict[str, Any]) -> tuple[int, int]:
    """Sort best first: grade, then points (uses a stored "fit" when there is one)."""
    fit = record.get("fit") if isinstance(record.get("fit"), dict) else buyer_fit(record)
    return GRADE_ORDER.get(str(fit.get("grade")), 3), -int(fit.get("points") or 0)


def with_fit(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each record with a fresh "fit" (stored ones are recomputed: the rules may have improved)."""
    return [{**r, "fit": buyer_fit(r)} for r in records]


def outcomes_by_grade(records: Iterable[dict[str, Any]], activity: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Does fit predict interest? For each grade: pitches sent, how many opened their page,
    played a clip, and replied. The check that keeps the score honest as data comes in."""
    rows: dict[str, dict[str, Any]] = {}
    for record in records:
        grade = buyer_fit(record)["grade"]
        row = rows.setdefault(grade, {"grade": grade, "label": GRADES[grade], "sent": 0, "opened": 0, "played": 0,
                                      "replied": 0})
        row["sent"] += 1
        page = activity.get(str(record.get("page_name") or "")) or {}
        row["opened"] += bool(page.get("opens") or page.get("played"))
        row["played"] += bool(page.get("played"))
        row["replied"] += bool(record.get("replied_at"))
    return sorted(rows.values(), key=lambda r: GRADE_ORDER.get(r["grade"], 9))
