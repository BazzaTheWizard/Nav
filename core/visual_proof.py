"""Round 41: the Nav Studios signature edit, "visual proof, not text summaries".

The speaker stays 100 % full-screen (1080 x 1920) for almost the whole clip. Graphics
prove what's said instead of summarising it:

Pillar 1, the split-screen gate (gate_splits): a split-screen card (a chart, THE
CONTRAST, STEP BY STEP, comparison boxes) is allowed only when what's said while it's up
has an explicit quantitative figure (a %, a multiple, a money amount: "300%", "$50M",
"3x"; core.card_checks.chartable_numbers) or a tight A-vs-B dichotomy said within 5.0 s
("X versus Y", "not X but Y", "instead of", "used to ... now"), and never more than
MAX_SPLIT_CARDS_PER_CLIP (1). The text-summary overlays (boxes, a number over the
speaker, the micro-stat) give way to the proof cards below.

Pillar 2, floating proof cards (core.proof_cards), planned from the Whisper words:
  brand_proof on the word a known company or brand is named (core.entity_enricher);
  wiki_proof when a notable person, event, book or idea is named (the Wikipedia REST
    summary API: title, description, thumbnail; cached, so a render never waits on it);
  stat_proof on a spoken figure (it rolls up from zero as it's said).
Pillar 3, newspaper_tearout: the clip's thesis line as a newsprint headline, its key
4-6 words highlighted as they're said.

The scheduler keeps them apart (MIN_GAP), clear of the title, the payoff and the split
card, one tear-out at most, and about one proof every 8 s. Every card still goes through
core.card_checks (the brand or name or figure must be said while it's up).

SIGNATURE_EDIT=0 in .env turns Round 41 off (the director, camera energy and the
smoothing go back to Round 40). WIKI_PROOF=0 skips the Wikipedia lookups.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.edl import MODES, Card, CardRow

logger = logging.getLogger(__name__)

MAX_SPLIT_CARDS_PER_CLIP = 1
DICHOTOMY_SECONDS = 5.0
TEXT_OVERLAYS = frozenset({"buckets", "kinetic_range", "stat_pill"})
PROOF_GAP = 1.5  # core.card_checks.MIN_GAP: plain video between two graphics
SPLIT_MARGIN = 0.8  # the split card's moves in and out
SECONDS_PER_PROOF = 8.0
MAX_PROOFS = 4
PER_KIND = {"newspaper_tearout": 1, "brand_proof": 2, "wiki_proof": 2, "stat_proof": 2}
PILL_SECONDS = 2.8  # within core.proof_cards.PILL_SECONDS (2.4-3.2)
PILL_LEAD = 0.25  # a pill is in this long before the word that triggers it
BRAND_KINDS = {"company", "private", "fund"}
KEY_SECONDS = 1.65  # a tear-out's key words are said within this: in 0.3 s before, the full highlight held
TEAR_LEAD, TEAR_TAIL = 0.30, 0.55  # ...and 0.15 s after the last word before its 0.4 s exit (1.8-2.5 s in all)
_CLAUSE_BREAKS = {"while", "and", "but", "so", "because", "which", "that", "when", "if", "who", "where", "although"}
_DANGLING = {"every", "a", "an", "the", "of", "to", "and", "in", "on", "for", "that", "with", "is", "it", "my", "our",
             "your", "their", "his", "her", "this", "single", "very", "so", "but", "or", "being", "been", "be", "like",
             "about", "than", "as", "at", "by", "from", "into", "are", "was", "were", "it's", "its", "we", "i", "you",
             "they", "he", "she", "not", "just", "really", "more", "most", "some", "any", "all", "no", "can", "will",
             "would", "should", "could", "do", "does", "did", "have", "has", "had", "get", "got"}

_DICHOTOMY_PAIRS = (("not", "but"), ("used", "now"), ("before", "now"), ("before", "after"), ("either", "or"),
                    ("then", "now"), ("old", "new"), ("less", "more"), ("more", "less"))
_DICHOTOMY_MARKERS = (("versus",), ("vs",), ("instead", "of"), ("rather", "than"), ("compared", "to"),
                      ("compared", "with"), ("as", "opposed", "to"), ("on", "the", "other", "hand"))
_NOT_NAMES = {"I", "The", "A", "An", "And", "But", "So", "We", "He", "She", "They", "It", "This", "That", "My", "Our",
              "You", "If", "When", "What", "Why", "How", "Then", "There", "Here", "In", "On", "At", "For", "Of", "To",
              "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February",
              "March", "April", "May", "June", "July", "August", "September", "October", "November", "December",
              "Yeah", "Okay", "OK", "Well", "Now", "Just", "Like", "Because", "Every", "Everyone", "Nobody", "Mr",
              "Mrs", "Dr", "God", "Oh", "No", "Yes", "Right", "Look", "Thanks", "Thank", "Our", "Your", "Their"}
_JOINERS = {"of", "the", "and", "de", "von", "van", "da", "du", "la"}
_CAP = re.compile(r"^[A-Z][A-Za-z'’.\-]*[a-z][A-Za-z'’.\-]*$")
_TOPICS = {
    "finance": ("THE MARKET BRIEF", "MARKET REALITY"),
    "tech": ("THE INDUSTRY BRIEF", "AI & SOFTWARE"),
    "executive": ("THE STRATEGY DESK", "BOARDROOM REALITY"),
    "editorial": ("THE ARCHIVE", "THE ARGUMENT"),
}


def enabled() -> bool:
    """Round 41's signature edit is on unless SIGNATURE_EDIT=0."""
    return os.getenv("SIGNATURE_EDIT", "1").strip().lower() not in {"0", "false", "no", "off"}


def _clean(text: Any) -> str:
    return re.sub(r"[^\w'$%.]+", "", str(text).lower().replace("’", "'")).strip(".'")


# ============================================================ Pillar 1: the gate
_SCALE_WORDS = {"percent", "million", "billion", "trillion", "thousand", "dollars", "bucks"}


def explicit_figures(words: list[Any]) -> list[Any]:
    """The explicit figures said: a %, a multiple or money (core.card_checks.chartable_numbers)
    that is a real figure, written with digits or said with its unit ("300%", "$50M",
    "3x", "forty percent", "two billion dollars"). "I was there two times" is not one."""
    from core.card_checks import chartable_numbers

    words = list(words)
    out = []
    for number in chartable_numbers(words):
        said = [str(w.text) for w in words[number.index:number.end + 3]]
        digits = bool(re.search(r"\d", str(words[number.index].text)))
        unit = any(_clean(t) in _SCALE_WORDS or "$" in t or "%" in t for t in said)
        if (digits and (number.scale != "x" or re.search(r"\d\s*x\b", " ".join(said).lower()))) or (unit and number.scale != "x"):
            out.append(number)
    return out


def quantitative(words: list[Any]) -> bool:
    """An explicit figure is said: a %, a multiple or money ("300%", "$50M", "3x")."""
    return bool(explicit_figures(words))


def dichotomy(words: list[Any], seconds: float = DICHOTOMY_SECONDS) -> bool:
    """A tight A-vs-B contrast said within ``seconds``: "X versus Y", "instead of", "not X
    but Y", "used to ... now", with words on both sides."""
    tokens = [_clean(w.text) for w in words]
    times = [(float(w.start), float(w.end)) for w in words]
    n = len(tokens)
    for k in range(n):
        for marker in _DICHOTOMY_MARKERS:
            if tuple(tokens[k:k + len(marker)]) == marker and 0 < k and k + len(marker) < n:
                a, b = max(0, k - 4), min(n - 1, k + len(marker) + 4)
                if times[b][1] - times[a][0] <= seconds:
                    return True
        for first, second in _DICHOTOMY_PAIRS:
            if tokens[k] != first:
                continue
            for j in range(k + 2, n):
                if times[j][1] - times[k][0] > seconds:
                    break
                if tokens[j] == second and j + 1 < n:
                    return True
    return False


def _spoken_during(card: Card, words: list[Any], lead: float = 1.0) -> list[Any]:
    return [w for w in words if card.start - lead <= float(w.start) < card.end]


def split_qualifies(card: Card, words: list[Any]) -> tuple[bool, str]:
    """(may this split-screen card stay, why)."""
    said = _spoken_during(card, words)
    if quantitative(said):
        return True, "an explicit figure is said"
    sides = [row.reveal_at for row in card.rows if row.reveal_at is not None]
    tight = len(sides) < 2 or max(sides) - min(sides) <= DICHOTOMY_SECONDS
    if tight and dichotomy(said):
        return True, "a tight A-vs-B contrast (within 5 s)"
    return False, "no explicit figure and no tight A-vs-B contrast: the speaker stays full-screen"


def gate_splits(cards: list[Card], words: list[Any], limit: int = MAX_SPLIT_CARDS_PER_CLIP) -> tuple[list[Card], list[str]]:
    """The cards with at most ``limit`` split-screen cards, each one qualified (figures
    first, then the earliest), and the text-summary overlays left out. Notes say why."""
    notes: list[str] = []
    kept: list[Card] = []
    splits: list[tuple[int, float, Card]] = []
    for card in cards:
        if MODES.get(card.template) == "split":
            ok, why = split_qualifies(card, words)
            if ok:
                splits.append((0 if why.startswith("an explicit") else 1, card.start, card))
            else:
                notes.append(f"Split screen refused for {card.template}: {why}.")
        elif card.template in TEXT_OVERLAYS:
            notes.append(f"Left out {card.template}: a text summary; the speaker's proof cards take its place.")
        else:
            kept.append(card)
    splits.sort(key=lambda item: (item[0], item[1]))
    for _rank, _start, card in splits[limit:]:
        notes.append(f"Split screen refused for {card.template}: at most {limit} per clip.")
    kept += [card for _rank, _start, card in splits[:limit]]
    return sorted(kept, key=lambda c: c.start), notes


# ============================================================ Wikipedia
def cache_dir() -> Path:
    from core import niche_data

    return niche_data.cache_dir() / "wiki_proof"


def _slug(name: str) -> str:
    return urllib.parse.quote(name.strip().replace(" ", "_"), safe="_()'-")


def wiki_lookup(name: str) -> dict[str, str] | None:
    """{"title", "description", "extract", "image"} from the Wikipedia REST summary API
    (https://en.wikipedia.org/api/rest_v1/page/summary/{slug}), its thumbnail saved next
    to it; None for no page, a disambiguation page or one with no description. Cached:
    NAV_OFFLINE=1 or WIKI_PROOF=0 only ever reads the cache."""
    if os.getenv("WIKI_PROOF", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    folder = cache_dir()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name.lower())[:80]
    meta = folder / f"{stem}.json"
    if meta.is_file():
        data = json.loads(meta.read_text())
        return data or None
    if os.getenv("NAV_OFFLINE") == "1":
        return None
    import requests

    headers = {"User-Agent": "NavStudios-graphics/1.0 (clip graphics)"}
    data: dict[str, str] = {}
    try:
        answer = requests.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{_slug(name)}", headers=headers,
                              timeout=6)
        if answer.status_code == 200:
            page = answer.json()
            if page.get("type") == "standard" and page.get("description"):
                data = {"title": str(page.get("title") or name), "description": str(page["description"]),
                        "extract": str(page.get("extract") or ""), "image": ""}
                source = ((page.get("thumbnail") or {}).get("source")) or ""
                if source:
                    picture = requests.get(source, headers=headers, timeout=6)
                    if picture.status_code == 200 and picture.content:
                        suffix = Path(urllib.parse.urlparse(source).path).suffix.lower() or ".jpg"
                        image = folder / f"{stem}{suffix if suffix in {'.jpg', '.jpeg', '.png', '.webp'} else '.jpg'}"
                        folder.mkdir(parents=True, exist_ok=True)
                        image.write_bytes(picture.content)
                        data["image"] = str(image)
    except Exception as exc:  # noqa: BLE001 - no Wikipedia: no card, never a failed clip
        logger.info("Wikipedia lookup failed for %s: %s", name, exc)
        return None
    folder.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps(data))
    return data or None


def dek(text: str, limit: int = 56) -> str:
    text = re.split(r"(?<=[.!?])\s", (text or "").strip())[0].strip().rstrip(".")
    if len(text) <= limit:
        return text[:1].upper() + text[1:]
    cut = text[:limit - 1].rsplit(" ", 1)[0]
    return cut[:1].upper() + cut[1:] + "…"


# ============================================================ Pillar 2 and 3: moments
@dataclass
class Moment:
    card: Card
    priority: int
    reason: str


def brand_moments(words: list[Any], lookup: Callable[[str], dict | None]) -> list[Moment]:
    """A brand_proof on the first word each known company or brand is named."""
    from core import entity_enricher as EE
    from core.proof_cards import KIND_LABELS

    out: list[Moment] = []
    seen: set[str] = set()
    for k, word in enumerate(words):
        window = " ".join(str(w.text) for w in words[k:k + 4])
        lead = len(str(word.text))
        for pattern, key in EE._PATTERNS:
            if key in seen:
                continue
            match = pattern.search(window)
            if match is None or match.start() > lead or EE.ENTITIES[key].kind not in BRAND_KINDS:
                continue
            seen.add(key)
            entity = EE.ENTITIES[key]
            info = lookup(entity.name) if lookup else None
            ticker = entity.ticker if entity.ticker and entity.ticker.isalpha() else ""
            # the badge: what the company is (Wikipedia's one line), else its real web address
            context = dek(info["description"]) if info else (entity.domain or KIND_LABELS.get(entity.kind, "Company"))
            tag = KIND_LABELS.get(entity.kind, "Company")
            start = float(word.start) - PILL_LEAD
            card = Card("brand_proof", start, start + PILL_SECONDS, eyebrow=tag, entity=key,
                        rows=[CardRow(entity.name, ticker, detail=context, reveal_at=start)])
            out.append(Moment(card, 2, f"{entity.name} named at {float(word.start):.1f} s"))
            break
    return out


def names(words: list[Any]) -> list[tuple[int, int, str]]:
    """(first word, last word, name) for every run of 2-4 capitalised words ("Warren
    Buffett", "Great Depression", "The Art of War" as "Art of War")."""
    from core import entity_enricher as EE

    found: list[tuple[int, int, str]] = []
    k, n = 0, len(words)
    while k < n:
        token = str(words[k].text).strip("\"'“”‘’(),.?!:;")
        if not _CAP.match(token) or token in _NOT_NAMES:
            k += 1
            continue
        j, parts = k, [token]
        while j + 1 < n and not re.search(r"[.?!,;:]$", str(words[j].text)):
            nxt = str(words[j + 1].text).strip("\"'“”‘’(),.?!:;")
            if _CAP.match(nxt) and nxt not in _NOT_NAMES:
                parts.append(nxt)
                j += 1
            elif nxt.lower() in _JOINERS and j + 2 < n and _CAP.match(str(words[j + 2].text).strip("\"'“”‘’(),.?!:;")):
                parts.append(nxt.lower())
                j += 1
            else:
                break
        if 2 <= len(parts) <= 4 and not EE.detect(" ".join(parts)):
            found.append((k, j, " ".join(parts)))
        k = j + 1
    return found


def wiki_moments(words: list[Any], lookup: Callable[[str], dict | None], skip: set[str] | None = None) -> list[Moment]:
    out: list[Moment] = []
    seen: set[str] = set(s.lower() for s in (skip or set()))
    for first, last, name in names(words):
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        info = lookup(name) if lookup else None
        if not info:
            continue
        title_tokens = set(re.findall(r"[a-z]+", info["title"].lower()))
        if not set(re.findall(r"[a-z]+", name.lower())) - {"of", "the", "and"} <= title_tokens:
            continue  # the page isn't the one named
        start = float(words[first].start) - PILL_LEAD
        card = Card("wiki_proof", start, start + PILL_SECONDS, eyebrow="From the archive", footnote="Wikipedia",
                    data_path=info.get("image", ""), rows=[CardRow(info["title"], detail=dek(info["description"]),
                                                                   reveal_at=start)])
        out.append(Moment(card, 3, f"{info['title']} named at {float(words[first].start):.1f} s"))
    return out


def stat_moments(words: list[Any]) -> list[Moment]:
    """A stat_proof on every explicit figure said: its value as said, the words after it."""
    from core.card_checks import _clean as clean, _hedge_near, _normalize_tokens

    tokens = _normalize_tokens([clean(w.text) for w in words])
    out: list[Moment] = []
    for number in explicit_figures(words):
        money = str(words[number.index].text).lstrip("\"'(").startswith("$") or "dollars" in tokens[number.end + 1:number.end + 3]
        amount = f"{number.value:g}"
        value = (f"{amount}x" if number.scale == "x" else f"{amount} bps" if number.scale == "bps"
                 else f"{amount}%" if number.scale == "%" else f"{'$' if money else ''}{amount}{number.scale or ''}")
        if _hedge_near(tokens, number.index):
            value = "~" + value
        label: list[str] = []
        for w in words[number.end + 1:number.end + 6]:
            text = str(w.text).strip("\"'“”()")
            if clean(text) in {"dollars", "percent", "million", "billion", "trillion", "times", "x"} and not label:
                if re.search(r"[.?!,;:]$", text):
                    break
                continue
            if label and clean(text) in _CLAUSE_BREAKS:
                break  # "in two years | while everyone...": the label is the figure's own phrase
            label.append(text.rstrip(".,;:?!"))
            if re.search(r"[.?!,;:]$", text) or len(label) >= 5:
                break
        if not label:
            label = [str(w.text).strip(".,;:?!\"") for w in words[max(0, number.index - 3):number.index]]
        start = float(number.start) - PILL_LEAD - 0.05
        card = Card("stat_proof", start, start + PILL_SECONDS, eyebrow="By the numbers",
                    rows=[CardRow(" ".join(label)[:44], value, highlight=True, reveal_at=start)],
                    context={"at": float(number.start)})
        out.append(Moment(card, 4, f"{value} said at {float(number.start):.1f} s"))
    return out


def topic(words: list[Any]) -> str:
    from core import palettes

    tokens = re.findall(r"[a-z$]+", " ".join(str(w.text) for w in words).lower())
    rates = {"finance": palettes._rate(tokens, palettes.FINANCE), "tech": palettes._rate(tokens, palettes.TECH),
             "executive": palettes._rate(tokens, palettes.EXECUTIVE), "editorial": palettes._rate(tokens, palettes.EDITORIAL)}
    best = max(rates, key=lambda key: (rates[key], key == "finance"))
    return best if rates[best] > 0 else "editorial"


def masthead(words: list[Any]) -> str:
    name, section = _TOPICS[topic(words)]
    return f"{name} // {section}"


def thesis(words: list[Any], earliest: float, latest: float) -> tuple[list[Any], list[Any], float] | None:
    """(the thesis sentence's words, its key 4-6 words, score): the clip's strongest claim
    (a climax setup, stakes and contrast words), its key words the answer after the hinge
    or, without one, the sentence's last words. None when no sentence makes a claim."""
    from core import climax_hook as H

    tokens = [H._clean(w.text) for w in words]
    hinges = {hinge: weight for _first, hinge, weight, _s in H._hinges(words, tokens)}
    best = None
    k = 0
    while k < len(words):
        j = k
        while j < len(words) - 1 and not H._sentence_end(words, j):
            j += 1
        sentence = list(range(k, j + 1))
        k = j + 1
        if not 6 <= len(sentence) <= 22 or float(words[sentence[0]].start) < earliest - 4.0:
            continue
        own = [h for h in hinges if sentence[0] <= h < sentence[-1]]
        score = max((hinges[h] for h in own), default=0.0)
        score += min(2.0, 0.5 * sum(1 for i in sentence if tokens[i] in H.STAKES))
        score += 0.75 * sum(1 for i in sentence if tokens[i] in {"not", "never", "only", "instead", "actually", "always"})
        if quantitative([words[i] for i in sentence]):
            score += 0.5
        if own:
            after = [i for i in sentence if i > max(own)]
        else:
            after = sentence[-6:]
        key = _key_phrase(words, tokens, [i for i in after if tokens[i]])
        # the card (its key words, then the held highlight) clears the title and the payoff
        if len(key) < 3 or float(words[key[0]].start) - TEAR_LEAD < earliest \
                or float(words[key[-1]].end) + TEAR_TAIL > latest:
            continue
        if best is None or score > best[2]:
            best = ([words[i] for i in sentence], [words[i] for i in key], score)
    return best if best and best[2] >= 1.5 else None


def _key_phrase(words: list[Any], tokens: list[str], candidates: list[int]) -> list[int]:
    """The key words: 3-6 of ``candidates`` from the first, said within KEY_SECONDS, ending
    on a whole phrase: never on "being", "the" or "every" (the longest that ends on a
    comma or a full stop, else the longest that ends on a content word). [] when none does."""
    best: list[int] = []
    for size in range(min(6, len(candidates)), 2, -1):
        key = candidates[:size]
        if float(words[key[-1]].end) - float(words[key[0]].start) > KEY_SECONDS or tokens[key[-1]] in _DANGLING:
            continue
        if re.search(r"[,.;:?!][\"'”’)]*$", str(words[key[-1]].text)):
            return key
        best = best or key
    return best


def tearout_moment(words: list[Any], earliest: float, latest: float) -> Moment | None:
    from core.proof_cards import TEAR_SECONDS

    found = thesis(words, earliest, latest)
    if found is None:
        return None
    sentence, key, score = found
    first_key = sentence.index(key[0])
    keep_from = max(0, first_key + len(key) - 14)  # at most 14 words of headline, the key words in it
    shown = sentence[keep_from:first_key + len(key)]
    text = " ".join(str(w.text).strip("\"“”") for w in shown).rstrip(",;:")
    text = text[:1].upper() + text[1:]
    start = float(key[0].start) - TEAR_LEAD
    end = min(start + TEAR_SECONDS[1], max(start + TEAR_SECONDS[0], float(key[-1].end) + TEAR_TAIL))
    card = Card("newspaper_tearout", round(start, 3), round(end, 3), eyebrow=masthead(words),
                rows=[CardRow(text, reveal_at=round(start, 3))],
                context={"key": [[str(w.text).strip(".,;:?!\"“”"), float(w.start), float(w.end)] for w in key],
                         "key_from": len(shown) - len(key)})
    return Moment(card, 1, f"the thesis line (score {score:.1f}), its key words highlighted as said")


# ============================================================ the plan
def plan(words: list[Any], clip_start: float, clip_end: float, hook: float, taken: list[Card],
         lookup: Callable[[str], dict | None] | None = None, skip_names: set[str] | None = None) -> list[Moment]:
    """The proof cards for a clip, scheduled around the cards it already has."""
    from core.card_checks import PAYOFF_SECONDS

    lookup = wiki_lookup if lookup is None else lookup
    first, last = clip_start + max(hook, 3.0), clip_end - PAYOFF_SECONDS
    clip_words = [w for w in words if clip_start <= float(w.start) < clip_end]
    moments: list[Moment] = []
    tear = tearout_moment(clip_words, first, last)
    if tear:
        moments.append(tear)
    moments += brand_moments(clip_words, lookup)
    moments += wiki_moments(clip_words, lookup, skip_names)
    moments += stat_moments(clip_words)
    busy = [(c.start - (SPLIT_MARGIN if MODES.get(c.template) == "split" else 0) - PROOF_GAP,
             c.end + (SPLIT_MARGIN if MODES.get(c.template) == "split" else 0) + PROOF_GAP) for c in taken]
    budget = min(MAX_PROOFS, max(1, int((clip_end - clip_start) // SECONDS_PER_PROOF)))
    counts: dict[str, int] = {}
    chosen: list[Moment] = []
    ranked = sorted(moments, key=lambda m: (m.priority, m.card.start))
    # Variety first: one of each kind in priority order, then a second of a kind if there's room.
    firsts = [m for k, m in enumerate(ranked) if all(o.card.template != m.card.template for o in ranked[:k])]
    for moment in firsts + [m for m in ranked if m not in firsts]:
        card = moment.card
        if len(chosen) >= budget or counts.get(card.template, 0) >= PER_KIND[card.template] or moment in chosen:
            continue
        if card.start < first or card.end > last:
            continue
        if any(card.start < b and a < card.end for a, b in busy):
            continue
        if card.template == "stat_proof" and any(MODES.get(c.template) == "split" and c.start - 1 <= card.start <= c.end
                                                  for c in taken):
            continue
        chosen.append(moment)
        counts[card.template] = counts.get(card.template, 0) + 1
        busy.append((card.start - PROOF_GAP, card.end + PROOF_GAP))
    return sorted(chosen, key=lambda m: m.card.start)


def apply(direction: Any, words: list[Any], clip_start: float, clip_end: float, hook: float,
          lookup: Callable[[str], dict | None] | None = None) -> Any:
    """Round 41 on a graphics director's Direction: the gate, then the proof cards, each
    one checked like every other graphic. Mutates and returns ``direction``."""
    from core import entity_enricher
    from core.card_checks import check_card

    edit = direction.edit
    edit.cards, notes = gate_splits(edit.cards, words)
    direction.notes.extend(notes)
    guests = {c.rows[0].label for c in edit.cards if c.template == "speaker_title" and c.rows}
    try:
        moments = plan(words, clip_start, clip_end, hook, edit.cards, lookup, guests)
    except Exception as exc:  # noqa: BLE001 - proof cards are a bonus, never a failed clip
        logger.warning("Visual proof planning failed: %s", exc)
        direction.notes.append(f"Visual proof unavailable ({exc}).")
        return direction
    for moment in moments:
        card = moment.card
        if card.template == "brand_proof" and card.entity:
            try:
                entity_enricher.enrich(card.entity)  # its logo, cached for the render (a monogram without one)
            except Exception:  # noqa: BLE001
                pass
        result = check_card(card, words, clip_start, clip_end, hook)
        if result.ok:
            edit.cards.append(card)
            direction.notes.append(f"Added {card.template} at {card.start - clip_start:.1f} s: {moment.reason}.")
        else:
            direction.notes.append(f"Dropped {card.template}: {result.errors[0]}")
    edit.cards.sort(key=lambda c: c.start)
    splits = sum(1 for c in edit.cards if MODES.get(c.template) == "split")
    full = 1.0 - sum(c.end - c.start for c in edit.cards if MODES.get(c.template) == "split") / max(1e-6, clip_end - clip_start)
    direction.notes.append(f"Signature edit: {splits} split-screen card(s), the speaker full-screen {full:.0%} of the clip.")
    return direction
