"""Round 61: where a stream peaks, from its chat and its loudness, as a yt-dlp-style heatmap.

A moment is a highlight when chat suddenly moves faster than it had been (and fills with
hype: KEKW, LUL, W, "no way", "CLIP IT", ????), and/or when the stream gets much louder than
it had been (screaming, explosions). Both are measured against the stream's own recent past
(a rolling median), so a big streamer's always-busy chat and a small streamer's quiet one are
judged the same way.

The output is the format yt-dlp gives for YouTube's "Most replayed" data:
[{"start_time", "end_time", "value" 0-1}, ...]. core.pipeline.pick_clips_with_agent takes it as
``heatmap=`` and core.heatmap_scorer ranks clips whose hook lands on a spike (value >= 0.65)
higher. No new ranking code: streams get the same treatment as replay data.

Weights and thresholds here are first guesses; tune them on real VODs.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable

import numpy as np

from core.clip_mode.chat import Message

BUCKET = 5.0  # seconds per heatmap bucket
BASELINE = 120.0  # seconds of recent past a moment is compared with
SPIKE_RATIO = 4.0  # this many times the baseline (after hype weighting) = value 1.0
HYPE_WEIGHT = 1.5  # a hype message counts this much more than an ordinary one
AUDIO_SPIKE_DB = 9.0  # this many dB above the recent median loudness = value 1.0
AUDIO_WEIGHT = 0.8  # loudness alone is weaker evidence than chat
AGREE_BONUS = 0.25
WARMUP_BUCKETS = 3  # the first buckets have no past to compare with  # chat and loudness spike together

HYPE = re.compile(
    r"\b(kekw|lul|lmao+|lmfao|omegalul|pog+|poggers|pogchamp|w+|l+|no+ way|clip (it|that)|clipped|"
    r"holy|wtf|bro+|insane|crazy|sheesh|ez|gg|monka\w*|pepelaugh|icant|dead|💀|😂)\b|\?{3,}|!{3,}",
    re.IGNORECASE)


def _rolling_median(values: np.ndarray, window: int) -> np.ndarray:
    out = np.empty_like(values)
    for i in range(len(values)):
        lo = max(0, i - window)
        past = values[lo:i]
        out[i] = np.median(past) if len(past) else values[i]
    return out


def chat_activity(messages: Iterable[Message], duration: float) -> np.ndarray:
    """Hype-weighted message count per bucket."""
    n = max(1, int(np.ceil(duration / BUCKET)))
    counts = np.zeros(n)
    for m in messages:
        i = int(m.t // BUCKET)
        if 0 <= i < n:
            counts[i] += HYPE_WEIGHT if HYPE.search(m.text or "") else 1.0
    return counts


def chat_values(messages: Iterable[Message], duration: float) -> np.ndarray:
    """0-1 per bucket: how far above its recent baseline chat is."""
    act = chat_activity(messages, duration)
    # smooth over 3 buckets: a reaction takes chat a few seconds
    act = np.convolve(act, np.ones(3) / 3, mode="same")
    base = np.maximum(_rolling_median(act, int(BASELINE / BUCKET)), 1.0)
    out = np.clip((act / base - 1.0) / (SPIKE_RATIO - 1.0), 0.0, 1.0)
    out[:WARMUP_BUCKETS] = 0.0
    return out


def loudness(video: str | Path) -> np.ndarray:
    """Short-term loudness (LUFS) per bucket, from FFmpeg's ebur128 filter."""
    run = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-vn", "-af",
                          "ebur128=metadata=0", "-f", "null", "-"], capture_output=True, text=True)
    points = [(float(t), float(s)) for t, s in re.findall(r"t:\s*([0-9.]+)\s+TARGET.*?S:\s*(-?[0-9.]+|-inf)",
                                                          run.stderr) if s != "-inf"]
    if not points:
        return np.zeros(0)
    n = int(np.ceil(points[-1][0] / BUCKET)) + 1
    sums, cnt = np.zeros(n), np.zeros(n)
    for t, s in points:
        i = int(t // BUCKET)
        sums[i] += max(s, -70.0)
        cnt[i] += 1
    return np.where(cnt > 0, sums / np.maximum(cnt, 1), -70.0)


def audio_values(lufs: np.ndarray) -> np.ndarray:
    """0-1 per bucket: how far above its recent median the stream is."""
    if not len(lufs):
        return lufs
    base = _rolling_median(lufs, int(BASELINE / BUCKET))
    out = np.clip((lufs - base) / AUDIO_SPIKE_DB, 0.0, 1.0)
    out[:WARMUP_BUCKETS] = 0.0  # the meter ramps up from silence at the start: no baseline yet
    return out


def fuse(chat: np.ndarray | None, audio: np.ndarray | None) -> np.ndarray:
    parts = [a for a in (chat, audio) if a is not None and len(a)]
    if not parts:
        return np.zeros(0)
    n = max(len(a) for a in parts)
    c = np.zeros(n) if chat is None or not len(chat) else np.pad(chat, (0, n - len(chat)))
    a = np.zeros(n) if audio is None or not len(audio) else np.pad(audio, (0, n - len(audio)))
    both = (c >= 0.5) & (a >= 0.5)
    return np.clip(np.maximum(c, AUDIO_WEIGHT * a) + AGREE_BONUS * both, 0.0, 1.0)


def heatmap(values: np.ndarray) -> list[dict[str, float]]:
    return [{"start_time": round(i * BUCKET, 3), "end_time": round((i + 1) * BUCKET, 3), "value": round(float(v), 4)}
            for i, v in enumerate(values)]


def stream_heatmap(video: str | Path | None, messages: list[Message] | None, duration: float) -> list[dict[str, float]]:
    """The fused heatmap for a VOD. Either signal may be missing."""
    chat = chat_values(messages, duration) if messages else None
    audio = audio_values(loudness(video)) if video else None
    return heatmap(fuse(chat, audio))
