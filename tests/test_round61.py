"""Round 61: Clip mode signals (core.clip_mode) and CLIP_LENGTH=gaming. No network, no model.

Run: timeout 600 .venv/bin/python -u tests/test_round61.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


from core import heatmap_scorer  # noqa: E402
from core.clip_mode import chat as C  # noqa: E402
from core.clip_mode import signals as SG  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="nav-r61-"))

# --- chat formats --------------------------------------------------------------------------
td = {"comments": [{"content_offset_seconds": 12.5, "message": {"body": "KEKW"}},
                   {"content_offset_seconds": 3.0, "message": {"fragments": [{"text": "hi "}, {"text": "chat"}]}},
                   {"content_offset_seconds": "bad"}]}
(TMP / "td.json").write_text(json.dumps(td))
m = C.load(TMP / "td.json")
check("chat: TwitchDownloader JSON, sorted, fragments joined, bad rows skipped",
      [(x.t, x.text) for x in m] == [(3.0, "hi chat"), (12.5, "KEKW")], m)
yt = [
    {"replayChatItemAction": {"videoOffsetTimeMsec": "61000", "actions": [{"addChatItemAction": {"item": {
        "liveChatTextMessageRenderer": {"message": {"runs": [{"text": "no way "},
                                                             {"emoji": {"shortcuts": [":fire:"]}}]}}}}}]}},
    {"replayChatItemAction": {"videoOffsetTimeMsec": "5000", "actions": [{"addChatItemAction": {"item": {
        "liveChatTextMessageRenderer": {"message": {"runs": [{"text": "first"}]}}}}}]}},
]
(TMP / "yt.live_chat.json").write_text("\n".join(json.dumps(x) for x in yt))
m = C.load(TMP / "yt.live_chat.json")
check("chat: yt-dlp live_chat lines", [(x.t, x.text) for x in m] == [(5.0, "first"), (61.0, "no way :fire:")], m)
(TMP / "c.csv").write_text("seconds,message\n10,hello\n2.5,W\n")
check("chat: CSV with a header", [(x.t, x.text) for x in C.load(TMP / "c.csv")] == [(2.5, "W"), (10.0, "hello")])
(TMP / "empty.txt").write_text("nothing here")
try:
    C.load(TMP / "empty.txt")
    check("chat: an unknown file is refused", False)
except ValueError:
    check("chat: an unknown file is refused", True)
check("chat: Twitch VOD id from the URL", C._TWITCH_VOD.search("https://www.twitch.tv/videos/2233445566").group(1)
      == "2233445566")

# --- chat spikes -> a heatmap the existing scorer understands ------------------------------
duration = 900.0
msgs = [C.Message(t, "hello") for t in range(0, 900, 2)]  # steady chat: one message every 2 s
msgs += [C.Message(400 + i * 0.1, "KEKW no way") for i in range(80)]  # a hype burst at 400-408 s
msgs += [C.Message(700 + i * 0.25, "chat") for i in range(10)]  # a small bump at 700 s
vals = SG.chat_values(msgs, duration)
heat = SG.heatmap(vals)
spikes = heatmap_scorer.spikes(heat)
check("chat spike: the burst at 400 s is a hot spike", any(a <= 402 <= b for a, b, _ in spikes), spikes)
check("chat spike: steady chat is not", all(not (100 <= a <= 380) for a, b, _ in spikes), spikes)
check("chat spike: the small bump stays below hot", all(not (690 <= a <= 715) for a, b, _ in spikes), spikes)
check("heatmap: yt-dlp's bucket format", set(heat[0]) == {"start_time", "end_time", "value"}
      and heat[1]["start_time"] == SG.BUCKET)
check("hype words count more", SG.chat_activity([C.Message(1, "KEKW")], 10)[0]
      > SG.chat_activity([C.Message(1, "ok")], 10)[0])

# --- loudness spikes ---------------------------------------------------------------------
wav = TMP / "a.wav"
subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                "anoisesrc=d=300:a=0.02:c=pink", "-af", "volume='if(between(t,200,206),20,1)':eval=frame",
                "-ar", "48000", str(wav)], check=True)
lufs = SG.loudness(wav)
av = SG.audio_values(lufs)
hot = [i * SG.BUCKET for i, v in enumerate(av) if v >= 0.65]
check("loudness: the loud burst at 200 s is found", any(195 <= t <= 206 for t in hot), hot)
check("loudness: quiet stretches are not", all(195 <= t <= 210 for t in hot), hot)

# --- fusing ---------------------------------------------------------------------------------
import numpy as np  # noqa: E402

f = SG.fuse(np.array([0.0, 0.6, 0.6, 0.0]), np.array([0.0, 0.0, 0.6, 1.0]))
check("fuse: chat alone, both (bonus), loudness alone (weaker)",
      f[1] == 0.6 and abs(f[2] - 0.85) < 1e-9 and abs(f[3] - SG.AUDIO_WEIGHT) < 1e-9, f)
check("fuse: either signal may be missing", len(SG.fuse(None, np.array([1.0]))) == 1 and len(SG.fuse(None, None)) == 0)

# --- CLIP_LENGTH=gaming -----------------------------------------------------------------
def limits(mode: str) -> dict:
    code = ("import json,sys; sys.path.insert(0,'.'); from core import clip_length as c; "
            "print(json.dumps([c.MIN_SECONDS,c.MAX_SECONDS,c.CAP_SECONDS,c.label()]))")
    env = {**os.environ, "CLIP_LENGTH": mode}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


check("clip length: gaming is 15-60 s, capped at 59 s", limits("gaming") == [15.0, 60.0, 59.0, "15-60 s"],
      limits("gaming"))
check("clip length: the default is unchanged", limits("idea") == [35.0, 180.0, None, "35 s to 3 min"])
check("clip length: legacy is unchanged", limits("legacy") == [30.0, 60.0, 59.0, "30-60 s"])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'ALL PASSED' if not FAILED else f'{len(FAILED)} FAILED: ' + ', '.join(FAILED)}")
sys.exit(1 if FAILED else 0)
