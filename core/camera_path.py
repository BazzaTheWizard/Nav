"""Round 42: the flowing camera, one continuous path for the whole clip.

Round 41's emphasis punch-ins jumped to 1.10x in 0.22 s and dropped back in a single
frame, about every 7-8 s (measured on two real clips: every zoom-out was a one-frame
0.907x step). That reads as a machine, not an editor. Now the camera moves like an
operator on a slider:

- a push-in eases in AND out (smootherstep, 1.1-1.5 s), so it starts softly, pulls the
  viewer in, and settles; while it holds it keeps creeping in very slowly (+1-1.5 %), so
  the frame never sits dead; then it releases over 2.0-2.6 s, just as softly. No hard cut,
  except where a real camera cut already changes the picture (the release is hidden in
  the cut, the way an editor cuts on the cut).
- few moves, each for a reason: the hook (from the very first frame), the payoff of the
  cold open's open loop (the answer, when it finally comes), the thesis line, and only
  then the speaker's strongest emphasis (loudest 10 % of words). At least NEUTRAL_GAP
  seconds of a still frame between moves, about one move every SECONDS_PER_MOVE, and the
  camera moved at most MAX_MOVING_SHARE of the clip.
- never under a split-screen card, a newspaper tear-out or a two-person split view; never
  on slides (webinar, explainer); a push never starts so close to a camera cut that the
  cut would interrupt it.

zoom_at() is the curve; path_filter() draws exactly that curve with FFmpeg's sub-pixel
perspective warp, frame by frame, on the finished picture (under the title card and the
captions, so text never zooms). CAMERA_FLOW=0 goes back to Round 41's punch-ins.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any

HOOK_AMOUNT = 0.045  # the hook's push: 1.00x -> 1.045x from frame 0
HOOK_PUSH = 2.8  # eased out: it is already moving on the first frame
HOOK_HOLD_UNTIL = 4.2
HOOK_RELEASE = 2.4
AMOUNT = 0.06  # an emphasis push: 1.00x -> 1.06x
PAYOFF_AMOUNT = 0.08  # the open loop's answer: the strongest push of the clip
PUSH = 1.3
CREEP = 0.012  # extra zoom over a hold: the frame never sits dead
HOLD = (1.4, 2.8)
RELEASE = 2.3
NEUTRAL_GAP = 6.0  # seconds of a still frame between two moves
SECONDS_PER_MOVE = 15.0
MAX_MOVING_SHARE = 0.45
CUT_GUARD = 1.0  # a push and the first second of its hold never cross a camera cut
TEASER_FROM, TEASER_TO = 0.04, 0.10  # the cold open drifts in 1.04x -> 1.10x
TOP_SHARE = 0.10
STANDOUT_DB = 3.0  # an emphasis word is at least this much louder than the clip's median word


OPENING_DRIFT = 0.03  # Round 48: the executive camera's one move, 1.00x -> 1.03x over the first seconds
OPENING_SECONDS = 3.0


def steady(settings: Any = None) -> bool:
    """Round 48: in the B2B modes the camera holds still. No emphasis, payoff or thesis pushes
    (zooming in and out every few seconds read as the frame breathing, worst on a letterboxed
    wide shot); the cuts between the two-shot and the close-ups (core.speaker_director) are the
    camera work. At most one opening drift (opening_drift) or the cold open's own drift.
    EXEC_CAMERA=flow brings back Round 42's flowing camera."""
    from core import executive

    return executive.b2b_clean_mode(settings) and os.getenv("EXEC_CAMERA", "steady").strip().lower() != "flow"


def opening_drift(duration: float, blocked: list[tuple[float, float]] | None = None) -> list[Move]:
    """The executive camera's only move: a slow drift in over the opening, then held to the
    end (never released, so it never breathes back out). None when the clip opens on a
    two-shot or close-up switch (those cuts are the opening's movement)."""
    if duration < OPENING_SECONDS + 2.0 or any(a < OPENING_SECONDS + 0.5 for a, _b in blocked or []):
        return []
    return [Move(0.0, OPENING_SECONDS, duration + 10.0, 0.0, OPENING_DRIFT, kind="opening", creep=0.0,
                 ease_out_only=True)]


def enabled() -> bool:
    """The flowing camera: on with Round 41's signature edit, unless CAMERA_FLOW=0."""
    from core import visual_proof

    return visual_proof.enabled() and os.getenv("CAMERA_FLOW", "1").strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Move:
    """One camera move (clip seconds): eases from ``base`` to ``base + amount`` over
    [start, start + push], creeps by ``creep`` until ``hold_end``, releases to 1.0x over
    ``release`` s. ``cut``: a camera cut that ends the move early (hidden in the cut)."""

    start: float
    push: float
    hold_end: float
    release: float
    amount: float
    kind: str = "emphasis"
    creep: float = CREEP
    base: float = 0.0
    cut: float | None = None
    ease_out_only: bool = False  # the hook: moving from the very first frame

    @property
    def end(self) -> float:
        full = self.hold_end + self.release
        return min(full, self.cut) if self.cut is not None else full


def smootherstep(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * u * (u * (6 * u - 15) + 10)


def ease_out(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return 1 - (1 - u) ** 3


def move_zoom(t: float, m: Move) -> float:
    """The extra zoom (0 = 1.00x) a move adds at clip second t."""
    if t < m.start or t >= m.end:
        return 0.0
    top = m.start + m.push
    if t < top:
        u = (t - m.start) / m.push
        return m.base + m.amount * (ease_out(u) if m.ease_out_only else smootherstep(u))
    held = m.base + m.amount
    if t < m.hold_end:
        return held + m.creep * (t - top) / max(1e-6, m.hold_end - top)
    return (held + m.creep) * (1 - smootherstep((t - m.hold_end) / m.release))


def zoom_at(t: float, moves: list[Move]) -> float:
    return 1.0 + sum(move_zoom(t, m) for m in moves)


def _sentence_end(words: list[Any], k: int) -> bool:
    if re.search(r"[.?!][\"'”’)]*$", str(words[k].text)):
        return True
    return k + 1 < len(words) and float(words[k + 1].start) - float(words[k].end) >= 0.5


def _hold_end(words: list[Any], k: int, top: float) -> float:
    """Hold until the sentence of word k ends (between HOLD[0] and HOLD[1] after the push)."""
    for j in range(k, len(words)):
        end = float(words[j].end)
        if end - top > HOLD[1]:
            break
        if end - top >= HOLD[0] and _sentence_end(words, j):
            return end
    return top + (HOLD[0] + HOLD[1]) / 2


def hook_move(duration: float, cuts: list[float], blocked: list[tuple[float, float]]) -> Move | None:
    """The hook's push from frame 0 (or from the first cut, if the opening shot is a flash)."""
    start = 0.0
    first = min((c for c in cuts if c > 0), default=None)
    if first is not None and first < 1.2:
        start = first
    move = Move(start, HOOK_PUSH, start + HOOK_HOLD_UNTIL, HOOK_RELEASE, HOOK_AMOUNT, kind="hook", creep=0.008,
                ease_out_only=True)
    later = [c for c in cuts if c > start + 0.05]
    if later and later[0] < move.end:
        if later[0] < start + 1.4:
            return None
        move = Move(move.start, move.push, move.hold_end, move.release, move.amount, "hook", move.creep, cut=later[0],
                    ease_out_only=True)
    if any(a < move.end and move.start < b for a, b in blocked) or move.end > duration - 0.5:
        return None
    return move


def plan(words: list[Any], loudness: list[float] | None, duration: float, *, cuts: list[float] | None = None,
         blocked: list[tuple[float, float]] | None = None, payoff_at: float | None = None,
         thesis_at: float | None = None, hook: bool = True) -> list[Move]:
    """The clip's camera moves (clip seconds): the hook, then by meaning (the open loop's
    payoff, the thesis line), then by emphasis (the loudest 10 % of words); spaced
    NEUTRAL_GAP apart, about one every SECONDS_PER_MOVE, never across ``blocked`` spans
    (split cards, tear-outs, two-person splits) or a camera cut mid-push."""
    cuts = sorted(c for c in (cuts or []) if 0 < c < duration)
    blocked = list(blocked or [])
    moves: list[Move] = []
    if hook:
        first = hook_move(duration, cuts, blocked)
        if first:
            moves.append(first)
    seeds: list[tuple[float, float, str]] = []  # (score, time, kind)
    if payoff_at is not None:
        seeds.append((10.0, payoff_at, "payoff"))
    if thesis_at is not None:
        seeds.append((8.0, thesis_at, "thesis"))
    if words and loudness:
        usable = [(k, db) for k, db in enumerate(loudness) if float(words[k].end) - float(words[k].start) >= 0.12]
        if usable:
            ordered = sorted(db for _k, db in usable)
            cut = ordered[min(len(ordered) - 1, int(math.floor(len(ordered) * (1 - TOP_SHARE))))]
            floor = max(cut, ordered[len(ordered) // 2] + STANDOUT_DB)  # it really stands out: never a flat clip's every word
            seeds += [(5.0 + min(2.5, (db - floor) / 6.0), float(words[k].start), "emphasis") for k, db in usable if db >= floor]
    budget = max(1, int(duration // SECONDS_PER_MOVE))
    moving = sum(m.end - m.start for m in moves)
    for _score, at, kind in sorted(seeds, key=lambda s: (-s[0], s[1])):
        if sum(1 for m in moves if m.kind != "hook") >= budget:
            break
        k = min(range(len(words)), key=lambda i: abs(float(words[i].start) - at)) if words else None
        amount = PAYOFF_AMOUNT if kind == "payoff" else AMOUNT
        start = at - PUSH + 0.1  # the push has landed as the word is said
        top = start + PUSH
        hold_end = _hold_end(words, k, top) if k is not None else top + 2.0
        move = Move(round(start, 3), PUSH, round(hold_end, 3), RELEASE, amount, kind=kind)
        crossing = [c for c in cuts if move.start - 0.2 < c < top + CUT_GUARD]
        if crossing or move.start < 1.0:
            continue
        later = [c for c in cuts if top + CUT_GUARD <= c < move.end]
        if later:
            move = Move(move.start, move.push, move.hold_end, move.release, move.amount, kind, cut=later[0])
        if move.end > duration - 0.6:
            continue
        if any(move.start < b + 0.5 and a - 0.5 < move.end for a, b in blocked):
            continue
        if any(move.start < m.end + NEUTRAL_GAP and m.start - NEUTRAL_GAP < move.end for m in moves):
            continue
        if moving + (move.end - move.start) > MAX_MOVING_SHARE * duration:
            continue
        moves.append(move)
        moving += move.end - move.start
    return sorted(moves, key=lambda m: m.start)


def teaser_move(seconds: float) -> Move:
    """The cold open: already at 1.04x on its first frame, drifting in to 1.10x as it
    builds to the cut (the flash then lands on the clip at 1.00x)."""
    return Move(0.0, seconds, seconds + 10, 0.0, TEASER_TO - TEASER_FROM, kind="teaser", creep=0.0, base=TEASER_FROM)


# ------------------------------------------------------------------ the FFmpeg curve
def _expr(m: Move, rate: float) -> str:
    """The move's extra zoom as an FFmpeg expression in the frame number ``in``."""
    fa, ft = m.start * rate, (m.start + m.push) * rate
    fh = m.hold_end * rate
    fe = m.end * rate
    nr = max(1.0, m.release * rate)
    push_u = f"min(1\\,max(0\\,(in-{fa:.3f})/{max(1.0, ft - fa):.3f}))"
    ease = (f"(1-pow(1-{push_u}\\,3))" if m.ease_out_only
            else f"(pow({push_u}\\,3)*({push_u}*(6*{push_u}-15)+10))")
    rel_u = f"min(1\\,max(0\\,(in-{fh:.3f})/{nr:.3f}))"
    smooth_rel = f"(pow({rel_u}\\,3)*({rel_u}*(6*{rel_u}-15)+10))"
    held = m.base + m.amount
    terms = [f"between(in\\,{fa:.3f}\\,{ft:.3f})*({m.base:.5f}+{m.amount:.5f}*{ease})"]
    if fh > ft:
        hold_to = min(fh, fe)  # a camera cut during the hold ends the move there
        terms.append(f"gt(in\\,{ft:.3f})*lt(in\\,{hold_to:.3f})*({held:.5f}+{m.creep:.5f}*(in-{ft:.3f})/{max(1.0, fh - ft):.3f})")
    if m.release > 0:
        terms.append(f"gte(in\\,{fh:.3f})*lt(in\\,{fe:.3f})*({held + m.creep:.5f}*(1-{smooth_rel}))")
    return "+".join(terms)


def path_filter(filter_complex: str, moves: list[Move], rate: float, center_y: float) -> str:
    """The moves on the finished picture ([base], 1080x1920): one perspective warp whose
    zoom is zoom_at(n / rate) on every frame n."""
    if not moves or filter_complex.count("[base]") != 1:
        return filter_complex
    zoom = f"(1+{'+'.join(f'({_expr(m, rate)})' for m in moves)})"
    cx, cy = "W/2", f"H*{center_y}"
    left, top = f"{cx}-({cx})/{zoom}", f"{cy}-({cy})/{zoom}"
    right, bottom = f"{cx}+(W-{cx})/{zoom}", f"{cy}+(H-{cy})/{zoom}"
    corners = (f"x0='{left}':y0='{top}':x1='{right}':y1='{top}':"
               f"x2='{left}':y2='{bottom}':x3='{right}':y3='{bottom}'")
    return (filter_complex.replace("[base]", "[unflowed]")
            + f";[unflowed]perspective={corners}:interpolation=cubic:eval=frame[base]")
