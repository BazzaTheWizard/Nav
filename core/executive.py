"""Round 45: LinkedIn B2B executive mode (b2b_clean_mode).

Corporate clients care about brand safety and executive polish, so by default a clip gets
NO mid-clip graphics: no split-screen cards, no newspaper tear-outs, no scraped Wikipedia
images, third-party logos or 1-2-3 bullet cards. The speaker stays full-screen; retention
comes from the climax cold open (core.climax_hook), the tight opening (core.opening) and
the smooth camera path (core.camera_path, 1.04x -> 1.10x).

VISUAL_OVERLAYS in .env (Settings.visual_overlays):
- "executive_minimal" (default): no graphics, plus one optional lower-third ID (name and
  title or company) from 1.0 s to 4.5 s of the main clip, only when it's certain who is on
  screen (the Round 33 face checks in core.name_cards). Never the wrong name on a face.
- "none": no graphics and no lower-third.
- "full": the creator look of rounds 28-41 (graphics director, cards, tear-outs).

The captions' look is the "linkedin_executive" style (CAPTION_STYLE, the default from
Round 45): Inter Display SemiBold in sentence case, two calm lines over a soft scrim, the
line filling from silver to white as it's spoken, one accent underline on the spoken figure
or company. No bounce, no scale, no all-caps. write_ass() writes it.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
FONTS = Path(__file__).resolve().parents[1] / "assets" / "fonts"

OVERLAY_MODES = ("executive_minimal", "none", "full")
DEFAULT_OVERLAYS = "executive_minimal"
EXECUTIVE_STYLE = "linkedin_executive"

# ---------------------------------------------------------------- the caption design
FONT = "NavExec Inter"  # assets/fonts/NavExec-InterDisplay-SemiBold.ttf (Inter Display, weight 600)
TEXT_FONT = "NavExec Inter Text"  # assets/fonts/NavExec-InterText-Regular.ttf (Inter, weight 450)
FONT_FILE = FONTS / "NavExec-InterDisplay-SemiBold.ttf"
TEXT_FONT_FILE = FONTS / "NavExec-InterText-Regular.ttf"
SIZE = 62  # ASS size (line height); the letters are ~51 px em on the 1080x1920 frame
SPACING = -0.5
LINE_CHARS = 30  # one line holds this many characters (880 px usable width)
MAX_CHARS = 56  # a caption (two lines) holds at most this many
MAX_GAP = 0.35  # a pause longer than this starts a new caption
MAX_SECONDS = 3.6  # ...and so does a caption running longer than this
WEAK_ENDINGS = frozenset("a an the to of and or but in on at for with from by as is are was were my your our their his her "
                         "its that this which who than into about".split())
HOLD = 0.5  # a caption stays up to this long after its last word (until the next one)
MARGIN_V = 680  # bottom edge at y = 1240: the same safe zone as the creator captions
MARGIN_H = 100
SILVER = "#A1A1AA"  # words not yet spoken
WHITE = "#FFFFFF"  # spoken
SOFT_WHITE = "#E4E4E7"  # the underline when the client has no brand accent
FADE_MS = 100
HALO = r"\bord5\3c&H000000&\3a&HA6&\blur8\shad0"  # black at 35%, soft: legible on bright frames
# The soft band behind the caption zone. Round 49: barely there (about 8% black at its centre,
# was 40%) and no longer edge to edge: the per-letter halo above carries legibility on bright
# frames, the band only takes the edge off a white wall or shirt behind the words, so it never
# reads as a grey box or a stripe across the frame.
SCRIM_BAND = (1010, 1320)
SCRIM_X = (110, 970)  # inset from the frame's edges; the blur feathers it out from there
SCRIM_ALPHA = "&HEB&"
SCRIM_BLUR = 70

# ---------------------------------------------------------------- the lower-third ID
LOWER_THIRD = (1.0, 4.5)  # clip seconds of the main clip (after the cold open)
LT_X, LT_TOP = 72, 930
NAME_SIZE, ROLE_SIZE = 46, 32
ROLE_COLOUR = "#D4D4D8"
NAME_MAX, ROLE_MAX = 32, 42


@dataclass(frozen=True)
class LowerThird:
    name: str
    role: str = ""
    start: float = LOWER_THIRD[0]
    end: float = LOWER_THIRD[1]
    how: str = ""  # how we know it's them (for the log)


def overlay_mode(settings: Any = None) -> str:
    """"executive_minimal" (default), "none" or "full": VISUAL_OVERLAYS in .env, else the
    settings' visual_overlays."""
    chosen = (os.getenv("VISUAL_OVERLAYS") or getattr(settings, "visual_overlays", "") or DEFAULT_OVERLAYS)
    chosen = chosen.strip().lower()
    return chosen if chosen in OVERLAY_MODES else DEFAULT_OVERLAYS


def title_card_on(settings: Any = None) -> bool:
    """Round 48: the creator title pill (core.title_card) only in the creator look
    (VISUAL_OVERLAYS=full) or with EXEC_TITLE_CARD=1. The executive look is the captions and
    the lower-third ID; the hook line goes in the LinkedIn post."""
    if os.getenv("EXEC_TITLE_CARD", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return overlay_mode(settings) == "full"


# ---------------------------------------------------------------- the topic header (Round 49)
TOPIC_SECONDS = 3.5  # on screen from the first frame, fading out by 3.5 s
TOPIC_STYLE = "executive"  # core.title_card.STYLES: Inter Display on a quiet dark glass card


def topic_header_on(settings: Any = None) -> bool:
    """Round 49: the executive topic header, a clean minimal title card with the clip's topic
    for its first TOPIC_SECONDS, then a fade. In the executive looks only (the creator look has
    its own title pill, title_card_on), unless EXEC_TOPIC_HEADER=0."""
    if os.getenv("EXEC_TOPIC_HEADER", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return not title_card_on(settings)


def title_plan(settings: Any = None) -> tuple[bool, str, float, bool]:
    """(show a title card?, its style, its seconds, executive header?) for a render: the
    creator pill (title_card_on, the settings' style and seconds) or the executive topic
    header (TOPIC_STYLE for TOPIC_SECONDS), or none."""
    if title_card_on(settings):
        return (True, getattr(settings, "hook_title_style", None) or "light",
                float(getattr(settings, "hook_title_seconds", 4.0) or 0.0), False)
    if topic_header_on(settings):
        return True, TOPIC_STYLE, TOPIC_SECONDS, True
    return False, "", 0.0, False


def b2b_clean_mode(settings: Any = None) -> bool:
    """True unless VISUAL_OVERLAYS=full: no mid-clip graphics of any kind."""
    return overlay_mode(settings) != "full"


def ass_colour(hex_colour: str) -> str:
    value = hex_colour.lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        value = "FFFFFF"
    return f"&H{value[4:6]}{value[2:4]}{value[0:2]}&".upper()


def accent(explicit: str | None = None) -> str:
    """The underline and ID bar colour: EXEC_ACCENT in .env, else the client's brand accent
    (storage/brand_kit.json) if one was set, else soft white."""
    from core.captions import DEFAULT_ACCENT

    if os.getenv("EXEC_ACCENT"):
        return os.getenv("EXEC_ACCENT", "").strip()
    if explicit and explicit.upper() != DEFAULT_ACCENT.upper():
        return explicit
    try:
        from core.edl import load_brand_kit

        kit = load_brand_kit().accent
        if kit and kit.upper() != DEFAULT_ACCENT.upper():
            return kit
    except Exception:  # noqa: BLE001
        pass
    return SOFT_WHITE


# ---------------------------------------------------------------- caption text
def _caption_token(text: str) -> str:
    from core.subtitle_engine import _safe_ass_text

    token = _safe_ass_text(text)
    return "I" if token == "i" else re.sub(r"^i(?=['’](?:m|ve|ll|d)\b)", "I", token)


def _drop_trailing(token: str) -> str:
    """A caption's last word loses a trailing comma or full stop ('?' and '!' stay)."""
    if token.endswith("..."):
        return token[:-3]
    if token.endswith(",") or token.endswith(";"):
        return token[:-1]
    if token.endswith(".") and not re.search(r"(?:^|\b)[A-Za-z]\.[A-Za-z]\.$", token):
        return token[:-1]
    return token


def chunks(words: list[Any]) -> list[list[Any]]:
    """Calm two-line captions: a new one at a sentence end, a pause over MAX_GAP, past
    MAX_CHARS characters or MAX_SECONDS seconds; after a comma once there's enough to read."""
    groups: list[list[Any]] = []
    current: list[Any] = []
    for word in words:
        if current:
            text = " ".join(w.text for w in [*current, word])
            last = current[-1].text.rstrip("\"'”’)")
            if (last.endswith((".", "?", "!")) or float(word.start) - float(current[-1].end) > MAX_GAP
                    or len(text) > MAX_CHARS or float(word.end) - float(current[0].start) > MAX_SECONDS
                    or (last.endswith((",", ";", ":")) and len(" ".join(w.text for w in current)) >= 18)):
                groups.append(current)
                current = []
        current.append(word)
    if current:
        groups.append(current)
    for k in range(len(groups) - 1):  # never end a caption on "a", "the", "to" ... (it reads "hiring a")
        group, after = groups[k], groups[k + 1]
        while (len(group) > 2 and re.sub(r"\W", "", group[-1].text).lower() in WEAK_ENDINGS
               and not group[-1].text.endswith((",", ".", "?", "!", ";", ":"))
               and float(after[0].start) - float(group[-1].end) <= MAX_GAP):
            after.insert(0, group.pop())
    merged: list[list[Any]] = []
    for group in groups:  # a lone word that isn't a sentence of its own joins the caption before it
        if (merged and len(group) == 1 and not merged[-1][-1].text.rstrip("\"'”’)").endswith((".", "?", "!"))
                and float(group[0].start) - float(merged[-1][-1].end) <= MAX_GAP
                and len(" ".join(w.text for w in [*merged[-1], *group])) <= MAX_CHARS + 4):
            merged[-1] = [*merged[-1], *group]
        else:
            merged.append(group)
    return merged


def line_break(tokens: list[str]) -> int | None:
    """Where the second line starts (a token index), or None for one line: balanced, after
    punctuation when it can, never a one-word line."""
    text = " ".join(tokens)
    if len(text) <= LINE_CHARS or len(tokens) < 2:
        return None
    best, best_cost = None, None
    for k in range(1, len(tokens)):
        first, second = " ".join(tokens[:k]), " ".join(tokens[k:])
        cost = max(len(first), len(second)) + abs(len(first) - len(second)) * 0.35
        if tokens[k - 1].endswith((",", ";", ":", "—")):
            cost -= 5
        if k == 1 or k == len(tokens) - 1:
            cost += 20  # an orphan
        if max(len(first), len(second)) > LINE_CHARS + 2:
            cost += 50
        if best_cost is None or cost < best_cost:
            best, best_cost = k, cost
    return best


def _keyword(group: list[Any]) -> int | None:
    """At most one keyword per caption: the first figure, else the first named company."""
    from core import captions as semantic

    try:
        kinds = semantic.keyword_kinds(w.text for w in group)
    except Exception:  # noqa: BLE001
        return None
    for wanted in (semantic.FIGURE, semantic.ENTITY):
        for k, kind in enumerate(kinds):
            if kind == wanted and len(re.sub(r"\W", "", group[k].text)) >= 2:
                return k
    return None


# ---------------------------------------------------------------- the ASS file
def header(hook_style: bool = True) -> str:
    base = f"""[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ShortForm,{FONT},{SIZE},&H00FFFFFF,&H00FFFFFF,&HA6000000,&HFF000000,0,0,0,0,100,100,{SPACING},0,1,0,0,2,{MARGIN_H},{MARGIN_H},{MARGIN_V},1
Style: Exec,{FONT},{SIZE},&H00FFFFFF,&H00FFFFFF,&HFF000000,&HFF000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
"""
    if hook_style:
        base += f"Style: Hook,{FONT},56,&H00FFFFFF,&H00FFFFFF,&H50000000,&H00000000,0,0,0,0,100,100,0,0,3,18,0,8,120,120,300,1\n"
    return base + "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"


def _t(cs: int) -> str:
    from core.subtitle_engine import _ass_time

    return _ass_time(cs / 100)


def _cs(seconds: float) -> int:
    return max(0, round(seconds * 100))


def _measure(text: str, font_file: Path, size: int) -> float:
    """Text width in pixels at an ASS size (libass: size = ascender + descender)."""
    em = size * 2048 / 2478
    try:
        from PIL import ImageFont

        return float(ImageFont.truetype(str(font_file), max(1, round(em))).getlength(text))
    except Exception:  # noqa: BLE001
        return len(text) * em * 0.55


def write_ass(
    ordered: list[Any],
    destination: Path,
    clip_duration: float | None,
    *,
    highlight_active_word: bool = True,
    hook_title: str | None = None,
    hook_seconds: float = 0.0,
    seam_spans: Any = None,
    seam_title: tuple[float, int] | None = None,
    caption_top: int | None = None,
    accent_colour: str | None = None,
    lower_third: LowerThird | None = None,
) -> Path:
    """The linkedin_executive captions for the clip's words (clip seconds, already cleaned
    by core.subtitle_engine). Same positions as the creator style: the usual safe zone, on the
    seam of a two-person split, under a title card on the seam, or at ``caption_top``."""
    from core.subtitle_engine import GENERIC_HOOK_TITLES, SEAM_POSITION, _hook_text

    line_accent = ass_colour(accent(accent_colour))
    silver, white = ass_colour(SILVER), ass_colour(WHITE)
    clip_end_cs = _cs(clip_duration) if clip_duration is not None else None
    seams = [(_cs(a), _cs(b)) for a, b in (seam_spans or [])]
    title_until_cs = _cs(seam_title[0]) if seam_title else 0
    under_title = f"{{\\an8\\pos(540,{seam_title[1] + 14})}}" if seam_title else ""
    events: list[str] = []

    def emit(layer: int, a: int, b: int, text: str, style: str = "ShortForm") -> None:
        if clip_end_cs is not None:
            b = min(b, clip_end_cs)
        if b > a:
            events.append(f"Dialogue: {layer},{_t(a)},{_t(b)},{style},,0,0,0,,{text}")

    def where(line_start_cs: int, a: int, b: int) -> tuple[str, bool]:
        """(position tags, in the usual bottom zone?) for a caption shown a..b."""
        under = line_start_cs < title_until_cs
        if caption_top is not None:
            return (under_title if under else f"{{\\an8\\pos(540,{caption_top})}}"), False
        if any(s <= (a + b) / 2 < e for s, e in seams):
            return (under_title if under else SEAM_POSITION), False
        return "", True

    if hook_title and hook_seconds > 0 and hook_title.strip().casefold() not in GENERIC_HOOK_TITLES:
        emit(5, 0, _cs(hook_seconds), _hook_text(hook_title), style="Hook")

    groups = chunks(ordered)
    sentence_start = True
    bottom_spans: list[tuple[int, int]] = []
    previous_end = 0
    for index, group in enumerate(groups):
        start_cs = max(previous_end, _cs(max(0.0, float(group[0].start) - 0.05)))
        next_start = _cs(float(groups[index + 1][0].start)) if index + 1 < len(groups) else None
        end_cs = _cs(float(group[-1].end) + HOLD)
        if next_start is not None:
            end_cs = min(end_cs, max(_cs(float(group[-1].end)), next_start))
        if clip_end_cs is not None:
            end_cs = min(end_cs, clip_end_cs)
        if end_cs <= start_cs:
            continue
        tokens = [_caption_token(w.text) for w in group]
        if sentence_start and tokens[0][:1].islower():
            tokens[0] = tokens[0][:1].upper() + tokens[0][1:]
        sentence_start = group[-1].text.rstrip("\"'”’)").endswith((".", "?", "!"))
        tokens[-1] = _drop_trailing(tokens[-1]) or tokens[-1]
        cut = line_break(tokens)
        keyword = _keyword(group)
        joins = [" " if k != cut else "\\N" for k in range(1, len(tokens))]

        def line(parts: list[str]) -> str:
            out = parts[0]
            for sep, part in zip(joins, parts[1:]):
                out += sep + part
            return out

        # One state per word: words up to the spoken one are white, the rest silver.
        bounds = [start_cs]
        for word in group[1:]:
            bounds.append(min(end_cs, max(bounds[-1], _cs(float(word.start)))))
        bounds.append(end_cs)
        if not highlight_active_word:
            bounds = [start_cs, end_cs]
        states = len(bounds) - 1
        gap_before = index == 0 or start_cs - previous_end >= 15
        gap_after = next_start is None or next_start - end_cs >= 15
        pos, bottom = where(start_cs, start_cs, end_cs)
        if bottom:
            bottom_spans.append((start_cs, end_cs))
        for state in range(states):
            a, b = bounds[state], bounds[state + 1]
            if b <= a:
                continue
            spoken = state if highlight_active_word else len(tokens) - 1
            fade_in = FADE_MS if state == 0 and gap_before else 0
            fade_out = FADE_MS if state == states - 1 and gap_after else 0
            fad = f"\\fad({fade_in},{fade_out})" if fade_in or fade_out else ""
            halo = line(tokens)
            emit(1, a, b, f"{pos}{{\\1a&HFF&{HALO}{fad}}}{halo}")
            if keyword is not None and spoken >= keyword:
                parts = [f"{{\\1a&H00&\\u1}}{tok}{{\\u0\\1a&HFF&}}" if k == keyword else tok
                         for k, tok in enumerate(tokens)]
                emit(2, a, b, f"{pos}{{\\1c{line_accent}\\1a&HFF&\\bord0\\shad0\\blur0{fad}}}{line(parts)}")
            parts = [f"{{\\1c{white if k <= spoken else silver}}}{tok}" for k, tok in enumerate(tokens)]
            emit(3, a, b, f"{pos}{{\\bord0\\shad0\\blur0{fad}}}{line(parts)}")
        previous_end = end_cs

    # The soft scrim behind the usual caption zone, steady while captions show there.
    if bottom_spans and caption_top is None:
        merged: list[list[int]] = []
        for a, b in bottom_spans:
            if merged and a - merged[-1][1] <= 150:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        top, bottom = SCRIM_BAND
        left, right = SCRIM_X
        for a, b in merged:
            emit(0, max(0, a - 20), b + 20,
                 f"{{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c&H000000&\\1a{SCRIM_ALPHA}\\blur{SCRIM_BLUR}\\fad(200,200)}}"
                 f"m {left} {top} l {right} {top} l {right} {bottom} l {left} {bottom}{{\\p0}}", style="Exec")

    if lower_third is not None:
        events.extend(lower_third_events(lower_third, line_accent, clip_end_cs))

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(header() + "\n".join(events) + "\n", encoding="utf-8")
    return destination


def lower_third_events(lt: LowerThird, accent_ass: str, clip_end_cs: int | None = None) -> list[str]:
    """The executive ID: an accent bar that grows in, the name and the title sliding in
    24 px with a fade over a soft blurred plate; all of it fades out at ``lt.end``."""
    from core.subtitle_engine import _safe_ass_text

    a, b = _cs(lt.start), _cs(lt.end)
    if clip_end_cs is not None:
        b = min(b, clip_end_cs - 50)
    if b - a < 150:
        return []
    name, role = _safe_ass_text(lt.name[:NAME_MAX]), _safe_ass_text(lt.role[:ROLE_MAX])
    width = max(_measure(name, FONT_FILE, NAME_SIZE), _measure(role, TEXT_FONT_FILE, ROLE_SIZE) if role else 0)
    height = 118 if role else 76
    x0, y0, x1, y1 = LT_X - 22, LT_TOP - 20, LT_X + 24 + width + 30, LT_TOP + height + 2
    r = 18
    plate = (f"m {x0 + r} {y0} l {x1 - r} {y0} b {x1} {y0} {x1} {y0} {x1} {y0 + r} l {x1} {y1 - r} "
             f"b {x1} {y1} {x1} {y1} {x1 - r} {y1} l {x0 + r} {y1} b {x0} {y1} {x0} {y1} {x0} {y1 - r} "
             f"l {x0} {y0 + r} b {x0} {y0} {x0} {y0} {x0 + r} {y0}")
    role_ass = ass_colour(ROLE_COLOUR)
    bar_h = height - 4
    out = [
        f"Dialogue: 6,{_t(a)},{_t(b)},Exec,,0,0,0,,{{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c&H000000&\\1a&H8C&"
        f"\\blur16\\fad(260,320)}}{plate}{{\\p0}}",
        f"Dialogue: 7,{_t(a)},{_t(b)},Exec,,0,0,0,,{{\\an7\\pos({LT_X},{LT_TOP})\\p1\\bord0\\shad0\\1c{accent_ass}"
        f"\\fscy0\\t(40,320,\\fscy100)\\fad(120,320)}}m 0 0 l 4 0 l 4 {bar_h} l 0 {bar_h}{{\\p0}}",
        f"Dialogue: 8,{_t(a)},{_t(b)},Exec,,0,0,0,,{{\\an7\\move({LT_X},{LT_TOP - 4},{LT_X + 24},{LT_TOP - 4},60,420)"
        f"\\fn{FONT}\\fs{NAME_SIZE}\\fsp-0.3\\1c&HFFFFFF&\\bord0\\shad0\\fad(340,320)}}{name}",
    ]
    if role:
        out.append(
            f"Dialogue: 8,{_t(a)},{_t(b)},Exec,,0,0,0,,{{\\an7\\move({LT_X},{LT_TOP + 56},{LT_X + 24},{LT_TOP + 56},140,500)"
            f"\\fn{TEXT_FONT}\\fs{ROLE_SIZE}\\1c{role_ass}\\bord0\\shad0\\fad(420,320)}}{role}")
    return out


# ---------------------------------------------------------------- who the ID names
def _client_speaker() -> Any:
    """The client's own executive (storage/brand_kit.json exec_name / exec_title), if set."""
    from core.graphics_director import Speaker
    from core.edl import load_brand_kit

    kit = load_brand_kit()
    name = str(getattr(kit, "exec_name", "") or "").strip()
    if len(name.split()) < 2:
        return None
    return Speaker(name, str(getattr(kit, "exec_title", "") or "").strip()[:ROLE_MAX], "host")


_IDENTITIES: dict[tuple, tuple[dict, list[str]]] = {}  # once per video, not once per clip


def plan_lower_third(source: Any, plan: Any, clip_start: float, clip_end: float, info: Any,
                     clip_words: list[Any], video_words: list[Any] | None, split: Any = None,
                     *, identify: Any = None, options: Any = None,
                     identity_source: Any = None) -> tuple[LowerThird | None, str]:
    """(the ID, a note). Only when the person on screen at LOWER_THIRD is certain: the named
    speaker's face (core.name_cards.identify: a self-introduction, the channel's returning
    host, or the only person) alone on screen for the whole span. Names come from the client's
    brand kit (exec_name / exec_title) or the video's own title and description; never invented."""
    if overlay_mode() != "executive_minimal":
        return None, "No lower-third: VISUAL_OVERLAYS isn't executive_minimal."
    if plan is None or source is None:
        return None, "No lower-third: this layout has no face track to check who is on screen."
    if clip_end - clip_start < LOWER_THIRD[1] + 2.0:
        return None, "No lower-third: the clip is too short."
    a, b = clip_start + LOWER_THIRD[0], clip_start + LOWER_THIRD[1]
    if split is not None and any(s < b and a < e for s, e in getattr(split, "spans", []) or []):
        return None, "No lower-third: the opening is the two-speaker split view."
    from core import graphics_director, name_cards

    speakers = []
    client = _client_speaker()
    if client is not None:
        speakers.append(client)
    try:
        for found in graphics_director.video_speakers(None, info):
            if not any(found.name.lower() == s.name.lower() for s in speakers):
                speakers.append(found)
    except Exception:  # noqa: BLE001
        pass
    if not speakers:
        return None, "No lower-third: the video's title and description name no one (set exec_name in the brand kit)."
    identify = identify or name_cards.identify
    options = options or name_cards.options
    who = identity_source or source  # Round 46: a trimmed clip is still checked against the whole video
    key = (str(who), getattr(info, "channel", ""), tuple(s.name for s in speakers), identify)
    try:
        if key not in _IDENTITIES:
            _IDENTITIES[key] = identify(who, plan, speakers, video_words, getattr(info, "channel", ""), Path(str(who)).stem)
        ids, _notes = _IDENTITIES[key]
    except Exception as exc:  # noqa: BLE001
        return None, f"No lower-third: face check failed ({exc})."
    for speaker in speakers:
        if speaker.name not in ids:
            continue
        face, how = ids[speaker.name]
        try:
            shots = options(source, plan, clip_start, clip_end, face)
        except Exception as exc:  # noqa: BLE001
            return None, f"No lower-third: face check failed ({exc})."
        for shot in shots:
            if shot.kind == "solo" and shot.start <= a + 0.05 and shot.end >= b - 0.05:
                return LowerThird(speaker.name, speaker.role, how=how), f"Lower-third: {speaker.name} ({how})."
    return None, "No lower-third: no named speaker is certainly the one on screen from 1.0 to 4.5 s."
