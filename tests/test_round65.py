"""Round 65: long VODs. Only the windows around chat and loudness spikes are transcribed
(core/clip_mode/windows.py, tools/clip_stream.py), a stopped run resumes, and long downloads are
retried and checked (core/clip_mode/vod.py).

A synthetic 3-hour stream stands in for a real one: quiet noise (the streamer talking), four loud
bursts, stretches of true silence, and a synthetic chat. The transcriber is a fake that "hears"
the audio it is handed, so the test can count the seconds it gets and check where its words land.
No network, no model.

Run: .venv/bin/python -u tests/test_round65.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["CLIP_LENGTH"] = "gaming"
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


import numpy as np  # noqa: E402

import tools.clip_stream as CS  # noqa: E402
from core import clip_length as L  # noqa: E402
from core import heatmap_scorer  # noqa: E402
from core.clip_mode import picker as P  # noqa: E402
from core.clip_mode import signals as SG  # noqa: E402
from core.clip_mode import vod as V  # noqa: E402
from core.clip_mode import windows as W  # noqa: E402
from core.clip_mode.chat import Message  # noqa: E402
from core.transcriber import Word  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="nav-r65-"))
DUR = 10800.0  # 3 hours
BURSTS = (1500.0, 4000.0, 7200.0, 9800.0)  # 6 s of loud audio from each
CHAT_ONLY = 8500.0  # chat goes wild, the audio does not
SILENT = ((3945.0, 3980.0), (5000.0, 6000.0), (9750.0, 9790.0))  # true silence
NO_CHAT_SPANS = ((3940.0, 3990.0), (5000.0, 6000.0))  # nobody types (but they do during 9750-9790)

# ---------------------------------------------------------------- the synthetic stream
audio = TMP / "stream.m4a"
loud = "+".join(f"between(t,{a:.0f},{a + 6:.0f})" for a in BURSTS)
quiet = "+".join(f"between(t,{a:.0f},{b:.0f})" for a, b in SILENT)
subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                f"anoisesrc=d={DUR:.0f}:a=0.02:c=pink:r=16000", "-af",
                f"volume='if(gt({loud},0),20,if(gt({quiet},0),0,1))':eval=frame", "-ac", "1", "-c:a", "aac", "-b:a", "32k",
                str(audio)], check=True)
check("a 3-hour stream to work on", abs(V.probe_seconds(audio) - DUR) < 2.0, V.probe_seconds(audio))

chat = [Message(float(s), "hello") for s in range(0, int(DUR), 3)
        if not any(a <= s < b for a, b in NO_CHAT_SPANS)]
chat += [Message(1504.0 + i * 0.1, "KEKW NO WAY") for i in range(90)]
chat += [Message(7204.0 + i * 0.1, "clip it W") for i in range(60)]
chat += [Message(CHAT_ONLY + 4 + i * 0.1, "KEKW ???? NO WAY") for i in range(90)]
chat.sort(key=lambda m: m.t)


class Ears:
    """A fake transcriber that listens: one "wait" per loud second, one "w" per second of ordinary
    noise, nothing in silence. Times are from the start of the piece it was handed, as a real
    transcriber's are. Counts the seconds of audio it is given."""

    def __init__(self, fail_on: int = 0) -> None:
        self.seconds: list[float] = []
        self.fail_on = fail_on

    def __call__(self, wav: Path) -> list[Word]:
        if self.fail_on and len(self.seconds) + 1 == self.fail_on:
            raise ConnectionError("the connection dropped")
        with wave.open(str(wav), "rb") as f:
            rate = f.getframerate()
            data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float64)
        self.seconds.append(len(data) / rate)
        words: list[Word] = []
        for second in range(int(len(data) / rate)):
            rms = float(np.sqrt(np.mean(data[second * rate:(second + 1) * rate] ** 2)))
            if rms > 2000:
                words.append(Word(text="wait", start=second + 0.1, end=second + 0.6))
            elif rms > 20:
                words.append(Word(text="w", start=second + 0.1, end=second + 0.6))
        if not words:
            raise RuntimeError("Transcription returned no word timestamps. Check that the input has audible speech.")
        return words


loudness_calls: list[str] = []
real_loudness = SG.loudness


def counted_loudness(video):
    loudness_calls.append(str(video))
    return real_loudness(video)


SG.loudness = counted_loudness
quiet_print = lambda *a, **k: None  # noqa: E731

# ---------------------------------------------------------------- run 1: stops in the middle
out = TMP / "with-chat"
out.mkdir()
ears = Ears(fail_on=3)
try:
    CS.analyse(audio, chat, DUR, out, transcribe=ears, say=quiet_print)
    check("run 1: the transcriber's failure stops the run", False)
except ConnectionError:
    check("run 1: the transcriber's failure stops the run", True)
progress = json.loads((out / "progress.json").read_text())["steps"]
cached = sorted((out / "windows").glob("*.json"))
check("run 1: the two finished windows are saved, and the progress file says where it stopped",
      len(cached) == 2 and progress["transcript"]["done"] is False and progress["transcript"]["pieces_done"] == 2
      and progress["loudness"]["done"] is True and progress["windows"]["done"] is True, progress)
plan = json.loads((out / "windows.json").read_text())
wins = [W.Window.from_dict(w) for w in plan["windows"]]
pieces = [p for w in wins for p in w.pieces]
check("run 1: only window audio reached the transcriber", len(ears.seconds) == 2
      and abs(sum(ears.seconds) - sum(b - a for a, b in pieces[:2])) < 0.5, ears.seconds)

# ---------------------------------------------------------------- the windows
total = sum(b - a for a, b in pieces)
check("windows: one around each loud burst and one around the chat-only spike", len(wins) == 5
      and all(any(w.start <= t - 55 and w.end >= t + 55 for w in wins) for t in (*BURSTS, CHAT_ONLY)),
      [(w.start, w.end) for w in wins])
check("windows: with chat, about a minute either side", all(115 <= w.end - w.start <= 150 for w in wins) and plan["pad"] == W.PAD,
      [w.end - w.start for w in wins])
check("windows: a small share of the stream (under 12 minutes of 3 hours)", total < 720 and plan["share_of_stream"] < 0.07
      and abs(plan["audio_seconds"] - total) < 0.5, (total, plan["share_of_stream"]))
w4000 = next(w for w in wins if w.start <= 4000 <= w.end)
# (35 s are silent; short-term loudness trails by 3 s and one bucket is kept at each end: 20 s go)
check("dead air: silence with no chat is cut out of its window", len(w4000.pieces) == 2
      and 3945.0 <= w4000.pieces[0][1] <= 3955.0 and 3975.0 <= w4000.pieces[1][0] <= 3980.0
      and abs((w4000.end - w4000.start) - w4000.seconds - 20.0) < 0.5 and plan["dead_air_seconds"] == 20.0, w4000.pieces)
w9800 = next(w for w in wins if w.start <= 9800 <= w.end)
check("dead air: silence while chat keeps talking is kept", len(w9800.pieces) == 1
      and w9800.pieces[0][0] <= 9750 and w9800.pieces[0][1] >= 9790, w9800.pieces)
check("windows: nothing from the 17 silent minutes", all(not (a < 6000 and b > 5000) for a, b in pieces))

# ---------------------------------------------------------------- run 2: carries on
ears2 = Ears()
said: list[str] = []
words, heat, wins2 = CS.analyse(audio, chat, DUR, out, transcribe=ears2, say=said.append)
check("run 2: the loudness pass is not repeated", len(loudness_calls) == 1 and any("kept from the last run" in s for s in said),
      (loudness_calls, said[:3]))
check("run 2: only the windows that were missing are transcribed", len(ears2.seconds) == len(pieces) - 2
      and abs(sum(ears2.seconds) - sum(b - a for a, b in pieces[2:])) < 0.5
      and any("2 of 6 windows already transcribed" in s for s in said), (ears2.seconds, said))
check("both runs together: the transcriber got the windows and nothing else (11 of 180 minutes)",
      abs(sum(ears.seconds) + sum(ears2.seconds) - total) < 1.0 and sum(ears.seconds) + sum(ears2.seconds) < 0.07 * DUR,
      sum(ears.seconds) + sum(ears2.seconds))
check("progress: every window has an ETA line with the audio left",
      sum("of audio left, about" in s for s in said) == len(pieces) - 3, said)
progress = json.loads((out / "progress.json").read_text())["steps"]
check("run 2: the progress file says the transcript is done", progress["transcript"]["done"] is True
      and progress["transcript"]["pieces_done"] == len(pieces) and progress["transcript"]["words"] == len(words), progress["transcript"])

# ---------------------------------------------------------------- times map back to the stream
shouts = [w for w in words if w.text == "wait"]
check("times: every loud second the fake heard sits on a burst's own seconds of the stream",
      len(shouts) >= 4 * 5 and all(any(t - 0.5 <= w.start <= t + 6.5 for t in BURSTS) for w in shouts),
      [round(w.start, 1) for w in shouts][:8])
check("times: each burst's shouts were found", all(sum(1 for w in shouts if t - 0.5 <= w.start <= t + 6.5) >= 5 for t in BURSTS))
check("times: words are in order, on the stream's clock, and only inside transcribed pieces",
      all(a.start <= b.start for a, b in zip(words, words[1:]))
      and all(any(a - 0.01 <= w.start and w.end <= b + 0.01 for a, b in pieces) for w in words), len(words))
check("times: the silent stretch that was kept has no words, as in a real transcript",
      not [w for w in words if 9751 <= w.start <= 9789])
check("the window transcript is saved", len(json.loads((out / "transcript_windows.json").read_text())) == len(words))

# ---------------------------------------------------------------- the heatmap and the picker
hot = [h for h in heat if h["value"] >= P.HOT]
ok_zone = W.allowed(wins, len(heat))
check("heatmap: hot only where the transcript reaches around the moment",
      hot and all(ok_zone[int(h["start_time"] // SG.BUCKET)] for h in hot), len(hot))
check("heatmap: loudness and the shouted words agree on the bursts (agreement bonus)",
      all(max(h["value"] for h in heat if t - 5 <= h["start_time"] <= t + 5) >= 0.99 for t in BURSTS),
      [max(h["value"] for h in heat if t - 5 <= h["start_time"] <= t + 5) for t in BURSTS])
clips = P.pick_stream_clips(words, chat, heat, DUR, 5, use_model=False)
check("picker: a clip on each of the five moments, 15-60 s", len(clips) == 5
      and all(any(c.start_time - 10 <= t <= c.end_time + 10 for c in clips) for t in (*BURSTS, CHAT_ONLY))
      and all(L.MIN_SECONDS <= c.end_time - c.start_time <= L.MAX_SECONDS for c in clips),
      [(c.start_time, c.end_time) for c in clips])
check("picker: every clip lies inside its window (captions have their words)",
      all(any(w.start <= c.start_time and c.end_time <= w.end for w in wins) for c in clips))

# ---------------------------------------------------------------- run 3: nothing left to do
ears3 = Ears()
words3, heat3, _ = CS.analyse(audio, chat, DUR, out, transcribe=ears3, say=quiet_print)
check("run 3: nothing is transcribed or measured again, and the result is the same",
      ears3.seconds == [] and len(loudness_calls) == 1 and words3 == words and heat3 == heat)

# ---------------------------------------------------------------- no chat: loudness alone, wider
out_nc = TMP / "no-chat"
out_nc.mkdir()
ears_nc = Ears()
said.clear()
words_nc, heat_nc, wins_nc = CS.analyse(audio, [], DUR, out_nc, transcribe=ears_nc, say=said.append)
plan_nc = json.loads((out_nc / "windows.json").read_text())
check("no chat: the four loud bursts get a window, the chat-only moment does not", len(wins_nc) == 4
      and all(any(w.start <= t <= w.end for w in wins_nc) for t in BURSTS)
      and not any(w.start <= CHAT_ONLY <= w.end for w in wins_nc), [(w.start, w.end) for w in wins_nc])
check("no chat: wider windows (90 s either side)", plan_nc["pad"] == W.PAD_NO_CHAT
      and all(w.start <= t - 85 and w.end >= t + 85 for w in wins_nc for t in BURSTS if w.start <= t <= w.end)
      and any("no chat" in s and "wider windows" in s for s in said), [(w.start, w.end) for w in wins_nc])
w9800 = next(w for w in wins_nc if w.start <= 9800 <= w.end)
check("no chat: silence counts as dead air on loudness alone", len(w9800.pieces) == 2 and w9800.pieces[0][1] <= 9760.0
      and w9800.pieces[1][0] >= 9785.0, w9800.pieces)
check("no chat: still only window audio is transcribed", abs(sum(ears_nc.seconds) - plan_nc["audio_seconds"]) < 1.0
      and sum(ears_nc.seconds) < 0.08 * DUR, sum(ears_nc.seconds))
clips_nc = P.pick_stream_clips(words_nc, [], heat_nc, DUR, 4, use_model=False)
check("no chat: the picker finds the four bursts", len(clips_nc) == 4
      and all(any(c.start_time - 10 <= t <= c.end_time + 10 for c in clips_nc) for t in BURSTS))

# ---------------------------------------------------------------- the cap
rng = np.random.default_rng(7)
n = int(7 * 3600 / SG.BUCKET)  # a 7-hour stream
values = np.zeros(n)
spots = rng.choice(np.arange(100, n - 100, 60), size=70, replace=False)  # 70 raised spots, 5 min apart at least
strength = np.linspace(1.0, 0.42, 70)
values[spots] = strength
report: dict = {}
capped = W.choose(values, n * SG.BUCKET, None, None, report=report)
check("cap: at most 30 windows and 45 minutes of audio from a 7-hour stream", len(capped) <= W.MAX_WINDOWS
      and report["audio_seconds"] <= W.MAX_AUDIO and report["runs"] == 70 and report["runs_left_out"] > 0, report)
kept = sorted(float(values[int(w.zones[0][0] // SG.BUCKET):int(w.zones[-1][1] // SG.BUCKET) + 1].max()) for w in capped)
check("cap: the strongest spots are the ones kept", kept[0] >= np.sort(strength)[-len(capped)] - 1e-9, (kept[0], len(capped)))
with_chat = W.choose(values, n * SG.BUCKET, None, [Message(1.0, "hi")])
check("cap: narrower windows (with chat) fit more moments into the same budget", len(with_chat) > len(capped),
      (len(with_chat), len(capped)))
check("cap: below WARM nothing is chosen", W.choose(np.full(n, W.WARM - 0.01), n * SG.BUCKET) == [])
both = W.choose(np.array([0.0] * 100 + [0.9] + [0.0] * 10 + [0.9] + [0.0] * 100), 212 * SG.BUCKET, None, [Message(1.0, "x")])
check("overlapping windows are merged into one", len(both) == 1 and both[0].start == 440.0 and both[0].end == 620.0
      and len(both[0].zones) == 2, [(w.start, w.end, w.zones) for w in both])
long_run = W.choose(np.array([0.0] * 100 + [0.9] * 60 + [0.0] * 100), 260 * SG.BUCKET, None, [Message(1.0, "x")])
check("a hot run that goes on for 5 minutes counts for its first 45 s", len(long_run) == 1
      and long_run[0].end - long_run[0].start == W.PAD + W.CORE_MAX + W.PAD, (long_run[0].start, long_run[0].end))

# ---------------------------------------------------------------- the signals are as they were
ws = [Word(text=t, start=300 + i * 0.5, end=300.3 + i * 0.5) for i, t in enumerate("oh my god no no no wait what".split() * 3)]
plain = SG.speech_values(ws, 900.0)
check("speech signal: unchanged without `covered`, and the same when everything is covered",
      np.array_equal(plain, SG.speech_values(ws, 900.0, covered=np.ones(180, dtype=bool))) and plain.max() > 0.5, plain.max())
part = np.zeros(180, dtype=bool)
part[50:70] = True
check("speech signal: with `covered`, values only where audio was transcribed",
      SG.speech_values(ws, 900.0, covered=part)[~part].max() == 0.0 and SG.speech_values(ws, 900.0, covered=part).max() > 0.5)
f = SG.fuse(np.array([0.0, 0.6, 0.6, 0.0]), np.array([0.0, 0.0, 0.6, 1.0]))
check("fuse: as in Round 61", f[1] == 0.6 and abs(f[2] - 0.85) < 1e-9 and abs(f[3] - SG.AUDIO_WEIGHT) < 1e-9)
check("WARM is the heat a bucket needs to become hot once speech agrees", abs(W.WARM + SG.AGREE_BONUS - P.HOT) < 1e-9)

# ---------------------------------------------------------------- the old path, behind --full-transcript
short = TMP / "ten-minutes.m4a"
subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "1200", "-t", "600", "-i", str(audio),
                "-c", "copy", str(short)], check=True)
import core.transcriber as T  # noqa: E402

whole_ears = Ears()
real_transcribe = T.transcribe_audio
T.transcribe_audio = lambda wav, settings, on_progress=None: whole_ears(Path(wav))
try:
    out_full = TMP / "full"
    out_full.mkdir()
    words_full, heat_full, wins_full = CS.analyse(short, [], 600.0, out_full, full_transcript=True, say=quiet_print)
    check("--full-transcript: the whole stream goes to the transcriber, no windows", wins_full is None
          and len(whole_ears.seconds) == 1 and abs(whole_ears.seconds[0] - 600.0) < 2.0
          and (out_full / "transcript.json").is_file() and not (out_full / "windows.json").exists(), whole_ears.seconds)
    check("--full-transcript: the burst at 1500 s (300 s into this cut) is hot", any(
        h["value"] >= P.HOT and 295 <= h["start_time"] <= 310 for h in heat_full))
    again = Ears()
    T.transcribe_audio = lambda wav, settings, on_progress=None: again(Path(wav))
    w2, _, wins_kept = CS.analyse(short, [], 600.0, out_full, say=quiet_print)
    check("a folder that already has a whole transcript keeps using it", again.seconds == [] and w2 == words_full
          and wins_kept is None)

    # ------------------------------------------------------------ the tool itself, twice (--plan)
    chat_csv = TMP / "chat.csv"
    chat_csv.write_text("seconds,message\n" + "".join(f"{m.t},{m.text}\n" for m in chat))
    CS.OUT_ROOT = TMP / "streams"
    tool_ears = Ears()
    T.transcribe_audio = lambda wav, settings, on_progress=None: tool_ears(Path(wav))
    argv = ["clip_stream.py", "--video", str(audio), "--chat", str(chat_csv), "--campaign", "Test: synthetic", "--count", "3",
            "--name", "three-hours", "--plan"]
    printed = io.StringIO()
    sys.argv = argv
    with contextlib.redirect_stdout(printed):
        CS.main()
    first = printed.getvalue()
    run = CS.OUT_ROOT / "three-hours"
    check("tool: numbered stages with estimates", all(f"[{k}/5] " in first for k in range(1, 6))
          and "Loudness of the whole stream (3:00:00; about" in first and "of audio instead of 3:00:00" in first, first[:600])
    check("tool: says how little it transcribes", "6 windows" in first or "5 windows" in first, first[:900])
    check("tool: 3 clips planned from window audio only", "3 clips planned (dry run)" in first
          and abs(sum(tool_ears.seconds) - total) < 1.0, (sum(tool_ears.seconds), first[-300:]))
    check("tool: everything a re-run needs is in the folder", all((run / f).is_file() for f in (
        "progress.json", "loudness.npy", "windows.json", "transcript_windows.json", "heatmap.json", "moments.json",
        "clips.json")) and len(list((run / "windows").glob("*.json"))) == len(pieces))
    before = sum(tool_ears.seconds)
    calls_before = len(loudness_calls)
    printed = io.StringIO()
    sys.argv = argv
    with contextlib.redirect_stdout(printed):
        CS.main()
    second = printed.getvalue()
    check("tool, run again: every step is kept from the last run", sum(tool_ears.seconds) == before
          and len(loudness_calls) == calls_before and second.count("kept from the last run") >= 2
          and "windows already transcribed" in second and "3 clips planned (dry run)" in second, second)
    printed = io.StringIO()
    sys.argv = argv[:argv.index("--count") + 1] + ["2"] + argv[argv.index("--count") + 2:]
    with contextlib.redirect_stdout(printed):
        CS.main()
    check("tool, another --count: picks again, transcribes nothing", sum(tool_ears.seconds) == before
          and "2 clips planned (dry run)" in printed.getvalue(), printed.getvalue()[-300:])
finally:
    T.transcribe_audio = real_transcribe
helps = subprocess.run([sys.executable, "tools/clip_stream.py", "--help"], cwd=ROOT, capture_output=True, text=True).stdout
check("--full-transcript is a flag of the tool", "--full-transcript" in helps and "--plan" in helps)

import gta_tab as G  # noqa: E402

plain_argv = G.stream_argv(url="https://www.twitch.tv/videos/1", campaign="Whop: x")
full_argv = G.stream_argv(url="https://www.twitch.tv/videos/1", campaign="Whop: x", full_transcript=True)
check("the tab: windows by default, --full-transcript when ticked", "--full-transcript" not in plain_argv
      and full_argv[-1] == "--full-transcript" and all(a in helps for a in full_argv if a.startswith("--")))

# ---------------------------------------------------------------- the progress file
pr = W.Progress(TMP / "p" / "progress.json")
pr.start("pick", "count=3")
check("progress: a started step is not done", not pr.done("pick", "count=3"))
pr.finish("pick", "count=3", clips=3)
pr2 = W.Progress(TMP / "p" / "progress.json")
check("progress: done survives a restart, but only for the same inputs", pr2.done("pick", "count=3")
      and not pr2.done("pick", "count=6") and pr2.info("pick")["clips"] == 3 and not pr2.done("render"))
(TMP / "p" / "progress.json").write_text("{half a file")
check("progress: a damaged file starts over instead of crashing", not W.Progress(TMP / "p" / "progress.json").done("pick", "count=3"))

# ---------------------------------------------------------------- long downloads: retry, backoff, completeness
whole_file = TMP / "whole.m4a"
subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=100", "-c:a",
                "aac", str(whole_file)], check=True)
cut_file = TMP / "cut.m4a"
subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(whole_file), "-t", "60", "-c", "copy",
                str(cut_file)], check=True)
check("complete: a whole file is, a short one is not, junk is not", V.complete(whole_file, 100.0) and not V.complete(cut_file, 100.0)
      and V.complete(cut_file) and not V.complete(TMP / "chat.csv", 0.0) and not V.complete(TMP / "nope.m4a"))
check("complete: 15 s (or 1 %) short is allowed", V.complete(whole_file, 110.0) and not V.complete(whole_file, 120.0)
      and V.complete(whole_file, 100.9))

attempts: list[str] = []
script = ["error", "short", "whole"]


def fake_fetch(url: str, out_dir: Path) -> None:
    what = script[len(attempts)]
    attempts.append(what)
    if what == "error":
        raise OSError("HTTP Error 503: fragment 812 not available")
    (out_dir / "audio.m4a").write_bytes((cut_file if what == "short" else whole_file).read_bytes())


real_fetch = V._fetch_audio
V._fetch_audio = fake_fetch
waits: list[float] = []
try:
    dl = TMP / "dl"
    got = V.audio_only("https://www.twitch.tv/videos/1", dl, expected=100.0, sleep=waits.append)
    check("download: an error, then a short file, then the whole one: retried with growing waits",
          attempts == ["error", "short", "whole"] and waits == [5.0, 20.0] and V.complete(got, 100.0), (attempts, waits))
    attempts.clear()
    check("download: a complete file already there is used as it is",
          V.audio_only("https://www.twitch.tv/videos/1", dl, expected=100.0, sleep=waits.append) == got and attempts == [])
    (dl / "audio.m4a").write_bytes(cut_file.read_bytes())  # as if an earlier run left a short file
    script[:] = ["whole"]
    V.audio_only("https://www.twitch.tv/videos/1", dl, expected=100.0, sleep=waits.append)
    check("download: a short file from an earlier run is fetched again", attempts == ["whole"] and V.complete(dl / "audio.m4a", 100.0))
    attempts.clear()
    waits.clear()
    script[:] = ["short", "error", "short", "short"]
    try:
        V.audio_only("https://www.twitch.tv/videos/2", TMP / "dl2", expected=100.0, sleep=waits.append)
        check("download: gives up after four tries", False)
    except RuntimeError as exc:
        check("download: gives up after four tries (5, 20, 60 s apart) and leaves no short file behind",
              len(attempts) == 4 and waits == [5.0, 20.0, 60.0] and "4 tries" in str(exc)
              and not list((TMP / "dl2").glob("audio.*")), (attempts, waits, str(exc)))
finally:
    V._fetch_audio = real_fetch

sections: list[tuple[float, float]] = []


def fake_section(url: str, start: float, end: float, out: Path) -> None:
    sections.append((start, end))
    out.write_bytes((cut_file if len(sections) == 1 else whole_file).read_bytes())  # 60 s first, then 100 s


real_section = V._fetch_section
V._fetch_section = fake_section
try:
    waits.clear()
    path, offset = V.section("https://www.twitch.tv/videos/1", 503.0, 583.0, TMP / "sec" / "section_1.mp4", sleep=waits.append)
    check("section: a file too short for its clip is fetched again", len(sections) == 2 and waits == [5.0] and offset == 500.0
          and sections[0] == (500.0, 586.0) and V.probe_seconds(path) > 83.0, (sections, waits))
    V.section("https://www.twitch.tv/videos/1", 503.0, 583.0, TMP / "sec" / "section_1.mp4", sleep=waits.append)
    check("section: a complete one is kept", len(sections) == 2)
finally:
    V._fetch_section = real_section
opts = V._ydl({}).params
check("yt-dlp never skips a missing fragment (a short file would shift every later second)",
      opts["skip_unavailable_fragments"] is False and opts["fragment_retries"] == 10 and opts["continuedl"] is True)

doc = (ROOT / "docs" / "CLIP_MODE_TUNING.md").read_text()
check("the numbers are written down", "Long VODs" in doc and "45 min" in doc and "MAX_WINDOWS" in doc and "DEAD_DB" in doc)

print(f"\n{len(FAILED)} failed" if FAILED else "\nAll Round 65 checks passed.")
sys.exit(1 if FAILED else 0)
