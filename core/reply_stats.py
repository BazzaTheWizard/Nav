"""Round 54: what gets replies. Every pitch you've sent, broken down so you can see which kind of
prospect and which kind of email gets answered, and harvest more of those.

Breakdowns (each row: sent, opened their page, played a clip, replied, reply rate):
- niche: what the show is about (SaaS and tech, sales, marketing, finance and VC, real estate,
  legal, HR and recruiting, consulting and agencies, health, sports, education, other)
- fit: buyer fit grade (core.fit)
- email: which first email went out (executive B2B pitch, clip pitch, direct pitch, partner pitch)
- subject: the subject line's shape (clip count + show, a topic, LinkedIn clips for..., other)
- contact: who it went to (a named person, a shared inbox, a personal gmail, a contact form)
- day: the weekday it was sent (your local time)
- reply came: after the first email, or after which follow-up

Small numbers lie: rows under MIN_SAMPLE sends are marked "too few to judge" and never named the
best. Opens and plays only count for pitches with a clip page.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

from core.fit import GRADES, WEBMAIL, buyer_fit

MIN_SAMPLE = 10
NICHES: tuple[tuple[str, re.Pattern[str]], ...] = tuple((name, re.compile(pattern, re.I)) for name, pattern in (
    ("Sports", r"\b(nfl|nba|mlb|nhl|ncaa|football|basketball|baseball|softball|soccer|hockey|rugby|sports?|athlet\w*|"
               r"steelers|lions|tigers|coach2coach)\b"),
    ("Health and fitness", r"\b(fitness|workout|strength|cscs|orthop\w*|medical|hospital|health|longevity|nutrition|"
                           r"physical therap\w*|pain|doctor)\b"),
    ("Education", r"\b(school|academy|academies|classroom|students?|teach\w*|homeschool|university|classical)\b"),
    ("Sales", r"\b(sales|selling|salespeople|prospecting|quota|president'?s club|closing|pipeline)\b"),
    ("Marketing and agencies", r"\b(marketing|agency|agencies|brand(?:ing)?|growth|seo|content|advertising|demand gen)\b"),
    ("SaaS and tech", r"\b(saas|software|ai|tech|startups?|founders?|product|engineering|developer|data|cloud|gtm)\b"),
    ("Finance and VC", r"\b(venture|vc|private equity|fintech|finance|investing|investors?|capital|wealth|stocks?|"
                       r"markets?|banking|money|dividends?|trading|economics|portfolio)\b"),
    ("Real estate", r"\b(real estate|property|properties|landlord|reit|commercial property|multifamily)\b"),
    ("Legal", r"\b(law|legal|lawyers?|attorneys?|litigation|compliance)\b"),
    ("HR and recruiting", r"\b(hr|recruit\w*|talent|hiring|people ops|workplace|leadership)\b"),
    ("Consulting and business", r"\b(consult\w*|business|ceo|entrepreneur\w*|operations|strategy)\b"),
))
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _text(record: dict[str, Any]) -> str:
    pitch = record.get("pitch") if isinstance(record.get("pitch"), dict) else {}
    return " ".join(str(x or "") for x in (record.get("channel_title"), record.get("video_title"), record.get("description"),
                                          pitch.get("topic"), pitch.get("theme")))


def niche(record: dict[str, Any]) -> str:
    text = _text(record)
    return next((name for name, pattern in NICHES if pattern.search(text)), "Other")


def email_kind(record: dict[str, Any]) -> str:
    body = str(record.get("full_email_body") or "")
    if record.get("partner"):
        return "Partner pitch (agency)"
    if record.get("kind") == "intro":
        return "Direct pitch (no clips)"
    if "LinkedIn-ready" in body:
        return "Executive B2B pitch (Round 49+)"
    if "://" in body or "clip page link" in body:
        return "Clip pitch with the link"
    return "Ask-first pitch (no link)"


def subject_kind(record: dict[str, Any]) -> str:
    subject = str(record.get("subject") or "")
    if not subject:
        return "No subject saved"
    if re.match(r"^\d+ (?:LinkedIn )?(?:clips?|shorts?)\b", subject, re.I):
        return "Clip count + show (\"3 clips from …\")"
    if re.match(r"^(?:LinkedIn clips for|White-label)", subject, re.I):
        return "\"LinkedIn clips for …\""
    if re.search(r"\b(?:clips?|shorts?)\b", subject, re.I):
        return "Topic + clips"
    return "Other"


def contact_kind(record: dict[str, Any]) -> str:
    from core.fit import SHARED_LOCAL

    email = str(record.get("contact_email") or "")
    if "@" not in email or "://" in email:
        return "Contact form / About page"
    if WEBMAIL.search(email):
        return "Personal email (gmail…)"
    if SHARED_LOCAL.match(email.split("@", 1)[0]):
        return "Shared inbox (info@, podcast@…)"
    return "A named person's work email"


def weekday(record: dict[str, Any]) -> str:
    try:
        return DAYS[datetime.fromisoformat(str(record.get("sent_at"))).astimezone().weekday()]
    except (TypeError, ValueError):
        return "Unknown"


def reply_came(record: dict[str, Any]) -> str:
    """'After the first email' / 'After follow-up 2' / 'No reply'."""
    if not record.get("replied_at"):
        return "No reply"
    sent = record.get("follow_ups") if isinstance(record.get("follow_ups"), list) else (
        [record["followed_up_at"]] if record.get("followed_up_at") else [])
    before = [t for t in sent if str(t) <= str(record["replied_at"])]
    return "After the first email" if not before else f"After follow-up {len(before)}"


DIMENSIONS = {
    "niche": ("What the show is about", niche),
    "fit": ("Buyer fit", lambda r: GRADES[buyer_fit(r)["grade"]]),
    "email": ("First email", email_kind),
    "subject": ("Subject line", subject_kind),
    "contact": ("Who it went to", contact_kind),
    "day": ("Day sent", weekday),
}


def breakdown(records: Iterable[dict[str, Any]], activity: dict[str, dict[str, Any]], dimension: str) -> list[dict[str, Any]]:
    """Rows for one dimension, best reply rate first (rows with too few sends last)."""
    label_of = DIMENSIONS[dimension][1]
    rows: dict[str, dict[str, Any]] = {}
    for record in records:
        key = label_of(record)
        row = rows.setdefault(key, {"group": key, "sent": 0, "with_page": 0, "opened": 0, "played": 0, "replied": 0,
                                    "yes": 0})
        row["sent"] += 1
        page = activity.get(str(record.get("page_name") or "")) if record.get("page_name") else None
        if record.get("page_name"):
            row["with_page"] += 1
            row["opened"] += bool(page and (page.get("opens") or page.get("played")))
            row["played"] += bool(page and page.get("played"))
        row["replied"] += bool(record.get("replied_at"))
        row["yes"] += record.get("reply") == "yes"
    for row in rows.values():
        row["rate"] = row["replied"] / row["sent"] if row["sent"] else 0.0
        row["enough"] = row["sent"] >= MIN_SAMPLE
    return sorted(rows.values(), key=lambda r: (not r["enough"], -r["rate"], -r["sent"]))


def when_replies_came(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """How many replies came after the first email and after each follow-up."""
    counts: dict[str, int] = {}
    for record in records:
        came = reply_came(record)
        if came != "No reply":
            counts[came] = counts.get(came, 0) + 1
    return [{"when": k, "replies": v} for k, v in sorted(counts.items())]


def best(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The group with the best reply rate among those with enough sends (and at least one reply)."""
    judged = [r for r in rows if r["enough"] and r["replied"]]
    return max(judged, key=lambda r: r["rate"]) if judged else None


def overall(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    records = list(records)
    replied = sum(bool(r.get("replied_at")) for r in records)
    return {"sent": len(records), "replied": replied, "rate": replied / len(records) if records else 0.0}
