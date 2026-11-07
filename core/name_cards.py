"""Where and when each speaker's name card goes.

Round 33: a name card is only ever on the right face, while that person talks.
Who: core.name_cards.identify ties each named speaker to a face only when it's certain (a
self-introduction on camera, the face that keeps coming back on the channel for the host,
or screen time when it can't be anyone else). A speaker who can't be pinned to a face gets
no card: never the wrong name on someone.
When: once per speaker (and once per face), on their first verified talking moment after
the title: the camera on them alone (or their own panel of a split while they do most of
the talking), their words running for 1.75 s and their mouth moving, from 3.5 s in. There
is no "right after the title, whoever is on screen" fallback any more.
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
from core.card_checks import MIN_GAP, NAME_GAP, PAYOFF_SECONDS
from core.cards import NAME_CARD_TOP, STAT_PILL_SIZE, STAT_PILL_TOP, name_card_size
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
    source_face: tuple[float, float, float] | None = None  # the face in source pixels (cx, cy, w): its mouth


def place(name: str, role: str, option: Option, size: tuple[int, int] | None = None,
          corner: bool = False) -> str | None:
    """Card.place for this option: the spot closest to ``prefer`` that keeps off the
    face (eyebrows to chin) and the captions, or None when there is no such spot.
    ``size``: the card's size (default: a name card for name and role); ``corner``: a
    side spot beats the centre (a micro-stat sits in a corner)."""
    width, height = size or name_card_size(name, role, option.scale)
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
            side_cost = (0.35 if align == "center" else 0) if corner else (0 if align == "center" else 0.35)
            cost = abs(y - option.prefer) / 400 + 3 * _overlap(box, head) / (width * height) + side_cost
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
        elif not shot.group and shot.has_face and not getattr(shot, "content", False):  # never on a shared page
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
            found.append(Option(a, b, "solo", face, FULL_REGION, 1.0, NAME_CARD_TOP, source_face=(cx, cy, fw)))
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


STAT_REGION = (150, 1100)  # a micro-stat stays in the top part, clear of the captions
STAT_DEFAULT = f"56,{STAT_PILL_TOP},1.00,left"


def stat_place(source: str | Path, plan: Any, clip_start: float, clip_end: float, start: float, end: float) -> str:
    """Where a micro-stat goes (Card.place): a corner of the top part clear of whoever is
    on screen (from their face while it's up), top left when nothing is in the way or the
    faces can't be read."""
    try:
        split = plan.split_view(clip_start, clip_end)
        middle = (start + end) / 2
        if split and any(a <= middle - clip_start < b for a, b, *_ in split.windows):
            return STAT_DEFAULT  # a two-person split: the top corner sits above the top panel's head
        shot = next((s for s in plan.shots if s.start <= middle < s.end), None)
        if shot is None or shot.group or not shot.has_face or getattr(shot, "content", False):
            return STAT_DEFAULT
        faces = [f for faces in S.faces_at(source, [start + 0.3, middle], plan.frame_width) for f in faces]
        if not faces:
            return STAT_DEFAULT
        scale = FRAME[1] / plan.crop_height
        biggest = max(faces, key=lambda f: f.w)
        face = ((biggest.cx - shot.x) * scale, (biggest.cy - plan.y) * scale, biggest.w * scale)
        option = Option(start, end, "solo", face, STAT_REGION, 1.0, STAT_PILL_TOP)
        return place("", "", option, size=STAT_PILL_SIZE, corner=True) or STAT_DEFAULT
    except Exception as exc:  # noqa: BLE001 - the default corner is fine
        logger.warning("Micro-stat placement failed: %s", exc)
        return STAT_DEFAULT


# --- Round 33: a name card only on the right face, while that person talks ---------------
NAME_DELAY = 0.2  # a name card comes in at least this long after the title card clears
EARLIEST = 3.5  # ...and never before this many seconds into the clip
SPEAKING_RUN = 1.75  # seconds of the speaker's own talking the card waits for (1.5-2.0 s)
MIN_SPEECH = 1.5  # ...of which at least this much is words (not a pause)
_IDENTIFIED: dict[tuple, tuple[dict, list[str]]] = {}
PANEL_TALKING = 0.6  # in a split, the speaker does at least this share of the mouth movement


def _free(t: float, taken: list[Card]) -> bool:
    """[t, t + NAME_CARD_SECONDS] is clear of every graphic (NAME_GAP from another name card,
    MIN_GAP from a Bento visual)."""
    for card in taken:
        gap = NAME_GAP if card.template == "speaker_title" else MIN_GAP
        if t < card.end + gap and card.start < t + NAME_CARD_SECONDS + gap:
            return False
    return True


def _speech(words: list[Any], t0: float, t1: float) -> float:
    """Seconds of words said in [t0, t1]."""
    return sum(max(0.0, min(float(w.end), t1) - max(float(w.start), t0)) for w in words)


def _starts_talking(words: list[Any], t: float) -> bool:
    """Someone is talking from t: a word under way at t, or starting within 0.35 s."""
    return any(float(w.start) <= t + 0.35 and float(w.end) > t for w in words)


def _speaking_meter(source: str | Path, option: Option) -> Callable[[float, float], float | None]:
    """share(t0, t1): how much of [t0, t1] the face of this solo shot is talking
    (core.speaker_id: its mouth against its forehead), measured once per shot; None when
    it can't be measured."""
    cache: list[np.ndarray | None] = []

    def share(t0: float, t1: float) -> float | None:
        if option.source_face is None:
            return None
        if not cache:
            cx, cy, w = option.source_face
            cache.append(S.mouth_activity(source, option.start, option.end, [(cx, cy, w), (cx, cy - 0.78 * w, w)]))
        activity = cache[0]
        if activity is None:
            return None
        lo, hi = int((t0 - option.start) * S.MOUTH_FPS), int((t1 - option.start) * S.MOUTH_FPS)
        part = activity[max(0, lo):max(lo + 2, hi)]
        if part.shape[0] < 4:
            return None
        return float(np.mean(part[:, 0] > S.SPEAKING_RATIO * (part[:, 1] + 0.5)))

    return share


def identify(source: str | Path, plan: Any, speakers: list[Any], video_words: list[Any] | None = None,
             channel: str = "", video_key: str = "") -> tuple[dict[str, tuple[tuple[float, ...], str]], list[str]]:
    """Which face is which named speaker: {name: (face feature, how we know)}. Only what's
    certain: a speaker who introduces themself on camera ("I'm Steve Eisman"); the host as
    the one face that comes back across the channel's videos; and screen time only when
    it can't be anyone else (one person in the video, or one guest who is clearly on screen
    most). Anyone else gets no card: better none than the wrong name on a face."""
    duration = S.video_duration(source)
    people = S.solo_people(source, duration, plan.frame_width) if duration else []
    notes: list[str] = []
    ids: dict[str, tuple[tuple[float, ...], str]] = {}

    def claimed(face: tuple[float, ...]) -> bool:
        return any(S.similarity(face, other) >= S.SAME_PERSON for other, _how in ids.values())

    for name, at in S.introductions(video_words or [], [s.name for s in speakers]):
        if name not in ids:
            face = S.face_while(source, at, plan.frame_width)
            if face is not None and not claimed(face):
                ids[name] = (face, f"introduces themself at {int(at // 60)}:{int(at % 60):02d}")
    recurring = S.recurring_faces(channel, video_key, people) if channel else []
    S.remember_people(channel, video_key, people)
    hosts = [s for s in speakers if s.kind == "host" and s.name not in ids]
    returning = [face for face in recurring if not claimed(face)]
    if len(hosts) == 1 and len(returning) == 1:
        ids[hosts[0].name] = (returning[0], "the face that comes back on the channel")
    free = [(face, seen) for face, seen in people if not claimed(face)
            and not any(S.similarity(face, r) >= S.SAME_PERSON for r in recurring)]
    guests = [s for s in speakers if s.kind != "host" and s.name not in ids]
    if len(people) == 1 and len(speakers) == 1 and free:
        ids[speakers[0].name] = (free[0][0], "the only person in the video")
    elif len(guests) == 1 and free:
        top, runner_up = free[0][1], (free[1][1] if len(free) > 1 else 0)
        if top >= 4 and top >= 1.5 * runner_up:
            ids[guests[0].name] = (free[0][0], "the guest: alone on screen most, by far")
            rest = [(face, seen) for face, seen in free[1:] if seen >= 3]
            hosts = [s for s in speakers if s.kind == "host" and s.name not in ids]
            if len(hosts) == 1 and len(speakers) == 2 and len(rest) == 1:
                ids[hosts[0].name] = (rest[0][0], "the host: the only other person alone on screen")
    for speaker in speakers:
        if speaker.name not in ids:
            notes.append(f"No name card for {speaker.name}: can't tell which face is theirs for sure "
                         "(no self-introduction, not a face the channel keeps coming back to, and screen "
                         "time doesn't single them out).")
    return ids, notes


def plan_name_cards(source: str | Path, plan: Any, clip_start: float, clip_end: float, hook: float,
                    taken: list[Card], speakers: list[Any], words: list[Any] | None = None,
                    video_words: list[Any] | None = None, channel: str = "") -> tuple[list[Card], list[str]]:
    """Round 33: one name card per identified speaker, and only where it's right. It comes in
    once, on the speaker's first verified talking moment after the title (from EARLIEST s):
    the camera on them alone (or their own panel of a split, while they do most of the
    talking), their voice running for SPEAKING_RUN s (the transcript's words) and their
    mouth moving (core.speaker_id). Never on another person's face, never twice for one
    speaker or one face, never over a listener. ``words``: the clip's words (source
    seconds); ``video_words``: the whole video's (for self-introductions)."""
    key = (str(source), channel, tuple((sp.name, sp.kind) for sp in speakers))
    if key not in _IDENTIFIED:  # once per video: a re-plan around a chart doesn't look again
        _IDENTIFIED[key] = identify(source, plan, speakers, video_words, channel, Path(str(source)).stem)
    ids, notes = _IDENTIFIED[key][0], list(_IDENTIFIED[key][1])
    earliest = clip_start + max(hook + NAME_DELAY, EARLIEST)
    latest = clip_end - PAYOFF_SECONDS
    spoken = [w for w in (words or []) if float(w.end) > clip_start and float(w.start) < clip_end]
    placed: list[Card] = []
    faces_used: list[tuple[float, ...]] = []
    for speaker in sorted(speakers, key=lambda s: s.kind == "host"):  # the guest first
        if speaker.name not in ids:
            continue
        face, how = ids[speaker.name]
        if any(S.similarity(face, other) >= S.SAME_PERSON for other in faces_used):
            notes.append(f"No name card for {speaker.name}: that face already has a name card in this clip.")
            continue
        card, why = None, "they're never on screen alone while talking in this clip"
        for option in sorted(options(source, plan, clip_start, clip_end, face), key=lambda o: o.start):
            meter = _speaking_meter(source, option) if option.kind == "solo" else None
            t = max(option.start + 0.3, earliest)
            last = min(option.end - 0.2 - NAME_CARD_SECONDS, latest - NAME_CARD_SECONDS)
            while t <= last + 1e-9 and card is None:
                ok = (_free(t, taken + placed) and _starts_talking(spoken, t)
                      and _speech(spoken, t, t + SPEAKING_RUN) >= MIN_SPEECH)
                if ok and option.kind == "solo":
                    share = meter(t, t + SPEAKING_RUN) if meter else None
                    opening = meter(t, t + 0.6) if meter else None  # ...and talking as the card comes in
                    ok = share is None or (share >= S.SPEAKING_SHARE and (opening is None or opening >= S.SPEAKING_SHARE))
                elif ok:
                    ok = option.talking is not None and option.talking(t, t + SPEAKING_RUN) >= PANEL_TALKING
                spot = place(speaker.name, speaker.role, option) if ok else None
                if spot:
                    card = Card("speaker_title", start=round(t, 3), end=round(t + NAME_CARD_SECONDS, 3),
                                rows=[CardRow(speaker.name, detail=speaker.role)], place=spot)
                    why = "on their solo shot" if option.kind == "solo" else "in their panel of the split"
                t += 0.25
            if card is not None:
                break
        if card is None:
            notes.append(f"No name card for {speaker.name}: {why}.")
            continue
        placed.append(card)
        faces_used.append(face)
        notes.append(f"Added {speaker.name}'s name card at {card.start - clip_start:.1f} s, {why}, while they talk "
                     f"(who: {how}).")
    return sorted(placed, key=lambda c: c.start), notes
