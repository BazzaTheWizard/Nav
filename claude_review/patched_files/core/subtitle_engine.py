from __future__ import annotations

import math
import re
from pathlib import Path

from core.transcriber import Word


MAX_WORDS_PER_CHUNK = 4
MAX_CHARACTERS_PER_CHUNK = 24
MAX_WORD_GAP_SECONDS = 0.4
_SOUND_CUE_PATTERN = re.compile(r"\[[^\]]*?\]|\([^)]*?\)")
_SPEAKER_MARKER_PATTERN = re.compile(r">>|>|--")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_REPEATED_PUNCTUATION_PATTERN = re.compile(r"([,.;:!?])\1+")


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def _safe_ass_text(text: str) -> str:
    return (
        text.replace("\\", "/")
        .replace("{", "（")
        .replace("}", "）")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _clean_caption_text(text: str) -> str:
    text = _SOUND_CUE_PATTERN.sub(" ", text)
    text = _SPEAKER_MARKER_PATTERN.sub(" ", text)
    text = _WHITESPACE_PATTERN.sub(" ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = _REPEATED_PUNCTUATION_PATTERN.sub(r"\1", text)
    return text.strip(" \t\r\n-–—")


def _split_phrases(words: list[Word]) -> list[Word]:
    """Split multi-word entries into single words with estimated timing.

    Word-level transcripts pass through unchanged. Sentence-level entries (for
    example YouTube caption lines or a provider that only returns segments)
    would otherwise become one giant subtitle card; here each word gets a slice
    of the phrase's time, weighted by its length.
    """
    result: list[Word] = []
    for word in words:
        tokens = word.text.split()
        if len(tokens) <= 1:
            result.append(word)
            continue
        weights = [len(token) + 1 for token in tokens]
        total_weight = sum(weights)
        duration = word.end - word.start
        cursor = word.start
        for token, weight in zip(tokens, weights):
            token_end = cursor + duration * weight / total_weight
            result.append(Word(text=token, start=cursor, end=token_end))
            cursor = token_end
    return result


def _make_groups(
    words: list[Word],
) -> list[list[Word]]:
    groups: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        proposed_text = " ".join(item.text for item in [*current, word])
        split_for_length = (
            current
            and len(proposed_text) > MAX_CHARACTERS_PER_CHUNK
        )
        split_for_pause = current and word.start - current[-1].end > MAX_WORD_GAP_SECONDS
        split_for_punctuation = current and current[-1].text.endswith(
            (".", ",", "?", "!", "。", "，", "？", "！")
        )
        split_for_word_count = len(current) >= MAX_WORDS_PER_CHUNK
        if current and (
            split_for_length
            or split_for_pause
            or split_for_punctuation
            or split_for_word_count
        ):
            groups.append(current)
            current = []
        current.append(word)
    if current:
        groups.append(current)
    return groups


def _normalize_words(
    words: list[Word],
    clip_start_time: float,
    clip_duration: float | None,
) -> list[Word]:
    if not math.isfinite(clip_start_time) or clip_start_time < 0:
        raise ValueError("Clip start time must be a finite, non-negative number.")
    if clip_duration is not None and (
        not math.isfinite(clip_duration) or clip_duration <= 0
    ):
        raise ValueError("Clip duration must be a positive, finite number.")

    clip_end_time = (
        clip_start_time + clip_duration if clip_duration is not None else math.inf
    )
    normalized: list[Word] = []
    for word in words:
        if word.end <= clip_start_time or word.start >= clip_end_time:
            continue
        text = _clean_caption_text(word.text)
        if not text:
            continue
        adjusted_start = max(0.0, word.start - clip_start_time)
        adjusted_end = max(0.0, word.end - clip_start_time)
        if clip_duration is not None:
            adjusted_start = min(adjusted_start, clip_duration)
            adjusted_end = min(adjusted_end, clip_duration)
        if adjusted_end > adjusted_start:
            normalized.append(
                Word(text=text, start=adjusted_start, end=adjusted_end)
            )
    return _split_phrases(sorted(normalized, key=lambda word: word.start))


def _centiseconds(seconds: float) -> int:
    return max(0, round(seconds * 100))


def create_ass_subtitles(
    words: list[Word],
    output_path: str | Path,
    clip_start_time: float = 0.0,
    clip_duration: float | None = None,
) -> Path:
    """Write short, clip-relative ASS subtitle chunks with non-overlapping timing."""
    if not words:
        raise ValueError("Cannot create subtitles without word timestamps.")
    ordered = _normalize_words(words, clip_start_time, clip_duration)
    if not ordered:
        raise ValueError("Cannot create subtitles without cues inside the clip.")
    if any(word.end <= word.start for word in ordered):
        raise ValueError("Every subtitle word must have an end time after its start time.")

    header = """[Script Info]
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ShortForm,Arial Black,72,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,90,90,140,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    groups = _make_groups(ordered)
    events: list[str] = []
    previous_end_cs = 0
    for index, group in enumerate(groups):
        event_start_cs = max(
            previous_end_cs,
            _centiseconds(max(0.0, group[0].start)),
        )
        event_end_cs = min(
            _centiseconds(group[-1].end),
            _centiseconds(clip_duration) if clip_duration is not None else math.inf,
        )
        if index + 1 < len(groups):
            event_end_cs = min(
                event_end_cs,
                _centiseconds(max(0.0, groups[index + 1][0].start)),
            )
        if event_end_cs <= event_start_cs:
            continue

        text = _safe_ass_text(" ".join(word.text for word in group).upper())
        events.append(
            "Dialogue: 0,"
            f"{_ass_time(event_start_cs / 100)},"
            f"{_ass_time(event_end_cs / 100)},ShortForm,,0,0,140,,{text}"
        )
        previous_end_cs = event_end_cs

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return destination
