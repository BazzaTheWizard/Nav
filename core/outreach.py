"""Creator outreach email: the one template used by the worker, the app and the AI step.

Edit the wording here and every path (worker drafts, app drafts, fallback) changes
together. The AI only supplies short phrases (the topic, what the best clip's point
is); it never writes the email itself.
"""
from __future__ import annotations

import re

AGENCY = "Nav Studios"  # one name everywhere: email, signature, Drive folder, site
# Your website, shown in the signature. After the new site is live, change this one
# line (no trailing slash). Unsent drafts in the Outbox switch to it automatically.
SITE_URL = "https://navstudiolab.com"
DRIVE_LINK_PLACEHOLDER = "[Google Drive link is added after the clips upload]"
SUBJECT = "3 Shorts from your latest video"  # used when there's no topic to name
MAX_SUBJECT_CHARS = 50


def site_host(url: str | None = None) -> str:
    """'https://navstudios.co' -> 'navstudios.co' (how the site shows in a signature)."""
    return re.sub(r"^https?://", "", SITE_URL if url is None else url).split("/")[0]


SIGNATURE = f"Best,\nBarry Navan\n{AGENCY} | {site_host()}"

_BODY = (
    "Hi {recipient},\n\n"
    "{hook}\n\n"
    "I cut {cut_text} from the {content_format} into {clip_noun} with captions:\n\n"
    "{drive_link}\n\n"
    "{yours} to post on Shorts, Reels, or TikTok. No watermark, no strings.\n\n"
    "If {useful}, would you want me to do 3–5 of these for every new upload?\n\n"
    "{signature}"
)
_OTHERS = {1: "one other", 2: "two others", 3: "three others", 4: "four others"}
_COUNT_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}

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
_QUOTE_PATTERNS = (re.compile(r'especially the part that starts "(.+?)"'), re.compile(r'especially when you said "(.+?)"'))
# "—especially the point about X." (round 7) or "Your point (at 12:34) about X stuck with me" (round 10)
_KEY_POINT_PATTERN = re.compile(r"(?:—especially the point about (.+?)\.\n|Your point (?:at [\d:]+ )?about (.+?) stuck with me)")
_GREETING_PATTERN = re.compile(r"^(?:Hi|Hello|Hey) (.+?),\n")
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


def _tidy(body: str) -> str:
    """Fix spacing when there's no topic or title to name."""
    return (
        body.replace("your recent video .", "your recent video.")
        .replace("your recent video —", "your recent video—")
        .replace("your recent video  ", "your recent video ")
    )


def clean_key_point(point: object) -> str:
    """The AI's one-line insight, ready for 'Your point at 12:34 about ___ stuck with me.'"""
    if not isinstance(point, str):
        return ""
    text = " ".join(point.split()).strip(" \"'“”‘’.,;:!?-—")
    text = re.sub(r"^(?:your point about|the point about|point about|about)\s+", "", text, flags=re.IGNORECASE)
    return text if 3 <= len(text.split()) <= 18 else ""


def clean_first_name(name: object) -> str:
    """'marcela' -> 'Marcela'; anything that isn't one plain first name -> ''."""
    if not isinstance(name, str):
        return ""
    text = name.strip().strip(".,!\"'")
    if not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’-]{1,19}", text):
        return ""
    if text.lower() in {"host", "unknown", "none", "null", "the", "guest", "speaker"}:
        return ""
    return text[0].upper() + text[1:]


def clean_format(content_format: object) -> str:
    """'episode' for podcasts and conversations, 'video' for everything else."""
    text = str(content_format or "").strip().lower()
    return "episode" if text.startswith(("episode", "podcast", "interview")) else "video"


def clean_short_topic(topic: object) -> str:
    """1–3 words for the subject line ('knee arthritis'); anything longer -> ''."""
    if not isinstance(topic, str):
        return ""
    text = " ".join(topic.split()).strip(" \"'“”‘’.,;:!?-—")
    text = re.sub(r"^(?:your|the|this|my|latest|recent)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(?:episode|video|podcast)$", "", text, flags=re.IGNORECASE)
    return text if 1 <= len(text.split()) <= 3 else ""


def build_subject(short_topic: str = "", content_format: str = "", clip_count: int = 3) -> str:
    """'3 Shorts from your knee arthritis episode' (at most MAX_SUBJECT_CHARS).

    Without a short enough topic: '3 Shorts from your latest episode'.
    """
    noun = "Short" if clip_count == 1 else "Shorts"
    kind = clean_format(content_format)
    topic = clean_short_topic(short_topic)
    subject = f"{clip_count} {noun} from your {topic} {kind}"
    if topic and len(subject) <= MAX_SUBJECT_CHARS:
        return subject
    return f"{clip_count} {noun} from your latest {kind}"


def clean_usual_take(take: object) -> str:
    """The common view a moment pushes against, in 1–4 words ('homework'); else ''."""
    if not isinstance(take, str):
        return ""
    text = " ".join(take.split()).strip(" \"'“”‘’.,;:!?-—")
    text = re.sub(r"^(?:the usual take on|the usual|usual take on|on)\s+", "", text, flags=re.IGNORECASE)
    return text if 1 <= len(text.split()) <= 4 and len(text) <= 40 else ""


def _hook(
    timestamp: str,
    point: str,
    quote: str,
    usual_take: str,
    content_format: str,
    video_title: str,
    video_topic: str,
) -> tuple[str, bool]:
    """The opening line, and whether it names one specific moment (its timestamp)."""
    flip = f", especially how it flips the usual take on {usual_take}" if usual_take else ""
    if timestamp and point:
        return f"Your point at {timestamp} about {point} stuck with me{flip}.", True
    if timestamp and quote:
        # Not "your point": the first words of a clip may be a guest's.
        return f'The moment at {timestamp} that starts "{quote.rstrip(".")}" stuck with me.', True
    if point:
        return f"Your point about {point} stuck with me{flip}.", False
    topic = clean_topic(video_topic)
    if topic:
        return f"Your recent {content_format} on {topic} stuck with me.", False
    title = _clean_title(video_title)
    if title:
        return f'Your recent {content_format}, "{title}", stuck with me.', False
    return f"Your recent {content_format} stuck with me.", False


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
    greeting_name: str = "",
    usual_take: str = "",
    content_format: str = "",
) -> str:
    """The first email. Without a Drive link yet, a placeholder marks where it goes.

    ``opening_at``: start of the best clip in the source video (seconds), shown as
    "Your point at 12:34 about ...". ``key_point``: what that clip's point is (from
    the AI step); if missing, the clip's opening words (``opening_line``) are quoted.
    ``usual_take``: the common view the moment pushes against; the "flips the usual
    take" part is left out when there isn't one. ``content_format``: "episode" for
    podcasts, otherwise "video". ``greeting_name``: the host's first name when known;
    otherwise the channel name is used. ``shorts_status`` is no longer used.
    """
    del shorts_status  # kept for older callers; this template doesn't mention Shorts habits
    kind = clean_format(content_format)
    point = clean_key_point(key_point)
    hook, names_moment = _hook(
        format_timestamp(opening_at) if opening_at is not None else "",
        point,
        clean_quote(opening_line),
        clean_usual_take(usual_take) if point else "",
        kind,
        video_title,
        video_topic,
    )
    count = max(1, int(clip_count))
    if names_moment:
        cut_text = "that moment" if count == 1 else f"that moment and {_OTHERS.get(count - 1, f'{count - 1} others')}"
    else:
        cut_text = f"{_COUNT_WORDS.get(count, str(count))} moment" + ("" if count == 1 else "s")
    return _BODY.format(
        recipient=clean_first_name(greeting_name) or channel_name.strip() or "there",
        hook=hook,
        cut_text=cut_text,
        content_format=kind,
        clip_noun="a vertical clip" if count == 1 else "vertical clips",
        drive_link=drive_link.strip() or DRIVE_LINK_PLACEHOLDER,
        yours="It's yours" if count == 1 else "They're yours",
        useful="it's useful" if count == 1 else "they're useful",
        signature=SIGNATURE,
    )


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


# "Your recent video on <topic> stuck with me" (round 10) or "your recent video on
# <topic>" followed by a dash or . , (older emails)
_TOPIC_IN_EMAIL = re.compile(r"[Yy]our recent (?:video|episode) on (.+?)(?: stuck with me|—|[.,]\s)")


def subject_for(email: str) -> str:
    """A subject when only the email text is known (older drafts): no topic to name."""
    return build_subject("", "episode" if " from the episode " in email else "video")


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


def rewrite_old_draft(
    email: str, channel_name: str, drive_link: str, video_title: str = "", shorts_status: str = ""
) -> str:
    """Rebuild a not-yet-sent draft from an older template with the current one,
    keeping its topic or title, key point or quoted line, and greeting name."""
    topic = topic_from_email(email)
    for pattern in _OLD_TITLE_PATTERNS:
        if video_title:
            break
        match = pattern.search(email)
        video_title = match.group(1) if match else ""
    old_quote = _OLD_QUOTE_PATTERN.search(email)
    quote = old_quote.group(1) if old_quote else ""
    for pattern in _QUOTE_PATTERNS:
        match = pattern.search(email)
        if match and not quote:
            quote = match.group(1)
    key_point = _KEY_POINT_PATTERN.search(email)
    greeting = _GREETING_PATTERN.search(email)
    greeting_name = greeting.group(1) if greeting and greeting.group(1) != channel_name.strip() else ""
    return build_outreach_email(
        channel_name,
        video_title=video_title,
        video_topic=topic,
        drive_link=drive_link,
        opening_line=quote,
        key_point=(key_point.group(1) or key_point.group(2)) if key_point else "",
        shorts_status=shorts_status,
        greeting_name=greeting_name,
    )


def greeting_name_of(email: str) -> str:
    """The name in an email's 'Hi ___,' line."""
    match = _GREETING_PATTERN.search(email)
    return match.group(1) if match else ""


def set_greeting(email: str, name: str) -> str:
    """Replace the name in the 'Hi ___,' line (adds one if missing)."""
    name = name.strip()
    if not name:
        return email
    if _GREETING_PATTERN.search(email):
        return _GREETING_PATTERN.sub(lambda _m: f"Hi {name},\n", email, count=1)
    return f"Hi {name},\n\n{email}"


# Website address used before SITE_URL existed; drafts without a saved address used it.
LEGACY_SITE_URL = "https://navstudios.base44.app"


def refresh_site_links(email: str, old_site_url: str) -> str:
    """Swap an older website address for the current SITE_URL (signature and links)."""
    old_host, new_host = site_host(old_site_url), site_host()
    if not old_host or old_host == new_host:
        return email
    return email.replace(old_host, new_host)
