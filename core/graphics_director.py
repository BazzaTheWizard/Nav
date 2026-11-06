"""🎬 Graphics director (Round 28): decides each clip's graphics on its own.

The AI reads the clip's sentences and proposes at most a couple of graphics (the right
template, the exact numbers as said, the sentences they cover) plus the guest's name
card. Nothing it proposes is trusted: every graphic goes through core.card_checks
(numbers said, same scale, hedges kept, sums add up, hook and payoff clear, restraint
caps) and is dropped if it fails. Clip picking never sees any of this, so whether a
graphic fits never changes which clips are made.

Density with taste (Round 30; was the Round 28 restraint doctrine): every clean visual
the talk supports, up to 3 per clip (2 under 25 s): a chart for a real data moment, a
micro-stat in a corner for a number that backs the point, boxes for 2-3 named ideas. Never
a visual for a number that's an aside or a joke, never two on top of each other, and none
is still the right answer when nothing is said that a viewer would want to see. A chart
can carry a second "in context" layer: what its numbers mean, worked out from what was
said (one item as a multiple of another, a part of a whole) or a number the speaker
said elsewhere in the same video. Never a figure from outside the video.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.card_checks import HOOK_SECONDS, MIN_GAP, PAYOFF_SECONDS, check_edit
from core.edl import MODES, TEMPLATES, BrandKit, Card, ClipEdit, CardRow, sentences

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

MIN_STRENGTH = 6  # the AI's own 1-10 score; below this the moment isn't worth a graphic
NAME_CARD_SECONDS = 3.3
_GUESTS: dict[tuple[str, str], tuple[str, str]] = {}  # (title, channel) -> the guest's (name, role)
SHORT_CLIP_SECONDS = 25  # under this, two graphics at most (three otherwise)
EARLIER_FACTS = 12  # numbers said elsewhere in the video, offered for a chart's context layer
DIRECTED = tuple(t for t in TEMPLATES if t not in {"speaker_title", "candlestick_ema"})
_STOP = {"of", "the", "and", "at", "for", "in", "on", "a", "an", "to", "&"}

SYSTEM_PROMPT = """You are the motion-graphics editor for vertical (9:16) clips cut from
business podcasts and interviews (B2B, SaaS, venture, consulting, finance), working to
the standard of The New York Times and Bloomberg. For ONE clip you decide which moments
get a visual and you write its exact text. Visuals are what make these clips stand out:
viewers remember a number or a trend they SEE. Find every moment a clean, simple visual
would make the point land, and add it. Never decorate: no visual is better than filler.

RULES
1. Visual moments: a figure that carries the point (money, a percentage, growth, a
   multiple, a count of customers, months, people), a comparison, a trend, a timeline of
   2-4 dated points, a financial walk (start, gains, losses, end), parts of a whole, or a
   named framework of 2-3 kinds or steps. A supporting number (said in passing but real,
   like "we're at 40 million ARR" or "18 months") is a micro-stat in a corner.
   Never: casual or rhetorical numbers ("a million times", "two sizes too small",
   "six times seven"), numbers in a joke or an aside, an age or a year said in passing,
   a hypothetical unless the hypothetical is the point.
2. Aim for 2-3 visual moments per clip when the talk has them, at most MAX_GRAPHICS, at
   most 2 of them charts. Spread them out (a chart, then a micro-stat later, say). An
   empty list is still right when nothing qualifies.
3. Exact words. Write every number as it was said, same number and scale
   ("17 trillion dollars" -> "$17T"; "about 11 or 12 trillion" -> "~$11–12T"). If the
   speaker hedges (about, around, roughly, or, between), keep it with "~" or a range.
   Labels, lines and descriptions are the speaker's own words, as short as possible.
   A change from one thing to another can be "C → A" or "$2M → $8M" (both ends said).
4. Timing: a graphic covers the sentences where its numbers or words are said.
   It cannot start before HOOK_END s (the title is on screen) or run into the last
   2 seconds. Leave at least 2 s between two graphics. One exception: when the clip's
   hook itself is the data moment (a kinetic_range number or change said in the first
   HOOK_END s), give that sentence and the next; it comes in right after the title as a
   recap. A number or box graphic stays up only a few seconds, so never stretch one
   over sentences that don't need it.
5. Show, don't tell. When the speaker describes a relationship, show its shape instead of
   printing a number they just said: a fall and a recovery (a crash, a dip, a drawdown,
   "and then it came back") -> drawdown_curve; a comparison -> bars; dated points ->
   timeline; growth over time -> trend_line; a whole and its parts -> share_split or
   sankey_flow; a start, gains and losses -> arr_bridge. kinetic_range is only for one
   standalone number with nothing to show it against; buckets for 2-3 named ideas.

TEMPLATES (items: label, value, detail, parent, dashed, highlight)
- kinetic_range: over the speaker, big. The clip's headline number. 1 item: value = one
  number, range or change ("96 hrs", "~$11–12T", "C → A"); label = optional line under
  it (≤44 chars, said).
- stat_pill: a micro-stat, small, in a top corner. For a supporting number. 1 item:
  value (≤10 chars, as said), label = 2-4 of the speaker's words (≤22 chars). Or a short
  trend: 2-4 items oldest first, label = when as said ("2022", "Q3"), value = the number.
- buckets: over the speaker. 2-3 items: label = a box (≤20 chars, said in this order);
  dashed = true for an expectation or projection next to what actually happened.
- timeline: chart. 2-4 items in the order said: value = when as said ("2008",
  "20 yrs ago"), detail = what happened (≤30 chars, speaker's words).
- metric_bars: chart. 2-4 numbers in one unit: label, value.
- ranked_bars: chart. 2-5 numbers in one unit, largest first: label, value.
- diverging_bar: chart. Exactly 2 numbers that pull against each other: label, value.
- share_split: chart. 3-5 shares of one whole that add to about 100%: label, value.
- trend_line: chart. 2-6 values over time, oldest first: label = the time, value.
- arr_bridge: chart. First item the start, last the end, + or − steps between
  ("+$3.2M", "−$0.4M"); it must add up.
- sankey_flow: chart. One starting item, then items that flow out of it (parent = the
  label they flow from, at most 2 levels); each split adds up to its parent.
- cac_payback: chart. 2 items: the cost to win a customer, the months to earn it back.
- drawdown_curve: chart. A fall and the recovery. Item 1: value = the fall as said, as a
  negative percentage ("−20%"), label = "" or the speaker's few words for it. Item 2:
  label = the recovery in the speaker's words ("they go back up", ≤26 chars), value = a
  number only if one was said ("4 yrs"), else "".
Set highlight = true on the one item the point is about. Chart headline: 2-4 words, or "".

IN CONTEXT (charts only, optional): a second, smaller layer under a number chart that
says what its numbers mean, so the viewer gets the bigger picture. Only when it's true
and adds something; never a figure from outside the video. Three kinds:
- {"kind": "ratio", "of": [i, j], "text": "..."}: item i as a multiple of item j (the
  app works out "2×"). E.g. two falls of 5% and 10%: "SoftBank's drop vs the Nikkei's".
- {"kind": "share", "of": [i, j], "text": "..."}: item i as a share of item j, the whole.
- {"kind": "earlier", "ref": "E3", "value": "$100B", "text": "..."}: a number the speaker
  said elsewhere in this video (from the list E1, E2... below) that puts the chart's
  numbers in a bigger frame (the whole market, the fund, the company); value as said.
The text (≤44 chars) names what is compared and has NO number or number word in it (the
figure is shown next to it). Write the text in plain words a viewer gets at a glance.

THE GUEST: if the video has a guest whose full name is in the video title or description,
always give {"name": ..., "role": ...}, with the role taken from the title or description
(≤42 chars, e.g. "Professor of Finance, MIT Sloan"). Every clip carries the guest's name
card (it's placed on the guest's own face), even when the host talks most in this clip.
Never the channel's own host. If the video has no named guest, null.

Return JSON only:
{"graphics": [{"template": "...", "first_sentence": 3, "last_sentence": 4, "strength": 1-10,
  "headline": "", "items": [{"label": "", "value": "", "detail": "", "parent": "",
  "dashed": false, "highlight": false}], "context": null or {...},
  "reason": "why this moment earns a visual"}],
 "speaker": {"name": "", "role": ""} or null}"""


@dataclass(frozen=True)
class VideoInfo:
    """What the director knows about the whole video (for the name card)."""

    title: str = ""
    channel: str = ""
    description: str = ""


@dataclass
class Direction:
    """The graphics for one clip and a plain-language note for each decision."""

    edit: ClipEdit
    notes: list[str] = field(default_factory=list)


def max_graphics(duration: float) -> int:
    return 2 if duration < SHORT_CLIP_SECONDS else 3


def ask_ai(prompt: str, system_prompt: str) -> str | None:
    """The same Gemini (or OpenAI) setup as clip picking. None when there's no key."""
    from dotenv import load_dotenv

    from core.agent_brain import _request_gemini, _request_openai

    load_dotenv(_PROJECT_ROOT / ".env")
    gemini_key, openai_key = os.getenv("GEMINI_API_KEY"), os.getenv("OPENAI_API_KEY")
    if gemini_key:
        return _request_gemini(gemini_key, prompt, system_prompt)
    if openai_key:
        return _request_openai(openai_key, prompt, system_prompt)
    return None


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def build_prompt(spans: list[tuple[float, float, str]], clip_start: float, duration: float, hook: float,
                 info: VideoInfo, earlier: list[tuple[float, str]] | None = None) -> str:
    lines = [f"S{k} [{s - clip_start:.1f}-{e - clip_start:.1f}] {text}" for k, (s, e, text) in enumerate(spans, 1)]
    facts = [f"E{k} [{_clock(at)} in the video] {text}" for k, (at, text) in enumerate(earlier or [], 1)]
    return (
        f"Video title: {info.title}\nChannel: {info.channel}\n"
        f"Video description (first part): {info.description[:1200]}\n\n"
        f"Clip length: {duration:.1f} s. No graphic before {hook:.1f} s or after {duration - PAYOFF_SECONDS:.1f} s. "
        f"At most {max_graphics(duration)} graphics.\n\nSentences (seconds from the clip's start):\n" + "\n".join(lines)
        + ("\n\nNumbers said elsewhere in this video (for a chart's \"earlier\" context only):\n" + "\n".join(facts)
           if facts else "")
    )


def earlier_facts(video_words: list[Any] | None, clip_start: float, clip_end: float,
                  clip_words: list[Any]) -> list[tuple[float, str]]:
    """Sentences outside the clip that carry a number (when, text), the ones closest in
    subject to the clip first (shared words), at most EARLIER_FACTS: what a chart's
    context layer may quote."""
    from core.card_checks import spoken_numbers

    if not video_words:
        return []
    import bisect

    topic = {w for w in _words(" ".join(str(x.text) for x in clip_words)) if len(w) >= 5}
    found: list[tuple[int, float, float, str]] = []
    starts = [float(w.start) for w in video_words]
    for start, end, text in sentences([w for w in video_words if not clip_start - 1 <= float(w.start) < clip_end + 1]):
        inside = video_words[bisect.bisect_left(starts, start):bisect.bisect_right(starts, end)]
        if not spoken_numbers(inside) or len(text) > 240:
            continue
        shared = len(topic & {w for w in _words(text) if len(w) >= 5})
        found.append((-shared, abs(start - clip_start), start, text))
    found.sort()
    return [(start, text) for _shared, _distance, start, text in found[:EARLIER_FACTS]]


def _system_prompt(duration: float, hook: float) -> str:
    return SYSTEM_PROMPT.replace("MAX_GRAPHICS", str(max_graphics(duration))).replace("HOOK_END", f"{hook:.1f}")


def _rows(template: str, items: Any) -> list[CardRow]:
    rows: list[CardRow] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        rows.append(CardRow(
            label=str(item.get("label") or "").strip(),
            value=str(item.get("value") or "").strip(),
            highlight=bool(item.get("highlight")),
            detail=str(item.get("detail") or "").strip(),
            dashed=bool(item.get("dashed")) and template == "buckets",
            parent=str(item.get("parent") or "").strip(),
        ))
    return rows


def proposals(answer: dict[str, Any], spans: list[tuple[float, float, str]],
              earlier: list[tuple[float, str]] | None = None) -> list[tuple[int, Card, str]]:
    """(strength, card, reason) for each usable proposal, strongest first."""
    found: list[tuple[int, Card, str]] = []
    for entry in answer.get("graphics") or []:
        if not isinstance(entry, dict) or entry.get("template") not in DIRECTED:
            continue
        try:
            first, last = int(entry["first_sentence"]), int(entry["last_sentence"])
            strength = int(entry.get("strength") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if not 1 <= first <= last <= len(spans):
            continue
        template = str(entry["template"])
        card = Card(template, start=spans[first - 1][0], end=spans[last - 1][1] + 0.15,
                    headline=str(entry.get("headline") or "").strip() if MODES[template] == "split" else "",
                    rows=_rows(template, entry.get("items")),
                    context=_context(entry.get("context"), earlier) if MODES[template] == "split" else {})
        found.append((strength, card, str(entry.get("reason") or "").strip()))
    return sorted(found, key=lambda item: -item[0])


def _context(raw: Any, earlier: list[tuple[float, str]] | None) -> dict[str, Any]:
    """A proposed context layer, in the shape core.card_checks checks; an "earlier" one
    gets the sentence it quotes (from the transcript, never from the AI) and its time."""
    if not isinstance(raw, dict) or raw.get("kind") not in {"ratio", "share", "earlier"}:
        return {}
    context: dict[str, Any] = {"kind": raw["kind"], "text": str(raw.get("text") or "").strip()}
    if raw["kind"] == "earlier":
        match = re.fullmatch(r"E(\d+)", str(raw.get("ref") or "").strip(), re.IGNORECASE)
        if not match or not earlier or not 1 <= int(match.group(1)) <= len(earlier):
            return {}
        at, said = earlier[int(match.group(1)) - 1]
        context.update(value=str(raw.get("value") or "").strip(), said=said, at=round(at, 1))
    else:
        try:
            context["of"] = [int(n) for n in raw.get("of") or []][:2]
        except (TypeError, ValueError):
            return {}
    return context


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def grounded_name(speaker: Any, info: VideoInfo) -> tuple[str, str] | None:
    """The name card's (name, role) if both come from the video's own title or
    description (never invented, never the channel itself); None otherwise."""
    if not isinstance(speaker, dict):
        return None
    name, role = str(speaker.get("name") or "").strip(), str(speaker.get("role") or "").strip()
    source = f"{info.title}\n{info.description}".lower()
    name_words = _words(name)
    if len(name_words) < 2 or not all(re.search(rf"\b{re.escape(w)}\b", source) for w in name_words):
        return None
    if " ".join(name_words) == " ".join(_words(info.channel)):
        return None
    role_words = [w for w in _words(role) if w not in _STOP]
    if role_words and sum(w in source or w.rstrip("s") in source for w in role_words) / len(role_words) < 0.75:
        role = ""  # a role we can't find in the title or description isn't shown
    return name, role


def _add_name_card(direction: Direction, named: tuple[str, str], clip_words: list[Any], clip_start: float,
                   clip_end: float, hook: float, plan: Any, source: Any, strengths: dict[int, int]) -> None:
    """The guest's name card: in every clip, on the guest's own face (core.name_cards),
    never counted toward the graphics limit. If the data graphics leave no free moment
    while the guest is on screen, the weakest one makes room."""
    from core import name_cards

    edit = direction.edit
    name, role = named
    card: Card | None = None
    if plan is not None and source is not None:
        try:
            card, where = name_cards.name_card(source, plan, clip_start, clip_end, hook, edit.cards, name, role)
            for weakest in sorted(edit.cards, key=lambda c: strengths.get(id(c), 0)) if card is None else []:
                others = [c for c in edit.cards if c is not weakest]
                card, where_now = name_cards.name_card(source, plan, clip_start, clip_end, hook, others, name, role)
                if card is not None:
                    edit.cards.remove(weakest)
                    direction.notes.append(f"Dropped {weakest.template} to make room for the guest's name card.")
                    where = where_now
                    break
        except Exception as exc:  # noqa: BLE001 - the clip still renders without it
            logger.warning("Name card placement failed: %s", exc)
            card, where = None, f"placing it failed ({exc})"
    else:  # no crop plan (another layout): the standard spot, at the first free moment
        start = clip_start + hook + 0.2
        for other in sorted(edit.cards, key=lambda c: c.start):
            if start < other.end + MIN_GAP and other.start < start + NAME_CARD_SECONDS + MIN_GAP:
                start = other.end + MIN_GAP
        where = "in the standard spot"
        if start + NAME_CARD_SECONDS <= clip_end - PAYOFF_SECONDS:
            card = Card("speaker_title", start=start, end=start + NAME_CARD_SECONDS, rows=[CardRow(name, detail=role)])
    if card is None:
        direction.notes.append(f"No name card for {name}: {where}.")
        return
    result = check_edit(ClipEdit(clip_start, clip_end, cards=edit.cards + [card]), clip_words, hook)[-1]
    if not result.ok:
        direction.notes.append(f"No name card for {name}: {result.errors[0]}")
        return
    edit.cards.append(card)
    edit.cards.sort(key=lambda c: c.start)
    direction.notes.append(f"Added the name card ({name}) at {_clock(card.start - clip_start)}, {where}.")


def _stat_spot(source: Any, plan: Any, clip_start: float, clip_end: float, card: Card) -> str:
    """A micro-stat's corner: clear of the face when the clip is a face crop."""
    from core import name_cards

    if plan is None or source is None:
        return name_cards.STAT_DEFAULT
    return name_cards.stat_place(source, plan, clip_start, clip_end, card.start, card.end)


def direct(
    words: list[Any],
    clip_start: float,
    clip_end: float,
    *,
    info: VideoInfo,
    hook_seconds: float,
    plan: Any = None,
    brand: BrandKit | None = None,
    ask: Callable[[str, str], str | None] | None = None,
    source: str | Path | None = None,
    video_words: list[Any] | None = None,
) -> Direction:
    """The clip's graphics, every one checked. ``words``: the clip's words (source
    times). ``plan`` and ``source``: the crop plan and the video, to find the guest's own
    face for the name card (and a clear corner for a micro-stat). ``video_words``: the
    whole video's words, for a chart's context layer (numbers said elsewhere in it).
    Never raises: anything that goes wrong means no graphics, with a note."""
    from core.agent_brain import _parse_json_response
    from core.service_pause import ServicePause

    edit = ClipEdit(start=clip_start, end=clip_end, cards=[], brand=brand or BrandKit())
    direction = Direction(edit)
    duration = clip_end - clip_start
    hook = max(HOOK_SECONDS, hook_seconds)
    clip_words = [w for w in words if clip_start - 0.05 <= float(w.start) < clip_end]
    spans = sentences(clip_words)
    if duration < hook + PAYOFF_SECONDS + 3 or not spans:
        direction.notes.append("No graphics: the clip is too short.")
        return direction
    try:
        earlier = earlier_facts(video_words, clip_start, clip_end, clip_words)
        raw = (ask or ask_ai)(build_prompt(spans, clip_start, duration, hook, info, earlier), _system_prompt(duration, hook))
        if raw is None:
            direction.notes.append("No graphics: no AI key (GEMINI_API_KEY or OPENAI_API_KEY).")
            return direction
        answer = _parse_json_response(raw)
    except ServicePause as exc:
        direction.notes.append(f"No graphics: the AI limit is used up for now ({exc}).")
        return direction
    except Exception as exc:  # noqa: BLE001 - graphics are optional; the clip still renders
        logger.warning("Graphics director failed: %s", exc)
        direction.notes.append(f"No graphics: the AI answer was unusable ({exc}).")
        return direction

    limit = max_graphics(duration)
    strengths: dict[int, int] = {}
    for strength, card, reason in proposals(answer, spans, earlier):
        what = f"{card.template} ({', '.join(r.value or r.label for r in card.rows)[:40]})"
        if strength < MIN_STRENGTH:
            direction.notes.append(f"Skipped {what}: not strong enough ({strength}/10).")
            continue
        if len(edit.cards) >= limit:
            direction.notes.append(f"Skipped {what}: {limit} graphic{'s' if limit > 1 else ''} is the most for this clip.")
            continue
        if card.context:  # time to read the layer under the chart (the checks drop it if there isn't)
            card.end = max(card.end, min(card.end + 2.0, clip_end - PAYOFF_SECONDS - 0.05))
        result = check_edit(ClipEdit(clip_start, clip_end, cards=edit.cards + [card]), clip_words, hook)[-1]
        if not result.ok and any("hedged" in e for e in result.errors):
            for row in card.rows:  # the one safe repair: show the hedge the speaker used
                if row.value and not row.value.startswith("~"):
                    row.value = "~" + row.value
            result = check_edit(ClipEdit(clip_start, clip_end, cards=edit.cards + [card]), clip_words, hook)[-1]
        if result.ok:
            if card.template == "stat_pill":
                card.place = _stat_spot(source, plan, clip_start, clip_end, card)
            edit.cards.append(card)
            edit.cards.sort(key=lambda c: c.start)
            strengths[id(card)] = strength
            direction.notes.append(f"Added {what} at {_clock(card.start - clip_start)}: {reason}")
            if card.context:
                direction.notes.append(f"...with an 'in context' layer: {card.context.get('value')} "
                                       f"{card.context.get('text')}")
            for warning in result.warnings:
                if warning.startswith("Context layer left out"):
                    direction.notes.append(f"...{warning}")
        else:
            direction.notes.append(f"Dropped {what}: {result.errors[0]}")

    named = grounded_name(answer.get("speaker"), info)
    if named:
        _GUESTS[(info.title, info.channel)] = named
    else:  # the same guest as the video's other clips
        named = _GUESTS.get((info.title, info.channel))
    if named:
        _add_name_card(direction, named, clip_words, clip_start, clip_end, hook, plan, source, strengths)
    # The renderer checks again; anything that wouldn't pass the second time is left out now.
    final = check_edit(edit, clip_words, hook)
    for card, result in list(zip(edit.cards, final)):
        if not result.ok:
            edit.cards.remove(card)
            direction.notes.append(f"Dropped {card.template}: {result.errors[0]}")
    if not edit.cards:
        direction.notes.append("No graphics: nothing in this clip needs one.")
    return direction
