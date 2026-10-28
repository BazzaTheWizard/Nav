"""Creator outreach email: the one template used by the worker, the app and the AI step.

Edit the wording here and every path (worker drafts, app drafts, fallback) changes
together. The AI only supplies ``video_topic``; it never writes the email itself.
"""
from __future__ import annotations

import re

AGENCY = "Nav Studios"  # one name everywhere: email, signature, Drive folder, site
# Your website, shown in the signature. After the new site is live, change this one
# line (no trailing slash). Unsent drafts in the Outbox switch to it automatically.
SITE_URL = "https://navstudios.base44.app"
DRIVE_LINK_PLACEHOLDER = "[Google Drive link is added after the clips upload]"
SUBJECT = "3 Shorts from your latest video"


def site_host(url: str | None = None) -> str:
    """'https://navstudios.co' -> 'navstudios.co' (how the site shows in a signature)."""
    return re.sub(r"^https?://", "", SITE_URL if url is None else url).split("/")[0]


SIGNATURE = f"Best,\nBarry Navan\n{AGENCY} | {site_host()}"

# Lead finder Shorts statuses where "you aren't posting much vertical content" is
# true. Anyone else (posting Shorts regularly, or unknown) gets the line without it.
LOW_SHORTS_STATUSES = frozenset({"none", "tried", "lapsed", "fading", "occasional"})

_BODY = (
    "Hi {recipient},\n\n"
    "I loved your recent video {topic_text}{highlight}.\n\n"
    "{noticed}I edited {clip_text} from that episode, complete with active framing and dynamic captions:\n"
    "{drive_link}\n\n"
    "Feel free to download and post them across Shorts, TikTok, or Reels—no strings attached.\n\n"
    "Open to having me turn your future uploads into 3–5 finished vertical clips like this?\n\n"
    "{signature}"
)

# No reply after a few days: the link again and the same question, shorter.
_FOLLOW_UP = (
    "Hi {recipient},\n\n"
    "Quick follow-up in case this got buried: the {clip_text} from your recent video {topic_text} "
    "are still here, free to post:\n"
    "{drive_link}\n\n"
    "Open to having me do this for your future uploads?\n\n"
    "{signature}"
)

# Text from older templates, used to spot unsent drafts written before an update.
OLD_TEMPLATE_MARKERS = (
    "aspiring entrepreneur",
    "Access the 3 edited clips here:",
    "I run Nav, a short-form",
    "hook retention cuts",
    "a private Google Drive folder for you:",  # Oct 2026: link in the first email
)
_OLD_TITLE_PATTERNS = (re.compile(r'I loved the "(.+?)"'), re.compile(r'your recent video "(.+?)"'))
_OLD_QUOTE_PATTERN = re.compile(r'opens on your line "(.+?)" \((\d+(?::\d{2}){1,2})\)')
_FILE_EXTENSION = re.compile(r"\.(?:mp4|mov|mkv|webm|avi|m4v|mp3|wav|m4a)$", re.IGNORECASE)


def clean_topic(topic: object) -> str:
    """A topic phrase that reads well after 'your recent video on ...'."""
    if not isinstance(topic, str):
        return ""
    text = " ".join(topic.split()).strip(" \"'“”‘’.,;:!-")
    text = re.sub(r"^(?:on|about)\s+", "", text, flags=re.IGNORECASE)
    if len(text) > 90:
        text = text[:90].rsplit(" ", 1)[0]
    return text


def _clean_title(title: str) -> str:
    text = _FILE_EXTENSION.sub("", " ".join(str(title or "").split()))
    return text.replace("_", " ").strip(" \"'“”")


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def clean_quote(text: str, max_words: int = 14) -> str:
    """Their opening words, cut at the first sentence end, at most ``max_words``."""
    words = " ".join(str(text or "").replace('"', "").split()).split()
    kept: list[str] = []
    for word in words[:max_words]:
        kept.append(word)
        if len(kept) >= 4 and word.endswith((".", "?", "!")):
            break
    quote = " ".join(kept).rstrip(",;:-–—")
    if len(kept) == max_words and not quote.endswith((".", "?", "!")):
        quote += "…"
    return quote if len(kept) >= 4 else ""


def _topic_text(video_title: str, video_topic: str) -> str:
    topic = clean_topic(video_topic)
    if topic:
        return f"on {topic}"
    title = _clean_title(video_title)
    return f'"{title}"' if title else ""


def _clip_text(clip_count: int) -> str:
    return f"{clip_count} ready-to-post (9:16) sample Short" + ("" if clip_count == 1 else "s")


def _tidy(body: str) -> str:
    """Fix spacing when there's no topic or title to name."""
    return (
        body.replace("your recent video .", "your recent video.")
        .replace("your recent video —", "your recent video—")
        .replace("your recent video  ", "your recent video ")
    )


def clean_key_point(point: object) -> str:
    """The AI's one-line insight, ready for '...especially the point about ___.'"""
    if not isinstance(point, str):
        return ""
    text = " ".join(point.split()).strip(" \"'“”‘’.,;:!-—")
    text = re.sub(r"^(?:the point about|about)\s+", "", text, flags=re.IGNORECASE)
    return text if 3 <= len(text.split()) <= 18 else ""


def _highlight(key_point: str, quote: str) -> str:
    point = clean_key_point(key_point)
    if point:
        return f"—especially the point about {point}"
    if quote:
        return f'—especially when you said "{quote.rstrip(".")}"'
    return ""


def build_outreach_email(
    channel_name: str,
    video_title: str = "",
    video_topic: str = "",
    drive_link: str = "",
    clip_count: int = 3,
    opening_line: str = "",
    opening_at: float | None = None,
    key_point: str = "",
    shorts_status: str = "",
) -> str:
    """The first email. Without a Drive link yet, a placeholder marks where it goes.

    ``key_point``: one specific insight from their video (from the AI step); if
    missing, ``opening_line`` (their words at the start of a clip) is quoted instead.
    ``shorts_status``: the lead finder's Shorts status; the "you aren't posting
    much vertical content" line is only used when that's actually true.
    """
    del opening_at  # kept for older callers; the timestamp isn't in this template
    noticed = (
        "I noticed you aren't posting much vertical content, so "
        if shorts_status in LOW_SHORTS_STATUSES
        else ""
    )
    body = _BODY.format(
        recipient=channel_name.strip() or "there",
        topic_text=_topic_text(video_title, video_topic),
        highlight=_highlight(key_point, clean_quote(opening_line)),
        noticed=noticed,
        clip_text=_clip_text(clip_count),
        drive_link=drive_link.strip() or DRIVE_LINK_PLACEHOLDER,
        signature=SIGNATURE,
    )
    return _tidy(body)


def build_follow_up(
    channel_name: str,
    video_title: str = "",
    video_topic: str = "",
    drive_link: str = "",
    clip_count: int = 3,
) -> str:
    """A short second email for creators who haven't replied after a few days."""
    body = _FOLLOW_UP.format(
        recipient=channel_name.strip() or "there",
        clip_text=f"{clip_count} sample Short" + ("" if clip_count == 1 else "s"),
        topic_text=_topic_text(video_title, video_topic),
        drive_link=drive_link.strip() or DRIVE_LINK_PLACEHOLDER,
        signature=SIGNATURE,
    )
    return _tidy(body)


# "your recent video on <topic>" followed by a dash (current) or . , (older emails)
_TOPIC_IN_EMAIL = re.compile(r"your recent video on (.+?)(?:—|[.,]\s)")


def subject_for(email: str) -> str:
    """One subject for every first email (kept as a function for older callers)."""
    del email
    return SUBJECT


def topic_from_email(email: str) -> str:
    match = _TOPIC_IN_EMAIL.search(email)
    return match.group(1) if match else ""


def fill_drive_link(email: str, drive_link: str) -> str:
    """Put the Drive folder link into a draft made before the upload (older drafts)."""
    if DRIVE_LINK_PLACEHOLDER in email:
        return email.replace(DRIVE_LINK_PLACEHOLDER, drive_link.strip())
    # Drafts from the old template.
    return email.replace(
        "Access the 3 edited clips here:\n\n",
        f"Access the 3 edited clips here:\n{drive_link.strip()}\n\n",
    )


def is_old_template(email: str) -> bool:
    return any(marker in email for marker in OLD_TEMPLATE_MARKERS)


def _seconds(timestamp: str) -> float:
    total = 0
    for part in timestamp.split(":"):
        total = total * 60 + int(part)
    return float(total)


def rewrite_old_draft(
    email: str, channel_name: str, drive_link: str, video_title: str = "", shorts_status: str = ""
) -> str:
    """Rebuild a not-yet-sent draft from an older template with the current one,
    keeping its topic or title and the quoted opening line."""
    topic = topic_from_email(email)
    for pattern in _OLD_TITLE_PATTERNS:
        if video_title:
            break
        match = pattern.search(email)
        video_title = match.group(1) if match else ""
    quote = _OLD_QUOTE_PATTERN.search(email)
    return build_outreach_email(
        channel_name,
        video_title=video_title,
        video_topic=topic,
        drive_link=drive_link,
        opening_line=quote.group(1) if quote else "",
        opening_at=_seconds(quote.group(2)) if quote else None,
        shorts_status=shorts_status,
    )


# Website address used before SITE_URL existed; drafts without a saved address used it.
LEGACY_SITE_URL = "https://navstudios.base44.app"


def refresh_site_links(email: str, old_site_url: str) -> str:
    """Swap an older website address for the current SITE_URL (signature and links)."""
    old_host, new_host = site_host(old_site_url), site_host()
    if not old_host or old_host == new_host:
        return email
    return email.replace(old_host, new_host)
