"""Round 55: idea-complete clips (35 s to 3 min, no 60 s cap) and real sample clips on the website.

1. core.clip_length: 35-180 s by default, no automatic cap, the teaser allowed up to 180 s, the
   judge reads a whole 3-minute clip, re-cuts up to 180 s. CLIP_LENGTH=legacy brings back
   Round 54's 30-60 s rules exactly (tests/run_all.py sets it for the suites before 55).
2. Boundaries follow the thought (core.hook_selector): a clip stays with the speaker until they
   land the point. It never ends before a "so that's why" / "but" that delivers it, halfway
   through a list they announced, on a question that's answered next, or on the other person's
   "yeah"; a dangling opener moves back to its setup. Nothing over 60 s is cut by the worker.
3. The AI prompts (picker, fallback picker, judge) ask for complete ideas of 35-180 s.
4. The website shows real sample clips (website/samples) as playable vertical videos, labelled as
   sample cuts, not client work; no illustration.

Run: timeout 300 .venv/bin/python tests/test_round55.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.pop("CLIP_LENGTH", None)
os.environ["NAV_NO_CLIP_PAGES"] = "1"
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


from core import agent_brain as A  # noqa: E402
from core import climax_hook as CH  # noqa: E402
from core import clip_length as L  # noqa: E402
from core import clipper  # noqa: E402
from core import hook_selector as H  # noqa: E402
from core.pipeline import STANDARD_EDIT  # noqa: E402
from core.transcriber import Word  # noqa: E402

# ============================== 1. The limits ==============================
check("Default: 35-180 s, idea-complete", (L.LEGACY, L.MIN_SECONDS, L.MAX_SECONDS) == (False, 35.0, 180.0))
check("The selector uses them", (H.MIN_CLIP_DURATION, H.MAX_CLIP_DURATION) == (35.0, 180.0))
check("No automatic 59 s cap (the worker and Make clips)", STANDARD_EDIT.max_seconds is None and L.CAP_SECONDS is None)
check("The climax teaser may run with clips up to the 180 s platform limit", CH.MAX_TOTAL_SECONDS == 180.0)
check("The judge reads a whole 3-minute clip", A.JUDGE_MAX_WORDS >= 600)
check("Re-cuts (Make clips trims, pitch trims) up to 180 s", clipper.MAX_CLIP_DURATION == 180.0)
try:
    clipper.rerender_clip("/nonexistent/source.mp4", 0.0, 150.0, [], "/nonexistent/clip.mp4")
    recut = "no error"
except ValueError as exc:
    recut = f"ValueError: {exc}"
except Exception as exc:  # noqa: BLE001 - any other error means the 150 s length passed the check
    recut = type(exc).__name__
check("A 150 s re-cut passes the length check", not recut.startswith("ValueError"), recut)
check("Timeouts grow with the clip", L.scaled_timeout(180, 600, 8) == 1440 and L.scaled_timeout(40, 600, 8) == 600)

legacy = subprocess.run(
    [sys.executable, "-c", "from core import clip_length as L, hook_selector as H, agent_brain as A, climax_hook as C;"
     "from core.pipeline import STANDARD_EDIT as S;"
     "print(L.MIN_SECONDS, L.MAX_SECONDS, S.max_seconds, C.MAX_TOTAL_SECONDS, A.JUDGE_MAX_WORDS,"
     " 'between 30 and 60 seconds' in A._SYSTEM_PROMPT, H.MAX_CLIP_DURATION)"],
    cwd=ROOT, capture_output=True, text=True, env={**os.environ, "CLIP_LENGTH": "legacy"}, timeout=120)
check("CLIP_LENGTH=legacy: Round 54's 30-60 s, 59 s cap, 59 s teaser, old prompt",
      legacy.stdout.split() == ["30.0", "60.0", "59.0", "59.0", "300", "True", "60.0"], legacy.stdout + legacy.stderr[-400:])

run_all = (ROOT / "tests" / "run_all.py").read_text()
block = run_all[run_all.index("    env = {"):run_all.index("    try:\n        result")]
block = "\n".join(line[4:] for line in block.splitlines())


def suite_env(name: str) -> dict:
    scope = {"os": os, "re": re, "test": ROOT / "tests" / name, "scratch": Path("/tmp")}
    exec(block, scope)  # noqa: S102 - our own run_all.py
    return scope["env"]


check("run_all.py: suites before 55 run with CLIP_LENGTH=legacy, this one with the new default",
      suite_env("test_round54.py").get("CLIP_LENGTH") == "legacy"
      and suite_env("test_round6.py").get("CLIP_LENGTH") == "legacy"
      and suite_env("test_round55.py").get("CLIP_LENGTH") != "legacy")


# ============================== 2. Boundaries follow the thought ==============================
def transcript(lines: list[tuple[str, float]], t0: float = 10.0, per_word: float = 0.32) -> list[Word]:
    """Sentences of words (0.32 s each), each followed by its pause."""
    words: list[Word] = []
    t = t0
    for sentence, pause in lines:
        for token in sentence.split():
            words.append(Word(text=token, start=round(t, 3), end=round(t + per_word - 0.04, 3)))
            t += per_word
        t += pause
    return words


def pick(words: list[Word], start: float, end: float) -> H.ClipResult | None:
    clip = H.ClipResult(start_time=start, end_time=end, viral_title="t", caption="c", hashtags=[], virality_score=80)
    out = H.prepare_candidates([clip], words, words[-1].end + 5)
    return out[0] if out else None


def said(words: list[Word], clip: H.ClipResult) -> str:
    return H.clip_text(words, clip.start_time, clip.end_time)


filler = ("Most teams price their software by the seat because it is easy to explain to finance and to procurement "
          "and nobody gets fired for it.")
# a. The proposal stops at 45 s, but "So that's why..." (the payoff) comes later: the clip keeps going.
argument = [("Here is the mistake I see in almost every pricing meeting.", 0.25)] + [(filler, 0.2)] * 7 + [
    ("Buyers do not care how many logins they get.", 0.2),
    ("So that's why we moved every customer to outcome pricing, and revenue per account doubled in a year.", 1.2),
    ("What would you change first if you started over?", 0.6),
    ("Honestly, I would start with the onboarding call and nothing else.", 1.0)] + [(filler, 0.3)] * 3
words = transcript(argument)
clip = pick(words, 9.9, 55.0)
check("A clip doesn't stop before the 'So that's why...' that lands the point",
      clip is not None and "revenue per account doubled in a year." in said(words, clip)
      and said(words, clip).rstrip().endswith("doubled in a year."),
      clip and (round(clip.end_time - clip.start_time, 1), said(words, clip)[-120:]))
check("...and it ends there, at the topic change (the next question), not later",
      clip is not None and "What would you change" not in said(words, clip))
check("...and it is longer than the old 60 s limit, kept whole", clip is not None and clip.end_time - clip.start_time > 60,
      clip and clip.end_time - clip.start_time)
check("The worker's cap leaves it alone", clip is not None and STANDARD_EDIT.max_seconds is None
      and H.cap_clip_length([clip], words, STANDARD_EDIT.max_seconds or 10**6)[0] == clip)

# b. The other person's "Yeah." after the point is not part of the clip.
words = transcript([("Revenue is a lagging indicator, and pipeline coverage is the leading one.", 0.3)]
                   + [(filler, 0.25)] * 4
                   + [("That is the number I would put on the board slide every single month.", 0.35), ("Yeah.", 0.15),
                      ("Yeah.", 0.5), ("Totally different topic, but tell me about hiring your first salesperson?", 0.6)]
                   + [(filler, 0.3)] * 3)
month_end = next(w.end for w in words if w.text == "month.")
clip = pick(words, 9.9, month_end + 1.6)  # the AI's end lands on the "Yeah. Yeah."
check("A clip never ends on the other person's 'Yeah.'",
      clip is not None and said(words, clip).endswith("every single month."), clip and said(words, clip)[-80:])

# c. A question the speaker answers next is not an ending.
words = transcript([("Everyone asks me how we got our first enterprise customer.", 0.3)] + [(filler, 0.25)] * 4
                   + [("So how did we actually get the meeting?", 0.25),
                      ("We wrote the CFO a one-page memo with her own numbers in it.", 1.1)] + [(filler, 0.3)] * 3)
end_of_question = next(w.end for w in words if w.text == "meeting?")
clip = pick(words, 9.9, end_of_question + 0.1)
check("A clip doesn't end on a question the speaker answers next",
      clip is not None and said(words, clip).endswith("numbers in it."), clip and said(words, clip)[-80:])

# d. "Three mistakes": the clip runs to the third.
list_talk = [("There are three mistakes founders make with their first sales hire.", 0.3),
             ("The first is hiring a big-company rep who has never sold without a brand behind them.", 0.3),
             (filler, 0.25), (filler, 0.25),
             ("The second is paying them only on commission in a market with nine-month sales cycles.", 0.3),
             (filler, 0.25), (filler, 0.25),
             ("The third is hiring them before the founder has closed the first ten deals personally.", 1.0),
             ("Let's talk about fundraising for a minute?", 0.5)] + [(filler, 0.3)] * 3
words = transcript(list_talk)
second_end = next(w.end for w in words if w.text == "sales" and words[words.index(w) + 1].text == "cycles.") + 0.5
clip = pick(words, 9.9, second_end)
check("A clip that announces three mistakes runs to the third",
      clip is not None and "The third is hiring them" in said(words, clip)
      and said(words, clip).endswith("ten deals personally."), clip and said(words, clip)[-90:])
check("H.unfinished_list spots the gap", H.unfinished_list("there are three reasons. the first. the second.")
      and not H.unfinished_list("three reasons: first, second and the third"))

# e. A dangling opener moves back to its setup instead of losing it.
words = transcript([(filler, 0.6), ("Our churn was eleven percent a month in the first year.", 0.25),
                    ("And that's why we rebuilt onboarding from scratch around the first thirty days.", 0.25)]
                   + [(filler, 0.25)] * 4 + [("That one change cut churn in half within two quarters.", 1.2)]
                   + [(filler, 0.3)] * 3)
dangling_start = next(w.start for w in words if w.text == "And")
clip = pick(words, dangling_start, dangling_start + 45)
check("A clip proposed on 'And that's why...' opens on the claim before it",
      clip is not None and said(words, clip).startswith("Our churn was eleven percent"), clip and said(words, clip)[:80])

# f. Never over 180 s, never under 35 s.
long_words = transcript([(filler, 0.25)] * 40)
clip = pick(long_words, 9.9, 235.0)
check("Never over 180 s", clip is not None and clip.end_time - clip.start_time <= 180.0 + 1e-6,
      clip and clip.end_time - clip.start_time)
clip = pick(long_words, 9.9, 32.0)
check("Never under 35 s (a short proposal grows to a complete stretch)",
      clip is not None and clip.end_time - clip.start_time >= 35.0, clip and clip.end_time - clip.start_time)
fallback = H._fallback_clips(2, long_words[-1].end + 5, long_words)
check("Fallback picks follow the same 35-180 s rule",
      fallback and all(35.0 <= c.end_time - c.start_time <= 180.0 for c in fallback),
      [round(c.end_time - c.start_time, 1) for c in fallback])
check("Guest thank-yous and outros are never a clip", H.moment_problem("Great chat. Thanks for having me.") is not None)

# ============================== 3. The prompts ==============================
prompt = A._SYSTEM_PROMPT
check("Picker prompt: complete ideas of 35-180 s, never trimmed to a time",
      "35 to 180 seconds" in prompt and "Never shorten a clip to hit a time" in prompt
      and "between 30 and 60 seconds" not in prompt and "30–50 second" not in prompt)
check("Picker prompt: stay until the thesis lands; end on the conclusion or a topic change",
      "fully land their thesis, argument or story resolution" in prompt and "natural topic transition" in prompt)
check("Fallback picker prompt says the same", "35 to 180 seconds" in H.IDEA_LENGTH_RULES
      and "never trimmed to hit a time" in H.IDEA_LENGTH_RULES)
check("The judge marks clips that stop before the point", "stops before the speaker finishes the point" in A._JUDGE_PROMPT)

# ============================== 3b. The teaser on a long clip ==============================
STORY = ("So we spent the first two years building the wrong product for the wrong people, and we burned most of the "
         "money. Our investors were patient, the team was great, the market was there, and still nothing worked for us. "
         "Then one Tuesday a customer called me. And what nobody tells you about product market fit is that it feels "
         "like being pulled, not pushing, and every metric moves at once. That is when we knew we had it.")
teaser_text = STORY + " " + " ".join(["and we kept on building it every day after that"] * 14) + "."
teaser_words = [Word(text=token, start=round(2.0 + 0.38 * k, 3), end=round(2.0 + 0.38 * k + 0.35, 3))
                for k, token in enumerate(teaser_text.split())]  # as in test_round40
teaser = CH.find_climax(teaser_words, 2.0, teaser_words[-1].end + 0.4)
check("A clip over 60 s still gets its climax teaser (Round 54 refused anything past 59 s)", teaser is not None
      and teaser_words[-1].end - 2.0 > 60, teaser_words[-1].end - 2.0)

# ============================== 4. The website ==============================
site_dir = ROOT / "website"
site = (site_dir / "index.html").read_text()
visible = re.sub(r"<[^>]+>", " ", re.sub(r"<(style|script)[\s\S]*?</\1>", " ", site))
videos = re.findall(r"<video\b[^>]*>", site)
sources = re.findall(r'<source src="(samples/[^"]+\.mp4)"', site)
posters = re.findall(r'poster="(samples/[^"]+\.jpg)"', site)
check("Website: real playable videos, no illustration", len(videos) >= 1 and "Illustration" not in site
      and 'class="person"' not in site and "illus-note" not in site, len(videos))
check("...muted autoplay loop, inline on phones, with a poster", all(
    all(attr in tag for attr in ("muted", "loop", "playsinline", "autoplay", "poster=")) for tag in videos))
check("...each file and poster is in website/samples", sources and posters and len(sources) == len(videos) == len(posters)
      and all((site_dir / path).is_file() for path in sources + posters), (sources, posters))
check("...light enough to load fast (under 8 MB each)", all((site_dir / path).stat().st_size < 8_000_000 for path in sources))
if shutil.which("ffprobe"):
    sizes = [subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                             "-of", "csv=p=0", str(site_dir / path)], capture_output=True, text=True).stdout.strip()
             for path in sources]
    check("...vertical 9:16, H.264 with sound", all(size == "720,1280" for size in sizes), sizes)
check("...one-click sound toggle on each", site.count('class="sound"') == len(videos) and "aria-pressed" in site)
check("Labelled as samples, not client work: 'Sample executive cuts' and 'Sample cut from ...'",
      "Sample executive cuts" in visible and visible.count("Sample cut from") == len(videos)
      and "Sample cut from How I Invest" in visible and "Sample cut from Y Combinator" in visible
      and "not client work" in visible.lower())
check("No percent figures without a source (Round 54's check still holds)", visible.count("%") == 1)
check("Copy says how long a clip runs", "35 seconds to 3 minutes" in visible)
deploy = (site_dir / "DEPLOY.md").read_text()
check("DEPLOY.md uploads the samples folder", "samples" in deploy and "cp -R samples" in deploy)

print(f"\n{len(FAILED)} check(s) failed" if FAILED else "\nAll checks passed")
sys.exit(1 if FAILED else 0)
