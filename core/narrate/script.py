"""Round 59: the Narrate script format, its parser and the script writer.

A script is plain text, one spoken line per row. Tags in [brackets] at the start of a line
are not spoken:

    HOOK: You've been doing this wrong since day one.
    [driving, city, night] Most players never touch the garage menu.
    [TEXT: "SAVE $40K"] [ZOOM] Here's the trick nobody tells you.
    # comments start with a hash and are ignored

- Shot tags ([driving, city, night]) say what footage to show under the line. A line with
  no shot tags keeps the previous line's tags.
- Markers are UPPER-CASE: [TEXT: "..."], [ZOOM], [SFX: whoosh]. They become pop-ups,
  punch-ins and sound cues at the line's first word.
- "HOOK:" marks the opening line; its text is also the title card.
- A "SOURCE: url" row records where a fact came from (kept, never spoken).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

MARKERS = ("TEXT", "ZOOM", "SFX", "FREEZE")
_TAG_BLOCK = re.compile(r"^\s*\[([^\]]*)\]\s*")


@dataclass
class Marker:
    kind: str  # TEXT, ZOOM, SFX or FREEZE
    value: str = ""


@dataclass
class Line:
    text: str  # the spoken words only
    tags: list[str] = field(default_factory=list)  # shot tags (inherited when the line has none)
    markers: list[Marker] = field(default_factory=list)
    hook: bool = False


@dataclass
class Script:
    lines: list[Line]
    sources: list[str] = field(default_factory=list)

    @property
    def hook(self) -> str:
        for line in self.lines:
            if line.hook:
                return line.text
        return self.lines[0].text if self.lines else ""

    @property
    def spoken(self) -> str:
        """The words you read, as one paragraph (what the voiceover is aligned to)."""
        return " ".join(line.text for line in self.lines)

    def word_count(self) -> int:
        return sum(len(line.text.split()) for line in self.lines)


def _marker(block: str) -> Marker | None:
    head, _, rest = block.partition(":")
    kind = head.strip().upper()
    if kind not in MARKERS or head.strip() != head.strip().upper():
        return None
    return Marker(kind, rest.strip().strip('"').strip("'"))


def parse(text: str) -> Script:
    """Parse a script. Raises ValueError when it has no spoken line."""
    lines: list[Line] = []
    sources: list[str] = []
    last_tags: list[str] = []
    for raw in text.splitlines():
        row = raw.strip()
        if not row or row.startswith("#"):
            continue
        if row.upper().startswith("SOURCE:"):
            sources.append(row.split(":", 1)[1].strip())
            continue
        hook = False
        if row.upper().startswith("HOOK:"):
            hook, row = True, row.split(":", 1)[1].strip()
        tags: list[str] = []
        markers: list[Marker] = []
        while (match := _TAG_BLOCK.match(row)):
            block = match.group(1).strip()
            marker = _marker(block)
            if marker is not None:
                markers.append(marker)
            elif block:
                tags.extend(t.strip().lower() for t in block.split(",") if t.strip())
            row = row[match.end():]
        spoken = row.strip()
        if not spoken:
            continue  # a row of tags only: they wait for the next spoken line
        if tags:
            last_tags = tags
        lines.append(Line(spoken, list(last_tags), markers, hook))
    if not lines:
        raise ValueError("The script has no spoken lines.")
    return Script(lines, sources)


# --- the writer --------------------------------------------------------------------------

FORMATS = {
    "tips": "a tip or a mistake to avoid in GTA 6 (\"Don't do this in GTA 6\")",
    "news": "a GTA 6 news or update story (Rockstar announcement, patch, GTA Online)",
    "details": "a detail most players missed in GTA 6",
    "comparison": "a GTA 6 vs GTA 5 (or vs real life) comparison",
}

SYSTEM_PROMPT = """You write voiceover scripts for vertical GTA 6 Shorts/Reels/TikToks, read aloud by the creator.
Rules:
- 110-150 spoken words. The first line starts with "HOOK:" and must stop the scroll in under 3 seconds.
- One idea per line, short sentences, spoken English, no emojis, no hashtags.
- Begin each line with shot tags in brackets describing gameplay footage that fits the line, chosen from:
  driving, flying, boat, walking, running, city, beach, swamp, highway, rooftop, interior, shop, garage,
  day, night, sunset, rain, chase, police, crowd, nature, aerial. Example: [driving, city, night]
- Optional pop-up markers after the tags: [TEXT: "short on-screen text"], [ZOOM], [SFX: whoosh].
  Use at most 3 markers in the script.
- Only state facts you were given in the brief. Never invent prices, numbers, locations or mechanics.
  If the brief lacks a fact the topic needs, write around it.
- Never describe graphic violence. Nothing violent in the hook.
- End with a line that loops back to the hook or gives a reason to follow.
- After the script, add one "SOURCE: <url>" row per source in the brief.
Return only the script."""


def writer_prompt(topic: str, fmt: str, brief: str) -> str:
    kind = FORMATS.get(fmt, fmt)
    return f"Format: {kind}\nTopic: {topic}\n\nBrief (facts you may use, with sources):\n{brief.strip() or '(none)'}"


def check(script: Script) -> list[str]:
    """The script's problems, in words (empty: good to record)."""
    problems: list[str] = []
    words = script.word_count()
    if words < 60:
        problems.append(f"Only {words} spoken words; a Short needs about 110-150.")
    if words > 190:
        problems.append(f"{words} spoken words; over about 60 seconds read aloud. Cut to 150.")
    if not any(line.hook for line in script.lines):
        problems.append('No "HOOK:" line.')
    if not any(line.tags for line in script.lines):
        problems.append("No shot tags: every line will get random footage.")
    if sum(len(line.markers) for line in script.lines) > 4:
        problems.append("More than 4 pop-up markers; it will look busy.")
    return problems


def write(topic: str, fmt: str = "tips", brief: str = "", model: str | None = None) -> Script:
    """Ask the model for a script (OPENAI_API_KEY in .env, as for the rest of Nav)."""
    from openai import OpenAI

    client = OpenAI()
    response = client.chat.completions.create(
        model=model or os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": writer_prompt(topic, fmt, brief)}],
        temperature=0.8,
    )
    return parse(response.choices[0].message.content or "")


def to_json(script: Script) -> str:
    return json.dumps({
        "lines": [{"text": l.text, "tags": l.tags, "hook": l.hook,
                   "markers": [{"kind": m.kind, "value": m.value} for m in l.markers]} for l in script.lines],
        "sources": script.sources,
    }, indent=2)
