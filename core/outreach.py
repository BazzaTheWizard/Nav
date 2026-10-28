"""Creator outreach email: the one template used by the worker, the app and the AI step.

Edit the wording here and every path (worker drafts, app drafts, fallback) changes
together. The AI only supplies ``video_topic``; it never writes the email itself.
"""
from __future__ import annotations

import re

AGENCY = "Nav Studios"  # one name everywhere: email, signature, Drive folder, site
PORTFOLIO_URL = "https://navstudios.base44.app/#samples"
DRIVE_LINK_PLACEHOLDER = "[Google Drive link is added after the clips upload]"
SIGNATURE = f"Best,\nBarry Navan\n{AGENCY}"
SUBJECT = "3 ready-to-post Shorts from your latest video"
_TOPIC_SUBJECT_MAX = 50

# 4 sentences. The quote of their own words proves the clips are real before any
# click: a phishing email can't quote someone's video back to them.
_BODY = (
    "Hi {recipient},\n\n"
    "I run " + AGENCY + ", a short-form post-production and marketing agency that turns "
    "long-form podcasts and video backlogs into high-retention vertical clips, and I cut "
    "{clip_text} from your recent video {topic_text}. "
    "{proof}a private Google Drive folder for you: {drive_link}\n\n"
    "You can check out examples of our hook retention cuts, dynamic captions, and "
    "production standards here: {portfolio_url}\n\n"
    "No channel passwords or API access needed: we deliver raw MP4 files only, so just "
    "reply if you'd like this for every new upload.\n\n"
    "{signature}"
)

_FOLLOW_UP = (
    "Hi {recipient},\n\n"
    "Quick follow-up on the {clip_text} I cut from your recent video {topic_text}: "
    "{drive_link}\n\n"
    "Happy to adjust the cuts or send them over another way, just reply.\n\n"
    "{signature}"
)

# Text from older templates, used to spot unsent drafts written before an update.
OLD_TEMPLATE_MARKERS = ("aspiring entrepreneur", "Access the 3 edited clips here:", "I run Nav, a short-form")
_OLD_TITLE_PATTERN = re.compile(r'I loved the "(.+?)"')
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
    return f"{clip_count} sample Short" + ("" if clip_count == 1 else "s")


def build_outreach_email(
    channel_name: str,
    video_title: str = "",
    video_topic: str = "",
    drive_link: str = "",
    clip_count: int = 3,
    opening_line: str = "",
    opening_at: float | None = None,
) -> str:
    """The full email. Without a Drive link yet, a placeholder marks where it goes.

    ``opening_line``/``opening_at``: the first words of one clip and where they are
    in their video, quoted back as proof the clips are real.
    """
    quote = clean_quote(opening_line)
    if quote and opening_at is not None:
        everything = "all three are" if clip_count == 3 else "they're all"
        proof = f'One opens on your line "{quote}" ({format_timestamp(opening_at)}), and {everything} in '
    else:
        proof = "They're in "
    body = _BODY.format(
        recipient=channel_name.strip() or "there",
        clip_text=_clip_text(clip_count),
        topic_text=_topic_text(video_title, video_topic),
        proof=proof,
        drive_link=drive_link.strip() or DRIVE_LINK_PLACEHOLDER,
        portfolio_url=PORTFOLIO_URL,
        signature=SIGNATURE,
    )
    return body.replace("your recent video .", "your recent video.")


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
        clip_text=_clip_text(clip_count),
        topic_text=_topic_text(video_title, video_topic),
        drive_link=drive_link.strip() or DRIVE_LINK_PLACEHOLDER,
        signature=SIGNATURE,
    )
    return body.replace("your recent video :", "your recent video:")


_TOPIC_IN_EMAIL = re.compile(r"from your recent video on (.+?)[.,]")


def subject_for(email: str) -> str:
    """Subject naming their video's topic when the email has one."""
    match = _TOPIC_IN_EMAIL.search(email)
    if match and len(match.group(1)) <= _TOPIC_SUBJECT_MAX:
        return f"3 Shorts from your video on {match.group(1)}"
    return SUBJECT


def topic_from_email(email: str) -> str:
    match = _TOPIC_IN_EMAIL.search(email)
    return match.group(1) if match else ""


def fill_drive_link(email: str, drive_link: str) -> str:
    """Put the Drive folder link into a draft made before the upload."""
    if DRIVE_LINK_PLACEHOLDER in email:
        return email.replace(DRIVE_LINK_PLACEHOLDER, drive_link.strip())
    # Drafts from the old template.
    return email.replace(
        "Access the 3 edited clips here:\n\n",
        f"Access the 3 edited clips here:\n{drive_link.strip()}\n\n",
    )


def is_old_template(email: str) -> bool:
    return any(marker in email for marker in OLD_TEMPLATE_MARKERS)


def rewrite_old_draft(email: str, channel_name: str, drive_link: str, video_title: str = "") -> str:
    """Rebuild a not-yet-sent draft from an older template with the current one."""
    topic = topic_from_email(email)
    if not video_title:
        match = _OLD_TITLE_PATTERN.search(email)
        video_title = match.group(1) if match else ""
    return build_outreach_email(channel_name, video_title=video_title, video_topic=topic, drive_link=drive_link)
