"""Round 45: LinkedIn B2B pivot. Executive mode (no mid-clip graphics, one verified
lower-third ID), the linkedin_executive captions, a LinkedIn post next to every clip, and the
offer page on the clip-page Worker.

Run: timeout 60 .venv/bin/python tests/test_round45.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for key in ("VISUAL_OVERLAYS", "CAPTION_STYLE", "EXEC_ACCENT", "LINKEDIN_POSTS"):
    os.environ.pop(key, None)

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or not detail else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


from config import Settings, load_settings  # noqa: E402
from core import clip_pages, executive, linkedin_post, outreach  # noqa: E402
from core.subtitle_engine import caption_style_name, create_ass_subtitles  # noqa: E402
from core.transcriber import Word  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="round45_"))


def words_of(text: str, start: float = 0.4) -> list[Word]:
    out, t = [], start
    for w in text.split():
        d = 0.18 + 0.035 * len(w)
        out.append(Word(text=w, start=t, end=t + d))
        t += d + 0.06
    return out


# ============================== A. B2B clean mode by default ==============================
settings = load_settings()
check("Default: VISUAL_OVERLAYS executive_minimal, b2b_clean_mode on",
      settings.visual_overlays == "executive_minimal" and settings.b2b_clean_mode
      and executive.overlay_mode(settings) == "executive_minimal" and executive.b2b_clean_mode(settings))
os.environ["VISUAL_OVERLAYS"] = "full"
check("VISUAL_OVERLAYS=full brings back the creator graphics", not executive.b2b_clean_mode() and
      executive.overlay_mode() == "full")
os.environ["VISUAL_OVERLAYS"] = "nonsense"
check("An unknown value falls back to executive_minimal", executive.overlay_mode() == "executive_minimal")
os.environ.pop("VISUAL_OVERLAYS")
pipeline_src = (ROOT / "core" / "pipeline.py").read_text()
check("The graphics director (cards, tear-outs, Wikipedia, logos) runs only in 'full' mode",
      'if graphics is not None and settings.auto_graphics and overlays == "full":' in pipeline_src
      and pipeline_src.count("direction = direct(") == 1)
check("Settings carries the fields (older settings objects still work)",
      Settings.__dataclass_fields__["visual_overlays"].default == "executive_minimal"
      and Settings.__dataclass_fields__["linkedin_posts"].default is True)

# --- the lower-third: only when the face check is certain ---
class Plan:  # a crop plan stand-in: the clip has a face track
    frame_width = 1920


class Shot:
    def __init__(self, kind, start, end):
        self.kind, self.start, self.end = kind, start, end


class Info:
    title = "Omeed Tabiei on startup law mistakes"
    description = ""
    channel = "Optimist Legal"


def ident(source, plan, speakers, *_a):
    return {s.name: ((0.1,), "the only person in the video") for s in speakers if s.name == "Omeed Tabiei"}, []


solo = lambda *_a: [Shot("solo", 100.0, 140.0)]  # noqa: E731
lt, note = executive.plan_lower_third("v.mp4", Plan(), 100.0, 140.0, Info(), [], [], identify=ident, options=solo)
check("Named speaker alone on screen for 1.0-4.5 s -> the ID", lt is not None and lt.name == "Omeed Tabiei"
      and (lt.start, lt.end) == (1.0, 4.5), note)
late = lambda *_a: [Shot("solo", 103.0, 140.0)]  # noqa: E731
check("Not on screen for the whole span -> no ID", executive.plan_lower_third(
    "v.mp4", Plan(), 100.0, 140.0, Info(), [], [], identify=ident, options=late)[0] is None)
panel = lambda *_a: [Shot("panel", 100.0, 140.0)]  # noqa: E731
check("Only in a split panel -> no ID", executive.plan_lower_third(
    "v.mp4", Plan(), 100.0, 140.0, Info(), [], [], identify=ident, options=panel)[0] is None)
check("Face not identified -> no ID (never a guess)", executive.plan_lower_third(
    "v.mp4", Plan(), 100.0, 140.0, Info(), [], [], identify=lambda *a: ({}, []), options=solo)[0] is None)
check("No face track (plan None) -> no ID", executive.plan_lower_third(
    "v.mp4", None, 100.0, 140.0, Info(), [], [], identify=ident, options=solo)[0] is None)


class Split:
    spans = [(100.5, 110.0)]


check("Opening on the two-speaker split -> no ID", executive.plan_lower_third(
    "v.mp4", Plan(), 100.0, 140.0, Info(), [], [], Split(), identify=ident, options=solo)[0] is None)
os.environ["VISUAL_OVERLAYS"] = "none"
check("VISUAL_OVERLAYS=none -> no ID", executive.plan_lower_third(
    "v.mp4", Plan(), 100.0, 140.0, Info(), [], [], identify=ident, options=solo)[0] is None)
os.environ.pop("VISUAL_OVERLAYS")

# ============================== B. linkedin_executive captions ==============================
check("Default caption style is linkedin_executive", caption_style_name() == "linkedin_executive")
TEXT = ("The biggest mistake founders make is hiring a sales leader before they've closed $1M in revenue "
        "themselves. You have to own the first ten deals.")
ws = words_of(TEXT)
os.environ["EXEC_ACCENT"] = "#5B8DEF"
ass = create_ass_subtitles(ws, TMP / "exec.ass", clip_duration=ws[-1].end + 1,
                           lower_third=executive.LowerThird("Omeed Tabiei", "Founder, Optimist Legal")).read_text()
os.environ.pop("EXEC_ACCENT")
dialogue = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
captions = [l for l in dialogue if ",ShortForm," in l]
text_layer = [l for l in captions if l.startswith("Dialogue: 3,")]
plain = [re.sub(r"\{[^}]*\}", "", l.split(",,", 1)[1].split(",", 4)[-1]) for l in text_layer]
check("Font: NavExec Inter (Inter Display SemiBold), not Arial Black", "Style: ShortForm,NavExec Inter,62" in ass
      and "Arial Black" not in ass.split("[Events]")[1])
check("Sentence case, not all caps", any("The biggest mistake" in p for p in plain)
      and not any(p.isupper() for p in plain), plain[:2])
check("No bounce or scale animation on captions",
      not any(re.search(r"\\fsc[xy]|\\t\(", l) for l in captions))
check("Silver for words not yet said, white once said",
      executive.ass_colour("#A1A1AA") in ass and executive.ass_colour("#FFFFFF") in ass)
lines_ok = all(p.count("\\N") <= 1 and all(len(part) <= executive.LINE_CHARS + 2 for part in p.split("\\N")) for p in plain)
check("At most two lines, each within the safe width", lines_ok, plain)
check("A caption never ends on 'a' / 'the' / 'to'", not any(
    re.search(r"\b(a|the|to|of|and)$", p.replace("\\N", " ").strip()) for p in plain), sorted(set(plain)))
check("The spoken figure gets the accent underline ($1M), the glyph stays white",
      any("\\u1}$1M" in l and "&HEF8D5B&" in l for l in captions if l.startswith("Dialogue: 2,"))
      and not any("\\1c&HEF8D5B&" in l for l in text_layer))
check("Soft scrim behind the caption zone (blurred band, no box)",
      # Round 49: softer (8%), wider blur, inset from the frame's edges
      any(",Exec," in l and f"\\blur{executive.SCRIM_BLUR}" in l and f"m {executive.SCRIM_X[0]} 1010" in l
          for l in dialogue))
check("Soft halo, no hard outline", "\\bord5\\3c&H000000&\\3a&HA6&\\blur8" in ass)
check("Trailing full stop dropped, '?' kept", not any(p.rstrip().endswith(".") for p in plain))
lt_lines = [l for l in dialogue if l.startswith("Dialogue: 8,") or l.startswith("Dialogue: 7,")]
check("Lower-third: name and title, 1.0-4.5 s, fading in and out",
      any("Omeed Tabiei" in l for l in lt_lines) and any("Founder, Optimist Legal" in l for l in lt_lines)
      and all(",0:00:01.00,0:00:04.50," in l for l in lt_lines) and all("\\fad(" in l for l in lt_lines))
ass_none = create_ass_subtitles(ws, TMP / "exec2.ass", clip_duration=ws[-1].end + 1).read_text()
check("No lower-third unless one is given", "Omeed" not in ass_none and "Dialogue: 8," not in ass_none)
bold = create_ass_subtitles(ws, TMP / "bold.ass", clip_duration=ws[-1].end + 1, style="bold").read_text()
check("CAPTION_STYLE=bold keeps the creator look", "Arial Black" in bold and "BIGGEST" in bold and "Biggest" not in bold)
check("The renderer loads the bundled fonts", "fontsdir='{FONTS}'" in (ROOT / "core" / "video_renderer.py").read_text())
for path, family in ((executive.FONT_FILE, "NavExec Inter"), (executive.TEXT_FONT_FILE, "NavExec Inter Text")):
    try:
        from PIL import ImageFont

        name = ImageFont.truetype(str(path), 40).getname()[0]
    except ImportError:
        name = family
    check(f"Font file {path.name} is family '{family}'", path.is_file() and name == family, name)
check("Inter's licence ships with it", (ROOT / "assets" / "fonts" / "OFL-Inter.txt").is_file())
if shutil.which("ffmpeg"):
    still = TMP / "still.png"
    run = subprocess.run(["ffmpeg", "-v", "debug", "-y", "-f", "lavfi", "-i", "color=c=0x303840:s=1080x1920:d=5",
                    "-vf", f"ass='{TMP / 'exec.ass'}':fontsdir='{executive.FONTS}'", "-ss", "3.0", "-frames:v", "1",
                    str(still)], check=False, timeout=40, capture_output=True, text=True, errors="replace")
    ok = still.is_file()
    if ok:
        from PIL import Image

        crop = Image.open(still).convert("L").crop((150, 1100, 930, 1240))
        ok = crop.getextrema()[1] > 230  # white caption letters were drawn
    picked = re.findall(r"fontselect: \(NavExec Inter, \d+, 0\) -> (\S+),", run.stderr)
    check("libass renders the captions (white letters in the caption zone)", ok)
    check("...with the bundled NavExec Inter, not a fallback font", picked[:1] == ["NavExecInter-Regular"], picked)

# ============================== C. LinkedIn post per clip ==============================
transcript = ws
good = json.dumps({
    "hook": "The biggest mistake founders make is hiring a sales leader before closing $1M themselves",
    "takeaways": ["You have to own the first ten deals", "Hiring a sales leader before $1M in revenue is the mistake",
                  "Founders make the biggest mistake hiring sales leaders too early"],
    "question": "When did your team hire its first sales leader, and was it too early?"})
post = linkedin_post.build(transcript, title="Own the first ten deals", ask=lambda p, s: good)
check("AI draft that's grounded is kept", post.source == "ai" and len(post.takeaways) == 3
      and post.question.endswith("?"), post)
invented = json.dumps({"hook": "Founders who wait until $5M grow 3x faster, says Sequoia",
                       "takeaways": ["You have to own the first ten deals", "Sequoia data shows 80% fail", "x"],
                       "question": "Agree?"})
post2 = linkedin_post.build(transcript, title="t", ask=lambda p, s: invented)
check("Invented numbers and names are replaced with the speaker's own words",
      "$5M" not in post2.hook and "Sequoia" not in post2.text() and "80%" not in post2.text()
      and post2.source == "ai+transcript", post2.text())
check("'Agree?' is not a closing question", post2.question != "Agree?" and post2.question.endswith("?"))
post3 = linkedin_post.build(transcript, title="t", ask=lambda p, s: None)
check("No AI key -> the post comes from the transcript", post3.source == "transcript" and post3.hook)
clip_file = TMP / "clip_01.mp4"
clip_file.write_bytes(b"")
written = linkedin_post.write_for_clip(clip_file, transcript, title="t", ask=lambda p, s: good)
body = written.read_text()
check("Written next to the clip as clip_01_linkedin_post.txt", written.name == "clip_01_linkedin_post.txt"
      and written.parent == clip_file.parent)
check("Shape: hook, blank line, 3 bullets, blank line, question", re.fullmatch(
    r"[^\n]+\n\n(• [^\n]+\n){3}\n[^\n]+\?\n", body) is not None, body)
check("No hashtags or emoji", "#" not in body and not re.search("[\U0001F000-\U0001FAFF]", body))
check("Pipeline writes the post for every clip (LINKEDIN_POSTS)", "linkedin_post.write_for_clip(" in pipeline_src)

# ============================== D. Offer page and clip pages ==============================
check("The clip page carries each clip's post", clip_pages._post_for({"path": str(clip_file)}) == body)
check("Plays tracked: copied a post, opened the offer", {"copy", "offer"} <= set(clip_pages.EVENT_TYPES))
activity = {"opens": 1, "played": {1}, "half": set(), "finished": set(), "downloaded": set(), "last": None,
            "copied": {1}, "offer": 1}
described = clip_pages.describe_activity(activity)
check("Sent tab says so", "copied the LinkedIn post for 1" in described and "opened the offer" in described, described)
js = (ROOT / "cloudflare" / "clip-pages-worker.js").read_text()
price = re.search(r'price: "([^"]+)"', js).group(1)
spots = int(re.search(r"spots: (\d+)", js).group(1))
trial = re.search(r'trial: "([^"]+)"', js).group(1)
check("Offer numbers match the emails (core/outreach.py)", outreach.FOUNDING_PRICE == f"{price}/month"
      and outreach.FOUNDING_SPOTS == spots and outreach.TRIAL == f"a {trial}", (price, spots, trial))
if shutil.which("node"):
    (TMP / "worker.mjs").write_text(js)
    (TMP / "render.mjs").write_text("""import worker from "./worker.mjs";
const info = { channel_title: "Optimist Legal", video_title: "V", kind: "video",
  clips: [{ start: 1, end: 40, post: "Hook\\n\\n• a\\n• b\\n• c\\n\\nQ?" }] };
const env = { CLIPS: { get: async (k) => k.endsWith("page.json") ? { json: async () => info } : null } };
const page = await (await worker.fetch(new Request("https://x.test/omeed-tabiei-6p8kmx"), env)).text();
const offer = await (await worker.fetch(new Request("https://x.test/offer"), env)).text();
console.log(JSON.stringify({ page, offer }));
""")
    out = subprocess.run(["node", str(TMP / "render.mjs")], capture_output=True, text=True, timeout=30)
    pages = json.loads(out.stdout or "{}")
    offer, page = pages.get("offer", ""), pages.get("page", "")
    check("/offer: the LinkedIn B2B positioning", "Turnkey LinkedIn &amp; multi-platform short-form video, aligned to "
          "your monthly content calendar." in offer, out.stderr[-300:])
    check("/offer: price, 12 clips, trial, spots", "$500" in offer and "<b>12</b>" in offer and "2-week trial" in offer
          and "3 spots" in offer)
    visible = re.sub(r"<[^>]+>", " ", re.sub(r"<(style|script)[\s\S]*?</\1>", " ", offer))
    check("/offer: no outside statistics on the page", not re.search(r"\d+\s?%", visible))
    check("Clip page: post with a copy button, and the offer strip", 'data-copy="1"' in page and "Copy post" in page
          and 'href="/offer?from=omeed-tabiei-6p8kmx"' in page and "LinkedIn, Shorts and Reels" in page)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(FAILED)} check(s) failed" if FAILED else "\nAll checks passed")
sys.exit(1 if FAILED else 0)
