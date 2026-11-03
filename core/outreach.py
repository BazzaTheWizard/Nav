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

# Round 25: ask first, send the link after a yes. The first email names one moment
# from their video and asks if they want the clips; no link, so it reads like a note
# from a person and spam filters have nothing to flag. "{name}" is the host's first
# name, or NO_NAME_GREETING until you type one in the Outbox's First name field.
_ASK_POINT = (
    "Hi {name},\n\n"
    "Your point at {at} about {point} would make a strong vertical clip{where}.\n\n"
    "I've already cut it{more}, captioned and ready to post. Free, and yours to keep either way.\n\n"
    "Want me to send {them} over?\n\n"
    "{signature}"
)
# When the AI gave no time or point for the best clip.
_ASK_PLAIN = (
    "Hi {name},\n\n"
    "I watched your recent {kind}{about} and cut {count} vertical {clip_noun} from it{where_plain}, "
    "captioned and ready to post. Free, and yours to keep either way.\n\n"
    "Want me to send {them} over?\n\n"
    "{signature}"
)
NO_NAME_GREETING = "there"

# Round 25 offer, used in the reply to a "yes" and in the LinkedIn close.
# Change these lines to change the price or the trial everywhere.
FOUNDING_PRICE = "$500/month"
FOUNDING_SPOTS = 3
TRIAL = "a 2-week trial"

# Follow-ups, sent as replies in the same thread ("Re: <subject>"), counted in days
# after the first email. Pitches whose first email already had the link (before
# round 25) get the "bump with link" and the last note only.
FOLLOW_UP_DAYS = {"bump": 3, "show": 7, "last": 12, "bump_link": 3}
FOLLOW_UP_STEPS = ("bump", "show", "last")
FOLLOW_UP_STEPS_LINK_SENT = ("bump_link", "last")
FOLLOW_UP_LABELS = {
    "bump": "Short bump (no link)",
    "show": "The link, so they can just look",
    "last": "Last note",
    "bump_link": "Short bump with the link again",
}

# Pre-round-25 follow-up (the link was already in the first email).
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
    "completely free to help you drive more reach",  # round 18-24 (link in the first email)
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


_CUT_COUNT = re.compile(r"I cut \d+ highlight moments?")
_SUBJECT_COUNT = re.compile(r"^(\d+) ([Ss])horts?\b")
_COUNT_WORDING = (  # (several clips, one clip) in the round 18 template
    ("into vertical 9:16 clips with captions", "into a vertical 9:16 clip with captions"),
    ("They’re completely yours", "It’s completely yours"),
    ("how they perform", "how it performs"),
)
# Round 25 wording: "I've already cut it, plus 2 more from the same episode, captioned"
# and "...and cut 3 vertical clips from it".
_ALREADY_CUT = re.compile(r"I've already cut it(?:, plus \d+ more from the same (episode|video))?, captioned")
_PLAIN_CUT = re.compile(r"and cut \d+ vertical clips? from it")
_SEND_THEM = ("Want me to send them over?", "Want me to send it over?")


def _more_text(count: int, kind: str) -> str:
    """', plus 2 more from the same episode' ('' for a single clip)."""
    return "" if count <= 1 else f", plus {count - 1} more from the same {clean_format(kind)}"


def set_clip_count(email: str, count: int) -> tuple[str, bool]:
    """The draft after removing clips, with the wording to match ('...plus 1 more...',
    'Want me to send them over?'). Returns (email, True), or (email unchanged, False)
    when the email doesn't use the template's wording (you rewrote it): then check
    the number yourself."""
    email = email or ""
    one = count == 1
    already = _ALREADY_CUT.search(email)
    plain = _PLAIN_CUT.search(email)
    if already or plain:
        if already:
            kind = already.group(1) or ("episode" if "episode" in email else "video")
            email = _ALREADY_CUT.sub(f"I've already cut it{_more_text(count, kind)}, captioned", email, count=1)
        else:
            email = _PLAIN_CUT.sub(f"and cut {count} vertical clip{'' if one else 's'} from it", email, count=1)
        several, single = _SEND_THEM
        email = email.replace(several, single) if one else email.replace(single, several)
        return email, True
    if not _CUT_COUNT.search(email):
        return email, False
    email = _CUT_COUNT.sub(f"I cut {count} highlight moment{'' if one else 's'}", email, count=1)
    for several, single in _COUNT_WORDING:
        email = email.replace(several, single) if one else email.replace(single, several)
    return email, True


def set_subject_count(subject: str, count: int) -> str:
    """'3 shorts from your knee pain video' -> '2 shorts from ...'. A subject that
    doesn't start with the number is left alone."""
    match = _SUBJECT_COUNT.match(subject or "")
    if not match:
        return subject
    noun = f"{match.group(2)}hort{'' if count == 1 else 's'}"
    return f"{count} {noun}{subject[match.end():]}"


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


def _where(business: bool, plain: bool = False) -> str:
    """Where the clip would work: LinkedIn for B2B experts, Shorts and Reels otherwise."""
    if business:
        return " for LinkedIn and Shorts" if plain else ", on LinkedIn as much as Shorts"
    return " for Shorts and Reels"


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
    business: bool = False,
) -> str:
    """The first email (round 25): one moment from their video, then "Want me to send
    them over?". No link: it goes in the reply to a yes, or in the day-7 follow-up.

    With the best clip's time (``opening_at``) and point (``key_point``) it opens
    "Your point at 12:34 about ___ ..."; without them, "I watched your recent episode
    on ___ and cut 3 vertical clips from it". ``greeting_name``: the host's first
    name; otherwise "Hi there" until you type the name in the Outbox. ``business``:
    the creator sells a service (B2B lead profile), so the email mentions LinkedIn.
    ``drive_link`` and the other arguments are accepted so every caller keeps
    working; the link is saved on the pitch, not put in this email.
    """
    del drive_link, opening_line, shorts_status, usual_take, channel_name
    count = max(1, int(clip_count))
    one = count == 1
    kind = clean_format(content_format)
    name = clean_first_name(greeting_name) or NO_NAME_GREETING
    point = clean_key_point(key_point)
    if point and opening_at is not None and opening_at >= 0:
        return _ASK_POINT.format(
            name=name,
            at=format_timestamp(opening_at),
            point=point,
            where=_where(business),
            more=_more_text(count, kind),
            them="it" if one else "them",
            signature=SIGNATURE,
        )
    topic = clean_topic(video_topic)
    title = _clean_title(video_title)
    about = f" on {topic}" if topic else (f' "{title}"' if title else "")
    return _ASK_PLAIN.format(
        name=name,
        kind=kind,
        about=about,
        count=count,
        clip_noun="clip" if one else "clips",
        where_plain=_where(business, plain=True),
        them="it" if one else "them",
        signature=SIGNATURE,
    )


def build_follow_up(
    channel_name: str,
    video_title: str = "",
    video_topic: str = "",
    drive_link: str = "",
    clip_count: int = 3,
) -> str:
    """The follow-up for pitches whose first email already had the link (before
    round 25). ``channel_name``: pass the host's first name; anything that isn't one
    plain first name (a channel name) gets "Hi there"."""
    body = _FOLLOW_UP.format(
        recipient=clean_first_name(channel_name) or NO_NAME_GREETING,
        clip_text=f"{clip_count} sample Short" + ("" if clip_count == 1 else "s"),
        topic_text=_topic_text(video_title, video_topic),
        drive_link=drive_link.strip() or DRIVE_LINK_PLACEHOLDER,
        signature=SIGNATURE,
    )
    return _tidy(body)


_COUNT_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}


def follow_up_steps(link_in_first_email: bool) -> tuple[str, ...]:
    """The follow-up sequence for a pitch: ("bump", "show", "last"), or
    ("bump_link", "last") when the first email already had the link."""
    return FOLLOW_UP_STEPS_LINK_SENT if link_in_first_email else FOLLOW_UP_STEPS


def build_follow_up_step(
    step: str,
    first_name: str = "",
    link: str = "",
    clip_count: int = 3,
    content_format: str = "",
    video_title: str = "",
    video_topic: str = "",
) -> str:
    """One follow-up, written as a reply in the same thread.

    "bump" (day 3): short, no link. "show" (day 7): the link, so they can just look.
    "last" (day 12): a polite last note. "bump_link": the older follow-up with the link,
    for pitches whose first email already had it.
    """
    count = max(1, int(clip_count))
    name = clean_first_name(first_name) or NO_NAME_GREETING
    kind = clean_format(content_format)
    if step == "bump_link":
        return build_follow_up(name if name != NO_NAME_GREETING else "", video_title, video_topic, link, count)
    if step == "bump":
        if count == 1:
            middle = "The clip is already made, so it's yours if you want it."
        else:
            middle = (
                f"The clips are already made, so I'm happy to send just the best one if "
                f"{_COUNT_WORDS.get(count, str(count))} feels like a lot."
            )
        body = f"Hi {name}, quick bump on this. {middle}"
    elif step == "show":
        clips = "clip" if count == 1 else f"{count} clips"
        them = "it" if count == 1 else "them"
        body = (
            f"Hi {name},\n\n"
            f"Easier to just show you: {link.strip() or DRIVE_LINK_PLACEHOLDER}\n\n"
            f"That's the {clips} from your {kind}, captioned and ready for Shorts, Reels or LinkedIn. "
            f"Post {them} if they're useful. If you'd like this for every new {kind}, just reply."
        )
    elif step == "last":
        body = (
            f"Hi {name},\n\n"
            "Last note from me. If clips aren't a priority right now, no worries. "
            "Should I check back in a couple of months?"
        )
    else:
        raise ValueError(f"Unknown follow-up step: {step!r}")
    return f"{body}\n\n{SIGNATURE}"


def offer_text(clip_count: int = 3, content_format: str = "") -> str:
    """The paid offer, in one paragraph (reply to a yes, LinkedIn close)."""
    count = max(1, int(clip_count))
    kind = clean_format(content_format)
    clips = "1 clip" if count == 1 else f"{count} clips"
    return (
        f"If you'd like this for every new {kind} ({clips}, each with a ready-to-post caption), "
        f"I'm taking {FOUNDING_SPOTS} founding clients at {FOUNDING_PRICE}, month to month. "
        f"Happy to start with {TRIAL} so you can see how they do first."
    )


def build_yes_reply(
    first_name: str = "",
    link: str = "",
    clip_count: int = 3,
    content_format: str = "",
) -> str:
    """Your reply when they say "yes, send them": the link, then the offer."""
    count = max(1, int(clip_count))
    name = clean_first_name(first_name) or NO_NAME_GREETING
    if count == 1:
        lead = "Here it is"
        what = "The clip is captioned and ready for Shorts, Reels or LinkedIn. It's yours to post."
    else:
        lead = "Here they are"
        what = (
            f"All {count} are captioned and ready for Shorts, Reels or LinkedIn. "
            "Download whichever you like; they're yours."
        )
    return (
        f"Hi {name},\n\n"
        f"{lead}: {link.strip() or DRIVE_LINK_PLACEHOLDER}\n\n"
        f"{what}\n\n"
        f"{offer_text(count, content_format)}\n\n"
        f"{SIGNATURE}"
    )


LINKEDIN_NOTE_MAX = 200  # characters in a connection note on a free LinkedIn account


def build_linkedin_messages(
    first_name: str = "",
    *,
    short_topic: str = "",
    key_point: str = "",
    clip_seconds: float | None = None,
    clip_count: int = 3,
    content_format: str = "",
    business: bool = False,
) -> dict[str, str]:
    """Three LinkedIn messages: "connect" (the connection note, at most
    LINKEDIN_NOTE_MAX characters), "after_accept" (once they accept) and "close"
    (after they say yes and you've sent the clip)."""
    name = clean_first_name(first_name)
    kind = clean_format(content_format)
    source = "B2B podcasts" if business else "long-form videos"
    topic = clean_short_topic(short_topic)
    opener = f"{name}, enjoyed" if name else "Enjoyed"
    connect = f"{opener} your {kind} on {topic}. I make short clips from {source} and would be glad to connect."
    if not topic or len(connect) > LINKEDIN_NOTE_MAX:
        connect = f"{opener} your latest {kind}. I make short clips from {source} and would be glad to connect."
    thanks = f"Thanks for connecting, {name}." if name else "Thanks for connecting."
    point = clean_key_point(key_point)
    if point:
        length = f" ({round(clip_seconds)} seconds)" if clip_seconds and clip_seconds > 0 else ""
        after = (
            f"{thanks} Your point about {point} would make a strong vertical clip, so I cut it{length}: "
            "captioned and ready for your feed.\n\n"
            "Want me to send it over? No strings either way."
        )
    else:
        after = (
            f"{thanks} I cut a few vertical clips from your latest {kind}, captioned and ready "
            "for your feed.\n\n"
            "Want me to send them over? No strings either way."
        )
    close = f"Here it is. {offer_text(clip_count, content_format)}"
    return {"connect": connect, "after_accept": after, "close": close}


# Shared inboxes are read by staff or nobody; the host's own email or LinkedIn gets read.
SHARED_INBOXES = frozenset({
    "info", "support", "contact", "contactus", "hello", "hi", "admin", "office", "team", "help",
    "sales", "press", "media", "marketing", "enquiries", "inquiries", "enquiry", "inquiry",
    "general", "mail", "service", "customerservice", "care", "noreply", "no-reply",
})


def is_shared_inbox(email: object) -> bool:
    """info@, support@, hello@ ... (an address no one person owns)."""
    text = str(email or "").strip().lower()
    if "@" not in text:
        return False
    return text.split("@", 1)[0] in SHARED_INBOXES


# "Your recent video on <topic> stuck with me" (round 10), "your recent video on <topic>
# and cut 3 ..." (round 25) or "your recent video on <topic>" followed by a dash or . ,
_TOPIC_IN_EMAIL = re.compile(r"[Yy]our recent (?:video|episode) on (.+?)(?: stuck with me| and cut \d|—|[.,]\s)")


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
    facts: dict | None = None,
) -> str:
    """Rebuild a not-yet-sent draft from an older template with the current one,
    keeping the first name it greets (unless that was just the channel name).
    ``facts``: the pitch's saved moment (round 25 records), when there is one."""
    del shorts_status
    facts = facts or {}
    name = greeting_name_of(email)
    at = facts.get("at")
    return build_outreach_email(
        channel_name,
        video_title=video_title,
        video_topic=str(facts.get("topic") or ""),
        drive_link=drive_link,
        clip_count=clip_count,
        opening_at=float(at) if isinstance(at, (int, float)) else None,
        key_point=str(facts.get("point") or ""),
        greeting_name="" if name == channel_name.strip() else name,
        content_format=str(facts.get("format") or ""),
        business=bool(facts.get("business")),
    )


def greeting_name_of(email: str) -> str:
    """The name the email opens with ('Hi Dana,', or 'Dana — I cut ...' in round 18
    drafts); '' while it still says "Hi there"."""
    match = _DASH_GREETING.match(email or "")
    if match:
        name = match.group(1).strip()
        return "" if name in {"Hi there", NO_NAME_GREETING} else name
    match = _GREETING_PATTERN.search(email or "")
    if not match:
        return ""
    return "" if match.group(1).strip().lower() == NO_NAME_GREETING else match.group(1)


def set_greeting(email: str, name: str) -> str:
    """Put ``name`` at the start of the email ('Hi Dana,'; 'Dana — I cut ...' in round
    18 drafts). Adds a greeting if there is none."""
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
