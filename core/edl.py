"""Round 27: the edit list (EDL) for a clip with data cards.

A clip is still one range of the source video (``start``-``end``, source seconds). On
top of it, ``cards`` say when the screen switches from the full-screen speaker to the
split view (speaker on top, a data card below) and back. Every time here is in source
seconds, like the transcript's words, so a card's times line up with what was said.

Later rounds add more source ranges (pause trimming, stitch cuts) to the same file.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Graphics library (round 27). Three tiers, NYT short-form standard:
#   overlay: the speaker stays full-screen; type, wireframe boxes or a name card over the frame.
#   split:   real multi-point data only; speaker on top, an animated chart below.
# Round 32: two qualitative Bento cards (split) for clips with no chartable numbers: the
# speaker's contrast (comparison_card) or framework in order (step_sequence).
TEMPLATES = (
    "kinetic_range", "buckets", "stat_pill", "speaker_title",
    "timeline", "metric_bars", "ranked_bars", "diverging_bar", "share_split", "trend_line",
    "arr_bridge", "sankey_flow", "cac_payback", "drawdown_curve", "candlestick_ema",
    "comparison_card", "step_sequence", "market_line",
)
TEMPLATE_LABELS = {
    "kinetic_range": "A · Number or range over the speaker",
    "buckets": "B · 2–3 boxes over the speaker (concepts, steps, expected vs actual)",
    "stat_pill": "A · Micro-stat in the corner (one number, or a short trend of 2–4)",
    "speaker_title": "Name card (guest's name and role)",
    "timeline": "C · Timeline (2–4 points in time)",
    "metric_bars": "C · Breakdown bars (2–4 numbers, one unit)",
    "ranked_bars": "C · Ranked bars (2–5 numbers; focus moves as each is said)",
    "diverging_bar": "C · Two-way split (2 numbers)",
    "share_split": "C · Parts of a whole (3–5 shares)",
    "trend_line": "C · Trend over time (2–6 points)",
    "arr_bridge": "C · ARR bridge / waterfall (start, + and − steps, end)",
    "sankey_flow": "C · Flow of money (Sankey: revenue into costs and profit)",
    "cac_payback": "C · CAC payback (cost to win a customer, months to earn it back)",
    "drawdown_curve": "C · Fall and recovery (a crash, a dip and the rebound)",
    "candlestick_ema": "C · Price chart (from a price file you upload, with its source)",
    "comparison_card": "C · Comparison card (two sides of the speaker's contrast, in their words)",
    "step_sequence": "C · Step sequence (2–4 steps of the speaker's framework, in order)",
    "market_line": "C · Market data (a real price, rate or revenue series, with its source)",
}
MODES = {name: ("overlay" if name in {"kinetic_range", "buckets", "stat_pill", "speaker_title"} else "split")
         for name in TEMPLATES}


@dataclass
class CardRow:
    """One item of a card: a number and its label, a box, or a point on a timeline.
    ``detail``: a timeline point's 3–5 word description. ``dashed``: a box drawn dashed
    (a projection or expectation, next to solid ones for what actually happened).
    ``parent``: in a Sankey, the label of the row this one flows out of.
    ``highlight``: the accent color (the punchline). ``reveal_at`` / ``reveal_to``: when
    it appears / when a range's second number appears (source seconds; set by the checks
    from the words where they're said)."""

    label: str
    value: str = ""
    highlight: bool = False
    detail: str = ""
    dashed: bool = False
    parent: str = ""
    reveal_at: float | None = None
    reveal_to: float | None = None


@dataclass
class Card:
    template: str
    start: float
    end: float
    eyebrow: str = ""
    headline: str = ""
    rows: list[CardRow] = field(default_factory=list)
    footnote: str = ""
    source: str = ""  # only for charts drawn from a file (candlestick_ema): where the data is from
    data_path: str = ""  # that file (CSV: date, open, high, low, close, volume)
    # Name cards only: where it sits, "x,y,scale,align" in output pixels (align: center,
    # left or right; x is the left edge for left, the right edge for right). Empty: the
    # standard lower-middle spot.
    place: str = ""
    # Charts only (Round 30): a second, smaller layer under the chart that says what the
    # numbers mean, built only from what was said: {"kind": "ratio" | "share", "of": [1, 2],
    # "text": ...} (computed from two of the chart's own items) or {"kind": "earlier",
    # "value": ..., "text": ..., "said": the sentence, "at": seconds} (a number the speaker
    # said elsewhere in the same video). "value" is filled in by the checks. Empty: none.
    context: dict[str, Any] = field(default_factory=dict)
    # Round 35: the company (or market) the card is about, a core.entity_enricher key
    # ("nvidia", "stripe", "inflation"): its logo goes in the header and its colour is the
    # card's accent. Empty: the client's accent, no logo.
    entity: str = ""

    def __post_init__(self) -> None:
        if self.template not in TEMPLATES:
            raise ValueError(f"Unknown card template: {self.template!r}")
        self.rows = [row if isinstance(row, CardRow) else CardRow(**row) for row in self.rows]


@dataclass
class BrandKit:
    """The client's look. ``accent``: one color, for the active box, the punchline number
    and its bar. Cards carry no wordmark, logo or footnote (round 27 overhaul): the
    graphic is the whole message. ``wordmark``/``logo_path`` are kept for later uses."""

    wordmark: str = ""
    accent: str = "#F4C430"
    logo_path: str = ""
    show_name: str = ""


@dataclass
class ClipEdit:
    start: float
    end: float
    cards: list[Card] = field(default_factory=list)
    brand: BrandKit = field(default_factory=BrandKit)

    def __post_init__(self) -> None:
        self.cards = [card if isinstance(card, Card) else Card(**card) for card in self.cards]
        if isinstance(self.brand, dict):
            self.brand = BrandKit(**self.brand)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ClipEdit":
        data: dict[str, Any] = json.loads(text)
        return cls(**data)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "ClipEdit":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


BRAND_KIT_PATH = Path(__file__).resolve().parents[1] / "storage" / "brand_kit.json"


def load_brand_kit() -> BrandKit:
    """The brand on every card (storage/brand_kit.json; Nav Studio Lab until you set one)."""
    try:
        return BrandKit(**json.loads(BRAND_KIT_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return BrandKit()


def save_brand_kit(brand: BrandKit) -> None:
    BRAND_KIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    BRAND_KIT_PATH.write_text(json.dumps(asdict(brand), indent=2), encoding="utf-8")


def sentences(words: list[Any], max_gap: float = 1.2) -> list[tuple[float, float, str]]:
    """(start, end, text) for each sentence in ``words`` (ends at . ? ! or a pause), so a
    card can start and end on a sentence."""
    out: list[tuple[float, float, str]] = []
    current: list[Any] = []
    for word in words:
        if current and float(word.start) - float(current[-1].end) > max_gap:
            out.append((float(current[0].start), float(current[-1].end), " ".join(w.text for w in current)))
            current = []
        current.append(word)
        if str(word.text).rstrip("\"'”’)").endswith((".", "?", "!")):
            out.append((float(current[0].start), float(current[-1].end), " ".join(w.text for w in current)))
            current = []
    if current:
        out.append((float(current[0].start), float(current[-1].end), " ".join(w.text for w in current)))
    return out
