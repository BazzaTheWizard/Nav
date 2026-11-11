"""Round 57: the project file (``<clip>.project.json``), schema ``nav.project/1``.

One JSON file per finished clip that says what the clip is made of, so a review comment
("shift the crop left at 0:10.3") can later change one part and re-render without
breaking the rest. The rule that keeps everything in sync:

    Every time in the file is a SOURCE time (seconds in the original video).
    The output timeline is never stored per item; it is derived from ``timeline.segments``.

So moving a cut, dropping the teaser or trimming a pause changes only ``segments``: the
words, cards and camera moves keep their source times and land wherever the new cut puts
them. Output times are worked out with ``to_output`` / ``to_source``.

Layout (v1):
    schema, id, created
    source        {path, fps, width, height}
    output        {width, height, fps, duration, frames}
    timeline      {segments: [{id, kind: "teaser" | "body", source_in, source_out, output_in, output_out}]}
                  output_in/out are stored for convenience and checked by validate(); segments
                  tile the output with no gaps, in order.
    framing       {layout, crop {x, y, w, h}, full_frame [[in, out]], split [[in, out]],
                   camera [{id, kind, at, push, hold, release, amount}], renderer {...}}
                  ``renderer`` holds the FFmpeg-level details (the crop-follow expression):
                  a record of the render, not something to edit.
    layers        captions {words: [{id, text, start, end}]}, title {text, seconds},
                  lower_third {name, role} | null, cards [{id, template, start, end, ...}]
    deliverables  {dirty, srt, clean, dialogue, sfx, ...} (paths relative to the file)
    comments      [] (review comments: {id, at (source s), output_at, text, status})
    history       [] (the edits applied, newest last)

v1 records a render; it does not yet drive one. The re-render from this file (needed for
comment edits) comes next: the renderer will read ``framing`` and ``layers`` from here.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA = "nav.project/1"
SUFFIX = ".project.json"
_EPS = 1e-3


def path_for(clip: Path) -> Path:
    return Path(clip).with_name(Path(clip).stem + SUFFIX)


# ----------------------------------------------------------------------- time mapping
class SourceMap:
    """Turns the pipeline's working times into source seconds.

    The pipeline works on ``working`` seconds: the source itself, or (Round 46 mid-clip trim)
    a trimmed copy that starts at 0. ``keep``: the kept (start, end) spans of the trim, in
    seconds from ``clip_start`` on the source; None when there was no trim."""

    def __init__(self, clip_start: float, keep: list[tuple[float, float]] | None = None) -> None:
        self.clip_start = float(clip_start)
        self.keep = [(float(a), float(b)) for a, b in keep] if keep else None

    def point(self, t: float) -> float:
        if self.keep is None:
            return round(float(t), 6)
        cursor = 0.0
        for a, b in self.keep:
            if t <= cursor + (b - a) + 1e-9:
                return round(self.clip_start + a + max(0.0, t - cursor), 6)
            cursor += b - a
        a, b = self.keep[-1]
        return round(self.clip_start + b, 6)

    def ranges(self, t0: float, t1: float) -> list[tuple[float, float, float]]:
        """[t0, t1] in working seconds as (source_in, source_out, working_in) pieces, split
        wherever the trim cut something out."""
        if self.keep is None:
            return [(round(t0, 6), round(t1, 6), t0)]
        out, cursor = [], 0.0
        for a, b in self.keep:
            lo, hi = max(t0, cursor), min(t1, cursor + (b - a))
            if hi - lo > 1e-6:
                out.append((round(self.clip_start + a + lo - cursor, 6), round(self.clip_start + a + hi - cursor, 6), lo))
            cursor += b - a
        return out


def segments(smap: SourceMap, body: tuple[float, float], teaser: Any = None) -> list[dict[str, Any]]:
    """The output timeline: the teaser's pieces (if any), then the body's, end to end."""
    out: list[dict[str, Any]] = []
    cursor = 0.0
    spans: list[tuple[str, float, float]] = []
    if teaser is not None:
        spans.append(("teaser", float(teaser.start), float(teaser.start) + float(teaser.offset)))
    spans.append(("body", float(body[0]), float(body[1])))
    for kind, t0, t1 in spans:
        for s_in, s_out, _w in smap.ranges(t0, t1):
            length = s_out - s_in
            out.append({"id": f"seg-{len(out) + 1}", "kind": kind, "source_in": s_in, "source_out": s_out,
                        "output_in": round(cursor, 6), "output_out": round(cursor + length, 6)})
            cursor += length
    return out


def to_output(project: dict[str, Any], source_t: float, kind: str | None = "body") -> float | None:
    """Where source second ``source_t`` plays in the finished clip (None: cut out). ``kind``:
    which segments to look in ("body" by default, since a teaser repeats body lines; None: any)."""
    for seg in project["timeline"]["segments"]:
        if kind is not None and seg["kind"] != kind:
            continue
        if seg["source_in"] - _EPS <= source_t <= seg["source_out"] + _EPS:
            return round(seg["output_in"] + (source_t - seg["source_in"]), 6)
    return None


def to_source(project: dict[str, Any], output_t: float) -> tuple[float, dict[str, Any]]:
    """The source second (and segment) on screen at output second ``output_t``. Anchors a
    review comment: the player gives an output time, the file stores the source time."""
    segs = project["timeline"]["segments"]
    for seg in segs:
        if seg["output_in"] - _EPS <= output_t < seg["output_out"] - _EPS:
            return round(seg["source_in"] + (output_t - seg["output_in"]), 6), seg
    last = segs[-1]
    return round(last["source_out"], 6), last


# ----------------------------------------------------------------------- building
def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


def build(*, clip_id: str, source: dict[str, Any], output: dict[str, Any], smap: SourceMap,
          body: tuple[float, float], teaser: Any = None, layout: str, crop: Any,
          full_frame: Iterable[tuple[float, float]] = (), split: Iterable[tuple[float, float]] = (),
          camera: Iterable[Any] = (), x_expression: str | None = None, words: Iterable[Any] = (),
          title: dict[str, Any] | None = None, lower_third: Any = None, cards: Iterable[Any] = (),
          deliverables: dict[str, str] | None = None, notes: Iterable[str] = ()) -> dict[str, Any]:
    """The project dict. Spans, camera moves and words are in clip seconds (seconds from
    ``body[0]`` on the working timeline), as the pipeline has them; cards in working seconds."""
    start = float(body[0])
    to_src: Callable[[float], float] = lambda clip_t: smap.point(start + float(clip_t))
    span = lambda spans: [[to_src(a), to_src(b)] for a, b in spans]
    moves = []
    for k, m in enumerate(camera, 1):
        m = _plain(m)
        moves.append({"id": f"cam-{k}", "kind": m.get("kind", "move"), "at": to_src(m["start"]),
                      "push": m.get("push"), "hold": round(float(m["hold_end"]) - float(m["start"]), 6),
                      "release": m.get("release"), "amount": m.get("amount"), "base": m.get("base", 0.0)})
    card_nodes = []
    for k, card in enumerate(cards, 1):
        c = _plain(card)
        c = {"id": f"card-{k}", **c, "start": smap.point(float(c["start"])), "end": smap.point(float(c["end"]))}
        for row in c.get("rows", []):
            for key in ("reveal_at", "reveal_to"):
                if row.get(key) is not None:
                    row[key] = smap.point(float(row[key]))
        card_nodes.append(c)
    lower = _plain(lower_third)
    if lower is not None and not isinstance(lower, dict):
        lower = {"name": getattr(lower, "name", ""), "role": getattr(lower, "role", "")}
    if isinstance(lower, dict):
        lower = {"name": lower.get("name", ""), "role": lower.get("role", "")}
    box = _plain(crop)
    return {
        "schema": SCHEMA,
        "id": clip_id,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "output": output,
        "timeline": {"segments": segments(smap, body, teaser)},
        "framing": {
            "layout": layout,
            "crop": {"x": box.get("x"), "y": box.get("y"), "w": box.get("width"), "h": box.get("height")}
            if isinstance(box, dict) else None,
            "full_frame": span(full_frame),
            "split": span(split),
            "camera": moves,
            "renderer": {"crop_x_expression": x_expression} if x_expression else {},
        },
        "layers": {
            "captions": {"words": [{"id": f"w-{k}", "text": str(w.text), "start": to_src(w.start), "end": to_src(w.end)}
                                   for k, w in enumerate(words, 1)]},
            "title": title,
            "lower_third": lower,
            "cards": card_nodes,
        },
        "deliverables": deliverables or {},
        "notes": list(notes),
        "comments": [],
        "history": [],
    }


# ----------------------------------------------------------------------- checking
def validate(project: dict[str, Any]) -> list[str]:
    """What's wrong with a project file ([] when it's sound)."""
    problems: list[str] = []
    if project.get("schema") != SCHEMA:
        problems.append(f"schema is {project.get('schema')!r}, expected {SCHEMA}")
    segs = project.get("timeline", {}).get("segments") or []
    if not segs:
        return problems + ["no timeline segments"]
    ids: list[str] = []
    cursor = 0.0
    for seg in segs:
        ids.append(seg["id"])
        if abs(seg["output_in"] - cursor) > _EPS:
            problems.append(f"{seg['id']} starts at {seg['output_in']} s, expected {cursor:.3f} (gap or overlap)")
        if seg["source_out"] <= seg["source_in"]:
            problems.append(f"{seg['id']} is empty or backwards")
        if abs((seg["output_out"] - seg["output_in"]) - (seg["source_out"] - seg["source_in"])) > _EPS:
            problems.append(f"{seg['id']}: output and source lengths differ")
        cursor = seg["output_out"]
    duration = project.get("output", {}).get("duration")
    fps = project.get("output", {}).get("fps") or 30.0
    if duration is not None and abs(cursor - float(duration)) > 1.5 / float(fps):
        problems.append(f"segments end at {cursor:.3f} s but the clip is {float(duration):.3f} s")
    layers = project.get("layers", {})
    for word in layers.get("captions", {}).get("words", []):
        ids.append(word["id"])
        if to_output(project, (word["start"] + word["end"]) / 2, kind=None) is None:
            problems.append(f"caption {word['id']} ({word['text']!r}) is outside every segment")
    for card in layers.get("cards", []):
        ids.append(card["id"])
    for move in project.get("framing", {}).get("camera", []):
        ids.append(move["id"])
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"duplicate ids: {', '.join(dupes)}")
    return problems


def save(project: dict[str, Any], path: Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps(project, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def probe(video: Path) -> dict[str, Any]:
    """{width, height, fps} of a video ({} when it can't be read)."""
    import shutil
    import subprocess

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return {}
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate", "-of", "json", str(video)],
                         capture_output=True, text=True, timeout=60)
    try:
        stream = json.loads(out.stdout)["streams"][0]
        num, den = (int(x) for x in re.split(r"/", stream["r_frame_rate"]))
        return {"width": stream["width"], "height": stream["height"], "fps": round(num / den, 3) if den else None}
    except (ValueError, KeyError, IndexError, ZeroDivisionError):
        return {}
