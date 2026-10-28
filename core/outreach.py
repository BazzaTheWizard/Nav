"""Creator outreach email: the one template used by the worker, the app and the AI step.

Edit the wording here and every path (worker drafts, app drafts, fallback) changes
together. The AI only supplies ``video_topic``; it never writes the email itself.
"""
from __future__ import annotations

import re

PORTFOLIO_URL = "https://navstudios.base44.app/#samples"
DRIVE_LINK_PLACEHOLDER = "[Google Drive link is added after the clips upload]"
SIGNATURE = "Best,\nBarry Navan\nNav Studios"
SUBJECT = "3 ready-to-post Shorts from your latest video"

_BODY = (
    "Hi {recipient},\n\n"
    "I run Nav, a short-form post-production and marketing agency that turns long-form "
    "podcasts and video backlogs into high-retention vertical clips. "
    "I already cut {clip_text} from your recent video {topic_text}, and they're in a "
    "private Google Drive folder for you: {drive_link}\n\n"
    "You can check out examples of our hook retention cuts, dynamic captions, and "
    "production standards here: {portfolio_url}\n\n"
    "No channel passwords or API access needed: we deliver raw MP4 files only, so just "
    "reply if you'd like this for every new upload.\n\n"
    "{signature}"
)

# Text from the old template, used to spot drafts written before this update.
OLD_TEMPLATE_MARKERS = ("aspiring entrepreneur", "Access the 3 edited clips here:")
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


def build_outreach_email(
    channel_name: str,
    video_title: str = "",
    video_topic: str = "",
    drive_link: str = "",
    clip_count: int = 3,
) -> str:
    """The full email. Without a Drive link yet, a placeholder marks where it goes."""
    topic = clean_topic(video_topic)
    title = _clean_title(video_title)
    if topic:
        topic_text = f"on {topic}"
    elif title:
        topic_text = f'"{title}"'
    else:
        topic_text = ""
    clip_text = f"{clip_count} sample Short" + ("" if clip_count == 1 else "s")
    body = _BODY.format(
        recipient=channel_name.strip() or "there",
        clip_text=clip_text,
        topic_text=topic_text,
        drive_link=drive_link.strip() or DRIVE_LINK_PLACEHOLDER,
        portfolio_url=PORTFOLIO_URL,
        signature=SIGNATURE,
    )
    return body.replace("your recent video , and", "your recent video, and")


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
    """Rebuild a not-yet-sent draft from the old template with the new one."""
    if not video_title:
        match = _OLD_TITLE_PATTERN.search(email)
        video_title = match.group(1) if match else ""
    return build_outreach_email(channel_name, video_title=video_title, drive_link=drive_link)
