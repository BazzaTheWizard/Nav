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
SOUNDS = {"whoosh": (0.180, -22.0), "tick": (0.040, -26.0)}  # kind: (seconds, peak dBFS)
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


SYNTHS = {"whoosh": whoosh, "tick": tick}


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
