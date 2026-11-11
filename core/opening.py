"""Round 42: the first seconds hold the strongest pull of the clip.

Viewers decide in the first 1-3 seconds. Both clips we measured opened mid-thought
("For some of us leaders...", "One is, we're living in a time and...") with no cold
open, because Round 40's teaser only fires on a classic cliffhanger ("the real lesson is
that..."). Three fixes, all from the Whisper words, no AI:

1. tighten(): the clip never opens on filler or dead air. Leading "um", "uh", "so",
   "and", "yeah", "you know"... and any silence before the first real word are cut (at
   most MAX_TRIM s, never below MIN_SECONDS), so word one is on frame one.
2. strength(): how hard the first sentence pulls (a question, a bold or contrarian claim,
   a figure, "you" addressed directly, vs. a context-dependent opener like "One is...",
   "That's why...", "It was...").
3. cold_open(): when the clip has no cliffhanger teaser and its opening doesn't already
   pull (strength < STRONG_OPENING), it opens on its most gripping complete line from
   later in the clip instead (a 2.2-3.6 s clause ending on its own punctuation: a
   question, a bold claim, a figure, the speaker leaning in), with the same flash and
   sub-bass hit into 0:00 (core.climax_hook.stitch). Only when that line is strong
   (score >= MIN_SCORE): never forced.

OPENING_HOOK=0 in .env turns 1-3 off.
"""
from __future__ import annotations

import os
import re
from typing import Any

from core import climax_hook as H

MAX_TRIM = 2.0  # seconds of filler or silence cut from the start at most
MIN_SECONDS = 15.0  # a clip is never tightened below this
FILLERS = {"um", "uh", "uhm", "erm", "er", "ah", "so", "and", "but", "yeah", "yes", "well", "okay", "ok", "like",
           "anyway", "alright", "right", "now", "i mean", "you know", "so yeah", "and so", "but yeah"}
WEAK_OPENERS = (("one", "is"), ("the", "other"), ("that's", "why"), ("thats", "why"), ("that", "was"), ("it", "was"),
                ("which", "is"), ("and", "then"), ("also",), ("another",), ("because",), ("which",), ("this", "is"),
                ("it's",), ("its",), ("they",), ("he",), ("she",))
CONTRAST = {"not", "never", "only", "actually", "instead", "but", "nobody", "everyone", "wrong", "truth", "secret",
            "real", "biggest", "worst", "best", "mistake", "stop", "nothing", "everything", "always"}
INTERROGATIVES = {"why", "how", "what", "who", "when", "would", "could", "should", "do", "does", "did",
                  "have", "has", "is", "are", "can", "imagine", "picture", "ever"}
STRONG_OPENING = 3.0
MIN_SCORE = 3.0
COLD_SECONDS = (2.2, 3.6)
MIN_COLD_CLIP = 20.0
MIN_COLD_AT = 8.0  # the cold open comes from 8 s into the clip or later (and its back 65 %)


def enabled() -> bool:
    return os.getenv("OPENING_HOOK", "1").strip().lower() not in {"0", "false", "no", "off"}


def _clean(text: Any) -> str:
    return H._clean(text)


# ------------------------------------------------------------------ 1. tighten
def tighten(start: float, end: float, words: list[Any]) -> tuple[float, str]:
    """The clip's new start (source seconds): past leading filler words and silence, so
    the first real word is on the first frame. (start, "") when nothing needs cutting."""
    spoken = sorted((w for w in words if start - 0.05 <= float(w.start) < end), key=lambda w: float(w.start))
    if not spoken:
        return start, ""
    k = 0
    while k < len(spoken) - 1:
        one = _clean(spoken[k].text)
        two = f"{one} {_clean(spoken[k + 1].text)}"
        if two in FILLERS:
            k += 2
        elif one in FILLERS:
            k += 1
        else:
            break
    first = spoken[min(k, len(spoken) - 1)]
    new_start = max(start, float(first.start) - 0.06)
    if new_start - start > MAX_TRIM or end - new_start < MIN_SECONDS:
        # too much to cut: just drop the silence before the very first word
        new_start = max(start, float(spoken[0].start) - 0.06)
        k = 0
    if new_start - start < 0.12:
        return start, ""
    cut = " ".join(str(w.text) for w in spoken[:k])
    return round(new_start, 3), (f"Opening tightened by {new_start - start:.2f} s"
                                 + (f" (cut \"{cut}\")" if cut else " (silence)") + ": the first word is on the first frame.")


# ------------------------------------------------------------------ 2. strength
def _line_score(tokens: list[str], texts: list[str], loud: float = 0.0) -> float:
    score = 0.0
    if texts and texts[-1].rstrip("\"'”’)").endswith("?"):
        score += 1.5  # a question opens a loop
    score += 0.6 * min(3, sum(1 for t in tokens if t in H.STAKES or t in CONTRAST))
    if any(re.search(r"\d", t) for t in texts) or any(t in {"percent", "million", "billion", "trillion"} for t in tokens):
        score += 1.5  # a figure
    if any(t in {"you", "your", "you're"} for t in tokens):
        score += 0.75  # said to the viewer
    if tokens and tokens[0] in INTERROGATIVES:
        score += 1.0  # it opens on a question word ("Why would you...", "What if...")
    if tokens and any(tuple(tokens[:len(w)]) == w for w in WEAK_OPENERS):
        score -= 1.5  # needs what came before it
    score -= 0.75 * sum(1 for t in tokens if t in {"um", "uh", "like", "kinda", "sorta"})
    return score + loud


def strength(words: list[Any], start: float, end: float) -> float:
    """How hard the clip's first sentence (its first ~6 s) pulls."""
    first: list[Any] = []
    for w in sorted((w for w in words if start - 0.05 <= float(w.start) < end), key=lambda w: float(w.start)):
        first.append(w)
        if float(w.end) - start > 6.0 or str(w.text).rstrip("\"'”’)").endswith((".", "?", "!")):
            break
    return _line_score([_clean(w.text) for w in first], [str(w.text) for w in first])


# ------------------------------------------------------------------ 3. cold open
def _clauses(words: list[Any]) -> list[tuple[int, int]]:
    """(first, last) word of every clause: ends on , . ? ! ; : or a 0.35 s pause."""
    out, first = [], 0
    for k, w in enumerate(words):
        gap = float(words[k + 1].start) - float(w.end) if k + 1 < len(words) else 9.0
        if re.search(r"[,.;:?!][\"'”’)]*$", str(w.text)) or gap >= 0.35:
            out.append((first, k))
            first = k + 1
    return out


def cold_open(words: list[Any], clip_start: float, clip_end: float, rate: float = 30.0,
              loudness: list[float] | None = None, max_total: float = H.MAX_TOTAL_SECONDS) -> H.Teaser | None:
    """The clip's most gripping complete line, from its back 65 % (8 s in or later), as a
    2.2-3.6 s cold open; None when the clip is under 20 s, its opening already pulls, or
    no line is strong enough. ``loudness``: dBFS per word of ``words`` (optional: the
    speaker leaning in counts)."""
    duration = clip_end - clip_start
    if duration < MIN_COLD_CLIP or strength(words, clip_start, clip_end) >= STRONG_OPENING:
        return None
    order = sorted(range(len(words)), key=lambda k: float(words[k].start))
    spoken = [words[k] for k in order if clip_start <= float(words[k].start) and float(words[k].end) <= clip_end]
    loud = None
    if loudness is not None and len(loudness) == len(words):
        by_word = {id(words[k]): loudness[k] for k in range(len(words))}
        loud = [by_word.get(id(w), -60.0) for w in spoken]
    mean_db = sum(loud) / len(loud) if loud else 0.0
    earliest = clip_start + max(MIN_COLD_AT, 0.35 * duration)
    from core.visual_proof import _DANGLING

    starts = sorted({a for a, _b in _clauses(spoken)})
    ends = {b for _a, b in _clauses(spoken)}
    sentence_q = {}  # word -> its sentence ends on "?" (a question even when the open cuts it short)
    first = 0
    for k, w in enumerate(spoken):
        if re.search(r"[.?!][\"'”’)]*$", str(w.text)) or k == len(spoken) - 1:
            q = str(w.text).rstrip("\"'”’)").endswith("?")
            for i in range(first, k + 1):
                sentence_q[i] = q
            first = k + 1
    best: tuple[tuple[float, bool, float], H.Teaser] | None = None
    for a in starts:
        start_t = float(spoken[a].start) - H.PRE_ROLL
        if start_t < earliest:
            continue
        for b in range(a + 2, len(spoken)):
            nxt = float(spoken[b + 1].start) if b + 1 < len(spoken) else clip_end
            cut = min(float(spoken[b].end) + 0.05, nxt - 0.01)
            length = cut - start_t
            if length > COLD_SECONDS[1]:
                break
            if length < COLD_SECONDS[0] or cut > clip_end - 2.0:
                continue
            texts = [str(w.text) for w in spoken[a:b + 1]]
            tokens = [_clean(t) for t in texts]
            if tokens[0] in {"and", "so", "but", "which", "because", "um", "uh"}:
                break
            complete = b in ends
            if not complete and tokens[-1] in _DANGLING:
                continue  # an open loop cuts on a strong word, never on "the" or "about"
            lean = 0.0
            if loud:
                lean = max(0.0, min(1.5, (sum(loud[a:b + 1]) / (b - a + 1) - mean_db) / 4.0))
            score = _line_score(tokens, texts, lean) + (0.5 if complete else 0.0)
            if not complete and sentence_q.get(b):
                score += 1.5  # the question it opens is still unanswered at the cut
            if score < MIN_SCORE:
                continue
            frames = int(round(length * rate))
            if duration + frames / rate > max_total + 1e-6:
                continue
            teaser = H.Teaser(start=round(cut - frames / rate, 4), cut=round(cut, 4), frames=frames, rate=rate,
                              hinge=texts[-1], line=" ".join(texts), score=round(score, 2),
                              reason=f"cold open: the clip's most gripping line (score {score:.1f}"
                                     f"{', cut on an open question' if not complete and sentence_q.get(b) else ''}), "
                                     f"its opening only scored {strength(words, clip_start, clip_end):.1f}")
            rank = (round(score, 3), complete, -abs(length - 3.2))
            if best is None or rank > best[0]:
                best = (rank, teaser)
    return best[1] if best else None
