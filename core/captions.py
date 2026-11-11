"""Round 36: semantic keyword highlighting for the bold captions (pycaps-style), and the
rule that keeps every caption clear of a Bento card.

Keywords keep their colour for the whole caption line, so the point reads before it's
spoken; the spoken word itself pops (1.08x) as it's said:

- figures ("$10B", "40%", "14x", "10 billion") -> emerald (the charts' gain colour);
- tickers ("$NVDA") -> gold;
- companies, funds and countries ("NVIDIA", "Sequoia", "China", "the United States")
  -> the brand accent (core.edl.BrandKit.accent);
- power words ("never", "biggest", "crash", "doubled") -> gold.

KEYWORD_EMPHASIS=off in .env turns the colours off (NUMBER_EMPHASIS=off still turns off
the figures alone).

Collision rule: while a Bento card is up (y BENTO_ZONE), the bold captions are not drawn
at all (core.dynamic_layout.words_outside) and one bold line sits on the seam above the
card, at the bottom of the speaker's viewport (Round 39: core.dynamic_layout.SPLIT_CAPTIONS);
caption_band() and clear_of_bento() state it so the tests can hold every layout to it.
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable

FIGURE = "figure"
TICKER = "ticker"
ENTITY = "entity"
POWER = "power"

EMERALD = "#34D399"
GOLD = "#F5C542"
DEFAULT_ACCENT = "#F4C430"
POP_SCALE = 108  # the spoken keyword pops to 1.08x
BENTO_ZONE = (1056, 1768)  # core.cards.CARD_TOP / CARD_BOTTOM: where a split card is drawn (Round 39)

POWER_WORDS = frozenset("""
never nobody everyone everything nothing biggest largest smallest fastest worst
record crash crashed collapse collapsed boom bubble panic secret mistake mistakes wrong lie lies myth truth
doubled tripled halved exploded skyrocketed plummeted soared tanked surged massive insane impossible
guaranteed fraud bankrupt bankruptcy recession dangerous
""".split())  # words that carry the point; everyday ones ("last", "only", "best") stay plain
_SCALE_AFTER = {"thousand", "million", "billion", "trillion", "percent", "x", "times", "fold", "bps", "basis"}
_NUMBER_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "twenty", "thirty",
                 "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "half", "dozen"}
_TICKER = re.compile(r"^[\"'(]*\$[A-Z]{1,5}[.,!?;:)\"']*$")
_ENTITY_KINDS = {"company", "fund", "private", "crypto", "index"}


def enabled() -> bool:
    return (os.getenv("KEYWORD_EMPHASIS") or "on").strip().lower() not in {"off", "0", "false", "no"}


def ass_colour(hex_colour: str) -> str:
    """'#34D399' -> '&H99D334&' (ASS colours are blue-green-red)."""
    value = hex_colour.lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        value = DEFAULT_ACCENT.lstrip("#")
    return f"&H{value[4:6]}{value[2:4]}{value[0:2]}&".upper()


def _bare(token: str) -> str:
    return re.sub(r"^[\"'(\[]+|[\"').,!?;:\]]+$", "", token)


def _named_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of companies, funds and countries named in ``text``."""
    spans: list[tuple[int, int]] = []
    try:
        from core import entity_enricher, niche_data

        patterns = [(p, entity_enricher.ENTITIES[key].kind in _ENTITY_KINDS) for p, key in entity_enricher._PATTERNS]
        patterns += [(p, True) for p, _country in niche_data._COUNTRY_PATTERNS]
    except Exception:  # noqa: BLE001 - highlighting is a finish, never a reason to fail
        return spans
    for pattern, wanted in patterns:
        if not wanted:
            continue
        for match in pattern.finditer(text):
            if not any(a < match.end() and match.start() < b for a, b in spans):
                spans.append((match.start(), match.end()))
    return spans


def keyword_kinds(tokens: Iterable[str]) -> list[str | None]:
    """The keyword kind of each caption token (FIGURE, TICKER, ENTITY, POWER or None),
    from the words as spoken (before upper-casing). Multi-word names ("United States")
    mark every word."""
    from core.subtitle_engine import is_figure

    words = [str(t) for t in tokens]
    kinds: list[str | None] = [None] * len(words)
    offsets, cursor = [], 0
    for word in words:
        offsets.append((cursor, cursor + len(word)))
        cursor += len(word) + 1
    for start, end in _named_spans(" ".join(words)):
        for k, (a, b) in enumerate(offsets):
            if a < end and start < b:
                kinds[k] = ENTITY
    for k, word in enumerate(words):
        bare = _bare(word).lower()
        if _TICKER.match(word):
            kinds[k] = TICKER
        elif is_figure(word.upper()):
            kinds[k] = FIGURE
            # "10 billion", "40 percent", "14 times": the scale is part of the figure
            if k + 1 < len(words) and _bare(words[k + 1]).lower() in _SCALE_AFTER:
                kinds[k + 1] = FIGURE
        elif kinds[k] is None and bare in POWER_WORDS:
            kinds[k] = POWER
    for k, word in enumerate(words):  # "ten billion": a spelled figure with its scale
        if _bare(word).lower() in _NUMBER_WORDS and k + 1 < len(words) \
                and _bare(words[k + 1]).lower() in {"million", "billion", "trillion", "percent"}:
            kinds[k] = kinds[k + 1] = FIGURE
    return kinds


def colours(accent: str | None = None) -> dict[str, str]:
    """ASS colour per keyword kind."""
    return {FIGURE: ass_colour(EMERALD), TICKER: ass_colour(GOLD), ENTITY: ass_colour(accent or brand_accent()),
            POWER: ass_colour(GOLD)}


def brand_accent() -> str:
    try:
        from core.edl import load_brand_kit

        return load_brand_kit().accent or DEFAULT_ACCENT
    except Exception:  # noqa: BLE001
        return DEFAULT_ACCENT


def keyword_pop(outline: float, word_ms: int, max_ms: int = 110) -> str:
    """ASS tags for a spoken keyword: a 1.08x pop that settles back within the word."""
    duration = int(max(60, min(max_ms, word_ms * 0.4)))
    base = int(outline)
    return (f"\\fscx{POP_SCALE}\\fscy{POP_SCALE}\\bord{base + 2}"
            f"\\t(0,{duration},\\fscx100\\fscy100\\bord{base})")


# ---------------------------------------------------------------------------- collisions
def caption_band(kind: str) -> tuple[int, int]:
    """Output rows a caption kind can cover: "bold" (full screen), "seam" (a two-person
    split), "calm" (over-the-speaker graphics), "split" (under a split chart, on the seam)."""
    from core import dynamic_layout

    return {"bold": dynamic_layout.BOLD_CAPTIONS, "seam": dynamic_layout.SEAM_CAPTIONS,
            "calm": dynamic_layout.CALM_CAPTIONS, "split": dynamic_layout.SPLIT_CAPTIONS}[kind]


def overlaps(band: tuple[float, float], zone: tuple[float, float] = BENTO_ZONE) -> bool:
    return band[0] < zone[1] and zone[0] < band[1]


def clear_of_bento(words: list[Any], cards: list[Any]) -> list[Any]:
    """The words whose bold captions may be drawn: none said while a card is up (the
    seam line above the card carries those)."""
    return [w for w in words if not any(float(w.start) < c.end and float(w.end) > c.start for c in cards)]
