"""Checks for the review fixes. Run all tests: .venv/bin/python tests/run_all.py"""
import importlib
import json
import random
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))

from core import transcriber as T  # noqa: E402
from core import subtitle_engine as S  # noqa: E402
from core import hook_selector as H  # noqa: E402
from core.transcriber import Word  # noqa: E402

failures = 0


def check(name, condition, detail=""):
    global failures
    status = "PASS" if condition else "FAIL"
    if not condition:
        failures += 1
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


# ---------- 1. Groq response parsing: word timestamps + segment mapping ----------
segments = [
    {"id": 0, "start": 0.0, "end": 2.4, "text": " Here is the first sentence."},
    {"id": 1, "start": 2.6, "end": 5.0, "text": " And this is the second one!"},
]
words_raw = [
    {"word": " Here", "start": 0.0, "end": 0.3}, {"word": " is", "start": 0.3, "end": 0.5},
    {"word": " the", "start": 0.5, "end": 0.7}, {"word": " first", "start": 0.7, "end": 1.2},
    {"word": " sentence.", "start": 1.2, "end": 2.4}, {"word": " And", "start": 2.6, "end": 2.9},
    {"word": " this", "start": 2.9, "end": 3.2}, {"word": " is", "start": 3.2, "end": 3.4},
    {"word": " the", "start": 3.4, "end": 3.6}, {"word": " second", "start": 3.6, "end": 4.2},
    {"word": " one!", "start": 4.2, "end": 5.0},
]
try:
    from groq.types.audio import Transcription
    groq_obj = Transcription.construct(text="...", segments=segments, words=words_raw)
except Exception:
    groq_obj = SimpleNamespace(text="...", segments=segments, words=words_raw)

parsed_segments = T._parse_timed_items(T._response_field(groq_obj, "segments"), "text")
parsed_words = T._parse_timed_items(T._response_field(groq_obj, "words"), "word")
mapped = T._attach_segments(parsed_words, parsed_segments)
check("Groq: one entry per word", len(mapped) == 11, f"{len(mapped)} entries")
check("Groq: words carry their sentence's segment bounds",
      mapped[0].segment_end == 2.4 and mapped[-1].segment_start == 2.6)
check("Groq: word text stripped", mapped[0].text == "Here")
check("Word-level detection", T.has_word_level_timing(mapped))
seg_level = [Word(text=t, start=s, end=e, segment_start=s, segment_end=e) for t, s, e in parsed_segments]
check("Segment-level detection", not T.has_word_level_timing(seg_level))

# ---------- 2. Groq upload bitrate fits long audio ----------
import wave
for minutes, expected_max in ((30, 64), (60, 48), (90, 32), (120, 24), (180, 16)):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.setnframes(0)
        # fake duration by patching _audio_duration
        original = T._audio_duration
        T._audio_duration = lambda _p, m=minutes: m * 60.0
        kbps = T._groq_upload_bitrate_kbps(path)
        T._audio_duration = original
    size_mb = kbps * 1000 * minutes * 60 / 8 / 1024 / 1024
    check(f"Groq bitrate for {minutes} min audio", kbps <= expected_max and size_mb < 24,
          f"{kbps} kbps ≈ {size_mb:.1f} MiB (limit 24 MiB)")

# ---------- 3. clip words from transcript ----------
clip = T.clip_words_from_transcript(mapped, 2.5, 5.0)
check("Clip words are clip-relative", clip is not None and clip[0].text == "And" and abs(clip[0].start - 0.1) < 1e-9)
check("Caption-line transcripts fall back to re-transcription",
      T.clip_words_from_transcript(seg_level, 0, 5) is None)

# ---------- 4. Subtitles: sentence-level entries are split into short cards ----------
def ass_cards(path):
    """Caption card texts (override tags removed, one entry per distinct card)."""
    cards = []
    for line in Path(path).read_text().splitlines():
        if line.startswith("Dialogue:") and ",ShortForm," in line:
            text = re.sub(r"{[^}]*}", "", line.split(",", 9)[-1])
            if not cards or cards[-1] != text:
                cards.append(text)
    return cards

long_sentence = Word(text="So the real reason most people fail at this is they never write anything down at all.",
                     start=0.0, end=5.0)
with tempfile.TemporaryDirectory() as d:
    out = S.create_ass_subtitles([long_sentence], Path(d) / "s.ass", clip_duration=6.0)
    cards = ass_cards(out)
check("Sentence-level entry becomes several short cards", len(cards) >= 4, f"{len(cards)} cards: {cards[:3]}…")
check("Every card ≤ 4 words", all(len(c.split()) <= 4 for c in cards))
with tempfile.TemporaryDirectory() as d:
    out = S.create_ass_subtitles(T.clip_words_from_transcript(mapped, 0, 5.0), Path(d) / "w.ass", clip_duration=5.0)
    word_cards = ass_cards(out)
check("Word-level input unchanged (split on punctuation/4 words)", word_cards[0] == "HERE IS THE FIRST", str(word_cards))

# ---------- 5. Clip selection on unpunctuated captions (YouTube auto-captions) ----------
random.seed(7)
vocab = "the we you know like really think going about people thing get make just right so and what that this".split()
captions = []
t = 0.0
while t < 30 * 60:  # 30-minute video, ~3 s caption lines, no punctuation
    dur = random.uniform(2.5, 4.0)
    text = " ".join(random.choice(vocab) for _ in range(random.randint(6, 10)))
    captions.append(Word(text=text, start=t, end=t + dur, segment_start=t, segment_end=t + dur))
    t += dur
new_fallback = H._fallback_clips(3, t, captions)
check("Unpunctuated captions still give clips", len(new_fallback) == 3,
      f"{[(round(c.start_time), round(c.end_time)) for c in new_fallback]}")
check("Clips within 30–60 s", all(30 <= c.end_time - c.start_time <= 60 for c in new_fallback))

# Old code on the same input (for comparison)
orig = ROOT.parent / "orig"
if orig.exists():
    import subprocess
    script = (
        "import sys, pickle; sys.path.insert(0, %r);"
        "from core import hook_selector as H; from core.transcriber import Word;"
        "words=[Word(**w) for w in pickle.load(open(%r,'rb'))];"
        "print(len(H._fallback_clips(3, %r, words)))"
    )
    with tempfile.TemporaryDirectory() as d:
        import pickle
        pk = Path(d) / "w.pkl"
        pickle.dump([w.__dict__ for w in captions], open(pk, "wb"))
        result = subprocess.run([sys.executable, "-c", script % (str(orig), str(pk), t)],
                                capture_output=True, text=True)
    print(f"       (original code on the same captions → {result.stdout.strip() or result.stderr.strip()[-200:]} clips)")

# ---------- 6. LLM proposals a few seconds outside 30–60 s are now snapped, not dropped ----------
sent_words = []
t = 0.0
for i in range(400):
    n = random.randint(8, 16)
    for j in range(n):
        token = random.choice(vocab) + ("." if j == n - 1 else "")
        sent_words.append(Word(text=token, start=t, end=t + 0.3, segment_start=None, segment_end=None))
        t += 0.32
    t += 0.5
proposal = H.ClipResult(start_time=100.0, end_time=125.0, viral_title="x", caption="x", hashtags=[], virality_score=90)
normalized = H._normalize_clip(proposal, t, sent_words)
check("25 s proposal is snapped to a valid 30–60 s clip", normalized is not None and 30 <= normalized.end_time - normalized.start_time <= 60,
      f"{normalized.start_time:.1f}-{normalized.end_time:.1f}" if normalized else "dropped")

# ---------- 7. Prompt size: grouped timeline vs one line per word ----------
per_word = "\n".join(f"{w.start:.2f}-{w.end:.2f}: {w.text}" for w in sent_words)
grouped = T.format_timeline(sent_words)
check("Timeline prompt is much smaller", len(grouped) * 3 < len(per_word),
      f"{len(per_word):,} → {len(grouped):,} characters ({len(per_word)/len(grouped):.1f}x smaller)")
print("       sample line:", grouped.splitlines()[0][:110])

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
