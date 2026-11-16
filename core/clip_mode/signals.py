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

Round 62: tuned on a real 2 h 12 min Twitch VOD (GTA V marathon). The first version compared
each bucket with a 2-minute rolling median and lit up 17 % of the stream, because the median
fell during quiet stretches. See docs/CLIP_MODE_TUNING.md for the numbers.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable

import numpy as np

from core.clip_mode.chat import Message
from core.transcriber import Word

BUCKET = 5.0  # seconds per heatmap bucket
BASELINE = 120.0  # seconds of recent past a moment is compared with
SPIKE_RATIO = 4.0  # this many times the baseline (after hype weighting) = value 1.0
HYPE_WEIGHT = 1.5  # a hype message counts this much more than an ordinary one
AUDIO_SPIKE_MIN_DB = 4.0  # a peak this far above the baseline starts to count
AUDIO_SPIKE_DB = 13.0  # ... and this far above it is value 1.0 (the loudest moment of a stream)
AUDIO_OUTSIDE_TOP = 0.45  # values of buckets outside the loudest share are scaled by this
AUDIO_BASELINE = 600.0  # seconds of recent past the loudness baseline is drawn from
AUDIO_TOP_SHARE = 0.06  # only the loudest 6 % of buckets can be hot on loudness alone
AUDIO_WEIGHT = 0.8  # loudness alone is weaker evidence than chat
AGREE_BONUS = 0.25
WARMUP_BUCKETS = 3  # the first buckets have no past to compare with
# Round 62: chat reacts after the moment (people type once they've seen it). Chat values are
# moved this many buckets earlier so a spike marks the moment itself, like a loudness spike
# or a "Most replayed" bucket does. 1 bucket = 5 s; typing lag on Twitch is about 3-8 s.
CHAT_LAG_BUCKETS = 1  # chat and loudness spike together

HYPE = re.compile(
    r"\b(kekw|lul|lmao+|lmfao|omegalul|pog+|poggers|pogchamp|w+|l+|no+ way|clip (it|that)|clipped|"
    r"holy|wtf|bro+|insane|crazy|sheesh|ez|gg|monka\w*|pepelaugh|icant|dead|💀|😂)\b|\?{3,}|!{3,}",
    re.IGNORECASE)


# Round 62: what a streamer says when something happens. Repeats ("no no no", "wait wait")
# count on top. Tuned on a UK GTA stream ("oh my days"); add a streamer's own phrases here.
REACTION = re.compile(
    r"\b(oh my (god|days|gosh)|omg|no way|what the|wtf|holy|oh (no|shit|fuck|my)|let'?s go+|yo+|bro+|nah+|"
    r"wait|yes+|yeah+ boy|come on|are you (kidding|serious)|i'?m dead|dead|insane|crazy|clutch|get out|"
    r"run|go go|help|stop|nooo+|ahh+|aaa+|ohh+|wooo+|yooo+)\b", re.IGNORECASE)
SPEECH_WEIGHT = 0.7  # like loudness, weaker than chat on its own
SPEECH_SPIKE = 6.0  # reaction points in a bucket (after smoothing) = value 1.0
MAX_REPEATS = 4  # repeats counted per bucket (Whisper sometimes loops one word 20 times)


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


def chat_values(messages: Iterable[Message], duration: float, lag_buckets: int = CHAT_LAG_BUCKETS) -> np.ndarray:
    """0-1 per bucket: how far above its recent baseline chat is, moved ``lag_buckets``
    earlier so the value sits on the moment chat is reacting to."""
    act = chat_activity(messages, duration)
    # smooth over 3 buckets: a reaction takes chat a few seconds
    act = np.convolve(act, np.ones(3) / 3, mode="same")
    base = np.maximum(_rolling_median(act, int(BASELINE / BUCKET)), 1.0)
    out = np.clip((act / base - 1.0) / (SPIKE_RATIO - 1.0), 0.0, 1.0)
    out[:WARMUP_BUCKETS] = 0.0
    if lag_buckets > 0 and len(out) > lag_buckets:
        out = np.concatenate([out[lag_buckets:], np.zeros(lag_buckets)])
    return out


def loudness(video: str | Path) -> np.ndarray:
    """Per bucket: (max momentary loudness, mean short-term loudness) in LUFS, from FFmpeg's
    ebur128 filter. Shape (n, 2). A scream or an explosion shows in the momentary (400 ms)
    peak; the short-term (3 s) mean is what the stream usually sounds like."""
    run = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-vn", "-af",
                          "ebur128=metadata=0", "-f", "null", "-"], capture_output=True, text=True)
    points = []
    for t, m, s in re.findall(r"t:\s*([0-9.]+)\s+TARGET.*?M:\s*(-?[0-9.]+|-inf)\s+S:\s*(-?[0-9.]+|-inf)", run.stderr):
        points.append((float(t), max(float(m), -70.0) if m != "-inf" else -70.0,
                       max(float(s), -70.0) if s != "-inf" else -70.0))
    if not points:
        return np.zeros((0, 2))
    n = int(np.ceil(points[-1][0] / BUCKET)) + 1
    peak = np.full(n, -70.0)
    sums, cnt = np.zeros(n), np.zeros(n)
    for t, m, s in points:
        i = int(t // BUCKET)
        peak[i] = max(peak[i], m)
        sums[i] += s
        cnt[i] += 1
    mean = np.where(cnt > 0, sums / np.maximum(cnt, 1), -70.0)
    return np.stack([peak, mean], axis=1)


def _rolling_percentile(values: np.ndarray, window: int, q: float) -> np.ndarray:
    out = np.empty_like(values)
    for i in range(len(values)):
        lo = max(0, i - window)
        past = values[lo:i]
        out[i] = np.percentile(past, q) if len(past) else values[i]
    return out


def audio_values(lufs: np.ndarray) -> np.ndarray:
    """0-1 per bucket: how far the bucket's loudest moment rises above what the stream has
    sounded like lately. The baseline is the recent 70th percentile of short-term loudness
    (normal loud talking), never below the stream's own 40th percentile, so a quiet stretch
    (loading screen, AFK) does not make the next sentence a spike. Only buckets whose peak is
    among the stream's loudest AUDIO_TOP_SHARE can reach HOT."""
    if not len(lufs):
        return np.zeros(0)
    if lufs.ndim == 1:  # an old (mean-only) array: treat it as both
        lufs = np.stack([lufs, lufs], axis=1)
    peak, mean = lufs[:, 0], lufs[:, 1]
    talking = mean[mean > -60.0]
    floor = np.percentile(talking, 40) if len(talking) else -40.0
    base = np.maximum(_rolling_percentile(mean, int(AUDIO_BASELINE / BUCKET), 70), floor)
    out = np.clip((peak - base - AUDIO_SPIKE_MIN_DB) / (AUDIO_SPIKE_DB - AUDIO_SPIKE_MIN_DB), 0.0, 1.0)
    out[:WARMUP_BUCKETS] = 0.0
    gate = np.percentile(peak, 100 * (1 - AUDIO_TOP_SHARE))
    out[peak < gate] *= AUDIO_OUTSIDE_TOP  # not among the loudest: never hot on its own
    return out


def speech_values(words: list[Word], duration: float, covered: np.ndarray | None = None) -> np.ndarray:
    """0-1 per bucket: reaction words and repeats in what the streamer says.

    ``covered`` (Round 65, long VODs): True for the buckets that were transcribed at all. The
    baseline is then drawn from the last two minutes of *transcribed* buckets, so the silence
    of the parts nobody transcribed is not mistaken for a quiet streamer. None: every bucket."""
    n = max(1, int(np.ceil(duration / BUCKET)))
    points = np.zeros(n)
    repeats = np.zeros(n)
    ordered = sorted(words, key=lambda w: w.start)
    prev = ""
    for w in ordered:
        i = int(w.start // BUCKET)
        if not (0 <= i < n):
            continue
        text = w.text.strip().lower().strip(".,!?")
        if REACTION.search(text):
            points[i] += 1.0
        if text and text == prev and repeats[i] < MAX_REPEATS:  # "no no no" (capped: Whisper can loop a word)
            points[i] += 0.6
            repeats[i] += 1
        if "!" in w.text:
            points[i] += 0.3
        prev = text
    # a reaction phrase of two words ("oh my", "no way") is matched on the joined text of a bucket
    joined: dict[int, list[str]] = {}
    for w in ordered:
        i = int(w.start // BUCKET)
        if 0 <= i < n:
            joined.setdefault(i, []).append(w.text.strip().lower())
    for i, parts in joined.items():
        points[i] += 0.8 * max(0, len(REACTION.findall(" ".join(parts))) - sum(1 for p in parts if REACTION.search(p)))
    points = np.convolve(points, np.array([0.25, 1.0, 0.25]), mode="same")
    if covered is not None:
        keep = np.flatnonzero(np.asarray(covered, dtype=bool)[:n])
        out = np.zeros(n)
        if len(keep):
            some = points[keep]
            values = np.clip((some - _rolling_median(some, int(BASELINE / BUCKET))) / SPEECH_SPIKE, 0.0, 1.0)
            values[:WARMUP_BUCKETS] = 0.0
            out[keep] = values
        return out
    base = _rolling_median(points, int(BASELINE / BUCKET))
    out = np.clip((points - base) / SPEECH_SPIKE, 0.0, 1.0)
    out[:WARMUP_BUCKETS] = 0.0
    return out


def fuse(chat: np.ndarray | None, audio: np.ndarray | None, speech: np.ndarray | None = None) -> np.ndarray:
    """One 0-1 value per bucket from the signals there are. Chat leads; loudness and speech
    are weaker alone but agreement between any two adds AGREE_BONUS."""
    parts = [a for a in (chat, audio, speech) if a is not None and len(a)]
    if not parts:
        return np.zeros(0)
    n = max(len(a) for a in parts)

    def fit(a: np.ndarray | None) -> np.ndarray:
        return np.zeros(n) if a is None or not len(a) else np.pad(a, (0, n - len(a)))

    c, a, s = fit(chat), fit(audio), fit(speech)
    agree = ((c >= 0.5).astype(int) + (a >= 0.5).astype(int) + (s >= 0.5).astype(int)) >= 2
    return np.clip(np.maximum.reduce([c, AUDIO_WEIGHT * a, SPEECH_WEIGHT * s]) + AGREE_BONUS * agree, 0.0, 1.0)


def heatmap(values: np.ndarray) -> list[dict[str, float]]:
    return [{"start_time": round(i * BUCKET, 3), "end_time": round((i + 1) * BUCKET, 3), "value": round(float(v), 4)}
            for i, v in enumerate(values)]


def stream_heatmap(video: str | Path | None, messages: list[Message] | None, duration: float,
                   words: list[Word] | None = None) -> list[dict[str, float]]:
    """The fused heatmap for a VOD. Any signal may be missing."""
    chat = chat_values(messages, duration) if messages else None
    audio = audio_values(loudness(video)) if video else None
    speech = speech_values(words, duration) if words else None
    return heatmap(fuse(chat, audio, speech))
