from __future__ import annotations

import math
import os
import re
from pathlib import Path

from core.transcriber import Word


MAX_WORDS_PER_CHUNK = 4
# 72 px Arial Black capitals: about 37 px a character, 860 px of usable width.
# 24 characters with wide letters (W, M) wrapped onto a second line.
MAX_CHARACTERS_PER_CHUNK = 21
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
    from core.plain_text import strip_controls  # Round 56: no NUL / escape codes / direction overrides

    return (
        strip_controls(text).replace("\\", "/")
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


# Vertical positions on the 1080x1920 canvas. TikTok, Reels and Shorts draw the
# username, description and buttons over roughly the bottom 35% and the top 15%,
# so captions sit just above 1248 px and the hook title just below 288 px.
CAPTION_MARGIN_V = 680
# Captions during a two-speaker split view: centred on the seam between the panels.
SEAM_POSITION = r"{\an5\pos(540,960)}"
SEAM_CAPTION_ZONE = (860, 1060)  # output pixels they can cover (two lines)
CAPTION_MARGIN_H = 110
HOOK_MARGIN_V = 300
HOOK_MAX_CHARACTERS = 70
HIGHLIGHT_COLOUR = "&H00FFFF&"  # yellow (ASS colours are BGR)
# Round 33: numbers stay green (#34D399, the charts' gain colour) for the whole caption
# line, so the figure the clip is about reads at a glance even before it's spoken. The
# spoken word is still yellow. NUMBER_EMPHASIS=off in .env turns it off.
NUMBER_COLOUR = "&H99D334&"
_NUMBER_TOKEN = re.compile(r"^[\"'(~≈+\-−]*[$€£]?\d")  # "$5T", "40%", "~12X"; not "Q3", "GPT4" or "W3"
_YEAR_TOKEN = re.compile(r"[\"'(]?(?:19|20)\d\d(?:S|'S)?[.,!?;:)\"']*")  # "2023", "1990S": a when, not a figure


def is_figure(token: str) -> bool:
    """A caption word that is a figure (money, a share, a multiple, a count), not a year."""
    return bool(_NUMBER_TOKEN.match(token)) and not _YEAR_TOKEN.fullmatch(token)


def number_emphasis() -> bool:
    return (os.getenv("NUMBER_EMPHASIS") or "on").strip().lower() not in {"off", "0", "false", "no"}
# Caption look. "bold" (default): thick outline and a soft drop shadow.
# "classic": the look before round 12. Pick one with CAPTION_STYLE=classic in .env.
CAPTION_STYLES = {
    "bold": {"outline": 6, "shadow": 3, "shadow_colour": "&H64000000", "blur": 0.6, "emphasis": "balanced"},
    "classic": {"outline": 3, "shadow": 1, "shadow_colour": "&H80000000", "blur": 0.0, "emphasis": "off"},
}

# Active-word emphasis: the spoken word turns yellow and briefly pulses, then
# settles. Every preset keeps the caption line still: the word grows in height
# (not width) and its outline thickens for a moment. Growing the width too, as
# round 12 did (115%), pushed the whole line 24 px sideways on every new word:
# the "shake". Pick one with EMPHASIS_INTENSITY=off|subtle|balanced|punchy in .env.
EMPHASIS_PRESETS: dict[str, dict[str, int] | None] = {
    "off": None,  # colour change only
    "subtle": {"height": 105, "outline_boost": 2, "max_ms": 90},
    "balanced": {"height": 108, "outline_boost": 3, "max_ms": 110},
    "punchy": {"height": 112, "outline_boost": 4, "max_ms": 130},
}
# Pacing: the pulse takes at most this share of the word's time on screen, and
# words shorter than MIN_PULSE_WORD_MS (fast speech) only change colour, so quick
# runs of words don't flicker.
PULSE_SHARE = 0.4
MIN_PULSE_WORD_MS = 120


# Round 45: the default look is "linkedin_executive" (core.executive): Inter Display SemiBold
# in sentence case, two calm lines over a soft scrim, silver -> white as it's spoken. The
# creator looks stay: CAPTION_STYLE=bold (or classic) in .env.
DEFAULT_CAPTION_STYLE = "linkedin_executive"


def caption_style_name(name: str | None = None) -> str:
    return (name or os.getenv("CAPTION_STYLE") or DEFAULT_CAPTION_STYLE).strip().lower()


def caption_style(name: str | None = None) -> dict[str, float | str]:
    chosen = caption_style_name(name)
    return CAPTION_STYLES.get(chosen, CAPTION_STYLES["bold"])


def emphasis_preset(name: str | None = None, style: dict[str, float | str] | None = None) -> dict[str, int] | None:
    """The emphasis preset: ``name``, else EMPHASIS_INTENSITY in .env, else the
    caption style's own default (bold: balanced, classic: off)."""
    chosen = (name or os.getenv("EMPHASIS_INTENSITY") or str((style or caption_style())["emphasis"])).strip().lower()
    return EMPHASIS_PRESETS.get(chosen, EMPHASIS_PRESETS["balanced"])


def _pulse(preset: dict[str, int] | None, outline: float, word_ms: int) -> str:
    """ASS tags for the spoken word's pulse ('' for none), scaled to its length."""
    if preset is None or word_ms < MIN_PULSE_WORD_MS:
        return ""
    duration = int(min(preset["max_ms"], word_ms * PULSE_SHARE))
    base = int(outline)
    return (
        f"\\fscy{preset['height']}\\bord{base + preset['outline_boost']}"
        f"\\t(0,{duration},\\fscy100\\bord{base})"
    )
GENERIC_HOOK_TITLES = frozenset({"key highlight", "clip", ""})


def _hook_text(title: str) -> str:
    text = " ".join(title.split())
    if len(text) > HOOK_MAX_CHARACTERS:
        text = text[:HOOK_MAX_CHARACTERS].rsplit(" ", 1)[0].rstrip(",.;:-") + "…"
    return _safe_ass_text(text)


def create_ass_subtitles(
    words: list[Word],
    output_path: str | Path,
    clip_start_time: float = 0.0,
    clip_duration: float | None = None,
    *,
    highlight_active_word: bool = True,
    hook_title: str | None = None,
    hook_seconds: float = 0.0,
    seam_spans: list[tuple[float, float]] | tuple[tuple[float, float], ...] | None = None,
    seam_title: tuple[float, int] | None = None,
    style: str | None = None,
    emphasis: str | None = None,
    caption_top: int | None = None,
    accent: str | None = None,
    lower_third: object = None,
) -> Path:
    """Write short, clip-relative ASS subtitle cards with non-overlapping timing.

    highlight_active_word: colour the word being spoken in each card.
    hook_title / hook_seconds: show the clip's title at the top for the first
    ``hook_seconds`` (0 turns it off).
    seam_spans: clip-relative spans shown as the two-speaker split view; captions
    there sit on the seam between the two panels instead of over the lower face.
    seam_title: (seconds, bottom y) of a title card shown on the seam (or, with
    ``caption_top``, under the slides); captions in that time sit just under it.
    caption_top: every caption's top edge at this y instead of the usual height
    (the webinar layout puts them under the slides).
    style: "bold" or "classic" (default: CAPTION_STYLE in .env, else "bold").
    emphasis: "off", "subtle", "balanced" or "punchy" (default: EMPHASIS_INTENSITY
    in .env, else the style's own default).
    accent: the brand colour for named companies and countries (Round 36, core.captions:
    figures emerald, tickers and power words gold; default: the brand kit's accent).
    style "linkedin_executive" (Round 45, the default): core.executive.write_ass, with
    ``lower_third`` (a core.executive.LowerThird) drawn from 1.0 to 4.5 s.
    """
    if not words:
        raise ValueError("Cannot create subtitles without word timestamps.")
    ordered = _normalize_words(words, clip_start_time, clip_duration)
    if not ordered:
        raise ValueError("Cannot create subtitles without cues inside the clip.")
    if any(word.end <= word.start for word in ordered):
        raise ValueError("Every subtitle word must have an end time after its start time.")

    if caption_style_name(style) == "linkedin_executive":
        from core import executive

        return executive.write_ass(
            ordered, Path(output_path), clip_duration, highlight_active_word=highlight_active_word,
            hook_title=hook_title, hook_seconds=hook_seconds, seam_spans=seam_spans, seam_title=seam_title,
            caption_top=caption_top, accent_colour=accent, lower_third=lower_third,
        )
    look = caption_style(style)
    header = f"""[Script Info]
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ShortForm,Arial Black,72,&H00FFFFFF,&H0000FFFF,&H00000000,{look["shadow_colour"]},-1,0,0,0,100,100,0,0,1,{look["outline"]},{look["shadow"]},2,{CAPTION_MARGIN_H},{CAPTION_MARGIN_H},{CAPTION_MARGIN_V},1
Style: Hook,Arial Black,58,&H00FFFFFF,&H00FFFFFF,&H50000000,&H00000000,-1,0,0,0,100,100,0,0,3,18,0,8,120,120,{HOOK_MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    clip_end_cs = (
        _centiseconds(clip_duration) if clip_duration is not None else None
    )
    groups = _make_groups(ordered)
    events: list[str] = []

    seams = [(_centiseconds(a), _centiseconds(b)) for a, b in (seam_spans or [])]
    title_until_cs = _centiseconds(seam_title[0]) if seam_title else 0
    under_title = f"{{\\an8\\pos(540,{seam_title[1] + 14})}}" if seam_title else ""

    blur = f"\\blur{look['blur']}" if look["blur"] else ""
    soften = f"{{{blur}}}" if blur else ""
    preset = emphasis_preset(emphasis, look)
    numbers = number_emphasis()
    # Round 36: semantic keywords (core.captions) keep their colour for the whole line and pop when spoken.
    from core import captions as semantic

    keywords = semantic.enabled()
    kind_of = dict(zip(map(id, ordered), semantic.keyword_kinds(w.text for w in ordered))) if keywords else {}
    if not numbers:
        kind_of = {k: v for k, v in kind_of.items() if v != semantic.FIGURE}
    palette = semantic.colours(accent)
    # After the spoken word: back to the plain style (keeping the soft edge).
    restore = f"{{\\r{blur}}}"

    def add_event(start_cs: int, end_cs: int, text: str, style: str = "ShortForm", layer: int = 0,
                  line_start_cs: int | None = None) -> None:
        middle = (start_cs + end_cs) / 2
        # A caption line keeps one position until it ends: one that starts under the title
        # stays there (it never jumps up mid-line when the title leaves).
        under = (start_cs if line_start_cs is None else line_start_cs) < title_until_cs
        if style == "ShortForm":
            text = soften + text
            if caption_top is not None:
                text = (under_title if under else f"{{\\an8\\pos(540,{caption_top})}}") + text
            elif any(a <= middle < b for a, b in seams):
                text = (under_title if under else SEAM_POSITION) + text
        if end_cs > start_cs:
            events.append(
                f"Dialogue: {layer},{_ass_time(start_cs / 100)},"
                f"{_ass_time(end_cs / 100)},{style},,0,0,0,,{text}"
            )

    if hook_title and hook_seconds > 0 and hook_title.strip().casefold() not in GENERIC_HOOK_TITLES:
        hook_end_cs = _centiseconds(hook_seconds)
        if clip_end_cs is not None:
            hook_end_cs = min(hook_end_cs, clip_end_cs)
        add_event(0, hook_end_cs, _hook_text(hook_title), style="Hook", layer=1)

    previous_end_cs = 0
    for index, group in enumerate(groups):
        event_start_cs = max(previous_end_cs, _centiseconds(max(0.0, group[0].start)))
        event_end_cs = _centiseconds(group[-1].end)
        if clip_end_cs is not None:
            event_end_cs = min(event_end_cs, clip_end_cs)
        if index + 1 < len(groups):
            event_end_cs = min(
                event_end_cs,
                _centiseconds(max(0.0, groups[index + 1][0].start)),
            )
        if event_end_cs <= event_start_cs:
            continue

        tokens = [_safe_ass_text(word.text.upper()) for word in group]
        kinds = [kind_of.get(id(word)) if keywords else (semantic.FIGURE if numbers and is_figure(token) else None)
                 for word, token in zip(group, tokens)]
        shown = [f"{{\\c{palette[kind]}}}{token}{restore}" if kind else token
                 for token, kind in zip(tokens, kinds)]  # how each word looks while it isn't the spoken one
        if not highlight_active_word:
            add_event(event_start_cs, event_end_cs, " ".join(shown))
        elif len(group) == 1:
            # A one-word card: the word itself is the spoken word.
            word_ms = (event_end_cs - event_start_cs) * 10
            if kinds[0]:  # a keyword keeps its colour and pops 1.08x
                pop = semantic.keyword_pop(look["outline"], word_ms) if preset is not None else ""
                add_event(event_start_cs, event_end_cs, f"{{\\c{palette[kinds[0]]}{pop}}}{tokens[0]}")
            else:
                pulse = _pulse(preset, look["outline"], word_ms)
                add_event(event_start_cs, event_end_cs, f"{{{pulse}}}{tokens[0]}" if pulse else tokens[0])
        else:
            # One event per word: same card text, the spoken word in yellow.
            boundaries = [event_start_cs]
            for word in group[1:]:
                boundaries.append(
                    min(event_end_cs, max(boundaries[-1], _centiseconds(word.start)))
                )
            boundaries.append(event_end_cs)
            for word_index in range(len(group)):
                word_ms = (boundaries[word_index + 1] - boundaries[word_index]) * 10
                kind = kinds[word_index]
                if kind:  # a keyword keeps its colour and pops 1.08x as it's said
                    spoken = (f"{{\\c{palette[kind]}"
                              f"{semantic.keyword_pop(look['outline'], word_ms) if preset is not None else ''}}}")
                else:
                    spoken = f"{{\\c{HIGHLIGHT_COLOUR}{_pulse(preset, look['outline'], word_ms)}}}"
                text = " ".join(
                    f"{spoken}{token}{restore}" if i == word_index else shown[i]
                    for i, token in enumerate(tokens)
                )
                add_event(boundaries[word_index], boundaries[word_index + 1], text, line_start_cs=event_start_cs)
        previous_end_cs = event_end_cs

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return destination
