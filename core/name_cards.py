"""Where and when the guest's name card goes (Round 29).

Every clip gets the guest's name card (when the guest's name is in the video's own title
or description). It never counts toward the clip's graphics limit and never overlaps a
data graphic.

Who: the guest's face (core.speaker_id): the card only goes where that face is.
When: ideally the guest's first solo shot after the title. When the clip opens on a
two-person split (or has no early solo shot), the card goes inside the guest's own panel
while they're talking (mouth movement), compact.
Where: around the face, never on it. The eyes-to-chin area is off limits; so are the
captions (the calm line during graphics, or the seam between split panels) and the
bottom strip the apps cover. The spot nearest the usual lower-middle wins.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from core import speaker_id as S
from core.card_checks import MIN_GAP, PAYOFF_SECONDS
from core.cards import NAME_CARD_TOP, name_card_size
from core.edl import Card, CardRow

logger = logging.getLogger(__name__)

NAME_CARD_SECONDS = 3.3
EARLY_SOLO = 12.0  # a solo shot this soon after the title beats an earlier split panel
FRAME = (1080, 1920)
FULL_REGION = (150, 1700)  # the card stays inside this band (apps cover the bottom strip)
CALM_CAPTIONS = (1355, 1460)  # the calm caption line during graphics (full screen)
TOP_PANEL = (130, 852)  # split view: the top panel, above the captions on the seam
BOTTOM_PANEL = (1068, 1700)  # the bottom panel, below them
PANEL_SCALE = 0.78  # a card inside a panel is compact
# The face, from its centre, in face widths: off limits from the eyebrows to below the
# chin; the hair above it may be covered a little, at a cost.
FACE_SIDE, FACE_UP, FACE_DOWN, HEAD_UP = 0.6, 0.45, 0.9, 1.05


@dataclass
class Option:
    """A shot (or panel) where the guest is on screen."""

    start: float  # source seconds
    end: float
    kind: str  # "solo" or "panel"
    face: tuple[float, float, float]  # the guest's face in output pixels (centre x, centre y, width)
    region: tuple[int, int]
    scale: float
    prefer: float  # the card's preferred top edge
    talking: Callable[[float, float], float] | None = None  # share of the talking that's the guest's


def place(name: str, role: str, option: Option) -> str | None:
    """Card.place for this option: the spot closest to ``prefer`` that keeps off the
    face (eyebrows to chin) and the captions, or None when there is no such spot."""
    width, height = name_card_size(name, role, option.scale)
    cx, cy, fw = option.face
    core = (cx - FACE_SIDE * fw, cy - FACE_UP * fw, cx + FACE_SIDE * fw, cy + FACE_DOWN * fw)
    head = (cx - FACE_SIDE * fw, cy - HEAD_UP * fw, cx + FACE_SIDE * fw, cy - FACE_UP * fw)
    best: tuple[float, str] | None = None
    top, bottom = option.region
    candidates = sorted(set(range(int(top), int(bottom - height) + 1, 6)) | {int(option.prefer)})
    for y in (y for y in candidates if top <= y <= bottom - height):
        if option.kind == "solo" and y < CALM_CAPTIONS[1] and y + height > CALM_CAPTIONS[0]:
            continue
        for align, x in (("center", FRAME[0] / 2), ("left", 40), ("right", FRAME[0] - 40)):
            left = x - width / 2 if align == "center" else x if align == "left" else x - width
            box = (left, y, left + width, y + height)
            if _overlap(box, core) > 0:
                continue
            cost = (abs(y - option.prefer) / 400 + 3 * _overlap(box, head) / (width * height)
                    + (0 if align == "center" else 0.35))
            if best is None or cost < best[0]:
                best = (cost, f"{x:.0f},{y},{option.scale:.2f},{align}")
    return best[1] if best else None


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def options(source: str | Path, plan: Any, clip_start: float, clip_end: float, guest: tuple[float, ...] | None,
            fallback_x: float | None = None) -> list[Option]:
    """Every shot or panel of this clip where the guest is on screen, with their face.
    ``guest``: the guest's face feature; None when faces can't be matched (then the
    person on screen most, ``fallback_x``, counts as the guest, on solo shots only)."""
    split = plan.split_view(clip_start, clip_end)
    windows = list(split.windows) if split else []
    shown_whole = plan.full_frame_ranges(clip_start, clip_end)
    found: list[Option] = []
    for shot in plan.shots:
        a, b = max(shot.start, clip_start), min(shot.end, clip_end)
        if b - a < NAME_CARD_SECONDS + 0.5:
            continue
        window = next((w for w in windows if w[0] - 0.05 <= a - clip_start and b - clip_start <= w[1] + 0.05), None)
        if any(s - 0.05 <= a - clip_start and b - clip_start <= e + 0.05 for s, e in shown_whole) and window is None:
            continue  # shown as the whole frame (a wide group shot): no clear spot for one person
        times = [a + (b - a) * share for share in (0.3, 0.7)]
        if window is not None and shot.speakers:
            if guest is None:
                continue
            seen = [S.best_match(faces, guest) for faces in S.faces_at(source, times, plan.frame_width)]
            hits = [face for face in seen if face is not None]
            if not hits:
                continue
            (lx, ly, lw), (rx, ry, rw) = shot.speakers
            on_left = np.median([face.cx for face in hits]) < (lx + rx) / 2
            scale = FRAME[0] / split.width
            if on_left:
                face = ((lx - window[2]) * scale, (ly - window[3]) * scale, lw * scale)
                region, prefer = TOP_PANEL, TOP_PANEL[1] - 150
            else:
                face = ((rx - window[4]) * scale, FRAME[1] / 2 + (ry - window[5]) * scale, rw * scale)
                region, prefer = BOTTOM_PANEL, BOTTOM_PANEL[0]
            pair = [(lx, ly, lw), (rx, ry, rw)]
            found.append(Option(a, b, "panel", face, region, PANEL_SCALE, prefer,
                                _talking(lambda a=a, b=b, pair=pair: S.mouth_activity(source, a, b, pair), a,
                                         0 if on_left else 1)))
        elif not shot.group and shot.has_face:
            faces_now = S.faces_at(source, times, plan.frame_width)
            if guest is not None:
                hits = [face for face in (S.best_match(faces, guest) for faces in faces_now) if face is not None]
            else:
                hits = [max(faces, key=lambda f: f.w) for faces in faces_now if faces]
                if fallback_x is None or not hits or abs(np.median([h.cx for h in hits]) - fallback_x) > plan.frame_width * 0.12:
                    hits = []
            if not hits:
                continue
            scale = FRAME[1] / plan.crop_height
            cx, cy, fw = (float(np.median([getattr(h, k) for h in hits])) for k in ("cx", "cy", "w"))
            face = ((cx - shot.x) * scale, (cy - plan.y) * scale, fw * scale)
            found.append(Option(a, b, "solo", face, FULL_REGION, 1.0, NAME_CARD_TOP))
    return found


def _talking(measure: Callable[[], np.ndarray | None], start: float, which: int) -> Callable[[float, float], float]:
    """Share of the mouth movement in [t0, t1] that's the guest's (0.5 when unknown).
    The movement is measured on first use only (panels are the fallback)."""
    cache: list[np.ndarray | None] = []

    def share(t0: float, t1: float) -> float:
        if not cache:
            cache.append(measure())
        activity = cache[0]
        if activity is None:
            return 0.5
        lo, hi = int((t0 - start) * S.MOUTH_FPS), int((t1 - start) * S.MOUTH_FPS)
        part = activity[max(0, lo):max(lo + 1, hi)]
        if part.size == 0:
            return 0.5
        total = float(part.sum())
        return float(part[:, which].sum()) / total if total > 0 else 0.5

    return share


def slot(option: Option, taken: list[Card], earliest: float, latest: float) -> float | None:
    """When the card goes in this shot: the earliest time clear of the other graphics;
    in a panel, the earliest such time the guest is doing most of the talking (or the
    time they talk most, if never mostly)."""
    first = max(option.start + 0.3, earliest)
    last = min(option.end - 0.2 - NAME_CARD_SECONDS, latest - NAME_CARD_SECONDS)
    free = []
    t = first
    while t <= last + 1e-9:
        if not any(t < c.end + MIN_GAP and c.start < t + NAME_CARD_SECONDS + MIN_GAP for c in taken):
            free.append(round(t, 3))
        t += 0.25
    if not free:
        return None
    if option.talking is None:
        return free[0]
    scored = [(option.talking(t, t + NAME_CARD_SECONDS), t) for t in free]
    talking = [t for share, t in scored if share >= 0.6]
    return talking[0] if talking else max(scored)[1]


def _most_on_screen(plan: Any, clip_start: float, clip_end: float) -> float | None:
    """Where (face centre x, source pixels) the person on screen most in solo shots sits:
    the guest, when faces can't be matched."""
    totals: dict[int, float] = {}
    for shot in plan.shots:
        if shot.has_face and not shot.group and shot.end > clip_start and shot.start < clip_end:
            key = round((shot.x + plan.crop_width / 2) / (plan.frame_width * 0.12))
            totals[key] = totals.get(key, 0.0) + min(shot.end, clip_end) - max(shot.start, clip_start)
    if not totals:
        return None
    return max(totals, key=totals.get) * plan.frame_width * 0.12


def name_card(
    source: str | Path, plan: Any, clip_start: float, clip_end: float, hook: float, taken: list[Card],
    name: str, role: str, fallback_x: float | None = None,
) -> tuple[Card | None, str]:
    """The guest's name card for this clip, and a note on where it went (or why not)."""
    duration = S.video_duration(source)
    guest = S.guest_face(source, duration, plan.frame_width) if duration else None
    if guest is None and fallback_x is None:
        fallback_x = _most_on_screen(plan, clip_start, clip_end)
    found = options(source, plan, clip_start, clip_end, guest, fallback_x)
    earliest, latest = clip_start + hook + 0.2, clip_end - PAYOFF_SECONDS
    timed = []
    # Solo shots first: an early one wins without measuring who talks in the panels.
    for option in sorted(found, key=lambda o: o.kind != "solo"):
        if option.kind == "panel" and any(k == "solo" and w <= earliest + EARLY_SOLO for w, o, _s in timed for k in [o.kind]):
            break
        when = slot(option, taken, earliest, latest)
        spot = place(name, role, option) if when is not None else None
        if when is not None and spot is not None:
            timed.append((when, option, spot))
    if not timed:
        if not found:
            who = "the guest" if guest is not None else "anyone"
            return None, f"no shot of {who} long enough for it"
        return None, "no free moment while the guest is on screen"
    solos = [item for item in timed if item[1].kind == "solo" and item[0] <= earliest + EARLY_SOLO]
    when, option, spot = min(solos or timed, key=lambda item: item[0])
    card = Card("speaker_title", start=when, end=when + NAME_CARD_SECONDS, rows=[CardRow(name, detail=role)], place=spot)
    where = "on their solo shot" if option.kind == "solo" else "in their panel of the split, while they talk"
    return card, where
