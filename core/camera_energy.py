"""Round 41, Pillar 4: human-editor camera energy on the full-screen speaker.

Emphasis punch-ins (a digital dolly): where the speaker leans on a word (its loudness in
the clip's top 20 %) or delivers the thesis line, the picture eases from 1.00x to 1.10x
over 0.22 s (a cubic ease-out, centred on the eye-line: PULL_IN_CENTER_Y, the point the
hook's pull-in uses), holds through the emphasis clause (2.0-3.5 s) and cuts cleanly back
to 1.00x on the next sentence boundary. One punch at a time, at least MIN_GAP apart,
about one every SECONDS_PER_PUNCH; never in the hook's pull-in, under a split-screen card
or a tear-out (core.pipeline passes those as ``exclude``).

FFmpeg draws it with the same sub-pixel perspective warp as the pull-in
(core.video_renderer), on the finished picture under the title card and the captions, so
text never zooms. The zoom is a function of the frame number, so every frame is exact.

Fluid panning: one_euro() is the One-Euro filter (Casiez et al., 2012) on the face track
before the dead-band re-framing (core.camera_crop.reframes): heavy smoothing while the
head barely moves (micro-nods never reach the crop), less lag when the speaker really
moves.
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

PUNCH = 1.10
RAMP = 0.22  # seconds from 1.00x to 1.10x
HOLD = (2.0, 3.5)  # seconds a punch lasts (its ramp included)
TOP_SHARE = 0.20  # a word is emphasis when it's among the loudest 20 % of the clip's words
MIN_GAP = 3.0  # seconds of 1.00x between two punches
SECONDS_PER_PUNCH = 9.0
MIN_WORD_SECONDS = 0.12
AUDIO_RATE = 16_000


def word_loudness(source: str | Path, start: float, end: float, words: list[Any]) -> list[float]:
    """Each word's RMS loudness (dBFS) in the source's audio. ``words``: clip seconds."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return [-90.0] * len(words)
    raw = subprocess.run([ffmpeg, "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
                          "-vn", "-ac", "1", "-ar", str(AUDIO_RATE), "-f", "s16le", "-"], capture_output=True).stdout
    audio = np.frombuffer(raw, "<i2").astype(np.float64) / 32768.0
    return loudness_of(audio, words)


def loudness_of(audio: np.ndarray, words: list[Any], rate: int = AUDIO_RATE) -> list[float]:
    out = []
    for w in words:
        a, b = int(float(w.start) * rate), int(float(w.end) * rate)
        part = audio[max(0, a):max(a + 1, b)]
        rms = float(np.sqrt(np.mean(part ** 2))) if part.size else 0.0
        out.append(20 * math.log10(max(rms, 1e-9)))
    return out


def _sentence_end(words: list[Any], k: int) -> bool:
    if re.search(r"[.?!][\"'”’)]*$", str(words[k].text)):
        return True
    return k + 1 < len(words) and float(words[k + 1].start) - float(words[k].end) >= 0.5


def _span_from(words: list[Any], k: int, duration: float) -> tuple[float, float] | None:
    """The punch for an emphasis on word k: in RAMP before it (it lands on the word), out
    on the first sentence boundary 2.0-3.5 s later (between the sentence's last word and
    the next one), or after the last word that keeps it under 3.5 s."""
    start = float(words[k].start) - RAMP
    best = None
    for j in range(k, len(words)):
        end_word = float(words[j].end)
        boundary = (end_word + float(words[j + 1].start)) / 2 if j + 1 < len(words) else end_word + 0.1
        if boundary - start > HOLD[1]:
            break
        if boundary - start >= HOLD[0]:
            best = boundary
            if _sentence_end(words, j):
                return start, min(boundary, duration - 0.5)
    if best is not None:
        return start, min(best, duration - 0.5)
    return None


def emphasis_spans(words: list[Any], loudness: list[float], duration: float,
                   exclude: list[tuple[float, float]] | None = None, thesis_at: float | None = None,
                   earliest: float = 1.0) -> list[tuple[float, float]]:
    """The punch-ins for a clip (clip seconds): the thesis line's first, then the loudest
    words' (top 20 %), each 2.0-3.5 s, landing on its word and out on a sentence boundary."""
    if not words:
        return []
    usable = [(k, db) for k, db in enumerate(loudness) if float(words[k].end) - float(words[k].start) >= MIN_WORD_SECONDS]
    seeds: list[int] = []
    if thesis_at is not None:
        seeds += [min(range(len(words)), key=lambda k: abs(float(words[k].start) - thesis_at))]
    if usable:
        cut = float(np.quantile([db for _k, db in usable], 1 - TOP_SHARE))
        seeds += [k for k, db in sorted(usable, key=lambda item: -item[1]) if db >= cut]
    blocked = list(exclude or [])
    budget = max(1, int(duration // SECONDS_PER_PUNCH))
    spans: list[tuple[float, float]] = []
    for k in seeds:
        if len(spans) >= budget:
            break
        span = _span_from(words, k, duration)
        if span is None:
            continue
        a, b = span
        if a < earliest or b - a < HOLD[0] - 1e-6 or b > duration - 0.5:
            continue
        if any(a < y + MIN_GAP and x - MIN_GAP < b for x, y in spans) or any(a < y and x < b for x, y in blocked):
            continue
        spans.append((round(a, 3), round(b, 3)))
    return sorted(spans)


def zoom_at(t: float, spans: list[tuple[float, float]]) -> float:
    """The zoom at clip second t (the same curve the filter draws)."""
    for a, b in spans:
        if a <= t < b:
            u = min(1.0, (t - a) / RAMP)
            return 1.0 + (PUNCH - 1.0) * (1 - (1 - u) ** 3)
    return 1.0


def punch_filter(filter_complex: str, spans: list[tuple[float, float]], rate: float, center_y: float) -> str:
    """The punches on the finished picture ([base], 1080x1920): a perspective warp whose
    zoom follows zoom_at frame by frame (frame n is clip second n / rate)."""
    if not spans or filter_complex.count("[base]") != 1:
        return filter_complex
    ramp = max(1.0, RAMP * rate)
    terms = []
    for a, b in spans:
        fa, fb = int(round(a * rate)), int(round(b * rate))
        terms.append(f"between(in\\,{fa}\\,{fb - 1})*(1-pow(1-min(1\\,(in-{fa})/{ramp:.4f})\\,3))")
    zoom = f"(1+{PUNCH - 1.0:.4f}*({'+'.join(terms)}))"
    cx, cy = "W/2", f"H*{center_y}"
    left, top = f"{cx}-({cx})/{zoom}", f"{cy}-({cy})/{zoom}"
    right, bottom = f"{cx}+(W-{cx})/{zoom}", f"{cy}+(H-{cy})/{zoom}"
    corners = (f"x0='{left}':y0='{top}':x1='{right}':y1='{top}':"
               f"x2='{left}':y2='{bottom}':x3='{right}':y3='{bottom}'")
    return (filter_complex.replace("[base]", "[unpunched]")
            + f";[unpunched]perspective={corners}:interpolation=cubic:eval=frame[base]")


# ------------------------------------------------------------------ One-Euro smoothing
def one_euro(track: list[tuple[float, float]], min_cutoff: float = 0.05, beta: float = 0.0015,
             d_cutoff: float = 1.0) -> list[tuple[float, float]]:
    """The One-Euro filter over (seconds, value) samples: a low-pass whose cutoff rises
    with speed (min_cutoff + beta * |speed|), so a still head is smoothed hard and a real
    move still follows. The defaults suit the face tracker's 2 samples a second: a still
    head's jitter drops about 3x, a walk across the frame lifts the cutoff past 1 Hz."""
    if len(track) < 2:
        return list(track)

    def alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / max(dt, 1e-6))

    out = [track[0]]
    x_prev, dx_prev = float(track[0][1]), 0.0
    for (t0, _v0), (t1, value) in zip(track, track[1:]):
        dt = max(1e-3, t1 - t0)
        dx = (float(value) - x_prev) / dt
        dx_hat = dx_prev + alpha(d_cutoff, dt) * (dx - dx_prev)
        cutoff = min_cutoff + beta * abs(dx_hat)
        x_hat = x_prev + alpha(cutoff, dt) * (float(value) - x_prev)
        out.append((t1, x_hat))
        x_prev, dx_prev = x_hat, dx_hat
    return out
