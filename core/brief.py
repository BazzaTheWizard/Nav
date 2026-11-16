"""Round 58: clips cut to a client's brief (themes they name), not the "best moments" picker.

The outreach picker (pipeline.pick_clips_with_agent) finds a video's strongest moments and drafts
a pitch. A client brief is different: "one clip on X, one on Y, one on Z, 20-45 s, each ending on
a complete statement, with a 4-6 word hook". The rules a prompt can't guarantee are enforced in
code here; the model only does what code can't (finding the moment that fits a theme, writing
the hook):

1. windows(): every span of whole sentences that may be a clip: the right length, a start that
   doesn't open mid-thought, an end on a declarative sentence (. or !, never ? or ...), no
   trailing conjunction, and none of hook_selector.ending_problems()'s hard problems (ends on the
   other person's "yeah", announces a list it doesn't finish, the next sentence lands the point).
2. The model reads the numbered sentences and proposes up to 3 moments per theme.
3. Each proposal snaps to the valid window that overlaps it most (or is dropped).
4. A second model pass reads each chosen clip's exact words cold: on-theme? standalone? Writes
   the hook; code checks it is 4-6 words and retries once. A weak or off-theme clip falls back to
   the theme's next candidate. Clips never overlap.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from core import hook_selector as H

logger = logging.getLogger(__name__)

# The last word of a clip may not be one of these (the speaker is about to go on).
TRAILING = {"and", "so", "but", "or", "because", "then", "which", "that", "like", "if", "when", "with",
            "to", "the", "a", "an", "of", "for", "um", "uh", "just", "also", "plus"}
HOOK_WORDS = (4, 6)
MIN_ON_THEME = 6  # of 10: below this the theme's next candidate is tried
Ask = Callable[[str, str], dict]  # (system prompt, user prompt) -> parsed JSON


@dataclass
class Theme:
    title: str
    look_for: str = ""


@dataclass
class Pick:
    theme: str
    start: float
    end: float
    hook: str
    why: str
    text: str
    last_sentence: str
    first_sentence: int
    last_sentence_index: int
    on_theme: int | None = None
    standalone: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return round(self.end - self.start, 2)


# ----------------------------------------------------------------------------- windows
def _ordered(words: list[Any]) -> list[Any]:
    return sorted(words, key=lambda w: (w.start, w.end))


def sentences(words: list[Any]) -> tuple[list[Any], list[tuple[float, float, int]]]:
    ordered = _ordered(words)
    return ordered, H._sentence_spans(ordered)


def closure_problem(ordered: list[Any], spans: list[tuple[float, float, int]], i: int, j: int) -> str | None:
    """Why a clip of sentences i..j does NOT end on a complete, declarative statement (None: it does)."""
    last = H._sentence_text(ordered, spans, j).strip()
    bare = last.rstrip("\"')]}”’ ")
    if bare.endswith(("...", "…")):
        return "trails off"
    if not bare.endswith((".", "!")):
        return "doesn't end on a declarative sentence"
    final = H._plain(bare).rstrip(".!").split()
    if final and final[-1] in TRAILING:
        return f"ends on '{final[-1]}'"
    for cost, reason in H.ending_problems(ordered, spans, i, j):
        if cost >= H.HARD:
            return reason
    return None


# A promo clip can't open on these either (hook_selector allows "So ..." and "Then ..." for long clips;
# a 20-45 s standalone promo that starts "So use it..." sounds cut in).
START_DANGLING = {"so", "then", "and", "but", "or", "because", "which", "also", "plus", "anyway"}


def start_problem(ordered: list[Any], spans: list[tuple[float, float, int]], i: int) -> str | None:
    text = H._sentence_text(ordered, spans, i)
    first = H._plain(text).lstrip(" -–—.,…").split()
    if first and first[0].strip(",") in START_DANGLING:
        return f"opens on '{first[0].strip(',')}'"
    return H.moment_problem(text)


def windows(words: list[Any], min_s: float, max_s: float) -> list[tuple[int, int, float, float]]:
    """Every valid clip: (first sentence, last sentence, start s, end s)."""
    ordered, spans = sentences(words)
    out = []
    for i in range(len(spans)):
        if start_problem(ordered, spans, i):
            continue
        for j in range(i, len(spans)):
            length = spans[j][1] - spans[i][0]
            if length > max_s:
                break
            if length >= min_s and closure_problem(ordered, spans, i, j) is None:
                out.append((i, j, spans[i][0], spans[j][1]))
    return out


def snap(window_list: list[tuple[int, int, float, float]], start: float, end: float,
         min_overlap: float = 0.5) -> tuple[int, int, float, float] | None:
    """The valid window that best matches a proposed [start, end] (overlap over union), or None."""
    best, best_score = None, 0.0
    for w in window_list:
        inter = max(0.0, min(end, w[3]) - max(start, w[2]))
        union = max(end, w[3]) - min(start, w[2])
        score = inter / union if union > 0 else 0.0
        if score > best_score:
            best, best_score = w, score
    return best if best_score >= min_overlap else None


def clean_hook(text: str) -> str | None:
    """The hook if it is 4-6 words (quotes and a trailing period removed), else None."""
    hook = re.sub(r"\s+", " ", str(text or "")).strip().strip("\"'“”‘’").rstrip(".").strip()
    return hook if HOOK_WORDS[0] <= len(hook.split()) <= HOOK_WORDS[1] else None


# ----------------------------------------------------------------------------- the model
PICK_PROMPT = """You are a senior social video editor cutting short promo clips from a workshop recording
to a client's brief. You get the transcript as numbered sentences ([S12 @ 03:21.4] text) and a list of
themes. For EACH theme, propose up to 3 candidate moments, best first.

A candidate is a run of whole sentences, from start_sentence to end_sentence (inclusive), that:
- runs {min_s:.0f}-{max_s:.0f} seconds (use the timestamps),
- is clearly about that theme (what to look for is given with each theme),
- makes sense to someone who has not seen the rest of the workshop (no "as I said", no answering a question we don't hear),
- opens on a line that grabs attention, and
- ENDS on a complete, declarative sentence that finishes the thought: never mid-thought, never on a question,
  never on "and", "so", "but", never right before the sentence that lands the point.
Never invent or rephrase what was said; only pick sentence numbers.

Return JSON only:
{{"themes": [{{"theme": 1, "candidates": [{{"start_sentence": 12, "end_sentence": 19, "why": "one line"}}]}}]}}"""

CHECK_PROMPT = """You review short promo clips cut from a workshop for a client brief. For each clip you get the
theme it should fit and the clip's exact words. Score cold, as a viewer scrolling past would meet it:
- on_theme (1-10): is the clip clearly about its theme?
- standalone (1-10): does it make sense with no other context?
- complete (1-10): does it end on a finished thought?
Then write the HOOK: {lo}-{hi} words shown on screen during the first 3 seconds. Punchy, plain words, true
to what the clip says (no claims the speaker doesn't make), no emoji, no hashtags, no ending period.
Return JSON only: {{"clips": [{{"clip": 1, "on_theme": 8, "standalone": 7, "complete": 9, "hook": "..."}}]}}"""


def gemini_or_openai() -> Ask:
    """The project's own model access (core.agent_brain: GEMINI_API_KEY, else OPENAI_API_KEY)."""
    from dotenv import load_dotenv

    from core import agent_brain as B

    load_dotenv(B._PROJECT_ROOT / ".env")
    gemini, openai = os.getenv("GEMINI_API_KEY"), os.getenv("OPENAI_API_KEY")
    if not gemini and not openai:
        raise RuntimeError("Set GEMINI_API_KEY or OPENAI_API_KEY in .env.")

    def ask(system: str, prompt: str) -> dict:
        text = B._request_gemini(gemini, prompt, system) if gemini else B._request_openai(str(openai), prompt, system)
        return B._parse_json_response(text)

    return ask


def _stamp(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{seconds % 60:04.1f}"


def numbered_transcript(words: list[Any]) -> str:
    ordered, spans = sentences(words)
    return "\n".join(f"[S{k} @ {_stamp(s)}] {H._sentence_text(ordered, spans, k)}" for k, (s, _e, _i) in enumerate(spans))


def propose(words: list[Any], themes: list[Theme], min_s: float, max_s: float, ask: Ask) -> list[list[dict]]:
    """The model's candidates per theme: [{start_sentence, end_sentence, why}, ...] in theme order."""
    brief = "\n".join(f"{n}. {t.title}" + (f" (look for: {t.look_for})" if t.look_for else "")
                      for n, t in enumerate(themes, 1))
    answer = ask(PICK_PROMPT.format(min_s=min_s, max_s=max_s),
                 f"THEMES:\n{brief}\n\nTRANSCRIPT:\n{numbered_transcript(words)}")
    out: list[list[dict]] = [[] for _ in themes]
    for entry in answer.get("themes", []):
        try:
            k = int(entry["theme"]) - 1
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= k < len(themes):
            out[k] = [c for c in entry.get("candidates", []) if isinstance(c, dict)]
    return out


def check(clips: list[tuple[Theme, str]], ask: Ask) -> list[dict]:
    """The second pass: scores and a hook per clip (hooks re-asked once if not 4-6 words)."""
    body = "\n\n".join(f"Clip {n} (theme: {t.title}{'; ' + t.look_for if t.look_for else ''}):\n{text}"
                       for n, (t, text) in enumerate(clips, 1))
    system = CHECK_PROMPT.format(lo=HOOK_WORDS[0], hi=HOOK_WORDS[1])
    rows = {int(r["clip"]): r for r in ask(system, body).get("clips", []) if isinstance(r, dict) and "clip" in r}
    bad = [n for n in range(1, len(clips) + 1) if clean_hook(rows.get(n, {}).get("hook", "")) is None]
    if bad:
        retry = ask(system + f"\nEvery hook MUST be {HOOK_WORDS[0]}-{HOOK_WORDS[1]} words. Count them.", body)
        for r in retry.get("clips", []):
            if isinstance(r, dict) and r.get("clip") in bad and clean_hook(r.get("hook", "")):
                rows.setdefault(int(r["clip"]), {}).update(hook=r["hook"])
    return [rows.get(n, {}) for n in range(1, len(clips) + 1)]


# ----------------------------------------------------------------------------- picking
def pick(words: list[Any], themes: list[Theme], *, min_s: float, max_s: float, ask: Ask | None = None,
         overrides: dict[int, tuple[float, float]] | None = None) -> list[Pick | None]:
    """One clip per theme (None where nothing valid was found). ``overrides`` {theme index: (start, end)}:
    your own times for a theme, still snapped to a valid window and checked."""
    ask = ask or gemini_or_openai()
    ordered, spans = sentences(words)
    valid = windows(words, min_s, max_s)
    if not valid:
        raise RuntimeError(f"No {min_s:.0f}-{max_s:.0f} s span of whole sentences ends on a complete statement "
                           "(is the transcript punctuated?).")
    overrides = overrides or {}
    proposals = propose(words, themes, min_s, max_s, ask) if len(overrides) < len(themes) else [[] for _ in themes]

    def as_window(cand: dict) -> tuple[int, int, float, float] | None:
        try:
            i, j = int(cand["start_sentence"]), int(cand["end_sentence"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0 <= i <= j < len(spans)):
            return None
        return snap(valid, spans[i][0], spans[j][1])

    options: list[list[tuple[tuple[int, int, float, float], str]]] = []
    for k, theme in enumerate(themes):
        if k in overrides:
            w = snap(valid, *overrides[k], min_overlap=0.3)
            options.append([(w, "your times")] if w else [])
            continue
        seen, opts = set(), []
        for cand in proposals[k]:
            w = as_window(cand)
            if w and w not in seen:
                seen.add(w)
                opts.append((w, str(cand.get("why") or "")))
        options.append(opts)

    picks: list[Pick | None] = [None] * len(themes)
    taken: list[tuple[float, float]] = []
    cursor = [0] * len(themes)
    pending = list(range(len(themes)))
    while pending:
        batch = []
        for k in pending:
            while cursor[k] < len(options[k]):
                w, why = options[k][cursor[k]]
                if not any(min(w[3], b) - max(w[2], a) > 0 for a, b in taken + [(x[0][2], x[0][3]) for _, x in batch]):
                    batch.append((k, (w, why)))
                    break
                cursor[k] += 1
        if not batch:
            break
        texts = [(themes[k], " ".join(H._sentence_text(ordered, spans, s) for s in range(w[0], w[1] + 1)))
                 for k, (w, _why) in batch]
        reviews = check(texts, ask)
        pending = []
        for (k, (w, why)), (_t, text), review in zip(batch, texts, reviews):
            hook = clean_hook(review.get("hook", ""))
            on_theme = _int(review.get("on_theme"))
            if k not in overrides and ((on_theme is not None and on_theme < MIN_ON_THEME) or hook is None):
                cursor[k] += 1  # off-theme or no usable hook: the theme's next candidate
                pending.append(k)
                continue
            picks[k] = Pick(theme=themes[k].title, start=round(w[2], 3), end=round(w[3], 3), hook=hook or "",
                            why=why, text=text, last_sentence=H._sentence_text(ordered, spans, w[1]),
                            first_sentence=w[0], last_sentence_index=w[1], on_theme=on_theme,
                            standalone=_int(review.get("standalone")),
                            notes=[] if hook else ["hook must be written by hand (model gave none of 4-6 words)"])
            taken.append((w[2], w[3]))
    return picks


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- brief files
@dataclass
class Brief:
    client: str
    themes: list[Theme]
    cta: str
    cta_sub: str = ""
    accent: str = "#1F3A5F"
    logo: str = ""  # a PNG with transparency; empty: the client name as a wordmark
    min_seconds: float = 20.0
    max_seconds: float = 45.0
    end_card_seconds: float = 3.0
    hook_seconds: float = 3.0

    @classmethod
    def load(cls, path: str | Path) -> "Brief":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["themes"] = [Theme(**t) if isinstance(t, dict) else Theme(str(t)) for t in data.get("themes", [])]
        return cls(**data)

    def speech_range(self) -> tuple[float, float]:
        """Clip lengths before the end card, so the finished clip lands in min-max seconds. The low end
        keeps 2 s spare: the mid-clip trim may take out pauses."""
        return self.min_seconds - self.end_card_seconds + 2.0, self.max_seconds - self.end_card_seconds


def save_words(words: list[Any], path: Path) -> None:
    path.write_text(json.dumps([asdict(w) for w in words]), encoding="utf-8")


def load_words(path: Path) -> list[Any]:
    from core.transcriber import Word

    return [Word(**w) for w in json.loads(path.read_text(encoding="utf-8"))]
