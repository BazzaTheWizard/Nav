"""Round 50: the client page's two posts per clip, and no phone number.

1. The Shorts & Reels caption (core.social_post): a title, a description (the episode, then the
   clip), and 5 hashtags; every line checked against the clip, invented ones replaced.
2. It's written next to every clip and goes up with the page (page.json "social").
3. The page: the LinkedIn post and the Shorts & Reels caption are drop-downs with a down arrow,
   closed until tapped, each with Copy buttons; on every clip that has them.
4. "Request a call" opens an email asking for a call this week (no phone number anywhere).
5. The package says 1 LinkedIn post + 1 Shorts & Reels caption per clip (page, offer, email).
6. Sent: copying the caption and asking for a call are tracked.

Run: timeout 120 .venv/bin/python tests/test_round50.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for key in ("SOCIAL_POSTS", "EXEC_EMAIL", "CLIP_COVERS", "NAV_NO_CLIP_PAGES"):
    os.environ.pop(key, None)
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


from core import clip_pages, outreach, social_post as SP  # noqa: E402
from core.transcriber import Word  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="round50_"))
TEXT = ("Most AI pricing is still per seat, and that's backwards. Buyers pay for outcomes, not logins. "
        "Usage pricing lets small teams start without a big contract. Seats punish the customers who use "
        "your product the most, so pricing has to follow usage.")
WORDS = [Word(text=w, start=i * 0.4, end=i * 0.4 + 0.3) for i, w in enumerate(TEXT.split())]
INFO = dict(title="Why usage pricing beats per-seat for AI tools", video_title="AI pricing: what buyers actually pay for",
            show="Acme Revenue Show", theme="AI pricing", kind="episode")

# ============================== 1. The Shorts & Reels caption ==============================
plain = SP.build(WORDS, **INFO, ask=lambda p, s: None)
check("No AI: a title, a description (the episode, then the clip) and 5 hashtags",
      plain.title == INFO["title"] and plain.description.startswith('From Acme Revenue Show\'s episode "AI pricing')
      and "In this clip: most AI pricing is still per seat" in plain.description
      and plain.description.endswith("Full episode: Acme Revenue Show.") and len(plain.hashtags) == 5, plain)
check("...hashtags from what was said: the theme, a phrase from the title, the show, a field tag",
      plain.hashtags[:2] == ["AIPricing", "UsagePricing"] and "AcmeRevenueShow" in plain.hashtags
      and "Sales" in plain.hashtags, plain.hashtags)
check("...the caption box: the description, a blank line, then the hashtags",
      plain.caption() == plain.description + "\n\n" + " ".join("#" + t for t in plain.hashtags))
good = json.dumps({
    "title": "Why AI pricing should follow usage, not seats",
    "about_video": "Acme Revenue Show's episode on what buyers actually pay for in AI pricing.",
    "about_clip": "Buyers pay for outcomes, not logins, so usage pricing lets small teams start without a big contract.",
    "hashtags": ["AIPricing", "UsagePricing", "SaaS", "Sales", "AcmeRevenueShow"]})
ai = SP.build(WORDS, **INFO, ask=lambda p, s: good)
check("AI draft kept when it's the speaker's own words", ai.source == "ai" and ai.title.startswith("Why AI pricing should")
      and "Buyers pay for outcomes, not logins" in ai.description and ai.hashtags[2] == "SaaS", ai)
invented = json.dumps({
    "title": "Salesforce cut prices by 40% overnight",
    "about_video": "A conversation with Marc Benioff about Salesforce.",
    "about_clip": "Salesforce cut prices by 40% and doubled revenue.",
    "hashtags": ["Salesforce", "viral", "fyp", "UsagePricing", "OpenAIPricing", "Sales"]})
fixed = SP.build(WORDS, **INFO, ask=lambda p, s: invented)
check("Invented names, numbers and claims are replaced from the transcript",
      "Salesforce" not in fixed.title + fixed.description and "40" not in fixed.description + fixed.title
      and "Benioff" not in fixed.description and fixed.source == "ai+transcript", fixed)
check("...and so are invented or spammy hashtags (viral, fyp, unsaid names)",
      not {"Salesforce", "viral", "fyp", "OpenAIPricing"} & set(fixed.hashtags) and "UsagePricing" in fixed.hashtags
      and len(fixed.hashtags) == 5, fixed.hashtags)
check("No emoji or hashtags inside the title", SP.build(WORDS, **INFO, ask=lambda p, s: json.dumps(
    {"title": "🔥 Usage pricing #AI", "about_video": "", "about_clip": "", "hashtags": []})).title == "Usage pricing")
check("Hashtags: acronyms kept (AI, SaaS, B2B)", SP.clean_tag("ai pricing") == "AIPricing"
      and SP._tag_words("SaaSGrowth") == ["saas", "growth"] and SP._tag_words("B2BSales") == ["b2b", "sales"])
clip = TMP / "clip_01.mp4"
clip.write_bytes(b"")
path = SP.write_for_clip(clip, WORDS, **INFO, ask=lambda p, s: good)
check("Written next to the clip (<clip>_social_post.json) and read back",
      path.name == "clip_01_social_post.json" and SP.read_for_clip(clip)["title"] == ai.title)
pipeline_src = (ROOT / "core" / "pipeline.py").read_text()
check("The pipeline writes it for every clip, right after the LinkedIn post (SOCIAL_POSTS=0 turns it off)",
      "social_post.write_for_clip(" in pipeline_src and "if social_post.enabled():" in pipeline_src
      and pipeline_src.index("linkedin_post.write_for_clip(") < pipeline_src.index("social_post.write_for_clip("))

# ============================== 2. Up with the page ==============================
(TMP / "clip_01_linkedin_post.txt").write_text("Hook.\n\n• a\n• b\n• c\n\nQ?\n")


class FakeClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload_file(self, local, bucket, key, ExtraArgs=None):
        self.objects[key] = b""

    def put_object(self, Bucket, Key, Body, **_k):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": SimpleNamespace(read=lambda: self.objects[Key])}


fake = FakeClient()
clip_pages._require = lambda: (SimpleNamespace(bucket="b", base_url="https://clips.test"), fake)
clip_pages._upload_poster = lambda *a, **k: 0.0
os.environ["CLIP_COVERS"] = "0"
name, _url = clip_pages.publish_page("Acme Revenue Show", "AI pricing", [{"path": str(clip), "start": 1, "end": 30}])
entry = json.loads(fake.objects[f"{name}/page.json"])["clips"][0]
check("page.json carries the caption next to the LinkedIn post",
      entry.get("social", {}).get("title") == ai.title and entry["social"]["hashtags"] == ai.hashtags
      and entry.get("post", "").startswith("Hook."), entry)
clip_pages.replace_clip(name, "clip_01.mp4", clip, 2.0, 29.0)
entry = json.loads(fake.objects[f"{name}/page.json"])["clips"][0]
check("...and keeps it after a trim", entry.get("social", {}).get("title") == ai.title and entry["start"] == 2.0)
os.environ.pop("CLIP_COVERS")

# ============================== 3-5. The page, the call, the package ==============================
js = (ROOT / "cloudflare" / "clip-pages-worker.js").read_text()
check("No phone number anywhere on the pages", "201-466" not in js and "tel:" not in js and "4664807" not in js)
if shutil.which("node"):
    (TMP / "w.mjs").write_text(js)
    social = {"title": ai.title, "description": ai.description, "hashtags": ai.hashtags}
    (TMP / "r.mjs").write_text("""import worker from "./w.mjs";
const info = %s;
const env = { CLIPS: { get: async (k) => k.endsWith("page.json") ? { json: async () => info } : null } };
const page = await (await worker.fetch(new Request("https://x.test/acme-revenue-show-k2m4qx"), env)).text();
const offer = await (await worker.fetch(new Request("https://x.test/offer?from=acme-revenue-show-k2m4qx"), env)).text();
console.log(JSON.stringify({ page, offer }));
""" % json.dumps({"channel_title": "Acme Revenue Show", "video_title": "AI pricing", "kind": "episode",
                  "clips": [{"start": 1, "end": 40, "post": "Hook.\n\n• a\n• b\n• c\n\nQ?", "social": social},
                            {"start": 50, "end": 90, "post": "Hook two.", "social": social},
                            {"start": 95, "end": 130}]}))
    out = subprocess.run(["node", str(TMP / "r.mjs")], capture_output=True, text=True, timeout=30)
    pages = json.loads(out.stdout or "{}")
    page, offer = pages.get("page", ""), pages.get("offer", "")
    drops = re.findall(r'<details class="drop" data-drop="(\w+)">', page)
    check("Each clip: a LinkedIn post drop-down, then a Shorts & Reels caption drop-down",
          drops == ["linkedin", "social", "linkedin", "social"], (drops, out.stderr[-300:]))
    check("...closed until tapped, with a down arrow that turns when open",
          "<details class=\"drop\" data-drop=\"linkedin\" open" not in page and page.count('class="chev"') == 4
          and 'd="M2.5 4.5 6 8l3.5-3.5"' in page and ".drop[open] .chev { transform: rotate(180deg)" in page
          and "Tap to view and copy" in page)
    body = re.search(r'data-drop="social">([\s\S]*?)</details>', page).group(1)
    check("Caption drop-down: the title and the caption (description + hashtags), each with a Copy button",
          "Shorts &amp; Reels caption" in body and "YouTube · Instagram · TikTok" in body and "Copy title" in body
          and "Copy caption" in body and 'id="rtitle1"' in body and 'id="rcap1"' in body
          and "#AIPricing #UsagePricing #SaaS" in body and "Why AI pricing should follow usage" in body)
    post_body = re.search(r'data-drop="linkedin">([\s\S]*?)</details>', page).group(1)
    check("LinkedIn drop-down: the post and Copy post inside it", 'id="post1"' in post_body
          and 'data-copy="1"' in post_body and "Copy post" in post_body)
    check("A clip without posts shows neither drop-down", page.count('data-drop="social"') == 2
          and 'id="rtitle3"' not in page)
    call = re.search(r'data-call href="mailto:([^"]+)"', page)
    mail = unquote(call.group(1).replace("&amp;", "&")) if call else ""
    check("'Request a call' opens an email to you asking for a call this week",
          call is not None and "Request a call" in page and mail.startswith("barrynavan@gmail.com?subject=Call about")
          and "Acme Revenue Show" in mail and "call this week" in mail and "times that work" in mail.lower(), mail[:200])
    check("...and is tracked (Sent shows who asked)", 'send("call")' in page)
    check("Package on the clip page: 1 LinkedIn post and 1 Shorts & Reels caption per clip",
          "each with 1 LinkedIn post and 1 Shorts &amp; Reels caption" in page
          and "<li>LinkedIn posts</li><li>Shorts &amp; Reels captions</li>" in page)
    check("...and on the offer page (facts, what's included, price list) with a call request, no phone",
          "posts written for every clip: 1 LinkedIn post and 1 Shorts &amp; Reels caption" in offer
          and "<h3>A Shorts & Reels caption for every clip</h3>" in offer)
    # Round 54 replaced the offer page's price list with the three plans; the package line moved into
    # the Plans intro (Round 56: this check had gone stale; it only runs where node is installed).
    check("...offer page price list and call button", "1 LinkedIn post and 1 Shorts &amp; Reels caption for every clip"
          in offer and "Request a call" in offer and "tel:" not in offer)
else:
    print("SKIP  node not installed: the page checks need it")
email = outreach.build_outreach_email("Acme Revenue Show", video_title="AI pricing", drive_link="https://x", clip_count=3,
                                      business=True)
check("The pitch email names both posts", "a LinkedIn post, a Shorts & Reels caption and a cover image for each" in email)
check("The yes-reply's offer too", "each with a LinkedIn post and a Shorts & Reels caption" in outreach.offer_text(3))

# ============================== 6. Sent ==============================
now = datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc)
stamp = int(now.timestamp() * 1000)
fake.objects = {f"_events/{name}/{stamp}_open_0_aa", f"_events/{name}/{stamp}_social_2_bb", f"_events/{name}/{stamp}_call_0_cc"}
fake.list_objects_v2 = lambda **_k: {"Contents": [{"Key": k} for k in fake.objects]}
activity = clip_pages.page_activity()[name]
row = clip_pages.views_feed({name: activity}, [{"page_name": name, "title": "Acme"}])[0]
check("Sent: copying the Shorts & Reels caption and asking for a call are recorded",
      row["took"] == ["reels caption 2"] and row["call"] == 1, row)
check("...and described", "started a call request" in clip_pages.describe_activity(activity))
app_src = (ROOT / "app.py").read_text()
check("Sent's page views show the call column", '"Call": "📞" if row.get("call") else ""' in app_src)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(FAILED)} check(s) failed" if FAILED else "\nAll checks passed")
sys.exit(1 if FAILED else 0)
