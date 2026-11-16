"""Round 62: the stream-highlight picker. Spikes -> moments -> clip boundaries -> a judge.

The podcast picker (core.pipeline.pick_clips_with_agent) reads the whole transcript, asks for
idea-complete sentences and snaps to sentence ends. A stream highlight is the opposite kind
of thing: the payoff is usually not a sentence (a crash, a clutch escape, a jump scare) and
the words are reactions. So this picker:

1. seeds candidates from the heatmap spikes (core.clip_mode.signals: chat + loudness, with
   chat already shifted onto the moment), best first, spaced apart;
2. shows the model each spike's context: the words said around it and a sample of chat's
   reaction, and asks what happened, when exactly the payoff is, a hook title, a label and a
   score (the stream prompt below). With no model key it keeps the spikes as they are;
3. cuts each clip from SETUP seconds before the payoff (enough to understand it) to shortly
   after the reaction fades (the end of the spike's hot run), 15-60 s in all, each edge moved
   to the nearest pause in speech so no word is cut, never to a sentence end;
4. asks a second question per clip, as a viewer meeting it cold (the stream judge), and
   blends that score in exactly as the podcast judge is blended (JUDGE_WEIGHT).

Everything the podcast pipeline does afterwards (render_selected_clip, captions, layouts) is
unchanged. ``ask`` can be any function(prompt, system_prompt) -> text; tests pass a fake.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core import clip_length as L
from core import heatmap_scorer
from core.agent_brain import _parse_json_response, _request_gemini, _request_openai
from core.clip_mode.chat import Message
from core.hook_selector import ClipResult
from core.transcriber import Word

logger = logging.getLogger(__name__)

HOT = heatmap_scorer.HOT_VALUE  # a spike: the same bar as a "Most replayed" moment
MIN_SPACING = 45.0  # seconds between two seeded moments (one highlight per burst)
CONTEXT_BEFORE = 25.0  # seconds of transcript shown before a spike
CONTEXT_AFTER = 20.0
CHAT_SAMPLE = 14  # chat messages shown per spike (the reaction, after the moment)
CHAT_WINDOW = 12.0  # seconds of chat after the reaction starts that count as the reaction
CHAT_LAG = 5.0  # chat starts reacting about this long after the moment (signals.CHAT_LAG_BUCKETS)
SETUP_DEFAULT = 6.0  # seconds before the payoff when the model gives none
SETUP_RANGE = (2.0, 14.0)
REACTION_MIN = 3.0  # seconds after the payoff a clip always keeps
REACTION_MAX = 20.0
PAUSE = 0.35  # a gap between words this long is a pause an edge may sit in
SNAP = 2.5  # how far an edge may move to reach a pause
JUDGE_WEIGHT = 0.5  # as core.pipeline.rescore_with_judge
MAX_SEEDS = 24  # spikes shown to the model at most (a 2-hour stream can have 60+)
LABELS = ("hype", "clutch", "fail", "funny", "rage", "scare", "wholesome", "other")

Ask = Callable[[str, str], str]

STREAM_PROMPT = """You pick highlight clips from a live-stream VOD for TikTok, Reels and Shorts.
You get numbered MOMENTS. Each has: when it is (seconds), the words spoken around it (from a
rough transcript; game audio and music may be in it), and a sample of what chat typed just after
it. Chat spikes and loudness spikes found these moments; most are real, some are nothing.
For every moment return one JSON object inside {"moments": [...]} with:
- "moment": its number.
- "keep": true if a viewer who has never seen this streamer would react to it (laugh, gasp, "no
  way"); false for sponsor reads, "be right back", setting up, chat reading, in-jokes that need
  history, slurs or anything a platform would remove.
- "label": one of hype, clutch, fail, funny, rage, scare, wholesome, other.
- "payoff_time": seconds, the exact moment the thing happens (the crash, the kill, the punchline,
  the scream). Use the transcript's own times. If the payoff is before the words that react to
  it, put it just before them.
- "setup_seconds": how many seconds before payoff_time a viewer needs to understand it, 2 to 14.
  A crash needs 3; a joke needs its setup line.
- "hook_title": under 50 characters, on screen at the start, no emojis, no spoilers of the payoff
  ("He had ONE job", "Chat called it"), never a question that the clip doesn't answer.
- "score": 1-10, how strongly a cold viewer reacts. 8+ is rare: a real jaw-drop or belly laugh.
- "why": one short sentence.
No markdown, JSON only."""

JUDGE_PROMPT = """You review highlight clips cut from a live stream. For each numbered clip you get its
spoken words and a sample of chat's reaction. Give three scores from 1 to 10:
- hook: within the first 2 seconds, is there something to react to or a reason to wait for it?
- cold: does it work for someone who doesn't know the streamer or the game? Score low if it
  needs context from earlier in the stream or an in-joke.
- payoff: is the moment itself inside the clip, with the reaction, and does the clip end soon
  after instead of running on?
Return JSON only: {"scores": [{"clip": 1, "hook": 7, "cold": 8, "payoff": 6}, ...]} with exactly
one entry per clip."""


@dataclass
class Seed:
    number: int
    at: float  # the moment (seconds)
    run_start: float  # the hot run it came from
    run_end: float
    value: float
    words: str = ""
    chat: list[str] = field(default_factory=list)


@dataclass
class Moment:
    seed: Seed
    payoff: float
    setup: float
    title: str
    label: str
    score: int  # 1-10
    why: str = ""


# --- seeds --------------------------------------------------------------------------------

def seeds(heatmap: list[dict], words: list[Word], chat: list[Message], duration: float,
          limit: int = MAX_SEEDS) -> list[Seed]:
    """The hot runs of the heatmap as moments, strongest first, at least MIN_SPACING apart."""
    runs = heatmap_scorer.spikes(heatmap, HOT)
    chosen: list[tuple[float, float, float]] = []
    for run in sorted(runs, key=lambda r: r[2], reverse=True):
        at = run[0]
        if all(abs(at - c[0]) >= MIN_SPACING for c in chosen):
            chosen.append(run)
        if len(chosen) >= limit:
            break
    chosen.sort()
    out: list[Seed] = []
    for n, (start, end, value) in enumerate(chosen, 1):
        at = max(0.0, min(start, duration))
        seed = Seed(n, at, start, end, value)
        seed.words = " ".join(w.text.strip() for w in words if at - CONTEXT_BEFORE <= w.start <= at + CONTEXT_AFTER)
        seed.chat = reaction_sample(chat, at)
        out.append(seed)
    return out


def reaction_sample(chat: list[Message], at: float, limit: int = CHAT_SAMPLE) -> list[str]:
    """Up to ``limit`` messages from chat's reaction to a moment at ``at``: the busiest
    CHAT_WINDOW seconds in the lag window after it, sampled evenly."""
    window = [m for m in chat if at <= m.t <= at + CHAT_LAG + CHAT_WINDOW and m.text.strip()]
    if not window:
        return []
    best_start, best = at, -1
    for s in range(int(at), int(at + CHAT_LAG) + 1):
        n = sum(1 for m in window if s <= m.t <= s + CHAT_WINDOW)
        if n > best:
            best_start, best = float(s), n
    reaction = [m.text.strip() for m in window if best_start <= m.t <= best_start + CHAT_WINDOW]
    step = max(1, len(reaction) // limit)
    return reaction[::step][:limit]


def _prompt(seeds_: list[Seed], streamer: str, game: str) -> str:
    items = [{"moment": s.number, "at_seconds": round(s.at, 1), "spike": round(s.value, 2),
              "words_around_it": s.words or "(nothing said)", "chat_after_it": s.chat} for s in seeds_]
    return json.dumps({"streamer": streamer or "unknown", "game": game or "unknown", "moments": items},
                      ensure_ascii=False)


def _default_ask() -> Ask | None:
    gemini, openai = os.getenv("GEMINI_API_KEY"), os.getenv("OPENAI_API_KEY")
    if gemini:
        return lambda prompt, system: _request_gemini(gemini, prompt, system)
    if openai:
        return lambda prompt, system: _request_openai(openai, prompt, system)
    return None


def _int(value, lo: int, hi: int, default: int) -> int:
    try:
        return int(min(hi, max(lo, round(float(value)))))
    except (TypeError, ValueError):
        return default


def moments(seeds_: list[Seed], ask: Ask | None, streamer: str = "", game: str = "") -> list[Moment]:
    """What each seed is, from the model; without one, every seed is kept as it stands."""
    if not seeds_:
        return []
    if ask is None:
        return [Moment(s, s.at, SETUP_DEFAULT, "", "other", _int(1 + 9 * s.value, 1, 10, 5), "no model: spike kept")
                for s in seeds_]
    by_number = {s.number: s for s in seeds_}
    answer = ask(_prompt(seeds_, streamer, game), STREAM_PROMPT)
    data = _parse_json_response(answer)
    out: list[Moment] = []
    for item in data.get("moments") or []:
        if not isinstance(item, dict):
            continue
        seed = by_number.get(_int(item.get("moment"), 0, 10 ** 6, 0))
        if seed is None or not item.get("keep", True):
            continue
        try:
            payoff = float(item.get("payoff_time", seed.at))
        except (TypeError, ValueError):
            payoff = seed.at
        if not (seed.at - CONTEXT_BEFORE <= payoff <= seed.run_end + CONTEXT_AFTER):
            payoff = seed.at  # the model wandered off; trust the spike
        setup = float(_int(item.get("setup_seconds"), int(SETUP_RANGE[0]), int(SETUP_RANGE[1]), int(SETUP_DEFAULT)))
        label = str(item.get("label") or "other").strip().lower()
        title = " ".join(str(item.get("hook_title") or "").split())[:50]
        out.append(Moment(seed, payoff, setup, title, label if label in LABELS else "other",
                          _int(item.get("score"), 1, 10, 5), str(item.get("why") or "")))
    if not out:
        logger.warning("The stream picker kept none of %d moments.", len(seeds_))
    return out


# --- boundaries ---------------------------------------------------------------------------

def pauses(words: list[Word]) -> list[float]:
    """Times inside gaps between words (and before the first / after the last word)."""
    ordered = sorted(words, key=lambda w: w.start)
    out: list[float] = []
    for a, b in zip(ordered, ordered[1:]):
        if b.start - a.end >= PAUSE:
            out.append(round((a.end + b.start) / 2, 3))
    return out


def snap(t: float, pause_times: list[float], words: list[Word], reach: float = SNAP) -> float:
    """``t`` moved to the nearest pause within ``reach``; if none, out of the word it is in."""
    near = [p for p in pause_times if abs(p - t) <= reach]
    if near:
        return min(near, key=lambda p: abs(p - t))
    for w in words:
        if w.start < t < w.end:
            return w.start if t - w.start < w.end - t else w.end
    return t


def bounds(m: Moment, words: list[Word], duration: float) -> tuple[float, float]:
    """(start, end) for a moment: setup before the payoff, the reaction after it, in 15-60 s."""
    pause_times = pauses(words)
    start = snap(m.payoff - m.setup, pause_times, words)
    reaction = max(REACTION_MIN, min(REACTION_MAX, m.seed.run_end + 2.0 - m.payoff))
    end = snap(m.payoff + reaction, pause_times, words)
    start = max(0.0, min(start, m.payoff - SETUP_RANGE[0]))
    end = min(duration, max(end, m.payoff + REACTION_MIN))
    if end - start < L.MIN_SECONDS:  # too short: more setup first, then more reaction
        start = max(0.0, end - L.MIN_SECONDS)
        if end - start < L.MIN_SECONDS:
            end = min(duration, start + L.MIN_SECONDS)
    if end - start > L.MAX_SECONDS:  # too long: keep the payoff, trim the setup first
        start = max(start, end - L.MAX_SECONDS)
        if start > m.payoff - SETUP_RANGE[0]:
            start = max(0.0, m.payoff - m.setup)
            end = start + L.MAX_SECONDS
    return round(start, 3), round(end, 3)


# --- the judge ----------------------------------------------------------------------------

def _clip_words(words: list[Word], start: float, end: float) -> str:
    return " ".join(w.text.strip() for w in words if start <= w.start <= end)


def judge(clips: list[tuple[ClipResult, Moment]], words: list[Word], chat: list[Message], ask: Ask | None) -> list[int] | None:
    """0-100 per clip from the stream judge, or None without a model or a usable answer."""
    if not clips or ask is None:
        return None
    parts = []
    for n, (clip, m) in enumerate(clips, 1):
        parts.append(f"Clip {n} ({m.label}): words: {_clip_words(words, clip.start_time, clip.end_time) or '(none)'}"
                     f"\nchat: {' | '.join(reaction_sample(chat, m.payoff)) or '(quiet)'}")
    try:
        data = _parse_json_response(ask("\n\n".join(parts), JUDGE_PROMPT))
        by_clip: dict[int, int] = {}
        for entry in data.get("scores") or []:
            if not isinstance(entry, dict):
                continue
            try:
                k = int(entry["clip"])
                hook, cold, payoff = (min(10.0, max(1.0, float(entry[x]))) for x in ("hook", "cold", "payoff"))
            except (KeyError, TypeError, ValueError):
                continue
            by_clip[k] = round((2 * hook + 2 * cold + payoff) / 50 * 100)
        if sorted(by_clip) != list(range(1, len(clips) + 1)):
            raise ValueError("the judge did not score every clip exactly once")
        return [by_clip[k] for k in range(1, len(clips) + 1)]
    except Exception as exc:  # noqa: BLE001 - the first pass's ranking stands
        logger.warning("Stream judge answer unusable: %s", exc)
        return None


# --- all together ---------------------------------------------------------------------------

def pick_stream_clips(words: list[Word], chat: list[Message], heatmap: list[dict], duration: float, count: int,
                      *, streamer: str = "", game: str = "", ask: Ask | None = None, use_model: bool = True,
                      use_judge: bool = True, report: dict | None = None) -> list[ClipResult]:
    """``count`` highlight clips from a stream, in time order, each with peak_retention_score.
    ``ask``: the model (default: Gemini or OpenAI from .env); ``use_model=False``: none, the
    spikes are kept as they are (a dry run)."""
    ask = (ask if ask is not None else _default_ask()) if use_model else None
    found = moments(seeds(heatmap, words, chat, duration), ask, streamer, game)
    paired: list[tuple[ClipResult, Moment]] = []
    for m in sorted(found, key=lambda x: (x.score, x.seed.value), reverse=True):
        start, end = bounds(m, words, duration)
        if any(not (end <= c.start_time or start >= c.end_time) for c, _ in paired):
            continue  # overlaps a better one
        clip = ClipResult(start_time=start, end_time=end, viral_title=m.title or m.label.title(),
                          caption=m.why, hashtags=[], virality_score=int(m.score * 10))
        paired.append((clip, m))
    scores = judge(paired, words, chat, ask) if use_judge else None
    if scores:
        paired = [(c.model_copy(update={"virality_score": round(JUDGE_WEIGHT * s + (1 - JUDGE_WEIGHT) * c.virality_score)}), m)
                  for (c, m), s in zip(paired, scores)]
    paired.sort(key=lambda cm: cm[0].virality_score, reverse=True)
    chosen = paired[:count]
    if report is not None:
        report["moments"] = [{"at": m.seed.at, "payoff": m.payoff, "label": m.label, "score": m.score, "why": m.why,
                              "start": c.start_time, "end": c.end_time, "title": c.viral_title,
                              "judge": (scores[i] if scores else None)} for i, (c, m) in enumerate(paired)]
    spikes = heatmap_scorer.spikes(heatmap, HOT)
    clips = heatmap_scorer.score_clips([c for c, _ in chosen], spikes, rank=False, raise_scores=False)
    return sorted(clips, key=lambda c: c.start_time)


def save_report(report: dict, path: Path) -> None:
    path.write_text(json.dumps(report, indent=2))
