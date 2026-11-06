from __future__ import annotations

import logging
import math
import re

from pydantic import BaseModel, Field, model_validator
from openai import OpenAI

from config import Settings
from core.transcriber import Word, format_timeline

logger = logging.getLogger(__name__)


MIN_CLIP_DURATION = 30.0
MAX_CLIP_DURATION = 60.0
PRE_ROLL_SECONDS = 0.15
POST_ROLL_SECONDS = 0.4
MIN_SILENCE_SECONDS = 0.35
MIN_START_SEPARATION_SECONDS = 20.0
MIN_CLIP_GAP_SECONDS = 15.0
# LLM proposals outside this range are discarded. Anything inside it is snapped to
# sentence boundaries and forced into MIN/MAX_CLIP_DURATION by _clip_bounds, so the
# model being a few seconds off no longer throws a good moment away.
MIN_PROPOSED_DURATION = 10.0
MAX_PROPOSED_DURATION = 120.0
# Average sentence longer than this means the transcript has (almost) no
# punctuation, e.g. auto-generated YouTube captions.
MAX_AVERAGE_SENTENCE_SECONDS = 20.0
_MAX_SPAN_SECONDS = MAX_CLIP_DURATION + PRE_ROLL_SECONDS + POST_ROLL_SECONDS


class ClipResult(BaseModel):
    start_time: float = Field(
        description=(
            "Clip start time in seconds from the source video's beginning. "
            "Choose a duration of 30-60 seconds."
        )
    )
    end_time: float = Field(
        description=(
            "Proposed clip end time in seconds from the source video's beginning. "
            "Aim for a 30-60 second duration ending on a finished sentence."
        )
    )
    viral_title: str = Field(description="Compelling short-form title for this clip.")
    caption: str = Field(description="Ready-to-post social caption that encourages engagement.")
    hashtags: list[str] = Field(description="A concise list of relevant social media hashtags.")
    virality_score: int = Field(ge=0, le=100, description="Estimated short-form shareability from 0 to 100.")

    @model_validator(mode="after")
    def validate_duration(self) -> "ClipResult":
        if not math.isfinite(self.start_time) or not math.isfinite(self.end_time):
            raise ValueError("Clip timestamps must be finite numbers.")
        return self


class ClipSelection(BaseModel):
    clips: list[ClipResult]


def _ends_sentence(text: str) -> bool:
    return bool(re.search(r"""[.!?]["')\]}»”’]*$""", text.strip()))


def _ends_phrase(ordered_words: list[Word], index: int) -> bool:
    """Fallback boundary for unpunctuated transcripts: a pause or a segment end."""
    word = ordered_words[index]
    if index + 1 >= len(ordered_words):
        return True
    next_word = ordered_words[index + 1]
    if next_word.start - word.end >= MIN_SILENCE_SECONDS:
        return True
    return (
        word.segment_end is not None
        and next_word.segment_start is not None
        and next_word.segment_start != word.segment_start
    )


def _collect_spans(
    ordered_words: list[Word],
    use_pauses: bool,
) -> list[tuple[float, float, int]]:
    spans: list[tuple[float, float, int]] = []
    sentence_start: float | None = None
    for index, word in enumerate(ordered_words):
        if sentence_start is None:
            sentence_start = word.start
        if _ends_sentence(word.text) or (
            use_pauses and _ends_phrase(ordered_words, index)
        ):
            spans.append((sentence_start, word.end, index))
            sentence_start = None
    return spans


def _sentence_spans(words: list[Word]) -> list[tuple[float, float, int]]:
    ordered_words = sorted(words, key=lambda word: (word.start, word.end))
    spans = _collect_spans(ordered_words, use_pauses=False)
    if ordered_words:
        transcript_seconds = ordered_words[-1].end - ordered_words[0].start
        if not spans or transcript_seconds / len(spans) > MAX_AVERAGE_SENTENCE_SECONDS:
            # Punctuation is missing (common with auto-generated captions). Without
            # this fallback no clip boundaries exist and zero clips are rendered.
            spans = _collect_spans(ordered_words, use_pauses=True)
    return spans


def _clip_bounds(
    sentences: list[tuple[float, float, int]],
    sentence_start_index: int,
    sentence_end_index: int,
    ordered_words: list[Word],
    total_video_duration: float,
) -> tuple[float, float] | None:
    sentence_start = sentences[sentence_start_index][0]
    sentence_end, final_word_index = sentences[sentence_end_index][1:]
    clip_start = max(0.0, sentence_start - PRE_ROLL_SECONDS)
    if sentence_start_index > 0:
        # Never open on the tail of the sentence before (its last word flashed as a caption).
        previous_end = ordered_words[sentences[sentence_start_index - 1][2]].end
        clip_start = max(clip_start, min(previous_end, sentence_start))
    final_word = ordered_words[final_word_index]
    next_word: Word | None = None
    for index in range(final_word_index + 1, len(ordered_words)):
        if ordered_words[index].start >= sentence_end:
            next_word = ordered_words[index]
            break
    gap = next_word.start - sentence_end if next_word is not None else math.inf
    segment_end = final_word.segment_end
    natural_boundary: float | None = None
    if next_word is None:
        natural_boundary = None
    elif gap >= MIN_SILENCE_SECONDS:
        if (
            segment_end is not None
            and sentence_end < segment_end <= next_word.start
        ):
            natural_boundary = segment_end
        else:
            natural_boundary = next_word.start
    else:
        natural_boundary = next_word.start

    clip_end = min(
        total_video_duration,
        sentence_end + POST_ROLL_SECONDS,
        natural_boundary if natural_boundary is not None else total_video_duration,
    )
    duration = clip_end - clip_start
    if not MIN_CLIP_DURATION <= duration <= MAX_CLIP_DURATION:
        return None
    return clip_start, clip_end


def _bounds_from_start(
    sentences: list[tuple[float, float, int]],
    start_index: int,
    ordered_words: list[Word],
    total_video_duration: float,
) -> list[tuple[float, float]]:
    """All valid clip bounds that begin at sentences[start_index]."""
    options: list[tuple[float, float]] = []
    start_time = sentences[start_index][0]
    for end_index in range(start_index, len(sentences)):
        if sentences[end_index][1] - start_time > _MAX_SPAN_SECONDS:
            break  # every later sentence ends even further away
        bounds = _clip_bounds(
            sentences,
            start_index,
            end_index,
            ordered_words,
            total_video_duration,
        )
        if bounds is not None:
            options.append(bounds)
    return options


def _normalize_clip(
    clip: ClipResult,
    total_video_duration: float,
    words: list[Word],
    *,
    ordered_words: list[Word] | None = None,
    sentences: list[tuple[float, float, int]] | None = None,
) -> ClipResult | None:
    proposed_duration = clip.end_time - clip.start_time
    if (
        clip.start_time < 0
        or clip.start_time >= total_video_duration
        or not MIN_PROPOSED_DURATION <= proposed_duration <= MAX_PROPOSED_DURATION
    ):
        return None

    if ordered_words is None:
        ordered_words = sorted(words, key=lambda word: (word.start, word.end))
    if sentences is None:
        sentences = _sentence_spans(ordered_words)
    if not sentences:
        return None

    sentence_index = min(
        range(len(sentences)),
        key=lambda index: abs(sentences[index][0] - clip.start_time),
    )
    bounds_options = _bounds_from_start(
        sentences,
        sentence_index,
        ordered_words,
        total_video_duration,
    )
    if not bounds_options:
        return None
    start_time, end_time = min(
        bounds_options,
        key=lambda bounds: (
            bounds[1] < clip.end_time,
            abs(bounds[1] - clip.end_time),
        ),
    )
    return clip.model_copy(
        update={"start_time": start_time, "end_time": end_time}
    )


# --- Moment quality ---------------------------------------------------------------
# Openings that only make sense after something said earlier, or that answer the
# other speaker. A clip that starts this way feels cut out of the middle.
_DANGLING_OPENING = re.compile(
    r"^(?:and|but|or|because|cause|which|also|plus|anyway|anyways|whereas|"
    r"yeah|yep|yes|right|exactly|absolutely|totally|correct|um+|uh+|"
    r"like i said|as i (?:said|mentioned)|as you (?:said|mentioned)|"
    r"that's why|that is why|which is why|that's because|so yeah|so anyway)\b",
    re.IGNORECASE,
)
# Ads, intros, outros and channel housekeeping: never a good Short.
_FILLER = re.compile(
    r"brought to you by|sponsored by|(?:today's|our|this week's) sponsor|promo code|"
    r"use (?:my |our |the )?code\b|links? (?:is |are )?in the description|"
    r"(?:hit|smash) (?:that |the )?(?:like|subscribe|bell)|and subscribe\b|"
    r"subscribe to (?:the|my|our) channel|patreon|"
    r"welcome (?:back )?to (?:the|my|our|another) (?:show|podcast|channel|episode|video)|"
    r"thanks? (?:you )?(?:so much )?for (?:watching|listening)|see you (?:next time|in the next)",
    re.IGNORECASE,
)
# How many sentences a dangling start may be moved forward before the clip is dropped.
MAX_START_SHIFT_SENTENCES = 2


def clip_text(words: list[Word], start: float, end: float) -> str:
    """The words spoken between ``start`` and ``end``."""
    return " ".join(
        word.text.strip()
        for word in sorted(words, key=lambda w: w.start)
        if start - 0.05 <= word.start < end
    ).strip()


def moment_problem(text: str) -> str | None:
    """Why a clip with this text makes a weak Short, or None if it looks fine."""
    if _FILLER.search(text):
        return "sponsor read, intro or outro"
    opening = text.lstrip(" \"'“”‘’-–—.,…")
    if _DANGLING_OPENING.match(opening):
        return "opens mid-thought"
    return None


def _fix_or_drop(
    clip: ClipResult,
    words: list[Word],
    ordered_words: list[Word],
    sentences: list[tuple[float, float, int]],
    video_duration: float,
) -> ClipResult | None:
    """Keep a clean clip; move a mid-thought start forward a sentence or two; else drop."""
    problem = moment_problem(clip_text(words, clip.start_time, clip.end_time))
    if problem is None:
        return clip
    if problem != "opens mid-thought":
        return None
    if not sentences:
        return None
    first = min(range(len(sentences)), key=lambda index: abs(sentences[index][0] - (clip.start_time + PRE_ROLL_SECONDS)))
    for index in range(first + 1, min(first + 1 + MAX_START_SHIFT_SENTENCES, len(sentences))):
        options = _bounds_from_start(sentences, index, ordered_words, video_duration)
        if not options:
            continue
        start, end = min(options, key=lambda bounds: abs(bounds[1] - clip.end_time))
        if moment_problem(clip_text(words, start, end)) is None:
            return clip.model_copy(update={"start_time": start, "end_time": end})
    return None


def _fallback_clips(
    requested_count: int,
    total_video_duration: float,
    words: list[Word],
    already: list[ClipResult] | None = None,
) -> list[ClipResult]:
    """Evenly spaced clean moments that also keep their distance from ``already``
    (the clips picked so far), which are included in the result."""
    if requested_count <= 0:
        return []

    ordered_words = sorted(words, key=lambda word: (word.start, word.end))
    sentences = _sentence_spans(ordered_words)
    candidates: list[ClipResult] = []
    for start_index in range(len(sentences)):
        options = _bounds_from_start(
            sentences,
            start_index,
            ordered_words,
            total_video_duration,
        )
        if not options:
            continue
        start_time, end_time = options[0]
        if moment_problem(clip_text(ordered_words, start_time, end_time)) is not None:
            continue
        candidates.append(
            ClipResult(
                start_time=start_time,
                end_time=end_time,
                viral_title="Key Highlight",
                caption="Check out this moment!",
                hashtags=["viral", "shorts", "podcast"],
                virality_score=0,
            )
        )

    return _select_spaced_candidates(candidates, list(already or []), requested_count, total_video_duration)


def _has_minimum_gap(candidate: ClipResult, chosen: list[ClipResult]) -> bool:
    return all(
        abs(candidate.start_time - selected.start_time)
        >= MIN_START_SEPARATION_SECONDS
        and (
            (
                candidate.end_time <= selected.start_time
                and selected.start_time - candidate.end_time
                >= MIN_CLIP_GAP_SECONDS
            )
            or (
                selected.end_time <= candidate.start_time
                and candidate.start_time - selected.end_time
                >= MIN_CLIP_GAP_SECONDS
            )
        )
        for selected in chosen
    )


def _select_spaced_candidates(
    candidates: list[ClipResult],
    initially_selected: list[ClipResult],
    requested_count: int,
    video_duration: float,
) -> list[ClipResult]:
    selected = list(initially_selected)
    remaining = [candidate for candidate in candidates if candidate not in selected]
    while len(selected) < requested_count:
        eligible = [
            candidate
            for candidate in remaining
            if _has_minimum_gap(candidate, selected)
        ]
        if not eligible:
            break
        if not selected:
            target_center = video_duration / 2
            next_candidate = min(
                eligible,
                key=lambda candidate: (
                    abs((candidate.start_time + candidate.end_time) / 2 - target_center),
                    candidate.start_time,
                ),
            )
        else:
            selected_centers = [
                (clip.start_time + clip.end_time) / 2 for clip in selected
            ]
            next_candidate = max(
                eligible,
                key=lambda candidate: (
                    min(
                        abs(
                            (candidate.start_time + candidate.end_time) / 2
                            - center
                        )
                        for center in selected_centers
                    ),
                    -candidate.start_time,
                ),
            )
        selected.append(next_candidate)
        remaining.remove(next_candidate)
    return selected


REPLAY_PEAK_THRESHOLD = 0.5
REPLAY_SKIP_OPENING_SECONDS = 20.0


def replay_peaks(
    heatmap: list[dict] | None,
    top_n: int = 6,
) -> list[tuple[float, float, float]]:
    """Turn YouTube's "Most replayed" heatmap into (start, end, intensity) peaks.

    Adjacent high-intensity buckets are merged into one region. The opening
    seconds are skipped because playback start always looks "replayed".
    """
    if not heatmap:
        return []
    points: list[tuple[float, float, float]] = []
    for item in heatmap:
        try:
            start = float(item["start_time"])
            end = float(item["end_time"])
            value = float(item["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(end) and end > start:
            points.append((start, end, value))
    points.sort()
    regions: list[tuple[float, float, float]] = []
    for start, end, value in points:
        if value < REPLAY_PEAK_THRESHOLD or end <= REPLAY_SKIP_OPENING_SECONDS:
            continue
        if regions and start - regions[-1][1] <= 1.0:
            previous_start, _previous_end, previous_value = regions[-1]
            regions[-1] = (previous_start, end, max(previous_value, value))
        else:
            regions.append((start, end, value))
    regions.sort(key=lambda region: region[2], reverse=True)
    return regions[:top_n]


def format_replay_peaks(peaks: list[tuple[float, float, float]]) -> str:
    return "\n".join(
        f"{start:.0f}-{end:.0f}s (replay intensity {value:.2f})"
        for start, end, value in peaks
    )


def prepare_candidates(
    proposals: list[ClipResult],
    words: list[Word],
    video_duration: float,
) -> list[ClipResult]:
    """Snap proposals to sentence boundaries, fix or drop weak moments, best first."""
    ordered_words = sorted(words, key=lambda word: (word.start, word.end))
    sentences = _sentence_spans(ordered_words)
    candidates_by_interval: dict[tuple[float, float], ClipResult] = {}
    dropped = 0
    for proposed_clip in proposals:
        normalized = _normalize_clip(
            proposed_clip,
            video_duration,
            words,
            ordered_words=ordered_words,
            sentences=sentences,
        )
        if normalized is None:
            continue
        normalized = _fix_or_drop(normalized, ordered_words, ordered_words, sentences, video_duration)
        if normalized is None:
            dropped += 1
            continue
        interval = (normalized.start_time, normalized.end_time)
        existing = candidates_by_interval.get(interval)
        if existing is None or normalized.virality_score > existing.virality_score:
            candidates_by_interval[interval] = normalized
    if dropped:
        logger.info("Dropped %d weak moment(s) (mid-thought start, ad, intro or outro).", dropped)
    return sorted(
        candidates_by_interval.values(),
        key=lambda clip: clip.virality_score,
        reverse=True,
    )


def choose_clips(
    candidates: list[ClipResult],
    words: list[Word],
    video_duration: float,
    requested_count: int,
) -> list[ClipResult]:
    """Best-scored candidates that don't overlap; spaced fallback picks fill any gap."""
    selected: list[ClipResult] = []
    for candidate in sorted(candidates, key=lambda clip: clip.virality_score, reverse=True):
        if _has_minimum_gap(candidate, selected):
            selected.append(candidate)
        if len(selected) == requested_count:
            break
    if len(selected) < requested_count:
        missing = requested_count - len(selected)
        # The spaced picks must fit around the clips already chosen; picking them
        # separately often left a lead with 2 clips, which fails the whole lead.
        selected = _fallback_clips(requested_count, video_duration, words, already=selected)
        logger.info("Only %d strong moment(s); filling up to %d with spaced picks.", requested_count - missing, missing)
    return sorted(selected[:requested_count], key=lambda clip: clip.start_time)


def cap_clip_length(clips: list[ClipResult], words: list[Word], max_seconds: float) -> list[ClipResult]:
    """Clips longer than ``max_seconds`` end earlier: on the last finished sentence that
    fits (when that still leaves MIN_CLIP_DURATION), else after the last whole word that
    fits. Shorter clips are unchanged. (YouTube blocks Shorts over one minute that carry
    a copyright claim, so Make clips' "59 s max" uses 59.)"""
    capped: list[ClipResult] = []
    for clip in clips:
        if clip.end_time - clip.start_time <= max_seconds:
            capped.append(clip)
            continue
        latest = clip.start_time + max_seconds - POST_ROLL_SECONDS
        inside = [word for word in words if word.start >= clip.start_time and word.end <= latest]
        sentence_ends = [
            word for word in inside
            if _ends_sentence(word.text) and word.end - clip.start_time >= MIN_CLIP_DURATION - POST_ROLL_SECONDS
        ]
        last = (sentence_ends or inside or [None])[-1]
        end = min(clip.start_time + max_seconds, last.end + POST_ROLL_SECONDS) if last else clip.start_time + max_seconds
        logger.info("Clip at %.1f s: %.1f s cut to %.1f s (%s max).", clip.start_time,
                    clip.end_time - clip.start_time, end - clip.start_time, f"{max_seconds:g} s")
        capped.append(clip.model_copy(update={"end_time": end}))
    return capped


def finalize_candidates(
    proposals: list[ClipResult],
    words: list[Word],
    video_duration: float,
    requested_count: int,
) -> list[ClipResult]:
    """Snap proposals to sentence boundaries and pick the best non-overlapping set.

    Used for both the OpenAI picker and the Gemini picker so every clip follows
    the same 30-60 s, sentence-aligned, well-spaced rules. Proposals are ranked
    by virality_score; any shortfall is filled with spaced heuristic clips.
    """
    return choose_clips(prepare_candidates(proposals, words, video_duration), words, video_duration, requested_count)


def select_clips(
    words: list[Word],
    video_duration: float,
    requested_count: int,
    settings: Settings,
    replay_peaks_hint: list[tuple[float, float, float]] | None = None,
) -> list[ClipResult]:
    if not settings.openai_api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is required for hook selection. Copy .env.example to .env "
            "and add your API key."
        )
    if requested_count < 1:
        raise ValueError("The requested clip count must be at least 1.")
    if not math.isfinite(video_duration) or video_duration <= 0:
        raise ValueError("The source video must have a positive, finite duration.")
    transcript = format_timeline(words)
    candidate_count = requested_count + 3
    client = OpenAI(api_key=settings.openai_api_key)
    completion = client.chat.completions.parse(
        model=settings.openai_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Find strong self-contained short-form moments in the timestamped "
                    "transcript. Return the requested number of candidates, ordered by "
                    "virality_score descending. Target 30-60 seconds per candidate, "
                    "stay within the source duration, and prefer complete sentences "
                    "and natural thought boundaries. Final clips start with a "
                    "0.15-second pre-roll and end at a finished thought with up to "
                    "0.4 seconds of post-roll, while staying within 30-60 seconds. "
                    "Snap endings to natural silence of at least 0.35 seconds where "
                    "available. Every candidate must be a completely distinct "
                    "moment covering a different topic or idea from a different "
                    "part of the video. "
                    "Do NOT select overlapping timestamps or the same scene with "
                    "different lengths. "
                    "Favor an immediate hook, a clear payoff, emotional specificity, "
                    "novelty, or useful insight. When 'Most replayed' moments are "
                    "listed, viewers rewatched those parts most: prefer candidates "
                    "that contain them, as long as the clip still stands alone. "
                    "For each candidate, provide a concise "
                    "viral_title, ready-to-post caption, and short list of hashtags "
                    "without # symbols."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Source duration: {video_duration:.2f} seconds\n"
                    f"Requested candidates: {candidate_count}\n"
                    f"Final clips to select: {requested_count}\n"
                    + (
                        "Most replayed moments (YouTube viewers):\n"
                        f"{format_replay_peaks(replay_peaks_hint)}\n"
                        if replay_peaks_hint
                        else ""
                    )
                    + "Timestamped transcript (seconds):\n"
                    f"{transcript}"
                ),
            },
        ],
        response_format=ClipSelection,
    )

    message = completion.choices[0].message
    if message.parsed is None:
        return _fallback_clips(requested_count, video_duration, words)

    return finalize_candidates(
        list(message.parsed.clips),
        words,
        video_duration,
        requested_count,
    )
