"""Round 50: a ready-to-post Shorts & Reels caption next to every clip (<clip>_social_post.json).

The second piece of post copy each clip ships with (the first is the LinkedIn post,
core.linkedin_post): what a team pastes into YouTube Shorts, Instagram Reels and TikTok.

- title: a short, plain headline for the clip (YouTube Shorts shows it; at most TITLE_MAX).
- description: one sentence on the video it comes from (the show and the episode), one on what
  is said in this clip, and "Full episode: <show>." Sentence case, no emoji.
- hashtags: HASHTAG_COUNT tags (Instagram and YouTube both suggest a handful, not dozens):
  the clip's own topics, the show, and a broad topic tag that fits.

Same honesty rules as the LinkedIn post: the AI drafts it, and every line is checked against
the transcript, the video's title, the show and the month's theme (core.linkedin_post.grounded).
A line that fails is replaced by one built from the speaker's own words; with no AI key the
whole caption is built that way. No invented claims, names or numbers. SOCIAL_POSTS=0 in .env
turns it off.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

TITLE_MAX = 70
ABOUT_MAX = 170  # each of the two description sentences
HASHTAG_COUNT = 5
TAG_MAX = 24
SUFFIX = "_social_post.json"
PLATFORMS = "YouTube Shorts, Instagram Reels and TikTok"

# Broad tags a clip may carry when its own words point at the field (never on their own say-so).
_BROAD = (
    ({"pricing", "price", "sales", "revenue", "deal", "deals", "pipeline", "quota", "selling"}, "Sales"),
    ({"marketing", "brand", "content", "audience", "campaign", "demand"}, "Marketing"),
    ({"ai", "model", "models", "automation", "agents", "llm"}, "AI"),
    ({"startup", "startups", "founder", "founders", "seed", "raise", "raised", "venture", "vc"}, "Startups"),
    ({"invest", "investing", "investor", "investors", "market", "markets", "stock", "stocks", "fund", "portfolio"}, "Investing"),
    ({"finance", "cash", "margin", "margins", "profit", "budget", "cfo"}, "Finance"),
    ({"hire", "hiring", "hired", "team", "teams", "culture", "manager", "managers", "leader", "leaders",
      "leadership"}, "Leadership"),
    ({"saas", "software", "product", "customers", "churn", "retention"}, "SaaS"),
    ({"law", "legal", "lawyer", "contract", "contracts", "compliance"}, "Law"),
)

SYSTEM_PROMPT = """You write the caption for a short vertical video clip (YouTube Shorts, Instagram Reels,
TikTok) cut from a podcast, webinar or talk. The company posts it with the clip.

Return one JSON object:
{"title": "...", "about_video": "...", "about_clip": "...", "hashtags": ["...", "...", "...", "...", "..."]}

Rules:
- "title": at most 70 characters, plain and specific: what the clip is about, in the speaker's
  terms. Sentence case. No clickbait ("You won't believe..."), no emoji, no hashtags.
- "about_video": one sentence describing the show and the episode this clip comes from, using
  only the show name, the episode title and the theme you are given.
- "about_clip": one sentence on what the speaker says in this clip, in their words (light
  trimming only). Never add a fact, number, name or claim that isn't in the transcript.
- "hashtags": exactly 5, without the # sign, CamelCase, no spaces: the clip's main topics as
  said in the transcript, the show's name if it's short, and one broad field tag
  (e.g. Sales, Marketing, AI, Startups, Leadership). No generic spam tags (viral, fyp, trending).
Return only the JSON."""


@dataclass
class SocialPost:
    title: str
    description: str
    hashtags: list[str] = field(default_factory=list)
    source: str = "transcript"  # "ai", "ai+transcript" or "transcript"

    def caption(self) -> str:
        """What goes in the caption box: the description, then the hashtags."""
        tags = " ".join(f"#{t}" for t in self.hashtags)
        return f"{self.description}\n\n{tags}".strip()

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def enabled() -> bool:
    return os.getenv("SOCIAL_POSTS", "1").strip().lower() not in {"0", "false", "no", "off"}


_ACRONYMS = {w.lower(): w for w in ("AI", "SaaS", "B2B", "B2C", "CEO", "CFO", "CTO", "VC", "GTM", "ROI", "ARR",
                                     "MRR", "IPO", "HR", "SEO", "M&A", "PE", "LLM")}


def _camel(text: str) -> str:
    """'AI pricing' -> 'AIPricing'; 'the revenue show' -> 'TheRevenueShow' (letters and digits only)."""
    parts = re.findall(r"[A-Za-z0-9]+", str(text or ""))
    return "".join(_ACRONYMS.get(p.lower()) or (p if p.isupper() or any(c.isupper() for c in p[1:])
                                                 else p[:1].upper() + p[1:]) for p in parts)


def _tag_words(tag: str) -> list[str]:
    """'AIPricing' -> ['ai', 'pricing']; 'SaaS' -> ['saas']."""
    if tag.lower() in {"saas", "b2b", "b2c", "ai"}:
        return [tag.lower()]
    for mixed, plain in (("SaaS", "Saas"), ("B2B", "Btob"), ("B2C", "Btoc"), ("M&A", "Ma")):
        tag = tag.replace(mixed, plain)
    words = [w.lower() for w in re.findall(r"[A-Z]{2,}(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", tag)]
    return [{"btob": "b2b", "btoc": "b2c"}.get(w, w) for w in words]


def clean_tag(tag: Any) -> str:
    text = _camel(str(tag or "").lstrip("#"))
    return text if 2 <= len(text) <= TAG_MAX and not text.isdigit() else ""


def _vocabulary(*texts: str) -> set[str]:
    from core.linkedin_post import _stem

    return {_stem(w) for text in texts for w in re.findall(r"[a-z0-9][a-z0-9'’-]*", str(text or "").lower())}


def tag_ok(tag: str, vocabulary: set[str]) -> bool:
    """A hashtag is fine when every word in it was said or is in the title, the show or the
    theme, or it's a broad field tag the clip's own words point at."""
    from core.linkedin_post import _stem

    if tag.lower() in {"viral", "fyp", "foryou", "foryoupage", "trending", "explore", "explorepage", "reels", "shorts"}:
        return False
    words = _tag_words(tag)
    return bool(words) and all(_stem(w) in vocabulary or w in vocabulary for w in words)


def broad_tags(text: str) -> list[str]:
    said = set(re.findall(r"[a-z0-9]+", text.lower()))
    return [tag for words, tag in _BROAD if said & words]


def _keywords(words: list[Any], title: str, theme: str = "") -> list[str]:
    """The clip's topics for hashtags, best first: the month's theme; two-word phrases said in
    the clip that are also in its title (or said twice); then single words said at least twice
    (most said first). Each is something the speaker actually talked about."""
    from core.linkedin_post import _STOP, _stem

    def tokens(text: str) -> list[str]:
        return [w.strip("'’-") for w in re.findall(r"[a-z0-9][a-z0-9'’-]*", text.lower())]

    said = tokens(" ".join(str(x.text) for x in words))
    content = [w if (len(w) >= 3 and w not in _STOP) or w == "ai" else "" for w in said]
    title_text = " ".join(tokens(title))
    out: list[str] = []
    theme_words = tokens(theme)
    if 1 <= len(theme_words) <= 3:
        out.append(" ".join(theme_words))
    pairs: dict[str, int] = {}
    for a, b in zip(content, content[1:]):
        if a and b and a != b:
            pairs[f"{a} {b}"] = pairs.get(f"{a} {b}", 0) + 1
    for pair, n in sorted(pairs.items(), key=lambda kv: -kv[1]):
        if pair in title_text or n >= 2:
            out.append(pair)
    counts: dict[str, int] = {}
    for w in content:
        if w and len(w) >= 4:
            counts[_stem(w)] = counts.get(_stem(w), 0) + 1
    first_form: dict[str, str] = {}
    for w in content:
        if w:
            first_form.setdefault(_stem(w), w)
    for stem, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if n >= 2:
            out.append(first_form[stem])
    return out


def _show_tag(show: str) -> str:
    tag = clean_tag(show)
    return tag if tag and len(tag) <= 20 and tag.lower() not in {"yourchannel", "unknowncreator", "clips"} else ""


def _sentence(text: Any, limit: int) -> str:
    from core.linkedin_post import _tidy

    line = _tidy(text, limit)
    if line and not line.endswith((".", "?", "!")):
        line += "."
    return line


def from_transcript(words: list[Any], *, title: str = "", video_title: str = "", show: str = "",
                    theme: str = "", kind: str = "episode") -> SocialPost:
    """The caption from the speaker's own words, the title, the show and the theme."""
    from core.linkedin_post import _tidy
    from core.linkedin_post import from_transcript as post_lines
    from core.outreach import clean_channel_name, clean_format, episode_title

    show = clean_channel_name(show)
    kind = clean_format(kind)
    episode = episode_title(video_title)
    clip_title = _tidy(title, TITLE_MAX) or _tidy(episode, TITLE_MAX)
    if show and episode and show.lower() not in episode.lower():
        about_video = f'From {show}\'s {kind} "{episode}".'
    elif episode:
        about_video = f'From the {kind} "{episode}".'
    elif show:
        about_video = f"From {show}" + (f", on {theme}." if theme else ".")
    else:
        about_video = ""
    lines = post_lines(words, title)
    about_clip = _sentence(lines.hook, ABOUT_MAX) if words else ""
    if about_clip:
        about_clip = "In this clip: " + about_clip[:1].lower() + about_clip[1:] if not about_clip[:2].isupper() \
            else "In this clip: " + about_clip
    description = " ".join(p for p in (about_video, about_clip, f"Full {kind}: {show}." if show else "") if p)
    transcript = " ".join(str(w.text) for w in words)
    tags: list[str] = []
    for phrase in _keywords(words, title, theme):
        tag = clean_tag(phrase)
        # skip one already inside a tag ('Pricing' after 'AIPricing') or one that holds a tag
        if tag and not any(tag.lower() in t.lower() or t.lower() in tag.lower() for t in tags):
            tags.append(tag)
        if len(tags) >= HASHTAG_COUNT - 2:
            break
    extras = [_show_tag(show), *broad_tags(f"{transcript} {title} {theme}"), "Podcast" if kind == "episode" else ""]
    for tag in extras:
        if tag and tag.lower() not in {t.lower() for t in tags} and len(tags) < HASHTAG_COUNT:
            tags.append(tag)
    return SocialPost(clip_title, description, tags, "transcript")


def build(words: list[Any], *, title: str = "", video_title: str = "", show: str = "", theme: str = "",
          kind: str = "episode", ask: Callable[[str, str], str | None] | None = None) -> SocialPost:
    """The clip's Shorts & Reels caption: the AI's draft, each part checked against the clip."""
    from core.linkedin_post import _tidy, grounded

    fallback = from_transcript(words, title=title, video_title=video_title, show=show, theme=theme, kind=kind)
    transcript = " ".join(str(w.text) for w in words)
    names_ok = f"{video_title} {show} {title} {theme}"
    if ask is None:
        from core.graphics_director import ask_ai as ask
    try:
        prompt = (f"Show: {show or 'unknown'}\nEpisode title: {video_title or 'unknown'}\nClip headline: {title}\n"
                  + (f"The company's content theme this month: {theme}\n" if theme else "")
                  + f"Transcript of the clip:\n{transcript}")
        raw = ask(prompt, SYSTEM_PROMPT)
    except Exception as exc:  # noqa: BLE001 - the transcript version is next
        logger.warning("Shorts & Reels caption AI failed: %s", exc)
        raw = None
    if not raw:
        return fallback
    try:
        from core.agent_brain import _parse_json_response

        answer = _parse_json_response(raw)
    except Exception:  # noqa: BLE001
        return fallback
    replaced = 0
    context = f"{transcript} {video_title} {show} {theme} {title}"
    clip_title = _tidy(answer.get("title"), TITLE_MAX)
    if not clip_title or not grounded(clip_title, context, names_ok):
        clip_title, replaced = fallback.title, replaced + 1
    about_video = _sentence(answer.get("about_video"), ABOUT_MAX)
    general = f"{video_title} {show} {theme} {kind} episode podcast show video conversation interview talk webinar"
    if not about_video or not grounded(re.sub(r"['’]s\b", "", about_video), general + " " + transcript, names_ok):
        about_video, replaced = "", replaced + 1
    about_clip = _sentence(answer.get("about_clip"), ABOUT_MAX)
    if not about_clip or not grounded(about_clip, transcript, names_ok):
        about_clip, replaced = "", replaced + 1
    if not about_video or not about_clip:  # the transcript version's sentences fill the gaps
        parts = fallback.description.split(" In this clip: ")
        about_video = about_video or parts[0].replace(f" Full {kind}: {show}.", "").strip()
        about_clip = about_clip or ("In this clip: " + parts[1].split(f" Full {kind}:")[0] if len(parts) > 1 else "")
    from core.outreach import clean_channel_name

    name = clean_channel_name(show)
    description = " ".join(p for p in (about_video, about_clip, f"Full {kind}: {name}." if name else "") if p)
    vocabulary = _vocabulary(context)
    broad = {t.lower() for t in broad_tags(context)}
    tags: list[str] = []
    for raw_tag in (answer.get("hashtags") or [])[:8]:
        tag = clean_tag(raw_tag)
        if tag and (tag_ok(tag, vocabulary) or tag.lower() in broad) and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
        elif tag:
            replaced += 1
        if len(tags) >= HASHTAG_COUNT:
            break
    for tag in fallback.hashtags:
        if len(tags) >= HASHTAG_COUNT:
            break
        if tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    return SocialPost(clip_title, description, tags, "ai" if not replaced else "ai+transcript")


def post_path(clip: Path) -> Path:
    clip = Path(clip)
    return clip.with_name(f"{clip.stem}{SUFFIX}")


def write_for_clip(clip: Path, words: list[Any], **kwargs: Any) -> Path:
    """Write <clip>_social_post.json next to the clip; returns its path."""
    post = build(words, **kwargs)
    path = post_path(Path(clip))
    path.write_text(json.dumps(post.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Shorts & Reels caption for %s (%s): %s", Path(clip).name, post.source, post.title)
    return path


def read_for_clip(clip: Path) -> dict[str, Any] | None:
    """The clip's saved caption ({"title", "description", "hashtags"}), or None."""
    try:
        data = json.loads(post_path(Path(clip)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("title") or not data.get("description"):
        return None
    tags = [clean_tag(t) for t in data.get("hashtags") or []]
    return {"title": str(data["title"])[:TITLE_MAX + 10], "description": str(data["description"])[:600],
            "hashtags": [t for t in tags if t][:8]}
