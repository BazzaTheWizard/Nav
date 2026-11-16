"""Round 59: the Narrate pop-ups and sound cues, drawn on the finished vertical clip.

render_selected_clip has already added the captions, title card and loudness. This pass adds:
- [TEXT: "..."]: a bold pop-up near the top third (above the captions), on screen POPUP_SECONDS,
  with a whoosh as it lands.
- [ZOOM]: a whoosh on the punch-in (the zoom itself was cut into the footage by assemble).
- [SFX: whoosh | tick | sub_hit]: that sound at the line's first word.

Times come in assembled-video seconds and are mapped through RenderedClip.output_time, so they
stay on the right word even when the pipeline trimmed pauses or tightened the opening.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

from core import audio_polish
from core.narrate.assemble import Cue

ROOT = Path(__file__).resolve().parents[2]
FONT = ROOT / "assets" / "fonts" / "Montserrat-ExtraBold.ttf"
POPUP_SECONDS = 1.8
POPUP_Y = 420  # px from the top of the 1080x1920 frame: clear of the title card and the captions
SFX_DB = -8.0


def _probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "default=nw=1:nk=1", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def sound_cues(cues: list[Cue], to_output: Callable[[float], float]) -> list[tuple[float, str]]:
    found: list[tuple[float, str]] = []
    for cue in cues:
        at = round(to_output(cue.at), 3)
        if cue.kind in {"TEXT", "ZOOM"}:
            found.append((at, "whoosh"))
        elif cue.kind == "SFX":
            kind = (cue.value or "whoosh").strip().lower().replace(" ", "_")
            found.append((at, kind if kind in audio_polish.SYNTHS else "whoosh"))
    return sorted(found)


def popup_filters(cues: list[Cue], to_output: Callable[[float], float], work: Path) -> list[str]:
    filters: list[str] = []
    for n, cue in enumerate(c for c in cues if c.kind == "TEXT" and c.value.strip()):
        a = to_output(cue.at)
        b = a + POPUP_SECONDS
        text_file = work / f"popup_{n}.txt"
        text_file.write_text(cue.value.strip().upper())
        # fade in over 0.12 s, out over 0.2 s
        alpha = f"if(lt(t,{a:.3f}+0.12),(t-{a:.3f})/0.12,if(gt(t,{b:.3f}-0.2),({b:.3f}-t)/0.2,1))"
        filters.append(
            f"drawtext=fontfile='{FONT}':textfile='{text_file}':fontsize=92:fontcolor=black:"
            f"box=1:boxcolor=0xFFD400:boxborderw=28:x=(w-text_w)/2:y={POPUP_Y}:"
            f"alpha='{alpha}':enable='between(t,{a:.3f},{b:.3f})'")
    return filters


def finish(clip_path: str | Path, cues: list[Cue], to_output: Callable[[float], float],
           out_path: str | Path | None = None) -> Path:
    """Draw the pop-ups and mix the sound cues into the clip. Returns the finished file."""
    clip_path = Path(clip_path)
    out_path = Path(out_path) if out_path else clip_path.with_name(clip_path.stem + "_final.mp4")
    work = clip_path.parent / f"{clip_path.stem}_finish"
    work.mkdir(parents=True, exist_ok=True)
    duration = _probe_duration(clip_path)
    filters = popup_filters(cues, to_output, work)
    track = audio_polish.cue_track(sound_cues(cues, to_output), duration, work / "cues.wav")
    if not filters and track is None:
        shutil.copyfile(clip_path, out_path)
        shutil.rmtree(work, ignore_errors=True)
        return out_path
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(clip_path)]
    if track is not None:
        cmd += ["-i", str(track)]
    graph: list[str] = []
    if filters:
        graph.append("[0:v]" + ",".join(filters) + "[v]")
    if track is not None:
        graph.append(f"[1:a]volume={SFX_DB}dB,aresample=48000[s];[0:a][s]amix=inputs=2:duration=first:normalize=0[a]")
    cmd += ["-filter_complex", ";".join(graph),
            "-map", "[v]" if filters else "0:v", "-map", "[a]" if track is not None else "0:a"]
    cmd += (["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"] if filters else ["-c:v", "copy"])
    cmd += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_path)]
    subprocess.run(cmd, check=True)
    shutil.rmtree(work, ignore_errors=True)
    return out_path
