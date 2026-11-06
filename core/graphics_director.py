"""🎬 Graphics director (Round 28): decides each clip's graphics on its own.

The AI reads the clip's sentences and proposes at most a couple of graphics (the right
template, the exact numbers as said, the sentences they cover) plus the guest's name
card. Nothing it proposes is trusted: every graphic goes through core.card_checks
(numbers said, same scale, hedges kept, sums add up, hook and payoff clear, restraint
caps) and is dropped if it fails. Clip picking never sees any of this, so whether a
graphic fits never changes which clips are made.

Restraint doctrine: no graphic unless a real data moment is spoken (a figure that
carries the point, a timeline, a financial walk, a breakdown, a 2-3 part framework);
one graphic for a clip under 30 s and at most two otherwise; one strong graphic beats
two average ones; none is a fine answer.
"""
from __future__ import annotations

import logging
import os
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.card_checks import HOOK_SECONDS, MIN_GAP, PAYOFF_SECONDS, check_edit
from core.edl import MODES, TEMPLATES, BrandKit, Card, ClipEdit, CardRow, sentences

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

MIN_STRENGTH = 7  # the AI's own 1-10 score; below this the moment isn't worth a graphic
NAME_CARD_SECONDS = 3.3
NAME_CARD_LATEST = 0.6  # share of the clip: a name card later than this is skipped
SHORT_CLIP_SECONDS = 30  # under this, one graphic at most
DIRECTED = tuple(t for t in TEMPLATES if t not in {"speaker_title", "candlestick_ema"})
_STOP = {"of", "the", "and", "at", "for", "in", "on", "a", "an", "to", "&"}

SYSTEM_PROMPT = """You are the motion-graphics editor for vertical (9:16) clips cut from
podcasts and interviews, working to the standard of The New York Times and Bloomberg.
For ONE clip you decide which moments get a graphic and you write its exact text.
Restraint is the job: most clips get one graphic or none.

RULES
1. Only real data moments: a figure that carries the point (money, a percentage, growth,
   a count), a timeline of 2-4 dated points, a financial walk (start, gains, losses, end),
   parts of a whole, or a named framework of 2-3 kinds or steps.
   Never: casual or rhetorical numbers ("a million times", "two sizes too small",
   "six times seven"), numbers in a joke or an aside, an age or a year said in passing,
   a hypothetical unless the hypothetical is the point.
2. At most MAX_GRAPHICS graphics. One strong graphic beats two average ones. An empty
   list is a good answer when nothing qualifies.
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
5. Use the lightest form that works: one number -> kinetic_range; 2-3 concepts ->
   buckets; a chart only when 3+ related numbers are said close together (a timeline
   needs 2+ dated points).

TEMPLATES (items: label, value, detail, parent, dashed, highlight)
- kinetic_range: over the speaker. 1 item: value = one number, range or change
  ("96 hrs", "~$11–12T", "C → A"); label = optional line under it (≤44 chars, said).
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
Set highlight = true on the one item the point is about. Chart headline: 2-4 words, or "".

NAME CARD: if the clip's main speaker is a guest whose full name is in the video title or
description, give {"name": ..., "role": ...} with the role taken from the title or
description (≤42 chars, e.g. "Professor of Finance, MIT Sloan"). Never the channel's own
host. If you are not sure who is speaking, null.

Return JSON only:
{"graphics": [{"template": "...", "first_sentence": 3, "last_sentence": 4, "strength": 1-10,
  "headline": "", "items": [{"label": "", "value": "", "detail": "", "parent": "",
  "dashed": false, "highlight": false}], "reason": "why this moment earns a graphic"}],
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
    return 1 if duration < SHORT_CLIP_SECONDS else 2


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
                 info: VideoInfo) -> str:
    lines = [f"S{k} [{s - clip_start:.1f}-{e - clip_start:.1f}] {text}" for k, (s, e, text) in enumerate(spans, 1)]
    return (
        f"Video title: {info.title}\nChannel: {info.channel}\n"
        f"Video description (first part): {info.description[:1200]}\n\n"
        f"Clip length: {duration:.1f} s. No graphic before {hook:.1f} s or after {duration - PAYOFF_SECONDS:.1f} s. "
        f"At most {max_graphics(duration)} graphics.\n\nSentences (seconds from the clip's start):\n" + "\n".join(lines)
    )


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


def proposals(answer: dict[str, Any], spans: list[tuple[float, float, str]]) -> list[tuple[int, Card, str]]:
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
                    rows=_rows(template, entry.get("items")))
        found.append((strength, card, str(entry.get("reason") or "").strip()))
    return sorted(found, key=lambda item: -item[0])


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


def _main_speaker_shots(plan: Any, clip_start: float, clip_end: float) -> list[tuple[float, float]]:
    """(start, end) of the single-person shots of whoever is on screen most (the guest's
    camera, usually), clip times in source seconds."""
    if plan is None:
        return [(clip_start, clip_end)]
    singles = [s for s in plan.shots if s.has_face and not s.group and s.end > clip_start and s.start < clip_end]
    if not singles:
        return []
    tolerance = plan.frame_width * 0.12
    groups: list[list[Any]] = []
    for shot in singles:
        for group in groups:
            if abs(statistics.median(s.x for s in group) - shot.x) <= tolerance:
                group.append(shot)
                break
        else:
            groups.append([shot])
    main = max(groups, key=lambda g: sum(min(s.end, clip_end) - max(s.start, clip_start) for s in g))
    return sorted((max(s.start, clip_start), min(s.end, clip_end)) for s in main)


def _name_slot(shots: list[tuple[float, float]], taken: list[Card], clip_start: float, clip_end: float,
               hook: float) -> float | None:
    """Earliest start for the name card: inside one of the guest's shots, after the title,
    clear of the other graphics, and early enough to still introduce them."""
    earliest = clip_start + hook + 0.2
    latest = clip_start + NAME_CARD_LATEST * (clip_end - clip_start)
    for shot_start, shot_end in shots:
        t = max(earliest, shot_start + 0.3)
        while t + NAME_CARD_SECONDS <= min(shot_end - 0.2, clip_end - PAYOFF_SECONDS) and t <= latest:
            clash = next((c for c in taken if t < c.end + MIN_GAP and c.start < t + NAME_CARD_SECONDS + MIN_GAP), None)
            if clash is None:
                return round(t, 3)
            t = clash.end + MIN_GAP
    return None


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
) -> Direction:
    """The clip's graphics, every one checked. ``words``: the clip's words (source
    times). ``plan``: the crop plan (where the guest's own shots are, for the name card).
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
        raw = (ask or ask_ai)(build_prompt(spans, clip_start, duration, hook, info), _system_prompt(duration, hook))
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
    for strength, card, reason in proposals(answer, spans):
        what = f"{card.template} ({', '.join(r.value or r.label for r in card.rows)[:40]})"
        if strength < MIN_STRENGTH:
            direction.notes.append(f"Skipped {what}: not strong enough ({strength}/10).")
            continue
        if len(edit.cards) >= limit:
            direction.notes.append(f"Skipped {what}: {limit} graphic{'s' if limit > 1 else ''} is the most for this clip.")
            continue
        result = check_edit(ClipEdit(clip_start, clip_end, cards=edit.cards + [card]), clip_words, hook)[-1]
        if not result.ok and any("hedged" in e for e in result.errors):
            for row in card.rows:  # the one safe repair: show the hedge the speaker used
                if row.value and not row.value.startswith("~"):
                    row.value = "~" + row.value
            result = check_edit(ClipEdit(clip_start, clip_end, cards=edit.cards + [card]), clip_words, hook)[-1]
        if result.ok:
            edit.cards.append(card)
            edit.cards.sort(key=lambda c: c.start)
            direction.notes.append(f"Added {what} at {_clock(card.start - clip_start)}: {reason}")
        else:
            direction.notes.append(f"Dropped {what}: {result.errors[0]}")

    named = grounded_name(answer.get("speaker"), info)
    if named:
        slot = _name_slot(_main_speaker_shots(plan, clip_start, clip_end), edit.cards, clip_start, clip_end, hook)
        if slot is None:
            direction.notes.append(f"No name card for {named[0]}: no clear shot of them early in the clip.")
        else:
            card = Card("speaker_title", start=slot, end=slot + NAME_CARD_SECONDS,
                        rows=[CardRow(named[0], detail=named[1])])
            result = check_edit(ClipEdit(clip_start, clip_end, cards=edit.cards + [card]), clip_words, hook)[-1]
            if result.ok:
                edit.cards.append(card)
                edit.cards.sort(key=lambda c: c.start)
                direction.notes.append(f"Added the name card ({named[0]}) at {_clock(slot - clip_start)}.")
            else:
                direction.notes.append(f"No name card: {result.errors[0]}")
    # The renderer checks again; anything that wouldn't pass the second time is left out now.
    final = check_edit(edit, clip_words, hook)
    for card, result in list(zip(edit.cards, final)):
        if not result.ok:
            edit.cards.remove(card)
            direction.notes.append(f"Dropped {card.template}: {result.errors[0]}")
    if not edit.cards:
        direction.notes.append("No graphics: nothing in this clip needs one.")
    return direction
