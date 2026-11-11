"""Round 35: frame-locked micro sound effects for the graphics.

Two ultra-subtle UI sounds, synthesised with numpy (deterministic, no samples to license)
and cached in assets/sfx/:
  - whoosh: a soft, low sub-whoosh (180 ms, peak -22 dBFS) on the frame a Bento visual
    comes in;
  - tick:   a muted mechanical tick (40 ms, peak -26 dBFS) when a step box or a comparison
    side lights up.
cue_track() lays every cue into one track at its exact sample (from core.cards.cues: the
graphic's own choreography, warped like its frames), and core.dynamic_layout mixes that
track under the clip's audio in the same FFmpeg pass that lays the graphics over the
video: the video stream is untouched by it (-fps_mode passthrough), so no frame moves and
lip-sync can't drift. SFX=off in .env turns it off.
"""
from __future__ import annotations

import os
import wave
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RATE = 48_000
SOUNDS = {"whoosh": (0.180, -22.0), "tick": (0.040, -26.0),
          "sub_hit": (0.180, -9.0)}  # kind: (seconds, peak dBFS); sub_hit: Round 40's teaser stitch
MIN_TICK_GAP = 0.14  # seconds between two ticks at least


def enabled() -> bool:
    return (os.getenv("SFX") or "on").strip().lower() not in {"off", "0", "false", "no"}


def sfx_dir() -> Path:
    return Path(os.getenv("NAV_SFX_DIR") or PROJECT_ROOT / "assets" / "sfx")


def _peak(signal: np.ndarray, dbfs: float) -> np.ndarray:
    return signal / (np.max(np.abs(signal)) or 1.0) * 10 ** (dbfs / 20)


def _lowpass(signal: np.ndarray, cutoff: np.ndarray | float) -> np.ndarray:
    """A one-pole low-pass whose cutoff can sweep over time (Hz, per sample)."""
    cutoff = np.broadcast_to(np.asarray(cutoff, dtype=float), signal.shape)
    alpha = 1 - np.exp(-2 * np.pi * cutoff / RATE)
    out, y = np.empty_like(signal), 0.0
    for k, (x, a) in enumerate(zip(signal, alpha)):
        y += a * (x - y)
        out[k] = y
    return out


def whoosh() -> np.ndarray:
    """A soft sub-whoosh: band-limited noise whose brightness sweeps down (900 -> 140 Hz)
    over a 55-Hz-to-38-Hz sub sine, a 25 ms rise and a smooth fall. 180 ms, -22 dBFS."""
    seconds, dbfs = SOUNDS["whoosh"]
    n = int(RATE * seconds)
    t = np.arange(n) / RATE
    noise = np.random.default_rng(35).standard_normal(n)
    air = _lowpass(_lowpass(noise, np.geomspace(900, 140, n)), np.geomspace(1200, 200, n))
    air -= _lowpass(air, 60.0)  # no rumble below the sub
    phase = 2 * np.pi * np.cumsum(np.linspace(55, 38, n)) / RATE
    sub = np.sin(phase)
    envelope = np.minimum(1.0, t / 0.025) * np.cos(np.clip((t - 0.025) / (seconds - 0.025), 0, 1) * np.pi / 2) ** 2
    mix = (0.75 * air / (np.max(np.abs(air)) or 1) + 0.45 * sub) * envelope
    return _peak(mix, dbfs)


def tick() -> np.ndarray:
    """A muted mechanical tick: a short 1.9 kHz knock with a hint of body (420 Hz) and a
    noise transient, gone in 40 ms. -26 dBFS."""
    seconds, dbfs = SOUNDS["tick"]
    n = int(RATE * seconds)
    t = np.arange(n) / RATE
    knock = np.sin(2 * np.pi * 1900 * t) * np.exp(-t / 0.0045)
    body = 0.5 * np.sin(2 * np.pi * 420 * t) * np.exp(-t / 0.009)
    click = 0.35 * np.random.default_rng(36).standard_normal(n) * np.exp(-t / 0.0012)
    mix = _lowpass(knock + body + click, 5200.0) * np.minimum(1.0, t / 0.0008)
    return _peak(mix, dbfs)


SUB_HIT_FROM, SUB_HIT_TO = 80.0, 45.0  # Hz: the thud's pitch drops from 80 to 45 Hz
SUB_HIT_DECAY = 0.180  # seconds: the whole hit, gone (-40 dB) by the end of it
SUB_HIT_ATTACK = 0.003  # a 3 ms rise: a thump, never a click


def sub_hit() -> np.ndarray:
    """Round 40: the sub-bass hit under the climax teaser's white flash. A pure sine whose
    pitch drops exponentially from 80 Hz to 45 Hz, a 3 ms rise and a tight exponential
    decay (-40 dB at 180 ms), the last 6 ms eased to exact silence. No noise, no
    harmonics above 160 Hz: felt on headphones and phones' bass, never a click.
    -9 dBFS; the stitch mixes it through SFX_LIMITER (1 ms attack) under the -1.5 dBTP
    master. The mastering high-pass (80 Hz) runs before it, so it isn't cut."""
    seconds, dbfs = SOUNDS["sub_hit"]
    n = int(round(RATE * seconds))
    t = np.arange(n) / RATE
    glide = SUB_HIT_TO + (SUB_HIT_FROM - SUB_HIT_TO) * np.exp(-t / 0.045)  # 80 -> 45 Hz
    phase = 2 * np.pi * np.cumsum(glide) / RATE
    tau = SUB_HIT_DECAY / np.log(100.0)  # 1 % (-40 dB) of the peak at SUB_HIT_DECAY
    envelope = (1 - np.exp(-t / SUB_HIT_ATTACK)) * np.exp(-t / tau)
    tail = int(RATE * 0.006)
    envelope[-tail:] *= np.cos(np.linspace(0, np.pi / 2, tail)) ** 2
    return _peak(np.sin(phase) * envelope, dbfs)


SYNTHS = {"whoosh": whoosh, "tick": tick, "sub_hit": sub_hit}


def _write(path: Path, signal: np.ndarray) -> None:
    pcm = np.clip(np.round(signal * 32767), -32768, 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with wave.open(str(temporary), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(pcm.tobytes())
    temporary.replace(path)


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        data = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(float) / 32767
        channels = handle.getnchannels()
    return data.reshape(-1, channels).mean(axis=1) if channels > 1 else data


def sound(kind: str) -> np.ndarray:
    """The sound, from assets/sfx/<kind>.wav (synthesised and saved the first time)."""
    path = sfx_dir() / f"{kind}.wav"
    if not path.is_file():
        _write(path, SYNTHS[kind]())
    return read_wav(path)


def clip_cues(edit: object, windows: list[tuple[float, float]]) -> list[tuple[float, str]]:
    """(clip seconds, kind) of every cue in a clip's graphics: each card's own cues
    (core.cards.cues), from where it appears on screen (``windows``: core.dynamic_layout's
    on-screen span per card, in the edit's card order)."""
    from core.cards import cues

    found: list[tuple[float, str]] = []
    for card, (a, _b) in zip(edit.cards, windows):
        for t, kind in cues(card, edit.brand):
            found.append((round(a + t, 4), kind))
    kept: list[tuple[float, str]] = []
    for t, kind in sorted(found):  # ticks never closer than MIN_TICK_GAP: a soft rhythm, not a rattle
        if kind == "tick" and any(k == "tick" and t - u < MIN_TICK_GAP for u, k in kept):
            continue
        kept.append((t, kind))
    return kept


def cue_track(cue_list: list[tuple[float, str]], duration: float, path: Path) -> Path | None:
    """One mono 48 kHz track with every cue at its exact sample (None when there are none)."""
    cue_list = [(t, kind) for t, kind in cue_list if 0 <= t < duration and kind in SYNTHS]
    if not cue_list:
        return None
    track = np.zeros(int(round(duration * RATE)) + 1)
    for t, kind in cue_list:
        clip = sound(kind)
        at = int(round(t * RATE))
        end = min(len(track), at + len(clip))
        track[at:end] += clip[:end - at]
    _write(Path(path), np.clip(track, -1.0, 1.0))
    return Path(path)


def onsets(signal: np.ndarray, threshold_db: float = -40.0, gap: float = 0.03) -> list[float]:
    """Seconds where a sound starts in ``signal``: the first sample over ``threshold_db``
    after at least ``gap`` s below it (for checking a track's cues)."""
    loud = np.flatnonzero(np.abs(signal) > 10 ** (threshold_db / 20))
    if not loud.size:
        return []
    starts = [loud[0]] + [b for a, b in zip(loud[:-1], loud[1:]) if b - a > gap * RATE]
    return [round(int(k) / RATE, 4) for k in starts]


# ---------------------------------------------------------------------------- Round 36: mastering
# Broadcast loudness mastering, used by core.video_renderer for every clip (two-pass):
#   highpass 80 Hz (rumble, desk bumps, mic handling) -> FFT denoise -> +2 dB presence at
#   3 kHz (one octave wide: consonants and clarity on phone speakers) -> loudnorm to
#   -14 LUFS integrated, -1.5 dBTP, LRA 11 (YouTube, TikTok and Spotify's reference).
TARGET_LUFS = -14.0
TRUE_PEAK_DB = -1.5
LOUDNESS_RANGE = 11.0
HIGHPASS_HZ = 80
# After the micro-SFX are mixed in (core.dynamic_layout): a limiter with a 1 ms look-ahead
# (an inaudible 1 ms shift, well under a frame) so a whoosh on a peak can't pass the ceiling.
SFX_LIMITER = f"alimiter=limit={10 ** ((TRUE_PEAK_DB - 0.3) / 20):.3f}:attack=1:release=50:level=false"
PRESENCE = "equalizer=f=3000:t=q:w=1:g=2"
# The denoiser delays the sound by exactly 25 ms (measured at 44.1 and 48 kHz), which would
# put captions and lips ahead of the voice: those 25 ms are trimmed off the start and padded
# back at the end.
DENOISE = "afftdn=nr=12:nf=-45:tn=1,atrim=start=0.025,asetpts=PTS-STARTPTS,apad=pad_dur=0.025"
MASTERING_CHAIN = f"highpass=f={HIGHPASS_HZ}:poles=2,{DENOISE},{PRESENCE}"


def loudnorm(target: float = TARGET_LUFS, true_peak: float = TRUE_PEAK_DB, measured: dict[str, str] | None = None) -> str:
    """The loudnorm filter: a first (measuring) pass without ``measured``, else the second,
    linear pass with the first pass's numbers."""
    base = f"loudnorm=I={target:g}:TP={true_peak:g}:LRA={LOUDNESS_RANGE:g}"
    if measured is None:
        return base + ":print_format=json"
    return (f"{base}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true:print_format=none")


# ---------------------------------------------------------------------------- Round 36: dead air
DEAD_AIR_SECONDS = 0.65  # a pause longer than this reads as dead air on a short
TAIL_GUARD = 0.12  # never cut this close after a word ends (its tail: breath, decay, a final "s")
LEAD_GUARD = 0.08  # ...or this close before the next word starts (its attack)
KEEP_PAUSE = 0.30  # a trimmed pause keeps this much silence, so speech still breathes


def dead_air(words: list[object], start: float, end: float, min_gap: float = DEAD_AIR_SECONDS) -> list[tuple[float, float]]:
    """Pauses longer than ``min_gap`` inside a clip, as (from, to) in clip seconds, from the
    word timings (the silero-vad pattern on transcript words: speech is where words are)."""
    spoken = sorted((float(w.start), float(w.end)) for w in words if float(w.end) > start and float(w.start) < end)
    gaps: list[tuple[float, float]] = []
    for (_a, previous_end), (next_start, _b) in zip(spoken, spoken[1:]):
        if next_start - previous_end > min_gap:
            gaps.append((round(previous_end - start, 3), round(next_start - start, 3)))
    return gaps


def keep_ranges(gaps: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    """Clip-second ranges to keep if the dead air were trimmed: each gap shrinks to
    KEEP_PAUSE, cut only between TAIL_GUARD after the last word and LEAD_GUARD before the
    next, so no word's tail or attack is clipped."""
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for a, b in sorted(gaps):
        cut_from = a + max(TAIL_GUARD, KEEP_PAUSE / 2)
        cut_to = b - max(LEAD_GUARD, KEEP_PAUSE / 2)
        if cut_to - cut_from < 0.05:
            continue
        keep.append((cursor, round(cut_from, 3)))
        cursor = round(cut_to, 3)
    keep.append((cursor, round(duration, 3)))
    return [(a, b) for a, b in keep if b - a > 0]


def silences(ffmpeg: str, source: str | Path, start: float, duration: float, noise_db: float = -35.0,
             min_gap: float = DEAD_AIR_SECONDS) -> list[tuple[float, float]]:
    """FFmpeg silencedetect over the clip's audio, as (from, to) clip seconds (a check on
    dead_air() against the actual sound; [] when it can't run)."""
    import re
    import subprocess

    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
             "-map", "0:a:0", "-vn", "-af", f"silencedetect=noise={noise_db:g}dB:d={min_gap:g}", "-f", "null", "-"],
            capture_output=True, text=True, timeout=max(120.0, 1.5 * float(duration)))
    except (OSError, subprocess.SubprocessError):
        return []
    starts = [float(v) for v in re.findall(r"silence_start: (-?[\d.]+)", result.stderr)]
    ends = [float(v) for v in re.findall(r"silence_end: (-?[\d.]+)", result.stderr)]
    return [(round(max(0.0, a), 3), round(b, 3)) for a, b in zip(starts, ends)]


def flag_dead_air(clips: list[object], words: list[object]) -> list[object]:
    """Each clip (core.hook_selector.ClipResult) with ``dead_air`` set: its pauses over
    DEAD_AIR_SECONDS (clip seconds), flagged for the editor. Trimming them would need a
    cut list through captions, graphics and the crop, so clips are never cut here."""
    return [clip.model_copy(update={"dead_air": [list(g) for g in dead_air(words, clip.start_time, clip.end_time)]})
            for clip in clips]
