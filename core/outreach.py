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
SUBJECT = "3 shorts from your recent video"  # used when there's no topic to name
MAX_SUBJECT_CHARS = 50


def site_host(url: str | None = None) -> str:
    """'https://navstudios.co' -> 'navstudios.co' (how the site shows in a signature)."""
    return re.sub(r"^https?://", "", SITE_URL if url is None else url).split("/")[0]


SIGNATURE = f"Best,\nBarry Navan\n{AGENCY} | {site_host()}"

# Round 18: Barry's template. "{greeting}" is the host's first name, or NO_NAME_GREETING
# until you type one in the Outbox's First name field.
_BODY = (
    "{greeting} — I cut {count} highlight {moments} from your recent {channel}upload into "
    "{clip_noun} with captions:\n\n"
    "{drive_link}\n\n"
    "{yours} completely yours to post across Shorts, Reels, or TikTok—unwatermarked, no strings attached.\n\n"
    "If you like how {they} {perform}, I’d love to produce your clips for the next week completely free "
    "to help you drive more reach.\n\n"
    "Worth exploring?\n\n"
    "{signature}"
)
NO_NAME_GREETING = "Hi there"

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
    "of these for every new upload?",  # rounds 10-17: "...do 3–5 of these for every new upload?"
)
# "Hi Dana,\n" (templates before round 18) and "Dana — I cut ..." (round 18).
_GREETING_PATTERN = re.compile(r"^(?:Hi|Hello|Hey) (.+?),\n")
_DASH_GREETING = re.compile(r"\A([^\n—]{1,60}?) — ")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+")
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
    """'3 shorts from your knee arthritis episode' (at most MAX_SUBJECT_CHARS).

    Without a short enough topic: '3 shorts from your recent episode'.
    """
    noun = "short" if clip_count == 1 else "shorts"
    kind = clean_format(content_format)
    topic = clean_short_topic(short_topic)
    subject = f"{clip_count} {noun} from your {topic} {kind}"
    if topic and len(subject) <= MAX_SUBJECT_CHARS:
        return subject
    return f"{clip_count} {noun} from your recent {kind}"


_OLD_SUBJECT = re.compile(r"^(\d+) Shorts? from your (?:latest )?")


def modern_subject(subject: str) -> str:
    """An unsent draft's subject in the round 18 style ('3 Shorts from your latest video'
    -> '3 shorts from your recent video'). A subject you wrote yourself is left alone."""
    match = _OLD_SUBJECT.match(subject or "")
    if not match:
        return subject
    count = int(match.group(1))
    rest = subject[match.end():]
    latest = "latest " in match.group(0)
    return f"{count} {'short' if count == 1 else 'shorts'} from your {'recent ' if latest else ''}{rest}"


def clean_usual_take(take: object) -> str:
    """The common view a moment pushes against, in 1–4 words ('homework'); else ''."""
    if not isinstance(take, str):
        return ""
    text = " ".join(take.split()).strip(" \"'“”‘’.,;:!?-—")
    text = re.sub(r"^(?:the usual take on|the usual|usual take on|on)\s+", "", text, flags=re.IGNORECASE)
    return text if 1 <= len(text.split()) <= 4 and len(text) <= 40 else ""


def clean_channel_name(name: object) -> str:
    """'Leighton Grey - Grey Matter 🇨🇦' -> 'Leighton Grey - Grey Matter' (for 'your recent ___ upload')."""
    text = _EMOJI.sub(" ", str(name or ""))
    return " ".join(text.split()).strip(" -–—|·•:")


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
    """The first email (Barry's round 18 template). Without a link yet, a placeholder
    marks where it goes.

    ``greeting_name``: the host's first name when known; otherwise the email opens with
    NO_NAME_GREETING ("Hi there") until you type the name in the Outbox. The channel
    name goes in "your recent ___ upload". The other arguments (the topic, the best
    clip's time and point, ...) belonged to the older templates; they're still accepted
    so every caller keeps working, but this template doesn't use them.
    """
    del video_title, video_topic, opening_line, opening_at, key_point, shorts_status, usual_take, content_format
    count = max(1, int(clip_count))
    one = count == 1
    channel = clean_channel_name(channel_name)
    return _BODY.format(
        greeting=clean_first_name(greeting_name) or NO_NAME_GREETING,
        count=count,
        moments="moment" if one else "moments",
        channel=f"{channel} " if channel else "",
        clip_noun="a vertical 9:16 clip" if one else "vertical 9:16 clips",
        drive_link=drive_link.strip() or DRIVE_LINK_PLACEHOLDER,
        yours="It’s" if one else "They’re",
        they="it" if one else "they",
        perform="performs" if one else "perform",
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
    """A subject when only the email text is known (older drafts): no topic to name.
    (Round 18 emails don't say "episode", so they get "video".)"""
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
    email: str,
    channel_name: str,
    drive_link: str,
    video_title: str = "",
    shorts_status: str = "",
    clip_count: int = 3,
) -> str:
    """Rebuild a not-yet-sent draft from an older template with the current one,
    keeping the first name it greets (unless that was just the channel name)."""
    del video_title, shorts_status  # the current template doesn't name the video
    name = greeting_name_of(email)
    return build_outreach_email(
        channel_name,
        drive_link=drive_link,
        clip_count=clip_count,
        greeting_name="" if name == channel_name.strip() else name,
    )


def greeting_name_of(email: str) -> str:
    """The name the email opens with ('Dana — I cut ...', or 'Hi Dana,' in older
    drafts); '' while it still says NO_NAME_GREETING."""
    match = _DASH_GREETING.match(email or "")
    if match:
        name = match.group(1).strip()
        return "" if name == NO_NAME_GREETING else name
    match = _GREETING_PATTERN.search(email or "")
    return match.group(1) if match else ""


def set_greeting(email: str, name: str) -> str:
    """Put ``name`` at the start of the email ('Dana — I cut ...'; 'Hi Dana,' in older
    drafts). Adds a greeting if there is none."""
    name = name.strip()
    if not name:
        return email
    if _DASH_GREETING.match(email):
        return _DASH_GREETING.sub(lambda _m: f"{name} — ", email, count=1)
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
