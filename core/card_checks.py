"""Round 27: the accuracy checks every data card passes before it's rendered.

The promise: every number on a card was said in the clip, at that moment, with the
speaker's own hedges. Nothing is computed, rounded or added. A card that fails is not
"fixed": the problems are listed and the card isn't drawn.

check_card() also finds the word where each number is said, so it appears on that word.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.edl import MODES, Card, ClipEdit

MIN_CARD_SECONDS = 2.5  # shorter than this, a chart is gone before anyone reads it
MIN_OVERLAY_SECONDS = 2.0
HOOK_SECONDS = 3.0  # the first seconds stay full-screen: the hook is the speaker
PAYOFF_SECONDS = 2.0  # the clip ends full-screen too, on the speaker's punchline
MAX_CARDS = 2  # charts (split view) per clip (Round 30: was 1)
MAX_OVERLAYS = 3  # numbers, boxes or micro-stats over the speaker per clip (plus one name card)
MAX_GRAPHICS = 3  # data graphics per clip in all: 2-3 clean visual moments where the talk has them
MAX_CHART_SHARE = 0.55  # charts cover at most this much of the clip: the speaker stays the star
OVERLAY_TEMPLATES = {"kinetic_range", "buckets", "stat_pill"}
CONTEXT_TEXT = 44  # characters in a context line
_NUMBER_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
                 "twenty", "thirty", "forty", "fifty", "hundred", "thousand", "million", "billion", "trillion",
                 "half", "twice", "double", "triple", "quarter", "third", "percent", "times"}
MIN_GAP = 1.5  # seconds of plain video between two graphics
RECAP_SECONDS = 3.0  # a number said in the hook can come back right after the title, as a recap
LINE_LOOKBACK = 8.0  # the line under a number may quote words said this long before it
OVERLAY_HOLD = 2.6  # a number or box graphic leaves this long after its last item comes in
MAX_OVERLAY_SECONDS = 5.0  # ...and is never up longer than this (it covers the speaker)
LIMITS = {"eyebrow": 30, "headline": 30, "label": 26, "value": 14, "footnote": 58, "box": 20, "detail": 30,
          "context": 44, "name": 28, "role": 42}
_ASIDES = {"dunno", "guess", "maybe", "like", "whatever"}  # "I dunno, five, six months?": an aside, not evidence

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
SCALES = {"thousand": "K", "k": "K", "million": "M", "mil": "M", "billion": "B", "bn": "B", "trillion": "T"}
_SCALE_WORDS = {"K": "thousand", "M": "million", "B": "billion", "T": "trillion", "%": "percent"}
HEDGES = {
    "about", "around", "roughly", "approximately", "approx", "nearly", "almost", "maybe", "probably",
    "somewhere", "between", "or", "ish", "think", "guess", "estimate", "estimated", "expected", "expect",
    "projected", "project", "up", "close", "under", "over", "plus", "something", "dunno",
}
_DIGITS = re.compile(r"^\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(%|[kmbt]n?)?(?:-\w+)?$", re.IGNORECASE)


@dataclass
class SpokenNumber:
    value: float
    scale: str | None  # "K", "M", "B", "T", "%" or None
    start: float
    index: int
    end: int = -1  # index of its last word


@dataclass
class CardCheck:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _clean(text: str) -> str:
    return re.sub(r"[^\w$%.,'-]", "", str(text or "")).strip(".,'").lower()


def spoken_numbers(words: list[Any]) -> list[SpokenNumber]:
    """Numbers said in ``words`` (Word objects): "$17", "1,200", "47%", "five and a half",
    "ninety six", each with its scale ("trillion" -> "T") when one follows."""
    found: list[SpokenNumber] = []
    tokens = [_clean(word.text) for word in words]
    i = 0
    while i < len(tokens):
        token, start, value, end = tokens[i], i, None, i
        scale: str | None = None
        match = _DIGITS.match(token)
        if match:
            value = float(match.group(1).replace(",", "") + (match.group(2) or ""))
            suffix = (match.group(3) or "").lower()
            if suffix == "%":
                scale = "%"
            elif suffix:
                scale = SCALES.get(suffix.rstrip("n"), None) or {"k": "K", "m": "M", "b": "B", "t": "T"}[suffix[0]]
        elif token in _TENS or token in _UNITS:
            value = float(_TENS.get(token, _UNITS.get(token, 0)))
            if token in _TENS and i + 1 < len(tokens) and tokens[i + 1] in _UNITS and _UNITS[tokens[i + 1]] < 10:
                value += _UNITS[tokens[i + 1]]
                end = i + 1
            if end + 1 < len(tokens) and tokens[end + 1] == "hundred":
                value *= 100
                end += 1
        elif token == "hundred":
            value = 100.0  # "a hundred", "the last hundred years" ("five hundred" is read above)
        if value is None:
            i += 1
            continue
        rest = tokens[end + 1:end + 4]
        if rest[:3] == ["and", "a", "half"]:
            value += 0.5
            end += 3
            rest = tokens[end + 1:end + 4]
        elif rest[:1] == ["point"] and len(rest) > 1 and rest[1] in _UNITS:
            value = float(f"{int(value)}.{_UNITS[rest[1]]}")
            end += 2
            rest = tokens[end + 1:end + 4]
        if scale is None:
            for offset, word in enumerate(tokens[end + 1:end + 3]):
                if word in SCALES:
                    scale = SCALES[word]
                    break
                if word in {"percent", "%"}:
                    scale = "%"
                    break
                if offset == 0 and word not in {"or", "to", "and"}:
                    break
        found.append(SpokenNumber(value, scale, float(words[start].start), start, end))
        i = end + 1
    # "between five and five and a half trillion": the first number takes the next one's scale.
    for k in range(len(found) - 2, -1, -1):
        joined = found[k].end + 1 < len(tokens) and tokens[found[k].end + 1] in {"and", "or", "to"} \
            and found[k + 1].index == found[k].end + 2
        if found[k].scale is None and found[k + 1].scale and joined:
            found[k].scale = found[k + 1].scale
    return found


@dataclass
class CardValue:
    numbers: list[float]
    scale: str | None
    hedged: bool


def signed_value(text: str) -> float:
    """'+$3.2M' -> 3.2, '−$0.4M' or '-$0.4M' -> -0.4 (a waterfall step)."""
    value = parse_value(text)
    magnitude = max(value.numbers) if value.numbers else 0.0
    return -magnitude if str(text).strip().startswith(("-", "−", "–")) else magnitude


def parse_value(text: str) -> CardValue:
    """'~$11–12T' -> numbers [11, 12], scale 'T', hedged (a range or '~')."""
    text = str(text or "").strip()
    numbers = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]
    scale = None
    if "%" in text:
        scale = "%"
    else:
        match = re.search(r"\d\s*(K|M|B|T|thousand|million|billion|trillion)\b", text, re.IGNORECASE)
        if match:
            word = match.group(1).lower()
            scale = SCALES.get(word) or word.upper()
    hedged = text.startswith(("~", "≈", "c.")) or len(numbers) > 1
    return CardValue(numbers, scale, hedged)


def _in_window(words: list[Any], start: float, end: float) -> list[Any]:
    return [word for word in words if start - 0.05 <= float(word.start) < end]


def _hedge_near(tokens: list[str], index: int) -> bool:
    return any(token in HEDGES for token in tokens[max(0, index - 5):index + 4])


def _limit(errors: list[str], field_name: str, text: str, limit: int) -> None:
    if len(text) > limit:
        errors.append(f"{field_name} is {len(text)} characters; the most is {limit}: \"{text}\"")


def _keywords(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) >= 4]


def _said(text: str, said: set[str], share: float = 0.5) -> bool:
    """At least ``share`` of the text's longer words were said (labels and descriptions
    are short summaries, so not every word has to be the speaker's)."""
    keys = _keywords(text)
    return not keys or sum(k in said or k.rstrip("s") in said for k in keys) / len(keys) >= share


def _match_numbers(value, numbers: list[SpokenNumber], used: set[int]) -> list[SpokenNumber] | None:
    hits = []
    for number in value.numbers:
        hit = next((n for n in numbers if abs(n.value - number) < 1e-9 and n.index not in used
                    and (value.scale is None or n.scale == value.scale)), None)
        if hit is None:
            return None
        hits.append(hit)
    return hits


_ARROW = re.compile(r"\s*(?:→|->|=>)\s*")


def _check_change(row: Any, window: list[Any], tokens: list[str], numbers: list[Any], errors: list[str],
                  lead_in: set[str] | None = None) -> None:
    """A change, "C → A" or "$2M → $8M": both ends said in the window, in that order.
    A number end is checked like any number; a word end (a grade, a rank) must be said as
    that word (a one-letter end must match its case, so "a" never counts as "A")."""
    _limit(errors, "Value", row.value, LIMITS["value"])
    _limit(errors, "Line", row.label, LIMITS["context"])
    ends = [part.strip() for part in _ARROW.split(row.value, maxsplit=1)]
    if len(ends) != 2 or not all(ends):
        errors.append(f"\"{row.value}\": write a change as \"from → to\".")
        return
    found: list[float] = []
    after = -1
    for end in ends:
        value = parse_value(end)
        if value.numbers:
            hits = _match_numbers(value, [n for n in numbers if n.index > after], set())
            if hits is None:
                errors.append(f"\"{end}\" isn't said while it's on screen.")
                return
            after = hits[0].index
            found.append(float(hits[0].start))
            continue
        target = _clean(end)
        index = next((k for k in range(after + 1, len(window)) if tokens[k] == target and
                      (len(target) > 1 or window[k].text.strip().strip(".,!?;:\"'").startswith(end))), None)
        if index is None:
            errors.append(f"\"{end}\" isn't said while it's on screen.")
            return
        after = index
        found.append(float(window[index].start))
    row.reveal_at, row.reveal_to = found[0], found[1]
    if row.label and not _said(row.label, lead_in if lead_in is not None else set(tokens)):
        errors.append("The line under it must be the speaker's words.")


def check_card(card: Card, words: list[Any], clip_start: float, clip_end: float,
               hook_seconds: float = HOOK_SECONDS) -> CardCheck:
    """Check one card against what was said, and set each item's ``reveal_at`` (and a
    range's ``reveal_to``) to the word where it's said. Errors stop the card; warnings
    are for you to look at. ``hook_seconds``: how long the opening stays clear (at least
    HOOK_SECONDS, or as long as the title card shows), so no graphic sits on the title."""
    result = CardCheck()
    errors = result.errors
    overlay = MODES.get(card.template) == "overlay"
    shortest = MIN_OVERLAY_SECONDS if overlay else MIN_CARD_SECONDS
    if card.end - card.start < shortest:
        errors.append(f"On screen {card.end - card.start:.1f} s; this needs at least {shortest} s.")
    hook = max(HOOK_SECONDS, hook_seconds)
    if card.template == "kinetic_range" and card.start < clip_start + hook < card.end - MIN_OVERLAY_SECONDS:
        # Said under the title (the hook): the number comes in right after the title,
        # as a recap of what was just said, never on top of the title.
        card.start = clip_start + hook + 0.2
    elif card.start < clip_start + hook:
        errors.append(f"Starts in the first {hook:g} s: the hook and its title stay clear.")
    if card.end > clip_end - PAYOFF_SECONDS:
        errors.append(f"Runs into the last {PAYOFF_SECONDS:.0f} s: the clip ends full-screen on the speaker.")
    _limit(errors, "Title", card.headline, LIMITS["headline"])
    window = _in_window(words, card.start, card.end)
    # A number graphic may recap what was said just before it (under the title, say), and
    # its line may quote the words that led up to it. Checking twice gives the same answer:
    # the start a first check moved onto the number's word still finds the line.
    recap_window = _in_window(words, max(clip_start, card.start - RECAP_SECONDS), card.end)
    lead_in = {_clean(w.text) for w in _in_window(words, max(clip_start, card.start - LINE_LOOKBACK), card.end)}
    clip_words = _in_window(words, clip_start, clip_end)
    tokens = [_clean(word.text) for word in window]
    said_in_clip = {_clean(w.text) for w in clip_words}
    numbers = spoken_numbers(window)
    rows = card.rows
    count = {"kinetic_range": (1, 1), "buckets": (2, 3), "speaker_title": (1, 1), "timeline": (2, 4),
             "metric_bars": (2, 4), "ranked_bars": (2, 5), "diverging_bar": (2, 2), "share_split": (2, 5),
             "trend_line": (2, 6), "arr_bridge": (3, 7), "sankey_flow": (3, 8), "cac_payback": (2, 2),
             "drawdown_curve": (2, 2), "candlestick_ema": (0, 0), "stat_pill": (1, 4)}[card.template]
    if not count[0] <= len(rows) <= count[1]:
        errors.append(f"This graphic takes {count[0]}{'' if count[0] == count[1] else f' to {count[1]}'} items.")
        return result
    used: set[int] = set()
    numeric = {"metric_bars", "ranked_bars", "diverging_bar", "kinetic_range", "share_split", "trend_line",
               "arr_bridge", "sankey_flow", "cac_payback", "stat_pill"}
    # One number with a line under it (kinetic_range, or a micro-stat of one number): the
    # line is the speaker's words. A micro-stat of 2-4 numbers is a short trend: each
    # number's label is when, as said ("2022", "Q3").
    single = card.template == "kinetic_range" or (card.template == "stat_pill" and len(rows) == 1)
    series = card.template == "stat_pill" and len(rows) > 1
    if card.context and (MODES.get(card.template) != "split" or card.template not in numeric):
        card.context = {}
        result.warnings.append("Context layer left out: only number charts take one.")
    if card.template == "speaker_title":
        _limit(errors, "Name", rows[0].label, LIMITS["name"])
        _limit(errors, "Role", rows[0].detail, LIMITS["role"])
        if not rows[0].label.strip():
            errors.append("The name card needs a name.")
        if not 2.5 <= card.end - card.start <= 4.5:
            errors.append("A name card stays on 2.5 to 4.5 s (3.3 s is best).")
        rows[0].reveal_at = card.start
        return result
    if card.template == "candlestick_ema":
        from core.cards import read_prices  # a chart from your file: no transcript numbers to check

        if not card.source.strip():
            errors.append("Say where the prices come from (Source), for example \"Bloomberg\" or \"Company filings\".")
        try:
            if len(read_prices(card.data_path)) < 10:
                errors.append("The price file needs at least 10 rows with open, high, low and close.")
        except OSError:
            errors.append("Upload the price file (CSV: date, open, high, low, close, volume).")
        result.warnings.append("Drawn from your file, not from what was said: it shows its source.")
        return result
    if card.template == "kinetic_range" and _ARROW.search(rows[0].value):
        _check_change(rows[0], recap_window, [_clean(w.text) for w in recap_window], spoken_numbers(recap_window),
                      errors, lead_in)
        if not errors:
            if rows[0].reveal_at < card.start:  # a recap: "C", then "→ A" a beat later
                rows[0].reveal_at = card.start + 0.12
                if rows[0].reveal_to < rows[0].reveal_at + 0.3:
                    rows[0].reveal_to = rows[0].reveal_at + 0.6
            card.start = max(card.start, rows[0].reveal_at - 0.12)
            _hold_overlay(card, rows, errors)
        return result
    if card.template == "drawdown_curve":
        _check_drawdown(rows, window, tokens, numbers, errors)
        return result
    if card.template in numeric:
        for position, row in enumerate(rows, start=1):
            _limit(errors, f"Item {position} value", row.value, LIMITS["value"])
            _limit(errors, f"Item {position} label", row.label,
                   LIMITS["context"] if card.template == "kinetic_range"
                   else 8 if series else 22 if card.template == "stat_pill" else LIMITS["label"])
            value = parse_value(row.value)
            if not value.numbers:
                errors.append(f"Item {position}: \"{row.value}\" has no number.")
                continue
            hits = _match_numbers(value, numbers, used)
            if hits is None and single:
                recap = spoken_numbers([w for w in recap_window if float(w.start) < card.start])
                hits = _match_numbers(value, recap, set())
                if hits is not None:  # said just before: it comes in as the graphic does
                    row.reveal_at, row.reveal_to = card.start + 0.12, None
                    if row.label and not _said(row.label, lead_in):
                        errors.append(f"The line under the number should use the speaker's words: \"{row.label}\".")
                    continue
            if hits is None:
                scale_word = _SCALE_WORDS.get(value.scale or "", "")
                errors.append(f"Item {position}: \"{row.value}\" isn't said while it's on screen (check the number"
                              f"{', that it was said in ' + scale_word if scale_word else ''}, and the start and end).")
                continue
            used.update(hit.index for hit in hits)
            if any(_hedge_near(tokens, hit.index) for hit in hits) and not value.hedged:
                errors.append(f"Item {position}: the speaker hedged (\"about\", \"or\", \"between\"...), so "
                              f"\"{row.value}\" must show it: \"~{row.value}\" or a range.")
            if any(token in _ASIDES for hit in hits for token in tokens[max(0, hit.index - 3):hit.index]):
                result.warnings.append(f"Item {position}: \"{row.value}\" sounds like an aside or a guess. "
                                       "Graphics work best for numbers that carry the point.")
            row.reveal_at = hits[0].start
            row.reveal_to = hits[1].start if len(hits) > 1 else None
            if single and row.label and not _said(row.label, lead_in):
                errors.append(f"The line under the number should use the speaker's words: \"{row.label}\".")
            if series and not (row.label.strip() and _said(row.label, said_in_clip, 1.0)):
                errors.append(f"Item {position}: label each point with when it is, as said (\"2022\", \"Q3\").")
        if card.template not in {"kinetic_range", "cac_payback"} and not errors:
            scales = {parse_value(row.value).scale for row in rows}
            if len(scales) > 1:
                errors.append("One unit per chart: these mix scales (" + ", ".join(sorted(str(x) for x in scales)) + ").")
        if not errors:
            _shape_checks(card, errors)
        if not errors and card.context and MODES.get(card.template) == "split":
            _check_context(card, result)
    elif card.template == "buckets":
        after = card.start
        for position, row in enumerate(rows, start=1):
            _limit(errors, f"Box {position}", row.label, LIMITS["box"])
            keys = [_clean(w) for w in re.findall(r"[A-Za-z0-9']+", row.label)]
            keys = [k for k in keys if len(k) >= 4] or keys[:1]
            spot = next((word for word in window if float(word.start) >= after and _clean(word.text) in keys), None)
            if spot is None:
                errors.append(f"Box {position} (\"{row.label}\"): none of its words are said after box {position - 1}.")
            else:
                row.reveal_at = after = float(spot.start)
    else:  # timeline: a point said in this window appears on its word; one said earlier is a recap
        clip_numbers = spoken_numbers(clip_words)
        recap = 0
        for position, row in enumerate(rows, start=1):
            _limit(errors, f"Point {position} time", row.value, LIMITS["value"])
            _limit(errors, f"Point {position} description", row.detail, LIMITS["detail"])
            value = parse_value(row.value)
            hits = _match_numbers(value, numbers, used) if value.numbers else None
            if hits:
                used.update(hit.index for hit in hits)
                row.reveal_at = hits[0].start
                if any(_hedge_near(tokens, hit.index) for hit in hits) and not value.hedged:
                    errors.append(f"Point {position}: the speaker hedged, so \"{row.value}\" must show it (~ or a range).")
            elif value.numbers and _match_numbers(value, [n for n in clip_numbers if n.start < card.start], set()):
                row.reveal_at = card.start + 0.2 + 0.7 * recap  # said earlier in the clip: drawn in as a recap
                recap += 1
            elif not value.numbers and _said(row.value, said_in_clip, 1.0):
                row.reveal_at = card.start + 0.2 + 0.7 * recap
                recap += 1
            else:
                errors.append(f"Point {position}: \"{row.value}\" isn't said in the clip before or while it's on screen.")
            if row.detail and not _said(row.detail, said_in_clip, 0.34):
                errors.append(f"Point {position}: the description should use the speaker's words: \"{row.detail}\".")
        times = [row.reveal_at for row in rows if row.reveal_at is not None]
        if times != sorted(times):
            errors.append("Timeline points must appear in order: put them in the order they come up.")
    if overlay and not errors:
        for row in rows:  # a recap (said under the title) comes in as the graphic does
            if row.reveal_at is not None and row.reveal_at < card.start:
                row.reveal_at = card.start + 0.12
                row.reveal_to = None
        first = min((row.reveal_at for row in rows if row.reveal_at is not None), default=card.start)
        # No empty frames: an overlay starts exactly as its first item comes in.
        card.start = max(card.start, first - 0.12)
        _hold_overlay(card, rows, errors)
    return result


def _check_drawdown(rows: list[Any], window: list[Any], tokens: list[str], numbers: list[Any],
                    errors: list[str]) -> None:
    """A fall and a recovery: item 1 the fall as said ("−20%"), item 2 the recovery in the
    speaker's words ("they go back up"), with a number only if one was said. The curve's
    shape is a sketch; only these two are claims, and both must be said."""
    fall, recovery = rows
    value = parse_value(fall.value)
    _limit(errors, "Item 1 value", fall.value, LIMITS["value"])
    _limit(errors, "Item 1 label", fall.label, LIMITS["label"])
    _limit(errors, "Item 2 label", recovery.label, LIMITS["label"])
    if not value.numbers or value.scale != "%":
        errors.append('Item 1 is the fall, as a percentage as said: e.g. "−20%".')
        return
    if fall.label and not _said(fall.label, set(tokens)):
        errors.append(f'Item 1\'s label should use the speaker\'s words (or be empty): "{fall.label}".')
        return
    if not fall.value.strip().startswith(("-", "−", "–")):
        errors.append(f'Mark the fall with a minus: "−{fall.value.strip()}".')
        return
    hits = _match_numbers(value, numbers, set())
    if hits is None:
        errors.append(f'Item 1: "{fall.value}" isn\'t said while it\'s on screen.')
        return
    if any(_hedge_near(tokens, hit.index) for hit in hits) and not value.hedged:
        errors.append(f'Item 1: the speaker hedged, so "{fall.value}" must show it (~ or a range).')
        return
    fall.reveal_at = hits[0].start
    if recovery.value.strip():
        found = _match_numbers(parse_value(recovery.value), numbers, {h.index for h in hits})
        if found is None:
            errors.append(f'Item 2: "{recovery.value}" isn\'t said while it\'s on screen.')
            return
        recovery.reveal_at = max(found[0].start, fall.reveal_at + 0.8)
    if not recovery.label.strip():
        errors.append('Item 2 is the recovery in the speaker\'s words, e.g. "they go back up".')
        return
    if not _said(recovery.label, set(tokens)):
        errors.append(f'Item 2 should use the speaker\'s words: "{recovery.label}".')
        return
    if recovery.reveal_at is None:
        keys = [_clean(word) for word in recovery.label.split() if len(_clean(word)) >= 3]
        after = [w for w in window if float(w.start) > fall.reveal_at and _clean(w.text) in keys]
        recovery.reveal_at = max(float(after[0].start) if after else fall.reveal_at + 1.2, fall.reveal_at + 0.8)


def _hold_overlay(card: Card, rows: list[Any], errors: list[str]) -> None:
    """A number or box graphic covers the speaker, so it stays only as long as it's needed:
    OVERLAY_HOLD after its last item comes in, never over MAX_OVERLAY_SECONDS."""
    last = max((t for row in rows for t in (row.reveal_at, row.reveal_to) if t is not None), default=card.start)
    longest = max(MAX_OVERLAY_SECONDS, last - card.start + 1.5)  # boxes said slowly still get 1.5 s each
    card.end = min(card.end, max(last + OVERLAY_HOLD, card.start + MIN_OVERLAY_SECONDS), card.start + longest)
    if card.end - card.start < MIN_OVERLAY_SECONDS:
        errors.append(f"Only {card.end - card.start:.1f} s after the first item appears; extend the end.")


def _shape_checks(card: Card, errors: list[str]) -> None:
    """Charts whose parts must add up, and the parts each chart needs."""
    rows = card.rows
    values = [parse_value(r.value) for r in rows]
    if card.template == "arr_bridge":
        if any(len(v.numbers) > 1 for v in values):
            errors.append("A waterfall needs exact numbers, not ranges.")
            return
        steps = rows[1:-1]
        unsigned = [r.value for r in steps if not r.value.strip().startswith(("+", "-", "−", "–"))]
        if unsigned:
            errors.append("Mark each step + or − (e.g. +$3.2M, −$0.4M): " + ", ".join(unsigned))
            return
        start, end = values[0].numbers[0], values[-1].numbers[0]
        total = start + sum(signed_value(r.value) for r in steps)
        if abs(total - end) > max(0.02 * abs(end), 1e-9):
            errors.append(f"It doesn't add up: start {start:g} plus the steps is {total:g}, but the end is {end:g}. "
                          "Check the numbers and signs (a waterfall never fills a gap itself).")
    elif card.template == "sankey_flow":
        labels = [r.label for r in rows]
        roots = [r for r in rows if not r.parent]
        if len(roots) != 1:
            errors.append("A flow chart needs exactly one starting row (the one with no 'flows from').")
            return
        missing = [r.parent for r in rows if r.parent and r.parent not in labels]
        if missing:
            errors.append("'Flows from' must name another row: " + ", ".join(missing))
            return
        by_label = {r.label: r for r in rows}
        for row in rows:  # depth: at most two splits (revenue -> costs -> cost lines)
            depth, current = 0, row
            while current.parent:
                depth, current = depth + 1, by_label[current.parent]
                if depth > 2:
                    errors.append("At most two levels of splits.")
                    return
        for parent in rows:
            kids = [r for r in rows if r.parent == parent.label]
            if kids:
                whole = max(parse_value(parent.value).numbers)
                parts = sum(max(parse_value(k.value).numbers) for k in kids)
                if abs(parts - whole) > 0.03 * whole:
                    errors.append(f"The parts of {parent.label} add up to {parts:g}, not {whole:g}: every flow must be said.")
    elif card.template == "share_split":
        if values[0].scale == "%":
            total = sum(max(v.numbers) for v in values)
            if not 95 <= total <= 105:
                errors.append(f"The shares add up to {total:g}%, not 100%: a part is missing or a number is off.")
    elif card.template == "trend_line":
        long = [r.label for r in rows if len(r.label) > 10]
        if long:
            errors.append("Keep the time labels short (10 characters, e.g. 2024 or Q3 '25): " + ", ".join(long))
    elif card.template == "cac_payback":
        if "month" not in rows[1].value.lower():
            errors.append("The second row is the payback period in months, e.g. \"12 months\".")


def _magnitude(row: Any) -> float:
    numbers = parse_value(row.value).numbers
    return max(numbers) if numbers else 0.0


def _ratio_text(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".") + "×"


def _check_context(card: Card, result: CardCheck) -> None:
    """The chart's context layer (Card.context): what the numbers mean, from what was
    said only. "ratio" (one item next to another: 2×) and "share" (a part of a whole:
    40%) are worked out here from two of the chart's own checked numbers; "earlier" is a
    number the speaker said elsewhere in the same video (the sentence is kept with it).
    Its line names things and never carries a number of its own. A context that doesn't
    hold is left out (the chart stays), with a warning saying why."""
    context = dict(card.context)
    kind, text = str(context.get("kind") or ""), str(context.get("text") or "").strip()

    def drop(reason: str) -> None:
        card.context = {}
        result.warnings.append(f"Context layer left out: {reason}")

    if not text or len(text) > CONTEXT_TEXT:
        return drop(f"its line needs 1 to {CONTEXT_TEXT} characters (\"{text}\").")
    words = set(re.findall(r"[a-z]+", text.lower()))
    if re.search(r"\d", text) or words & _NUMBER_WORDS:
        return drop(f"its line can't carry a number of its own; the number is shown next to it (\"{text}\").")
    if kind in {"ratio", "share"}:
        try:
            first, second = (int(n) for n in context.get("of") or [])
            a, b = card.rows[first - 1], card.rows[second - 1]
        except (TypeError, ValueError, IndexError):
            return drop("it must name two of the chart's items by number.")
        if first == second:
            return drop("it compares an item with itself.")
        if parse_value(a.value).scale != parse_value(b.value).scale:
            return drop("its two numbers are in different units.")
        x, y = _magnitude(a), _magnitude(b)
        if x <= 0 or y <= 0:
            return drop("one of its numbers is zero.")
        def stems(words: str) -> set[str]:
            return {k.replace("'s", "").strip("'").rstrip("s") for k in _keywords(words)}

        names = stems(a.label) | stems(b.label)
        if names and not names & stems(text):
            return drop(f"its line should name what it compares ({a.label} or {b.label}).")
        if kind == "ratio":
            ratio = max(x, y) / min(x, y)
            if not 1.25 <= ratio <= 50:
                return drop(f"{ratio:.2f}× is too close to 1 (or too far) to mean something.")
            context["value"] = _ratio_text(ratio)
        else:
            if x >= y:
                return drop("the part must be smaller than the whole.")
            context["value"] = f"{100 * x / y:.0f}%"
        context["reveal"] = max(r.reveal_at for r in (a, b) if r.reveal_at is not None) + 0.9
    elif kind == "earlier":
        said, value = str(context.get("said") or ""), parse_value(str(context.get("value") or ""))
        if not said or not value.numbers or len(str(context.get("value"))) > LIMITS["value"]:
            return drop("an earlier number needs its value and the sentence it was said in.")
        heard = [w for w in re.findall(r"\S+", said)]
        spoken = spoken_numbers([_Plain(token, k) for k, token in enumerate(heard)])
        if _match_numbers(value, spoken, set()) is None:
            return drop(f"\"{context.get('value')}\" isn't in the sentence it's quoted from.")
        if not _said(text, {_clean(token) for token in heard}, 0.6):
            return drop("its line should use the words of the sentence it quotes.")
        context["reveal"] = max((r.reveal_at for r in card.rows if r.reveal_at is not None), default=card.start) + 0.9
    else:
        return drop(f"unknown kind \"{kind}\".")
    if context["reveal"] > card.end - 1.9:  # up at least 1.5 s before the chart leaves
        return drop("there's no time left to read it before the chart leaves.")
    card.context = context


@dataclass
class _Plain:
    """A word of a quoted sentence (for spoken_numbers), timed by its position."""

    text: str
    start: float
    end: float = 0.0

    def __post_init__(self) -> None:
        self.end = self.start + 0.5


def check_edit(edit: ClipEdit, words: list[Any], hook_seconds: float = HOOK_SECONDS) -> list[CardCheck]:
    """Every card in the clip, plus the clip-wide rules: at most 2 charts (covering at most
    MAX_CHART_SHARE of the clip), 3 number, box or micro-stat graphics, 3 data graphics in
    all and 1 name card, with at least MIN_GAP seconds between graphics."""
    results = [check_card(card, words, edit.start, edit.end, hook_seconds) for card in edit.cards]
    charts = [k for k, card in enumerate(edit.cards) if MODES.get(card.template) == "split"]
    overlays = [k for k, card in enumerate(edit.cards) if card.template in OVERLAY_TEMPLATES]
    names = [k for k, card in enumerate(edit.cards) if card.template == "speaker_title"]
    data = sorted(charts + overlays)
    for kind, members, most in (("chart", charts, MAX_CARDS), ("number, box or micro-stat graphics", overlays, MAX_OVERLAYS),
                                ("name card", names, 1), ("data graphics", data, MAX_GRAPHICS)):
        for k in members[most:]:
            results[k].errors.append(f"At most {most} {kind} per clip: each one has to land.")
    covered = 0.0
    for k in charts:
        covered += edit.cards[k].end - edit.cards[k].start
        if covered > MAX_CHART_SHARE * max(1e-6, edit.end - edit.start) and len(charts) > 1:
            results[k].errors.append(f"Charts would cover over {MAX_CHART_SHARE:.0%} of the clip: the speaker stays the star.")
    ordered = sorted(range(len(edit.cards)), key=lambda k: edit.cards[k].start)
    for first, second in zip(ordered, ordered[1:]):
        if edit.cards[second].start < edit.cards[first].end + MIN_GAP:
            results[second].errors.append(f"Starts less than {MIN_GAP:g} s after the graphic before it: give the speaker room.")
    return results
