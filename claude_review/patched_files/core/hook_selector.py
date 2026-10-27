from __future__ import annotations

import math
import re

from pydantic import BaseModel, Field, model_validator
from openai import OpenAI

from config import Settings
from core.transcriber import Word, format_timeline


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


def _fallback_clips(
    requested_count: int,
    total_video_duration: float,
    words: list[Word],
) -> list[ClipResult]:
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

    return _select_spaced_candidates(candidates, [], requested_count, total_video_duration)


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


def select_clips(
    words: list[Word],
    video_duration: float,
    requested_count: int,
    settings: Settings,
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
                    "novelty, or useful insight. For each candidate, provide a concise "
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
                    "Timestamped transcript (seconds):\n"
                    f"{transcript}"
                ),
            },
        ],
        response_format=ClipSelection,
    )

    message = completion.choices[0].message
    if message.parsed is None:
        return _fallback_clips(requested_count, video_duration, words)

    ordered_words = sorted(words, key=lambda word: (word.start, word.end))
    sentences = _sentence_spans(ordered_words)
    candidates_by_interval: dict[tuple[float, float], ClipResult] = {}
    for proposed_clip in message.parsed.clips:
        normalized = _normalize_clip(
            proposed_clip,
            video_duration,
            words,
            ordered_words=ordered_words,
            sentences=sentences,
        )
        if normalized is None:
            continue
        interval = (normalized.start_time, normalized.end_time)
        existing = candidates_by_interval.get(interval)
        if existing is None or normalized.virality_score > existing.virality_score:
            candidates_by_interval[interval] = normalized

    candidates = sorted(
        candidates_by_interval.values(),
        key=lambda clip: clip.virality_score,
        reverse=True,
    )
    selected: list[ClipResult] = []
    for candidate in candidates:
        if _has_minimum_gap(candidate, selected):
            selected.append(candidate)
        if len(selected) == requested_count:
            break
    if len(selected) < requested_count:
        fallback_candidates = _fallback_clips(
            requested_count,
            video_duration,
            words,
        )
        selected = _select_spaced_candidates(
            fallback_candidates,
            selected,
            requested_count,
            video_duration,
        )
    return sorted(selected[:requested_count], key=lambda clip: clip.start_time)
