"""Round 36: how the graphics director uses the multi-niche Bento cards.

- niche_context(): the AI's "context" for a map, odds gauge, framework or tear-sheet, in
  the shape core.card_checks checks (anything else is dropped).
- enrich(): fills what only the code may fetch, with its source on the card: World Bank
  figures next to a map's countries, Polymarket odds on a gauge the speaker didn't put a
  number on, a Wikipedia line under a tear-sheet. The model never writes a fetched figure.
- moments(): Bento ideas found in the transcript alone, offered like the market charts:
  two or more countries named around trade, chips or war -> a map; "a 70% chance" -> odds.
Every look-up is optional (core.niche_data.enabled()); a card that can't be enriched is
still drawn from what was said.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from core import niche_data
from core.edl import Card, CardRow

logger = logging.getLogger(__name__)

GEO_STRENGTH = 6  # a map of the countries a speaker names around trade or conflict
ODDS_STRENGTH = 7  # odds the speaker states ("a 70% chance") read better as a gauge
NICHE_TEMPLATES = {"geo_map_card", "prediction_gauge_card", "flow_sankey_card", "framework_matrix_card",
                   "headline_card"}
_ODDS = re.compile(
    r"\b(\d{1,3})\s*(?:%|percent|per cent)\s+(?:chance|odds|probability|likelihood|likely|shot)\b"
    r"|\b(?:chance|odds|probability|likelihood)\s+(?:is|are|of|at|was)?\s*(?:about|around|roughly)?\s*(\d{1,3})\s*(?:%|percent)",
    re.IGNORECASE)


def niche_context(template: str, raw: Any) -> dict[str, Any]:
    """The AI's context for a Round 36 card, cleaned. Fetch requests ("stat", "market",
    "topic") stay until enrich() has used them."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    if template == "geo_map_card":
        arcs = raw.get("arcs")
        if isinstance(arcs, list):
            pairs = []
            for arc in arcs[:3]:
                try:
                    a, b = int(arc[0]), int(arc[1])
                except (TypeError, ValueError, IndexError, KeyError):
                    continue
                pairs.append([a, b])
            if pairs:
                out["arcs"] = pairs
        if str(raw.get("stat") or "") in niche_data.INDICATORS:
            out["stat"] = str(raw["stat"])
    elif template == "prediction_gauge_card":
        if str(raw.get("market") or "").strip():
            out["market"] = str(raw["market"]).strip()[:80]
    elif template == "framework_matrix_card":
        layout = str(raw.get("layout") or "").strip().lower()
        if layout in {"matrix", "flywheel"}:
            out["layout"] = layout
        for axis in ("x", "y"):
            if str(raw.get(axis) or "").strip():
                out[axis] = str(raw[axis]).strip()
    elif template == "headline_card":
        if str(raw.get("topic") or "").strip():
            out["topic"] = str(raw["topic"]).strip()[:80]
    return out


def enrich(card: Card, notes: list[str] | None = None) -> Card:
    """Fill a Round 36 card's fetched data (mutates and returns ``card``)."""
    notes = notes if notes is not None else []
    context = card.context
    stat = str(context.pop("stat", "") or "")
    market = str(context.pop("market", "") or "")
    topic = str(context.pop("topic", "") or "")
    if card.template not in NICHE_TEMPLATES or not niche_data.enabled():
        return card
    try:
        if card.template == "geo_map_card" and stat and not any(r.value.strip() for r in card.rows):
            _world_bank(card, stat, notes)
        elif card.template == "prediction_gauge_card" and market and not any(r.value.strip() for r in card.rows):
            _polymarket(card, market, notes)
        elif card.template == "headline_card" and topic and not card.footnote.strip():
            line = niche_data.dek(niche_data.wiki_summary(topic))
            if line:
                card.footnote, card.source = line, "Wikipedia"
                notes.append(f"Tear-sheet background from Wikipedia ({topic}).")
    except Exception as exc:  # noqa: BLE001 - a fetched figure is a bonus, never a reason to fail
        logger.info("Niche data for %s failed: %s", card.template, exc)
        notes.append(f"No outside data for the {card.template} ({exc}); it shows what was said.")
    return card


def _world_bank(card: Card, stat: str, notes: list[str]) -> None:
    label = niche_data.INDICATORS[stat][1]
    found = []
    for row in card.rows:
        place = niche_data.country(row.label)
        if place is None:
            return
        found.append(niche_data.country_stat(place.iso3, stat))
    for row, (value, _year) in zip(card.rows, found):
        row.value, row.detail = niche_data.fmt_stat(value, stat), label
    year = max(year for _value, year in found)
    card.source, card.context["data"] = f"World Bank ({year})", "world_bank"
    notes.append(f"Map figures: {label} from the World Bank ({year}).")


def _polymarket(card: Card, query: str, notes: list[str]) -> None:
    markets = niche_data.polymarket_search(query, limit=1)
    if not markets:
        raise niche_data.NicheDataError(f"no open Polymarket market for \"{query}\"")
    market = markets[0]
    yes = round(market.yes * 100)
    first = card.rows[0].label if card.rows and card.rows[0].label.strip() else market.outcomes[0]
    second = card.rows[1].label if len(card.rows) > 1 and card.rows[1].label.strip() else market.outcomes[1]
    card.rows = [CardRow(first[:14], f"{yes}%", highlight=yes >= 50), CardRow(second[:14], f"{100 - yes}%",
                                                                              highlight=yes < 50)]
    card.source, card.footnote = "Polymarket", f"{niche_data.fmt_volume(market.volume)} traded"
    card.context["data"] = "polymarket"
    notes.append(f"Live odds from Polymarket: {market.question} ({yes}%).")


def moments(clip_words: list[Any], spans: list[tuple[float, float, str]], clip_start: float, clip_end: float,
            hook: float, payoff: float = 2.0) -> list[tuple[int, Card, str]]:
    """(strength, card, reason) for maps and odds found in the transcript, best first."""
    first, last = clip_start + hook + 0.2, clip_end - payoff - 5.0
    ideas: list[tuple[int, Card, str]] = []
    for k, (start, end, text) in enumerate(spans):
        if end < first + 1.0 or start > last:  # a sentence that runs past the title still counts
            continue
        # a sentence and the next one: "China makes them. Then they ship to the US."
        stop = spans[k + 1][1] if k + 1 < len(spans) and spans[k + 1][1] - start <= 9.0 else end
        both = text + (" " + spans[k + 1][2] if stop != end else "")
        places = niche_data.countries_in(both)[:3]
        geo = niche_data.GEO_WORDS.search(both)
        if len(places) >= 2 and geo:
            rows = [CardRow(p.name, highlight=(j == len(places) - 1)) for j, p in enumerate(places)]
            arcs = [[j, j + 1] for j in range(1, len(places))]
            ideas.append((GEO_STRENGTH, Card("geo_map_card", start=max(start, first), end=stop + 0.15, rows=rows,
                                             context={"arcs": arcs}),
                          f"{' and '.join(p.name for p in places)} named around \"{geo.group(0)}\": a map"))
        odds = _ODDS.search(text)
        if odds:
            value = int(odds.group(1) or odds.group(2))
            if 0 < value < 100:
                rows = [CardRow("Yes", f"{value}%", highlight=value >= 50), CardRow("No", f"{100 - value}%")]
                ideas.append((ODDS_STRENGTH, Card("prediction_gauge_card", start=max(start, first),
                                                  end=end + 0.15, rows=rows, eyebrow="The odds"),
                              f"the speaker puts odds on it ({value}%): a gauge"))
    taken: list[tuple[int, Card, str]] = []
    for idea in sorted(ideas, key=lambda i: (-i[0], i[1].start)):
        if all(abs(idea[1].start - other[1].start) >= 6.0 for other in taken):
            taken.append(idea)
    return taken
