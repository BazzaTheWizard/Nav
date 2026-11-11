"""Round 45: a ready-to-post LinkedIn caption next to every clip (<clip>_linkedin_post.txt).

The shape B2B marketing teams post: one punchy hook line, three concise takeaways in the
host's own words, and one closing question that invites comments. The AI drafts it from
the clip's transcript; nothing it writes is trusted: every hook and takeaway must be the
speaker's own words (most of its content words said in the clip), every number in it must
have been said, and every name must be in the clip or the video's title. A line that fails
is replaced by the speaker's own sentence. With no AI key the whole post comes from the
transcript. No emoji, no hashtags, no invented claims. LINKEDIN_POSTS=0 in .env turns it off.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

HOOK_MAX = 110
TAKEAWAY_MAX = 120
QUESTION_MAX = 120
GROUNDED_SHARE = 0.7  # this share of a line's content words must be the speaker's own
BULLET = "•"
SUFFIX = "_linkedin_post.txt"

_STOP = set("""a an the and or but so of to in on at for with from by as is are was were be been being it its
this that these those there here they them their we our you your i me my he she his her not no yes do does
did done have has had will would can could should just really very also than then into about over more most
some any all what which who whom when where why how if because while like get got going gonna kind sort thing
things lot lots way one ones out up down off again only even still much many""".split())
_FILLER = re.compile(r"^(?:(?:um+|uh+|so|and|but|like|yeah|okay|ok|well|you know|i mean|right)[,.]?\s+)+", re.I)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]+")
_NUMBER = re.compile(r"\d[\d,.]*")

SYSTEM_PROMPT = """You write LinkedIn post copy for a short vertical video clip cut from a B2B
podcast, webinar or executive talk. The company posts it on LinkedIn with the clip.

Return one JSON object: {"hook": "...", "takeaways": ["...", "...", "..."], "question": "..."}

Rules:
- Write in the speaker's own voice and words. Reuse their phrasing from the transcript
  (first person if they speak in first person). Light trimming only: drop filler ("um",
  "you know", "like"), fix grammar, shorten.
- Never add a fact, number, name, company or claim that isn't in the transcript.
- "hook": one line, at most 110 characters: the most surprising, contrarian or useful
  claim in the clip, as they put it. No clickbait ("You won't believe..."), no emoji.
- "takeaways": exactly 3, each at most 120 characters, one concrete idea each, in the
  order they come in the clip, in their words. Not a summary of the theme.
- "question": one closing question, at most 120 characters, that invites peers to share
  their own experience with this exact topic. Not "Agree?" or "Thoughts?".
- No hashtags, no emoji, no quotation marks around lines, no markdown.
Return only the JSON."""


@dataclass
class Post:
    hook: str
    takeaways: list[str]
    question: str
    source: str = "transcript"  # "ai", "ai+transcript" or "transcript"

    def text(self) -> str:
        bullets = "\n".join(f"{BULLET} {line}" for line in self.takeaways)
        return f"{self.hook}\n\n{bullets}\n\n{self.question}\n"


def _content(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z'’-]{2,}", text.lower()) if w not in _STOP]


def _stem(word: str) -> str:
    word = word.replace("’", "'")
    for end in ("'s", "ing", "ed", "es", "s", "ly"):
        if word.endswith(end) and len(word) - len(end) >= 3:
            word = word[: -len(end)]
            break
    return word[:-1] if word.endswith("e") and len(word) > 3 else word  # "hire" = "hiring" = "hired"


def _numbers(text: str) -> set[str]:
    return {n.rstrip(".,").replace(",", "") for n in _NUMBER.findall(text)}


def grounded(line: str, transcript: str, names_ok: str = "") -> bool:
    """True when the line is the speaker's own: GROUNDED_SHARE of its content words said in
    the clip, every number said, every capitalised name in the clip or the title."""
    said = {_stem(w) for w in _content(transcript)}
    words = _content(line)
    if not words:
        return False
    if sum(_stem(w) in said for w in words) / len(words) < GROUNDED_SHARE:
        return False
    if not _numbers(line) <= _numbers(transcript):
        return False
    allowed = (transcript + " " + names_ok).lower()
    tokens = line.split()
    for k, token in enumerate(tokens):
        name = re.sub(r"^[^A-Za-z]+|[^A-Za-z]+$", "", token)
        starts_sentence = k == 0 or tokens[k - 1].endswith((".", "!", "?", ":"))
        if len(name) >= 3 and name[0].isupper() and not starts_sentence and name.lower() not in allowed \
                and name not in {"AI", "CEO", "CFO", "CTO", "VC", "VCs", "SaaS", "B2B"}:
            return False
    return True


def _tidy(line: Any, limit: int) -> str:
    text = _EMOJI.sub("", str(line or ""))
    text = re.sub(r"#\w+", "", text)
    text = " ".join(text.split()).strip(" \"'“”‘’-–—•*")
    text = _FILLER.sub("", text)
    if len(text) > limit:
        cut = text[:limit]
        for mark in (". ", "; ", ", ", " — ", " - "):
            if mark in cut[limit // 2:]:
                cut = cut[: cut.rindex(mark)]
                break
        else:
            cut = cut.rsplit(" ", 1)[0]
        text = cut.rstrip(" ,;:-–—")
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text


def _sentences(words: list[Any]) -> list[str]:
    from core.edl import sentences

    return [" ".join(text.split()) for _a, _b, text in sentences(words)]


def from_transcript(words: list[Any], title: str = "") -> Post:
    """The post from the speaker's own sentences: the strongest as the hook, the next three
    strongest (in clip order) as takeaways."""
    from core import captions as semantic

    candidates = []
    for k, sentence in enumerate(_sentences(words)):
        clean = _tidy(sentence, 400)
        n = len(clean.split())
        if n < 5:
            continue
        try:
            kinds = semantic.keyword_kinds(clean.split())
        except Exception:  # noqa: BLE001
            kinds = []
        score = (3 if semantic.FIGURE in kinds else 0) + (1 if semantic.ENTITY in kinds else 0) \
            + (1 if semantic.POWER in kinds else 0) + (2 if 8 <= n <= 24 else 0) - (2 if clean.endswith("?") else 0)
        candidates.append((score, k, clean))
    ranked = sorted(candidates, key=lambda c: (-c[0], c[1]))
    if not ranked:
        hook = _tidy(title, HOOK_MAX) or "One moment from our latest episode."
        return Post(hook, [], question(title), "transcript")
    hook = _tidy(ranked[0][2], HOOK_MAX)
    rest = sorted(ranked[1:4], key=lambda c: c[1])
    takeaways = [_tidy(c[2], TAKEAWAY_MAX) for c in rest]
    return Post(hook, takeaways, question(title), "transcript")


def question(title: str = "", theme: str = "") -> str:
    """The transcript version's closing question; with the prospect's theme of the month, about it."""
    theme = " ".join(str(theme or "").split())[:60]
    return f"How is your team approaching {theme} right now?" if theme else "How is your team approaching this right now?"


def build(words: list[Any], *, title: str = "", speaker: str = "", video_title: str = "", theme: str = "",
          ask: Callable[[str, str], str | None] | None = None) -> Post:
    """The clip's post: the AI's draft, line by line checked against the transcript."""
    transcript = " ".join(str(w.text) for w in words)
    fallback = from_transcript(words, title)
    if theme:  # Round 46: the closing question ties into what they're covering this month
        fallback.question = question(title, theme)
    names_ok = f"{video_title} {speaker} {title}"
    if ask is None:
        from core.graphics_director import ask_ai as ask
    try:
        prompt = (f"Speaker: {speaker or 'unknown'}\nVideo: {video_title or 'unknown'}\n"
                  f"Clip headline: {title}\n"
                  + (f"The company's content theme this month: {theme}. If the clip relates to it, aim the "
                     f"closing question at it.\n" if theme else "")
                  + f"Transcript of the clip:\n{transcript}")
        raw = ask(prompt, SYSTEM_PROMPT)
    except Exception as exc:  # noqa: BLE001 - the transcript version is next
        logger.warning("LinkedIn post AI failed: %s", exc)
        raw = None
    if not raw:
        return fallback
    try:
        from core.agent_brain import _parse_json_response

        answer = _parse_json_response(raw)
    except Exception:  # noqa: BLE001
        return fallback
    hook = _tidy(answer.get("hook"), HOOK_MAX)
    replaced = 0
    if not grounded(hook, transcript, names_ok):
        hook, replaced = fallback.hook, replaced + 1
    takeaways: list[str] = []

    def key(text: str) -> str:
        return re.sub(r"\W", "", text.lower())

    spare = [t for t in fallback.takeaways if key(t) != key(hook)]
    for line in (answer.get("takeaways") or [])[:3]:
        line = _tidy(line, TAKEAWAY_MAX)
        if grounded(line, transcript, names_ok) and key(line) != key(hook) and key(line) not in map(key, takeaways):
            takeaways.append(line)
            spare = [t for t in spare if key(t) != key(line)]
        elif spare:
            takeaways.append(spare.pop(0))
            replaced += 1
    while len(takeaways) < 3 and spare:
        takeaways.append(spare.pop(0))
    asked = _tidy(answer.get("question"), QUESTION_MAX)
    if not asked.endswith("?") or _numbers(asked) - _numbers(transcript) or len(asked.split()) < 5 \
            or asked.lower().rstrip("?") in {"agree", "thoughts", "what do you think"}:
        asked = fallback.question
    return Post(hook, takeaways, asked, "ai" if not replaced else "ai+transcript")


def post_path(clip: Path) -> Path:
    return clip.with_name(f"{clip.stem}{SUFFIX}")


def write_for_clip(clip: Path, words: list[Any], **kwargs: Any) -> Path:
    """Write <clip>_linkedin_post.txt next to the clip; returns its path."""
    post = build(words, **kwargs)
    path = post_path(Path(clip))
    path.write_text(post.text(), encoding="utf-8")
    logger.info("LinkedIn post for %s (%s): %s", Path(clip).name, post.source, post.hook)
    return path
