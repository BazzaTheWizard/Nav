"""🎬 Graphics director (Round 28): decides each clip's graphics on its own.

The AI reads the clip's sentences and proposes its visuals (the right template, the exact
numbers as said, the sentences they cover) and names the people who speak. Nothing it
proposes is trusted: every graphic goes through core.card_checks (numbers said, same
scale, hedges kept, sums add up, hook and payoff clear, restraint caps) and is dropped if
it fails. Clip picking never sees any of this, so whether a graphic fits never changes
which clips are made.

Round 32: every clip gets 1-2 Bento visuals, at least BENTO_SPACING apart.
- Tier 1 (quantitative) first: a chart or a number whenever a real metric is said (a
  multiple, a share, a market size, a split), with the checks' semantic number sense
  ("14 times safer than a human driver" is Human driver 1x vs 14x).
- Tier 2 (qualitative) when the talk has no chartable metric: the speaker's contrast or
  framework as a comparison card, 2-3 boxes, a step sequence or a timeline. The AI always
  names one in "fallback".
- Never a chart on a number said in passing (core.card_checks.incidental_card).
- A visual that fails a check is repaired before it's given up on (the hedge shown, the
  card moved onto the sentences where its words are said, moved clear of the title, the
  ending or a name card). If nothing survives: a second, qualitative-only AI request, and
  then a visual built from the transcript itself, so no clip ships without one.
Name cards are planned first and separately (core.name_cards.plan_name_cards): one per
speaker whose face is certain, on their own shot while they talk (Round 33), never
counted as a Bento visual.
"""
from __future__ import annotations

import copy
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.card_checks import (HOOK_SECONDS, MAX_GRAPHICS, MIN_CARD_SECONDS, MIN_GAP, MIN_OVERLAY_SECONDS,
                              PAYOFF_SECONDS, chartable_numbers, check_edit, incidental_card, parse_value,
                              same_amount, spoken_numbers)
from core import niche_director
from core.edl import MODES, TEMPLATES, BrandKit, Card, ClipEdit, CardRow, sentences

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

MIN_STRENGTH = 6  # the AI's own 1-10 score; below this a Tier 1 moment isn't worth a graphic
FALLBACK_STRENGTH = 3  # a qualitative card (Tier 2) is the fallback: it needs less
NAME_CARD_SECONDS = 3.3
MIN_BENTO = 1  # Round 32: never a clip without a Bento visual
BENTO_SPACING = 12.0  # seconds from one Bento visual's start to the next one's (Round 32)
_GUESTS: dict[tuple[str, str], list["Speaker"]] = {}  # (title, channel) -> the video's identified speakers
_GUESTS_LOCK = threading.Lock()  # Make clips renders two clips at once
SHORT_CLIP_SECONDS = 25  # under this, one Bento visual (two otherwise)
EARLIER_FACTS = 12  # numbers said elsewhere in the video, offered for a chart's context layer
DIRECTED = tuple(t for t in TEMPLATES if t not in {"speaker_title", "candlestick_ema"})
QUALITATIVE = ("comparison_card", "buckets", "step_sequence", "timeline",  # Tier 2
               "geo_map_card", "flow_sankey_card", "framework_matrix_card", "headline_card")  # Round 36
_STOP = {"of", "the", "and", "at", "for", "in", "on", "a", "an", "to", "&"}

SYSTEM_PROMPT = """You are the motion-graphics editor for vertical (9:16) clips cut from
business podcasts and interviews (B2B, SaaS, venture, consulting, finance), working to
the standard of The New York Times and Bloomberg. For ONE clip you decide its visuals and
write their exact text, and you name the people who speak. Visuals are what make these
clips stand out: viewers remember a number or an idea they SEE.

RULES
1. Every clip gets 1 to MAX_GRAPHICS visuals, at least 12 s apart (start to start). Never zero.
2. Tier 1 first (quantitative): whenever a real metric is said, chart it: a multiple
   ("14 times safer"), a share ("20% of the S&P 500"), a market size ("$5 trillion"), a
   split ("half ... and half", "70/30"), growth ("doubled"), money, a count that carries
   the point. Write the same amount the way the chart needs it:
   - "14 times safer than a human driver" -> metric_bars [{"label": "Human driver", "value": "1x"},
     {"label": "Autonomous", "value": "14x", "highlight": true}] (the baseline nobody says, 1x,
     labelled with what the speaker compares against, in their words);
   - "20% of the S&P 500" -> share_split [{"label": "...", "value": "20%"}, {"label": "Rest of the S&P 500",
     "value": "80%"}] (the rest of a whole may be the computed remainder);
   - "half between Seed and B, half from C+" -> diverging_bar 50% / 50%; "a third" -> 33%;
     "a quarter" -> 25%; "70-30" -> 70% and 30%; "doubled" -> "2x"; "tripled" -> "3x";
     "tenfold" -> "10x"; "50 basis points" -> "50 bps"; "$5 trillion" -> "$5T";
   - "between 10 and 14 times" -> value "10–14x", or "14x" (or the middle, "12x") with the
     exact range in its label ("Waymo · 10–14x").
   Stages and relative times are fine time labels: "Seed–Series B", "Series C+", "Today", "+5 yrs".
3. Never chart a number said in passing: a year ("in 2018"), a time ago ("3 or 4 months
   ago"), an everyday count ("I had 2 questions"), a joke, an aside, a rhetorical number
   ("a million times"), a hypothetical unless the hypothetical is the point.
4. Tier 2 (qualitative) when the clip has no chartable metric: show the speaker's core
   framework or contrast: comparison_card (two sides: then vs now, them vs us, the myth vs
   what happened), buckets (2-3 named ideas), step_sequence (2-4 steps in order),
   timeline (2-4 moments), framework_matrix_card (a 2x2 or a flywheel), flow_sankey_card
   (a value chain), geo_map_card (countries in a trade, chip or conflict story) or
   headline_card (one quotable rule or thesis, as a tear-sheet). ALWAYS also give your best Tier 2 idea in "fallback", even when
   there are Tier 1 visuals: it's used only if they fail the checks.
5. Exact words. Write every number as it was said, same number and scale
   ("17 trillion dollars" -> "$17T"; "about 11 or 12 trillion" -> "~$11–12T"). If the
   speaker hedges (about, around, roughly, or, between), keep it with "~" or a range.
   Labels, lines and descriptions are the speaker's own words, as short as possible.
   A change from one thing to another can be "C → A" or "$2M → $8M" (both ends said).
6. Timing: a graphic covers the sentences where its numbers or words are said.
   It cannot start before HOOK_END s (the title is on screen) or run into the last
   2 seconds. One exception: when the clip's hook itself is the data moment (a
   kinetic_range number or change said in the first HOOK_END s), give that sentence and
   the next; it comes in right after the title as a recap. Every visual is on screen 4.5 to
   6 s: it builds in about a second, then holds still so the viewer can read it. So a
   moment needs about 6 s of clip after it starts (before the last 2 s); never pick one
   said right at the end.
7. Show, don't tell. When the speaker describes a relationship, show its shape instead of
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
  "20 yrs ago", "Seed–Series B", "Today"), detail = what happened (≤30 chars, speaker's words).
- metric_bars: chart. 2-4 numbers in one unit: label, value.
- ranked_bars: chart. 2-5 numbers in one unit, largest first: label, value.
- diverging_bar: chart. Exactly 2 numbers that pull against each other: label, value.
- share_split: chart. 2-5 shares of one whole that add to about 100%: label, value.
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
- comparison_card: chart (Tier 2). Exactly 2 items in the order said: label = the side's
  heading in the speaker's words (≤24 chars, e.g. "Venture firms, before"), detail = their
  line for it (≤44 chars), value = an optional short tag (≤14 chars: "Before", "Now", or a
  number as said); highlight = the side the point lands on.
- step_sequence: chart (Tier 2). 2-4 items in the order said: label = the step in the
  speaker's words (≤24 chars), detail = optional (≤30 chars).
- geo_map_card: chart (Tier 2). A map for geopolitics, trade, supply chains, sanctions,
  war: 1-3 items, each a country the speaker names (label = the country), in the order
  named; highlight the one the point lands on; value = a number only if said about that
  country. context (optional): {"arcs": [[1, 2]]} draws a moving trade/capital line from
  item 1 to item 2; {"stat": "gdp"|"population"|"inflation"|"gdp_growth"|"debt"|"exports"}
  asks the app to show that World Bank figure next to each country (never write it yourself).
- prediction_gauge_card: chart (Tier 1). Odds of an outcome (elections, a rate cut, a
  deal closing): item 1 = the side the speaker's odds are for (label ≤14 chars, "Cut",
  "Yes", a name), value = its odds as said ("70%"); item 2 optional = the other side,
  value "" (the app shows the rest). Headline = the question (≤30 chars, "A December cut?").
  If the speaker asks about odds without a number, give items with value "" and context
  {"market": "<3-6 word Polymarket search>"}: the app fetches the live odds with the source.
- flow_sankey_card: chart (Tier 2, or Tier 1 with amounts). A value chain or where money
  goes: 2-5 items in order (label = a company or stage as said, ≤20 chars; detail = its
  role, ≤22 chars, e.g. "Lithography"; value = an amount only if said, one unit). E.g.
  "ASML makes the machines, TSMC the chips, NVIDIA designs them" -> ASML, TSMC, NVIDIA.
- framework_matrix_card: chart (Tier 2). The speaker's model of how things work: a 2x2
  (exactly 4 items in reading order: top-left, top-right, bottom-left, bottom-right;
  context {"layout": "matrix", "x": "<axis, ≤18 chars>", "y": "<axis>"}) or a flywheel /
  loop (3-4 items in order, context {"layout": "flywheel"}). label = the speaker's words
  (≤24 chars), detail optional (≤30 chars).
- headline_card: chart (Tier 2). A tear-sheet of one quotable line: 1 item, label = the
  speaker's exact sentence (≤110 chars), value = the 2-5 words of it to highlight (must be
  inside the quote), detail = the speaker's name if known. Optional context
  {"topic": "<law, event or private company named>"}: the app adds a one-line Wikipedia
  background with its source. Use it for a rule, a thesis or a prediction worth reading.
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

THE SPEAKERS: name every person who speaks in this video whose full name is in the video
title, the description or the channel name: the guest(s) AND the host. Each gets a name
card: {"name": ..., "role": ..., "kind": "guest" or "host"}, the role taken from the title
or description (≤42 chars, e.g. "Professor of Finance, MIT Sloan"; for the host "Host" or
"Host, <show>"). Never a company or a show as a name, never a name that isn't there.

Return JSON only:
{"graphics": [{"template": "...", "first_sentence": 3, "last_sentence": 4, "strength": 1-10,
  "headline": "", "items": [{"label": "", "value": "", "detail": "", "parent": "",
  "dashed": false, "highlight": false}], "context": null or {...},
  "reason": "why this moment earns a visual"}],
 "fallback": {one Tier 2 visual in the same shape} or null,
 "speakers": [{"name": "", "role": "", "kind": "guest"}]}"""

RETRY_PROMPT = """

SECOND LOOK: none of the visuals proposed for this clip passed the checks ({why}). Propose
ONE qualitative visual (comparison_card, buckets, step_sequence, timeline, framework_matrix_card,
flow_sankey_card, geo_map_card or headline_card) of the
speaker's main contrast or framework, in their exact words, timed to the sentences where
those words are said. Return {"graphics": [that one visual]}."""


@dataclass(frozen=True)
class VideoInfo:  # Round 46: + theme, the prospect's content-calendar angle (core.linkedin_harvester)
    """What the director knows about the whole video (for the name cards)."""

    title: str = ""
    channel: str = ""
    description: str = ""
    theme: str = ""  # Round 46: what the prospect is covering this month ("" when unknown)


@dataclass(frozen=True)
class Speaker:
    """A person who speaks in the video, named in its title, description or channel."""

    name: str
    role: str = ""
    kind: str = "guest"  # "guest" or "host"
    from_ai: bool = False  # named by the AI (grounded); else read from the title or description


@dataclass
class Direction:
    """The graphics for one clip and a plain-language note for each decision."""

    edit: ClipEdit
    notes: list[str] = field(default_factory=list)


def max_graphics(duration: float) -> int:
    """Bento visuals in a clip (Round 32: 1-2; name cards never count)."""
    return 1 if duration < SHORT_CLIP_SECONDS else MAX_GRAPHICS


def tier(card: Card) -> int:
    """1: a quantitative visual (a chart or a number); 2: a qualitative one."""
    return 2 if card.template in QUALITATIVE else 1


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
                 info: VideoInfo, earlier: list[tuple[float, str]] | None = None,
                 clip_words: list[Any] | None = None) -> str:
    lines = [f"S{k} [{s - clip_start:.1f}-{e - clip_start:.1f}] {text}" for k, (s, e, text) in enumerate(spans, 1)]
    facts = [f"E{k} [{_clock(at)} in the video] {text}" for k, (at, text) in enumerate(earlier or [], 1)]
    metrics = []
    for number in chartable_numbers(clip_words or []):  # Round 32: a hint, so a real metric gets its chart
        where = next((k for k, (s, e, _t) in enumerate(spans, 1) if s - 0.05 <= number.start <= e + 0.05), None)
        if where:
            metrics.append(f"S{where}: {' '.join(str(w.text) for w in clip_words[number.index:number.end + 3])}")
    hint = ("Chartable metrics said in this clip (Tier 1): " + "; ".join(metrics[:8]) if metrics else
            "No chartable metric is said in this clip: give a Tier 2 (qualitative) visual.")
    return (
        f"Video title: {info.title}\nChannel: {info.channel}\n"
        f"Video description (first part): {info.description[:1200]}\n\n"
        f"Clip length: {duration:.1f} s. No graphic before {hook:.1f} s or after {duration - PAYOFF_SECONDS:.1f} s. "
        f"At most {max_graphics(duration)} graphics, at least 1, starting at least {BENTO_SPACING:g} s apart.\n"
        f"{hint}\n\nSentences (seconds from the clip's start):\n" + "\n".join(lines)
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
        context = (niche_director.niche_context(template, entry.get("context")) if template in niche_director.NICHE_TEMPLATES
                   else _context(entry.get("context"), earlier) if MODES[template] == "split" else {})
        card = Card(template, start=spans[first - 1][0], end=spans[last - 1][1] + 0.15,
                    headline=str(entry.get("headline") or "").strip() if MODES[template] == "split" else "",
                    rows=_rows(template, entry.get("items")), context=context)
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


# Words that make a "name" a company, a show or a channel, never a person.
_ORG_WORDS = {"podcast", "podcasts", "show", "channel", "media", "studio", "studios", "tv", "news", "radio", "network",
              "group", "capital", "ventures", "partners", "labs", "inc", "llc", "official", "daily", "weekly", "club",
              "school", "academy", "university", "institute", "fund", "funds", "company", "co", "team", "live",
              "breakfast", "uncapped", "decile", "founders", "tips", "marketing", "after", "dark"}
_TITLE_WORDS = {"why", "how", "what", "when", "where", "who", "the", "this", "that", "your", "you", "watch", "before",
                "growth", "investing", "power", "law", "episode", "ep", "part", "full", "interview", "ai", "inside",
                "brutal", "advice", "lessons", "story", "my", "our", "is", "are", "on", "with", "and", "of", "a", "an"}
_HOST_ROLE_WORDS = {"host", "hosts", "cohost", "co", "hosted"}
_NAME = r"[A-Z][a-z]+(?:[-'’][A-Z]?[a-z]+)?(?:\s+[A-Z]\.)?\s+(?:[A-Z][a-z]+|[A-Z][a-z]*[A-Z][a-z]+)(?:[-'’][A-Z]?[a-z]+)?"


def _person_like(name: str) -> bool:
    words = _words(name)
    return 2 <= len(words) <= 4 and not set(words) & (_ORG_WORDS | _TITLE_WORDS)


def grounded_name(speaker: Any, info: VideoInfo) -> tuple[str, str] | None:
    """A name card's (name, role) if both come from the video's own title, description or
    channel name (never invented, never a company or a show); None otherwise. Round 32: the
    host too (a channel named after its host is that person: "Lex Fridman")."""
    if not isinstance(speaker, dict):
        return None
    name, role = str(speaker.get("name") or "").strip(), str(speaker.get("role") or "").strip()
    source = f"{info.title}\n{info.description}\n{info.channel}".lower()
    name_words = _words(name)
    if len(name_words) < 2 or not all(re.search(rf"\b{re.escape(w)}\b", source) for w in name_words):
        return None
    if not _person_like(name):
        return None  # "Founders Podcast", "Decile Group": the show or the company, not a person
    host = str(speaker.get("kind") or "").lower() == "host"
    role_words = [w for w in _words(role) if w not in _STOP and not (host and w in _HOST_ROLE_WORDS)]
    if role_words and sum(w in source or w.rstrip("s") in source for w in role_words) / len(role_words) < 0.75:
        role = ""  # a role we can't find in the title or description isn't shown
    if len(role) > 42:  # the card's limit: the part before a comma ("General Partner"), else none
        role = role.split(",")[0].strip() if len(role.split(",")[0].strip()) <= 42 else ""
    return name, role


def _clean_role(text: str) -> str:
    role = re.split(r"[|(\[,;]| - | – | — |\.\s|\.$| to | who | and ", text, maxsplit=1)[0].strip(" ,.:;–—-")
    return role[:1].upper() + role[1:] if len(role) <= 42 else ""


def names_from_text(info: VideoInfo) -> list[Speaker]:
    """The people the video's own title and description name as its speakers (Round 32),
    for when the AI names nobody: "David George on Growth Investing", "... | Brian Folmer,
    FirstLook VC", "– Neil Patel & Eric Siu", "Goldman Sachs Chairman and CEO David
    Solomon on AI"; "Connor Sattely talks with Jonathan Bragdon, founder of Capacity
    Capital" (a host and a guest)."""
    found: list[Speaker] = []

    def add(name: str, role: str = "", kind: str = "guest") -> None:
        name = re.sub(r"\s+", " ", name).strip()
        if _person_like(name) and not any(set(_words(name)) & set(_words(s.name)) == set(_words(name)) for s in found):
            found.append(Speaker(name, _clean_role(role), kind))

    title, description = info.title or "", info.description or ""
    for match in re.finditer(rf"({_NAME})\s+(?:sits down with|talks (?:to|with)|speaks with|chats with|interviews|"
                             rf"is joined by|welcomes|hosts)\s+({_NAME})(?:,\s*([^.\n]{{3,60}}))?", description):
        add(match.group(1), "", "host")
        add(match.group(2), match.group(3) or "")
    for match in re.finditer(rf"(?:[Hh]osted by|[Hh]ost)\s+({_NAME})", description):
        add(match.group(1), "", "host")
    for match in re.finditer(rf"[Oo]ur guest(?: today)?(?: is)?\s+({_NAME})(?:,?\s+(?:is\s+)?(?:the\s+)?([^.\n]{{3,60}}))?",
                             description):
        add(match.group(1), match.group(2) or "")
    lead = re.match(rf"^({_NAME})(?:'s)?\s*(?:on|:|-|–|—|\||says|explains|talks|reveals|shares|breaks down)\s", title)
    if lead and not re.match(rf"^{_NAME}'s", title):
        add(lead.group(1))
    for match in re.finditer(rf"(?:CEO|Founder|Co-founder|Chairman|President|Partner|Professor|Dr\.?|GP)\s+"
                             rf"({_NAME})(?=\s+(?:on|says|explains|talks)\b|\s*[:|,–—-]|\s*$)", title):
        before = title[:match.start(1)].strip()
        add(match.group(1), before if 3 <= len(before) <= 42 else "")
    for match in re.finditer(rf"(?:\bwith|\bft\.?|\bfeat\.?|\bfeaturing|[|:–—])\s+({_NAME})(?:\s*(?:&|and)\s+({_NAME}))?"
                             rf"(?:,\s*([^|()\[\]]{{3,42}}))?", title):
        add(match.group(1), match.group(3) or "" if not match.group(2) else "")
        if match.group(2):
            add(match.group(2))
    return found


def video_speakers(answer: dict[str, Any] | None, info: VideoInfo) -> list[Speaker]:
    """Every identified speaker of the video: the ones the AI names (only if grounded in the
    title, description or channel), else the ones the title and description name
    themselves; remembered for the video's other clips (the AI may leave one out)."""
    raw = []
    if isinstance(answer, dict):
        listed = answer.get("speakers")
        raw = [s for s in listed if isinstance(s, dict)] if isinstance(listed, list) else []
        if isinstance(answer.get("speaker"), dict):  # the Round 28 shape: the guest only
            raw.append({**answer["speaker"], "kind": answer["speaker"].get("kind") or "guest"})
    named: list[Speaker] = []
    for entry in raw:
        grounded = grounded_name(entry, info)
        if grounded and not any(_words(grounded[0]) == _words(s.name) for s in named):
            kind = "host" if str(entry.get("kind") or "").lower() == "host" else "guest"
            named.append(Speaker(grounded[0], grounded[1], kind, from_ai=True))
    if not named:
        named = names_from_text(info)
    key = (info.title, info.channel)
    with _GUESTS_LOCK:
        known = list(_GUESTS.get(key, []))
        for speaker in named:
            same = next((k for k, s in enumerate(known) if _words(s.name) == _words(speaker.name)), None)
            if same is None:
                known.append(speaker)
            elif speaker.role and (not known[same].role or (speaker.from_ai and not known[same].from_ai)):
                known[same] = speaker  # the AI's grounded role wins over one read from the description
        if known:
            _GUESTS[key] = known
    return known


def _add_name_card(direction: Direction, named: tuple[str, str], clip_words: list[Any], clip_start: float,
                   clip_end: float, hook: float, plan: Any, source: Any, strengths: dict[int, int]) -> None:
    """Round 29's single-guest name card (kept for its checks; since Round 32 the director
    plans every speaker's card with _plan_names). The guest's name card: in every clip, on the guest's own face (core.name_cards),
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


def _what(card: Card) -> str:
    return f"{card.template} ({', '.join(r.value or r.label for r in card.rows)[:40]})"


_SALIENT_STOP = {
    "about", "above", "actually", "after", "again", "against", "always", "among", "another", "anything", "around",
    "because", "before", "being", "below", "between", "better", "bunch", "called", "certain", "could", "doing", "during",
    "either", "enough", "especially", "every", "everybody", "everyone", "everything", "exactly", "first", "going",
    "gonna", "great", "having", "honestly", "however", "itself", "kinda", "know", "later", "least", "little", "maybe",
    "might", "money", "never", "often", "other", "others", "people", "point", "pretty", "probably", "quite", "rather",
    "really", "right", "second", "should", "since", "something", "sometimes", "sort", "still", "stuff", "super", "sure",
    "their", "there", "these", "thing", "things", "think", "those", "though", "three", "today", "together", "totally",
    "trying", "under", "until", "users", "using", "very", "wanna", "where", "whether", "which", "while", "whole", "would",
    "years", "yeah", "basically", "literally", "definitely", "obviously", "already", "almost", "anyway", "ourselves",
    "talking", "saying", "thought", "looked", "looking", "getting", "makes", "making", "means", "meant", "wanted",
}


def transcript_fallback(clip_words: list[Any], spans: list[tuple[float, float, str]], clip_start: float,
                        clip_end: float, hook: float) -> list[tuple[int, Card, str]]:
    """Round 32's last resort, built from the transcript alone (no AI): the clip's main
    metric as said (Tier 1, a number over the speaker), else its key ideas as 2-3 boxes in
    the order said (Tier 2). Both still go through every check."""
    from core.card_checks import _clean, _hedge_near, _normalize_tokens

    ideas: list[tuple[int, Card, str]] = []
    first, last = clip_start + hook + 0.3, clip_end - PAYOFF_SECONDS - MIN_OVERLAY_SECONDS
    tokens = _normalize_tokens([_clean(w.text) for w in clip_words])
    rank = {"x": 0, "T": 1, "B": 1, "M": 2, "%": 3, "bps": 4, "K": 5}
    metrics = sorted((n for n in chartable_numbers(clip_words) if first <= n.start <= last and n.kind != "split"),
                     key=lambda n: (rank.get(n.scale or "", 6), n.start))
    for number in metrics[:1]:
        money = str(clip_words[number.index].text).lstrip("\"'(").startswith("$") \
            or "dollars" in tokens[number.end + 1:number.end + 3]
        amount = f"{number.value:g}"
        text = (f"{amount}x" if number.scale == "x" else f"{amount} bps" if number.scale == "bps"
                else f"{amount}%" if number.scale == "%" else f"{'$' if money else ''}{amount}{number.scale or ''}")
        if _hedge_near(tokens, number.index):
            text = "~" + text
        span = next((s for s in spans if s[0] - 0.05 <= number.start <= s[1] + 0.05), None)
        if span:
            ideas.append((5, Card("kinetic_range", start=max(span[0], first - 0.3), end=span[1] + 0.15,
                                  rows=[CardRow("", text, highlight=True)]), "the clip's main number, as said"))
    counts: dict[str, int] = {}
    for token in tokens:
        if len(token) >= 5 and token.isalpha() and token not in _SALIENT_STOP:
            counts[token] = counts.get(token, 0) + 1
    best: tuple[float, float, float, list[str]] | None = None
    for k in range(len(spans)):
        for j in range(k, min(k + 2, len(spans))):
            a, b = spans[k][0], spans[j][1]
            if a < first - 0.3 or b > clip_end - PAYOFF_SECONDS or b - a > 9.0:
                continue
            seen: list[str] = []
            for word in clip_words:
                token = _clean(word.text)
                if a - 0.05 <= float(word.start) <= b and token in counts and token not in seen:
                    seen.append(token)
            top = sorted(seen, key=lambda t: (-counts[t], seen.index(t)))[:3]
            boxes = [t for t in seen if t in top]
            if len(boxes) >= 2:
                score = sum(counts[t] for t in boxes) + 0.5 * len(boxes)
                if best is None or score > best[0]:
                    best = (score, a, b, boxes)
    if best:
        _score, a, b, boxes = best
        ideas.append((4, Card("buckets", start=a, end=b + 0.15, rows=[CardRow(t.capitalize()[:20]) for t in boxes]),
                      "the clip's key ideas, in the order said"))
    return ideas


class _Picker:
    """Chooses the clip's Bento visuals around its name cards: each offered visual is
    checked with everything already chosen, repaired when a fix is safe, and kept only
    when every graphic in the clip still passes."""

    def __init__(self, direction: Direction, words: list[Any], spans: list[tuple[float, float, str]],
                 clip_start: float, clip_end: float, hook: float, limit: int, names: list[Card],
                 replan: Callable[[list[Card]], list[Card]], plan: Any, source: Any) -> None:
        self.direction, self.words, self.spans = direction, words, spans
        self.clip_start, self.clip_end, self.hook, self.limit = clip_start, clip_end, hook, limit
        self.names, self.replan, self.plan, self.source = names, replan, plan, source
        self.chosen: list[Card] = []
        self.errors: list[str] = []

    def note(self, text: str) -> None:
        self.direction.notes.append(text)

    def _check(self, card: Card, names: list[Card] | None = None) -> tuple[bool, list[str], bool]:
        names = self.names if names is None else names
        cards = sorted(self.chosen + names + [card], key=lambda c: c.start)
        results = check_edit(ClipEdit(self.clip_start, self.clip_end, cards=cards), self.words, self.hook)
        mine = next(r for c, r in zip(cards, results) if c is card)
        clash = any(card.start < n.end + MIN_GAP and n.start < card.end + MIN_GAP for n in names)
        others = [e for c, r in zip(cards, results) if c is not card for e in r.errors]
        return all(r.ok for r in results), mine.errors + others, clash

    def _anchor(self, card: Card) -> tuple[float, float] | None:
        """The sentences where the card's numbers or words are said: the card's new times."""
        numbers = [(n, parse_value(part).scale) for row in card.rows for part in re.split(r"\s*(?:→|->)\s*", row.value)
                   for n in parse_value(part).numbers]
        keys = {w for row in card.rows for w in _words(f"{row.label} {row.detail}") if len(w) >= 5}
        hit = []
        for start, end, text in self.spans:
            inside = [w for w in self.words if start - 0.05 <= float(w.start) <= end + 0.05]
            said = spoken_numbers(inside)
            has_number = any(any(same_amount(n, scale, s) for s in said) for n, scale in numbers)
            if has_number or (not numbers and keys & set(_words(text))):
                hit.append((start, end))
        if not hit:
            return None
        start = max(hit[0][0], self.clip_start + self.hook + 0.2) if card.template != "kinetic_range" else hit[0][0]
        shortest = MIN_OVERLAY_SECONDS if MODES[card.template] == "overlay" else MIN_CARD_SECONDS
        end = min(max(hit[-1][1] + 0.15, start + shortest + 0.5), self.clip_end - PAYOFF_SECONDS - 0.05)
        return start, end

    def _repair(self, card: Card, errors: list[str]) -> Card | None:
        """One safe fix for the first problem it can fix, or None."""
        fixed = copy.deepcopy(card)
        text = " ".join(errors)
        if "hedged" in text and any(r.value and not r.value.startswith("~") for r in fixed.rows):
            for row in fixed.rows:  # show the hedge the speaker used
                if row.value and not row.value.startswith("~") and not re.search(r"→|->", row.value):
                    row.value = "~" + row.value
            return fixed
        if "isn't said" in text or "none of its words are said" in text or "aren't said" in text:
            anchored = self._anchor(card)
            if anchored and (abs(anchored[0] - card.start) > 0.05 or abs(anchored[1] - card.end) > 0.05):
                fixed.start, fixed.end = anchored
                return fixed
        if "Starts in the first" in text:
            fixed.start = self.clip_start + self.hook + 0.2
            return fixed if fixed.end - fixed.start >= MIN_OVERLAY_SECONDS else None
        if "Runs into the last" in text:
            fixed.end = self.clip_end - PAYOFF_SECONDS - 0.05
            return fixed if fixed.end - fixed.start >= MIN_OVERLAY_SECONDS else None
        if "after the graphic before it" in text:  # move clear of the graphic in its way
            others = self.chosen + self.names
            before = [c for c in others if c.start <= card.start and c.end + MIN_GAP > card.start]
            after = [c for c in others if c.start > card.start and c.start < card.end + MIN_GAP]
            if before:
                fixed.start = max(c.end for c in before) + MIN_GAP + 0.01
            elif after:
                fixed.end = min(c.start for c in after) - MIN_GAP - 0.01
            shortest = MIN_OVERLAY_SECONDS if MODES[card.template] == "overlay" else MIN_CARD_SECONDS
            return fixed if fixed.end - fixed.start >= shortest else None
        if "On screen" in text and "needs at least" in text:
            following = [e for s, e, _t in self.spans if e > card.end + 0.2]
            if following:
                fixed.end = min(following[0] + 0.15, self.clip_end - PAYOFF_SECONDS - 0.05)
                return fixed
        return None

    def offer(self, card: Card, strength: int, reason: str) -> bool:
        card = copy.deepcopy(card)
        what = _what(card)
        if len(self.chosen) >= self.limit:
            self.note(f"Skipped {what}: {self.limit} Bento visual{'s' if self.limit > 1 else ''} is the most for this clip.")
            return False
        if incidental_card(card, self.words):
            self.note(f"Skipped {what}: its numbers are said in passing, not a metric.")
            return False
        if card.context.get("kind"):  # time to read the layer under the chart (the checks drop it if there isn't)
            card.end = max(card.end, min(card.end + 2.0, self.clip_end - PAYOFF_SECONDS - 0.05))
        errors: list[str] = []
        clash = False
        for _attempt in range(5):
            ok, errors, clash = self._check(card)
            if ok:
                return self._keep(card, strength, reason, what)
            fixed = self._repair(card, errors)
            if fixed is None:
                break
            card = fixed
        if clash:  # a name card is in the way: move the name cards around this visual instead
            names = self.replan(self.chosen + [card])
            if len(names) >= len(self.names):
                ok, errors, _clash = self._check(card, names)
                if ok and self._keep(card, strength, reason, what):
                    self.names[:] = names
                    self.note("Moved the name cards to make room: " + ", ".join(
                        f"{n.rows[0].label} at {n.start - self.clip_start:.1f} s" for n in names) + ".")
                    return True
        self.errors.append(errors[0] if errors else "it didn't pass the checks")
        self.note(f"Dropped {what}: {errors[0] if errors else 'it did not pass the checks'}")
        return False

    def _keep(self, card: Card, strength: int, reason: str, what: str) -> bool:
        near = [c for c in self.chosen if abs(c.start - card.start) < BENTO_SPACING]
        if near:
            self.note(f"Skipped {what}: less than {BENTO_SPACING:g} s from the {near[0].template} (one idea at a time).")
            return False
        if card.template == "stat_pill":
            card.place = _stat_spot(self.source, self.plan, self.clip_start, self.clip_end, card)
        self.chosen.append(card)
        self.chosen.sort(key=lambda c: c.start)
        self.note(f"Added {what} at {_clock(card.start - self.clip_start)}: {reason}")
        if card.context.get("kind"):
            self.note(f"...with an 'in context' layer: {card.context.get('value')} {card.context.get('text')}")
        return True


def _plan_names(speakers: list[Speaker], clip_words: list[Any], clip_start: float, clip_end: float, hook: float,
                plan: Any, source: Any, taken: list[Card], notes: list[str] | None = None,
                video_words: list[Any] | None = None, channel: str = "") -> list[Card]:
    """Every identified speaker's name card (core.name_cards), clear of ``taken``; each
    one checked like any graphic. Round 33: only on the speaker's own face while they talk;
    with no crop plan (or no way to see faces) there is no card: never a guess."""
    from core import name_cards

    if not speakers:
        return []
    cards: list[Card] = []
    said: list[str] = []
    if plan is None or source is None:
        said.append("No name cards: this layout has no face track to check who is on screen.")
    else:
        try:
            cards, said = name_cards.plan_name_cards(source, plan, clip_start, clip_end, hook, taken, speakers,
                                                     words=clip_words, video_words=video_words, channel=channel)
        except Exception as exc:  # noqa: BLE001 - the clip renders without name cards
            logger.warning("Name card placement failed: %s", exc)
            said = [f"No name cards: placing them on faces failed ({exc})."]
    kept: list[Card] = []
    for card in cards:
        result = check_edit(ClipEdit(clip_start, clip_end, cards=sorted(taken + kept + [card], key=lambda c: c.start)),
                            clip_words, hook)
        if all(r.ok for r in result):
            kept.append(card)
        else:
            errors = [e for r in result for e in r.errors]
            said.append(f"No name card for {card.rows[0].label}: {errors[0]}")
    if notes is not None:
        notes.extend(said)
    return kept


MARKET_STRENGTH = 7  # a real-data chart of a market the speaker names: as strong as a good idea from the AI


def _brand_entities(cards: list[Card], notes: list[str]) -> None:
    """Round 35: a Bento card about one company (named in its own text) wears its logo and
    colour (core.entity_enricher); the logo is fetched now, once, so the render is offline."""
    from core import entity_enricher

    for card in cards:
        if card.template == "flow_sankey_card":  # Round 36: each company in a value chain wears its own logo
            for row in card.rows:
                key = entity_enricher.card_entity(row.label)
                if key:
                    try:
                        entity_enricher.enrich(key)
                    except Exception as exc:  # noqa: BLE001 - a logo is a bonus
                        logger.warning("Logo for %s failed: %s", key, exc)
            continue
        if card.template == "geo_map_card":
            continue  # a map's colour is the brand's: no single company owns it
        if not card.entity:
            text = " ".join([card.eyebrow, card.headline] + [f"{r.label} {r.detail}" for r in card.rows])
            card.entity = entity_enricher.card_entity(text)
        if card.entity:
            try:
                found = entity_enricher.enrich(card.entity)
            except Exception as exc:  # noqa: BLE001 - a logo is a bonus
                logger.warning("Logo for %s failed: %s", card.entity, exc)
                continue
            if found is not None:
                notes.append(f"{_what(card)} wears {found.name}'s "
                             + ("logo and " if found.logo_kind and found.logo_kind != "initials" else "")
                             + (f"colour ({found.color})." if found.color else "logo."))


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
    """The clip's graphics, every one checked: 1-2 Bento visuals (Round 32) and a name card
    for each identified speaker. ``words``: the clip's words (source times). ``plan`` and
    ``source``: the crop plan and the video, to put each name card on its speaker's own
    shot (and a micro-stat in a clear corner). ``video_words``: the whole video's words,
    for a chart's context layer (numbers said elsewhere in it). Never raises: anything that
    goes wrong means fewer graphics, with a note."""
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
    asker = ask or ask_ai
    answer: dict[str, Any] | None = None
    earlier: list[tuple[float, str]] = []
    prompt, system = "", _system_prompt(duration, hook)
    try:
        earlier = earlier_facts(video_words, clip_start, clip_end, clip_words)
        prompt = build_prompt(spans, clip_start, duration, hook, info, earlier, clip_words)
        raw = asker(prompt, system)
        if raw is None:
            direction.notes.append("AI graphics unavailable: no AI key (GEMINI_API_KEY or OPENAI_API_KEY); "
                                   "the visuals come from the transcript.")
        else:
            answer = _parse_json_response(raw)
    except ServicePause as exc:
        direction.notes.append(f"AI graphics unavailable: the AI limit is used up for now ({exc}); "
                               "the visuals come from the transcript.")
    except Exception as exc:  # noqa: BLE001 - the AI is optional; the clip still gets its visuals
        logger.warning("Graphics director failed: %s", exc)
        direction.notes.append(f"AI graphics unavailable: the AI answer was unusable ({exc}); "
                               "the visuals come from the transcript.")

    # 1. Name cards first: one per identified speaker, never counted as a Bento visual.
    speakers = video_speakers(answer, info)
    names = _plan_names(speakers, clip_words, clip_start, clip_end, hook, plan, source, [], direction.notes,
                        video_words, info.channel)

    def replan(taken: list[Card]) -> list[Card]:
        return _plan_names(speakers, clip_words, clip_start, clip_end, hook, plan, source, taken,
                           video_words=video_words, channel=info.channel)

    # 2. Bento visuals: Tier 1 (quantitative) first, Tier 2 (qualitative) as the fallback.
    limit = max_graphics(duration)
    picker = _Picker(direction, clip_words, spans, clip_start, clip_end, hook, limit, names, replan, plan, source)
    ideas = proposals(answer, spans, earlier) if answer else []
    spare = answer.get("fallback") if answer else None
    backup = [(s, c, f"fallback: {r}") for s, c, r in proposals({"graphics": [spare]}, spans, earlier)] \
        if isinstance(spare, dict) else []
    # Tier 1 first, and within it a chart (a Bento card: Plot, ECharts, Lightweight Charts) before a number
    # over the speaker; then the strongest.
    for _strength, card, _reason in ideas + backup:  # Round 36: World Bank, Polymarket, Wikipedia, with sources
        if card.template in niche_director.NICHE_TEMPLATES:
            niche_director.enrich(card, direction.notes)
    ranked = sorted(ideas, key=lambda idea: (tier(idea[1]), MODES[idea[1].template] != "split", -idea[0])) + backup
    weak: list[tuple[int, Card, str]] = []
    for strength, card, reason in ranked:
        floor = MIN_STRENGTH if tier(card) == 1 and not reason.startswith("fallback") else FALLBACK_STRENGTH
        if strength < floor:
            direction.notes.append(f"Skipped {_what(card)}: not strong enough ({strength}/10).")
            weak.append((strength, card, reason))
            continue
        picker.offer(card, strength, reason)
    # Round 36: maps and odds found in the transcript itself (countries around trade or war, "a 70% chance").
    if len(picker.chosen) < limit:
        try:
            for strength, card, reason in niche_director.moments(clip_words, spans, clip_start, clip_end, hook,
                                                                 PAYOFF_SECONDS):
                if len(picker.chosen) >= limit:
                    break
                picker.offer(card, strength, f"from the transcript: {reason}")
        except Exception as exc:  # noqa: BLE001 - a bonus idea, never a reason to fail
            logger.warning("Niche moments failed: %s", exc)
    # Round 35: a market the speaker names without saying its numbers ("NVIDIA's run", "the
    # 2008 crash", "inflation") gets a chart of the real data, with its source (core.market_data).
    if len(picker.chosen) < limit:
        from core import market_data

        try:
            for card, reason in market_data.market_cards(clip_words, clip_start, clip_end):
                if len(picker.chosen) >= limit:
                    break
                picker.offer(card, MARKET_STRENGTH, reason)
        except Exception as exc:  # noqa: BLE001 - market data is a bonus, never a reason to fail
            logger.warning("Market data failed: %s", exc)
            direction.notes.append(f"Market data unavailable ({exc}).")
    if not picker.chosen:  # never zero: a second look at what was scored low
        for strength, card, reason in weak:
            if picker.offer(card, strength, f"{reason} (taken on a second look: the clip needs one visual)"):
                break
    if not picker.chosen and answer is not None:  # a second AI request, qualitative only
        why = "; ".join(picker.errors[:3]) or "there were none"
        try:
            raw = asker(prompt + RETRY_PROMPT.replace("{why}", why), system)
            second = _parse_json_response(raw) if raw else {}
            for strength, card, reason in sorted(proposals(second, spans, earlier), key=lambda i: -i[0]):
                if picker.offer(card, strength, f"second look: {reason}"):
                    break
        except Exception as exc:  # noqa: BLE001 - the transcript fallback is next
            logger.warning("Graphics director's second look failed: %s", exc)
    if not picker.chosen:  # last resort: built from the transcript itself
        for strength, card, reason in transcript_fallback(clip_words, spans, clip_start, clip_end, hook):
            if picker.offer(card, strength, f"from the transcript: {reason}"):
                break

    # 3. The renderer checks again; anything that wouldn't pass the second time is left out now.
    edit.cards = sorted(picker.chosen + picker.names, key=lambda c: c.start)
    final = check_edit(edit, clip_words, hook)
    for card, result in list(zip(edit.cards, final)):
        if not result.ok:
            edit.cards.remove(card)
            direction.notes.append(f"Dropped {card.template}: {result.errors[0]}")
    bento = [c for c in edit.cards if c.template != "speaker_title"]
    _brand_entities(bento, direction.notes)
    from core import visual_proof

    if visual_proof.enabled():
        # Round 41: the signature edit. At most one split-screen card, and only with a figure or a
        # tight A-vs-B contrast; the speaker stays full-screen with floating proof cards and a
        # newspaper tear-out instead of text summaries.
        return visual_proof.apply(direction, clip_words, clip_start, clip_end, hook)
    if len(bento) < MIN_BENTO:
        direction.notes.append("No Bento visual: nothing in this clip passed the checks"
                               + (f" ({picker.errors[0]})" if picker.errors else "") + ".")
    return direction
