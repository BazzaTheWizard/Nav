"""Round 53: follow-ups that fit what each prospect did on their clip page.

Before: everyone got the same bump on day 3 and the last note on day 12, and many follow-ups
were never sent at all (people who played every clip never heard from you again). Now the
next follow-up depends on what they did (core.clip_pages.page_activity, your own visits
already left out):

- 🔥 watched or took something (played a clip, downloaded, copied a post, opened the offer):
  a "warm" note two days after sending (or a day after they watched, whichever is later):
  which clip to post first, and the founding offer.
- 👀 opened but didn't play: on day 3, a short bump that points at the best clip.
- 💤 never opened: on day 3 the email probably never reached them. If we know the company's
  marketing lead (core.marketing_contact) and haven't written to them, a NEW email to that
  person ("new door"); otherwise a resend with a fresh subject as a new email.
- Then a warm note if they watch later, and the last note on day 12.

At most three follow-ups per pitch. The emails never mention the tracking.

``apply(record, page, now)`` returns the record with the worker's follow-up fields replaced
(follow_up_due / _step / _label / _subject / _body / _number / _total / _on) plus
follow_up_to (who to send it to), follow_up_new_thread, follow_up_why, engagement.
Direct pitches (no clip page) keep the worker's plan.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core import outreach as O

WARM_AFTER_SEND = timedelta(days=2)
WARM_AFTER_WATCH = timedelta(hours=20)
FIRST_DAY = timedelta(days=3)
LAST_DAY = timedelta(days=12)
MIN_GAP = timedelta(days=3)  # between two follow-ups
MAX_FOLLOW_UPS = 3
LEVELS = {3: "🔥 took something", 2: "🔥 watched", 1: "👀 opened", 0: "💤 not opened"}
LABELS = {
    "warm": "Warm follow-up: which clip to post first, and the founding offer",
    "nudge": "Short bump that points at the best clip",
    "new_door": "New email to their marketing lead",
    "resend": "Resend with a fresh subject (a new email, not a reply)",
    "last": "Last note",
}


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def engagement(page: dict[str, Any] | None) -> dict[str, Any]:
    """{"level": 0-3, "label", "first" (first time they did something), "last", "detail"}."""
    if not page:
        return {"level": 0, "label": LEVELS[0], "first": None, "last": None, "detail": "Not opened yet"}
    took = bool(page.get("downloaded") or page.get("copied") or page.get("assets") or page.get("offer")
                or page.get("call"))
    played = sorted(page.get("played") or [])
    level = 3 if took else 2 if played else 1 if page.get("opens") else 0
    opened = [t for t in (page.get("opened_at") or []) if isinstance(t, datetime)]
    first = min(opened) if opened else page.get("last")
    parts = []
    if played:
        parts.append("played clip" + ("s " if len(played) > 1 else " ") + ", ".join(str(n) for n in played))
    if page.get("downloaded"):
        parts.append("downloaded " + ", ".join(str(n) for n in sorted(page["downloaded"])))
    if page.get("copied"):
        parts.append("copied a post")
    if page.get("assets"):
        parts.append("took captions, a cover or a caption")
    if page.get("offer"):
        parts.append("opened the offer")
    if page.get("call"):
        parts.append("asked for a call")
    if not parts and page.get("opens"):
        parts.append(f"opened the page {page['opens']}×")
    return {"level": level, "label": LEVELS[level], "first": first, "last": page.get("last"),
            "detail": "; ".join(parts) or "Not opened yet"}


def _log(record: dict[str, Any]) -> tuple[list[str], int, datetime | None]:
    """(the steps logged by name, how many follow-ups went out, when the latest went out).
    Follow-ups from before Round 53 have a time but no step name."""
    sent = record.get("follow_ups")
    times = [str(t) for t in sent] if isinstance(sent, list) else (
        [str(record["followed_up_at"])] if record.get("followed_up_at") else [])
    steps = [str(entry.get("step")) for entry in record.get("follow_up_log") or [] if isinstance(entry, dict)]
    stamps = [w for w in (_when(t) for t in times) if w]
    return steps, max(len(times), len(steps)), (max(stamps) if stamps else None)


def marketer_of(record: dict[str, Any], prospect: dict[str, Any] | None = None) -> dict[str, Any]:
    """The company's marketing contact we could write to (not the person already pitched)."""
    for source in (record.get("marketing_contact"), (prospect or {}).get("marketing_contact")):
        if isinstance(source, dict) and source.get("email") and source.get("status") != "bad_domain" \
                and str(source.get("email")).lower() != str(record.get("contact_email") or "").lower():
            return source
    return {}


def _facts(record: dict[str, Any]) -> dict[str, Any]:
    pitch = record.get("pitch") if isinstance(record.get("pitch"), dict) else {}
    clips = [c for c in record.get("clips") or [] if isinstance(c, dict)]
    best = max(clips, key=lambda c: c.get("judge") or 0) if clips else {}
    return {
        "first": O.clean_first_name(str(record.get("greeting_name") or "")),
        "link": str(record.get("page_url") or record.get("drive_link") or ""),
        "point": str(pitch.get("point") or best.get("point") or ""),
        "at": pitch.get("at") if isinstance(pitch.get("at"), (int, float)) else best.get("start"),
        "count": len(clips) or 3,
        "kind": str(pitch.get("format") or "episode"),
        "show": str(record.get("channel_title") or ""),
        "episode": str(record.get("video_title") or ""),
    }


email_facts = _facts  # the Outbox builds the marketing lead's email from the same facts


def adaptive_on() -> bool:
    """ADAPTIVE_FOLLOW_UPS=0 in .env brings back the fixed day 3 / day 12 follow-ups
    (tests/run_all.py pins it off for the suites written before Round 53)."""
    import os

    return os.getenv("ADAPTIVE_FOLLOW_UPS", "1").strip().lower() not in {"0", "false", "no", "off"}


def plan(record: dict[str, Any], page: dict[str, Any] | None, now: datetime | None = None,
         prospect: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The next follow-up for a sent pitch with a clip page, or None (keep the worker's plan)."""
    if record.get("kind") == "intro" or not record.get("page_name"):
        return None
    now = now or datetime.now(timezone.utc)
    sent_at = _when(record.get("sent_at"))
    if sent_at is None:
        return None
    steps, done, latest = _log(record)
    eng = engagement(page)
    finished = done >= MAX_FOLLOW_UPS or "last" in steps or (done >= 2 and not steps)  # the old 2-step plan ran out
    base = {"engagement": eng, "follow_ups_sent": done, "follow_up_number": done + 1}
    if finished:
        return {**base, "follow_up_step": None, "follow_up_due": False, "follow_up_on": "",
                "follow_up_total": done, "follow_up_why": "All follow-ups sent"}
    marketer = marketer_of(record, prospect)
    watched_at = _when(eng["first"]) or sent_at
    if done == 0:
        if eng["level"] >= 2:
            step, due_at = "warm", max(sent_at + WARM_AFTER_SEND, watched_at + WARM_AFTER_WATCH)
        elif eng["level"] == 1:
            step, due_at = "nudge", sent_at + FIRST_DAY
        elif marketer and not record.get("marketer_sent_at"):
            step, due_at = "new_door", sent_at + FIRST_DAY
        else:
            step, due_at = "resend", sent_at + FIRST_DAY
    elif eng["level"] >= 2 and "warm" not in steps and done < MAX_FOLLOW_UPS - 1:
        step = "warm"
        due_at = max((latest or sent_at) + MIN_GAP, watched_at + WARM_AFTER_WATCH)
    else:
        step = "last"
        due_at = max(sent_at + LAST_DAY, (latest or sent_at) + MIN_GAP)
    total = done + (2 if step != "last" else 1)
    facts = _facts(record)
    subject = f"Re: {record.get('subject') or O.subject_for(str(record.get('full_email_body') or ''))}"
    to, new_thread = str(record.get("contact_email") or ""), False
    why = {"warm": f"They {eng['detail']} and haven't replied.",
           "nudge": "They opened the page but didn't play a clip.",
           "last": "No reply yet."}.get(step, "")
    if step == "warm":
        body = O.build_warm_follow_up(facts["first"], facts["point"], facts["link"], facts["kind"])
    elif step == "nudge":
        body = O.build_nudge_follow_up(facts["first"], facts["point"], facts["link"])
    elif step == "new_door":
        subject, body = O.build_marketer_email(str(marketer.get("name") or "").split(" ")[0],
                                               str(record.get("contact_name") or facts["first"]), facts["show"],
                                               facts["episode"], facts["link"], facts["point"], facts["at"],
                                               facts["count"], facts["kind"])
        to, new_thread = str(marketer.get("email")), True
        why = (f"Not opened in 3 days. {marketer.get('name')} ({marketer.get('role')}) runs marketing there: "
               "write to them instead, as a new email.")
    elif step == "resend":
        subject, body = O.build_resend(facts["first"], facts["show"], facts["episode"], facts["link"], facts["count"],
                                       facts["kind"])
        new_thread = True
        why = "Not opened in 3 days: the first email may be in spam or Promotions."
    else:
        body = O.build_follow_up_step("last", facts["first"])
    return {**base, "follow_up_step": step, "follow_up_due": now >= due_at, "follow_up_on": due_at.date().isoformat(),
            "follow_up_total": total, "follow_up_label": LABELS[step], "follow_up_subject": subject,
            "follow_up_body": body, "follow_up_to": to, "follow_up_new_thread": new_thread,
            "follow_up_why": why}


def apply(record: dict[str, Any], page: dict[str, Any] | None, now: datetime | None = None,
          prospect: dict[str, Any] | None = None) -> dict[str, Any]:
    """The record with this plan's follow-up fields (unchanged when the plan doesn't apply)."""
    planned = plan(record, page, now, prospect) if adaptive_on() else None
    return {**record, **planned} if planned else record


def urgency(record: dict[str, Any]) -> tuple[int, int, str]:
    """Sort key for the Sent list: due first, then the most engaged, then the oldest send."""
    eng = record.get("engagement") or {}
    return (0 if record.get("follow_up_due") else 1, -int(eng.get("level") or 0), str(record.get("sent_at") or ""))
