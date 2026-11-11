"""Round 46: mid-clip silence and filler trimming, the B2B LinkedIn Video Gap harvester, the
locked offer, calendar-angle personalization, SRT captions, and run_all's pins for older suites.

Run: timeout 60 .venv/bin/python tests/test_round46.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for key in ("VISUAL_OVERLAYS", "CAPTION_STYLE", "MIDCLIP_TRIM", "EXEC_ACCENT"):
    os.environ.pop(key, None)
os.environ["MIDCLIP_ZOOM"] = "1"  # Round 48 made the trim cuts plain; this suite checks Round 46's zoom
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


from core import clip_pages, linkedin_harvester as LH, linkedin_post, midclip, outreach, srt_export  # noqa: E402
from core.transcriber import Word  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="round46_"))
W = lambda text, a, b: Word(text=text, start=a, end=b)  # noqa: E731

# ============================== 1. Offer locked, older suites pinned ==============================
js = (ROOT / "cloudflare" / "clip-pages-worker.js").read_text()
num = lambda key: int(re.search(rf"{key}: (\d+)", js).group(1))  # noqa: E731
check("Offer: 12 clips/month (3 x 4), 24 h, $500/month, 3 spots, 2-week trial, page and emails agree",
      outreach.CLIPS_PER_MONTH == 12 == num("clipsPerEpisode") * num("episodesPerMonth")
      and outreach.CLIPS_PER_EPISODE == num("clipsPerEpisode") and outreach.EPISODES_PER_MONTH == num("episodesPerMonth")
      and outreach.TURNAROUND_HOURS == 24 and 'turnaround: "within 24 hours"' in js
      and outreach.FOUNDING_PRICE == "$500/month" and 'price: "$500"' in js
      and outreach.FOUNDING_SPOTS == num("spots") == 3 and outreach.TRIAL == "a 2-week trial" and 'trial: "2-week trial"' in js)
run_all = (ROOT / "tests" / "run_all.py").read_text()
check("run_all: suites before 45 get CAPTION_STYLE=bold and VISUAL_OVERLAYS=full",
      'int(number.group(1)) < 45' in run_all and '"CAPTION_STYLE": "bold", "VISUAL_OVERLAYS": "full"' in run_all)
check("run_all: suites before 46 run without mid-clip trimming",
      'int(number.group(1)) < 46' in run_all and 'env["MIDCLIP_TRIM"] = "0"' in run_all)

# ============================== 2. Mid-clip trimming: the plan ==============================
check("Trimming on by default in the B2B modes", midclip.enabled())
os.environ["VISUAL_OVERLAYS"] = "full"
check("...off with VISUAL_OVERLAYS=full", not midclip.enabled())
os.environ.pop("VISUAL_OVERLAYS")
os.environ["MIDCLIP_TRIM"] = "0"
check("...and with MIDCLIP_TRIM=0", not midclip.enabled())
os.environ.pop("MIDCLIP_TRIM")

words = [W("We", 0.0, 0.2), W("priced", 0.25, 0.6), W("it", 0.65, 0.8), W("by", 1.7, 1.9),  # 0.90 s mid-sentence
         W("outcome.", 1.95, 2.4), W("Then", 3.4, 3.6),  # 1.00 s after a sentence end
         W("revenue", 3.65, 4.1), W("doubled", 4.6, 5.0), W("fast", 5.05, 5.4)]  # 0.50 s: kept
p = midclip.plan(words, 6.0)
gap = lambda a, b: round(p.to_trimmed(b) - p.to_trimmed(a), 3)  # noqa: E731
check("A 0.90 s pause mid-sentence keeps 0.20 s", gap(0.8, 1.7) == 0.20, gap(0.8, 1.7))
check("A 1.00 s pause after a sentence keeps 0.30 s", gap(2.4, 3.4) == 0.30, gap(2.4, 3.4))
check("A 0.50 s pause is left alone", gap(4.1, 4.6) == 0.50)
check("0.12 s kept after the last word's tail, 0.08 s before the next attack",
      p.cuts[0].start == 0.92 and p.cuts[0].end == 1.62, [(c.start, c.end) for c in p.cuts])
check("Cuts confined to real silence (FFmpeg silencedetect)", (lambda q: [(c.start, c.end) for c in q.cuts])(
    midclip.plan(words, 6.0, silences=[(1.0, 1.5)])) == [(1.0, 1.5)])
check("No silence there -> no cut (a breath, room noise, a laugh)", midclip.plan(words, 6.0, silences=[]).cuts == [])
um = [W("The", 0, 0.2), W("margin", 0.25, 0.6), W("is,", 0.65, 0.8), W("um,", 0.95, 1.2), W("thirty", 1.35, 1.7),
      W("percent", 1.75, 2.2), W("now.", 2.25, 2.6)]
pu = midclip.plan(um, 3.0)
kept = [w.text for w in pu.remap_words(um)]
check("'um' between words is cut, and so is the dead air around it", "um," not in kept and len(pu.cuts) == 1
      and round(pu.to_trimmed(1.35) - pu.to_trimmed(0.8), 3) == 0.20, (kept, [(c.start, c.end) for c in pu.cuts]))
glued = [W("The", 0, 0.2), W("margin", 0.25, 0.6), W("um", 0.6, 0.8), W("is", 0.8, 1.0), W("up.", 1.05, 1.4)]
check("A filler glued to the next word is left alone", midclip.plan(glued, 2.0).cuts == [])
soft = [W("And", 0, 0.2), W("pricing", 0.25, 0.7), W("is", 0.75, 0.9), W("like", 1.2, 1.4), W("the", 1.7, 1.8),
        W("whole", 1.85, 2.1), W("game.", 2.15, 2.5)]
check("Isolated 'like' (0.30 s of silence both sides) is cut", [w.text for w in midclip.plan(soft, 3.0).remap_words(soft)]
      == ["And", "pricing", "is", "the", "whole", "game."])
verb = [W("I", 0, 0.1), W("really", 0.15, 0.4), W("like", 0.45, 0.65), W("this", 0.7, 0.9), W("deal.", 0.95, 1.3)]
check("'like' as a verb stays", midclip.plan(verb, 2.0).cuts == [])
yk = [W("It's", 0, 0.2), W("about", 0.25, 0.5), W("trust,", 0.55, 0.9), W("you", 1.2, 1.3), W("know,", 1.32, 1.5),
      W("with", 1.8, 2.0), W("buyers.", 2.05, 2.5)]
check("Isolated 'you know' is cut", [w.text for w in midclip.plan(yk, 3.0).remap_words(yk)]
      == ["It's", "about", "trust,", "with", "buyers."])
edge = [W("Um,", 0.0, 0.3), W("so", 0.6, 0.8), W("pricing.", 0.85, 1.3)]
check("A filler at the clip's start is the opening's job, not a mid-clip cut", midclip.plan(edge, 2.0).cuts == [])
close = [W("A", 0, 0.2), W("b", 1.0, 1.2), W("c", 1.3, 1.45), W("d", 2.3, 2.5), W("e", 2.55, 2.8)]
pc = midclip.plan(close, 3.0)
check("Never a segment shorter than 0.4 s", all(b - a >= midclip.MIN_SEGMENT for a, b in pc.keep), pc.keep)
check("Segments alternate 1.00x / 1.06x", p.zooms == [1.0, 1.06, 1.0][:len(p.keep)] and len(p.zooms) == len(p.keep))
remapped = p.remap_words(words)
check("Words land on the trimmed timeline in order, none overlapping", all(
    a.end <= b.start + 1e-6 for a, b in zip(remapped, remapped[1:])) and remapped[-1].end <= p.trimmed_duration + 1e-6)
check("A time inside a cut maps to the splice", p.to_trimmed(1.2) == p.to_trimmed(p.cuts[0].start))

# ============================== 3. Mid-clip trimming: the render ==============================
graph = midclip.filter_graph(midclip.TrimPlan(6.0, p.cuts, [1.0, 1.06, 1.0], [None, (0.5, 0.35), None]), 1920, 1080)
check("Equal-power 35 ms crossfade on every splice", graph.count("acrossfade=d=0.035:c1=qsin:c2=qsin") == len(p.cuts))
check("Audio overlaps by half the fade each side (stays in sync)", "atrim=start=0.0000:end=0.9375" in graph
      and "atrim=start=1.6025" in graph, graph[:400])
check("1.06x segment: eye-centred crop scaled back to the frame", "crop=1810:1018:" in graph and "scale=1920:1080" in graph)
if shutil.which("ffmpeg"):
    src = TMP / "talk.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=8",
                    "-f", "lavfi", "-i", "aevalsrc=0.4*sin(2*PI*330*t)*(lt(t\\,3)+gte(t\\,4.6)):s=48000:d=8",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src)],
                   check=True, capture_output=True, timeout=40)
    talk = [W("Pricing", 0.2, 0.9), W("by", 1.0, 1.4), W("outcome", 1.5, 2.95), W("beats", 4.65, 5.2),
            W("hours", 5.3, 6.0), W("every", 6.1, 6.6), W("time.", 6.7, 7.5)]
    silent = midclip.detect_silences(src, 0.0, 8.0)
    tp = midclip.plan(talk, 8.0, silent)
    check("The real silence is found and cut", len(tp.cuts) == 1 and 3.0 <= tp.cuts[0].start <= 3.1
          and 4.5 <= tp.cuts[0].end <= 4.6, (silent, [(c.start, c.end) for c in tp.cuts]))
    out = TMP / "trimmed.mp4"
    midclip.render_trimmed(src, 0.0, tp, out)
    probe = lambda s: float(subprocess.run(["ffprobe", "-v", "error", "-select_streams", f"{s}:0", "-show_entries",  # noqa: E731
                                            "stream=duration", "-of", "csv=p=0", str(out)],
                                           capture_output=True, text=True).stdout.strip() or 0)
    check("Trimmed copy: video and audio both the trimmed length (sync kept)",
          abs(probe("v") - tp.trimmed_duration) < 0.05 and abs(probe("a") - tp.trimmed_duration) < 0.05,
          (probe("v"), probe("a"), tp.trimmed_duration))
    check("No face in the frame (slides) -> no zoom on that segment", tp.zooms == [1.0, 1.0])

pipeline_src = (ROOT / "core" / "pipeline.py").read_text()
check("Pipeline: the trimmed copy feeds framing, captions, camera and teaser (start 0)",
      "source_video, words, start, end = trimmed_path, clip_words, 0.0, candidate.trimmed_duration" in pipeline_src
      and pipeline_src.index("midclip.render_trimmed") < pipeline_src.index("plan = analyze_crop_plan"))
check("Pipeline: the lower-third's face check still uses the whole original video",
      "identity_source=identity_source" in pipeline_src and "identity_words, split" in pipeline_src)
from core import executive  # noqa: E402
seen: list[str] = []


class Info:
    title, description, channel = "Omeed Tabiei on startup law", "", "Optimist Legal"


class Plan:
    frame_width = 1920


class Shot:
    kind, start, end = "solo", 0.0, 30.0


def spy(who, *_a):
    seen.append(str(who))
    return {"Omeed Tabiei": ((0.1,), "the only person in the video")}, []


lt, _ = executive.plan_lower_third("trimmed.mp4", Plan(), 0.0, 30.0, Info(), [], [], identify=spy,
                                   options=lambda *a: [Shot()], identity_source="original.mp4")
check("...the identity is checked on the original, the shot on the trimmed copy", seen == ["original.mp4"] and lt is not None)
from core.hook_selector import ClipResult  # noqa: E402
from core.pipeline import RenderedClip  # noqa: E402

clip = ClipResult(start_time=100.0, end_time=106.0, viral_title="t", caption="c", hashtags=[], virality_score=90)


@dataclass
class Tease:
    offset: float = 3.0


rc = RenderedClip(path=TMP / "x.mp4", clip=clip, layout_mode="smart_crop", crop=None, crop_plan=None, trim=p, teaser=Tease())
check("RenderedClip.output_time follows the cuts, then the teaser", rc.output_time(103.4) == round(p.to_trimmed(3.4) + 3.0, 6),
      (rc.output_time(103.4), p.to_trimmed(3.4)))

# ============================== 4. SRT captions ==============================
srt = srt_export.to_srt(srt_export.cues(words))
check("SRT: numbered cues, HH:MM:SS,mmm, sentence case", srt.startswith("1\n00:00:00,000 --> ") and "We priced it" in srt
      and not re.search(r"\b[A-Z]{4,}\b", srt), srt[:160])


@dataclass
class Teaser:
    start: float = 10.0
    cut: float = 11.0
    offset: float = 1.0


path = srt_export.write_for_clip(TMP / "clip_01.mp4", [W("Pricing", 0.1, 0.6), W("matters.", 0.7, 1.2)], Teaser(),
                                 [W("Climax", 10.1, 10.5), W("line.", 10.55, 10.95), W("Pricing", 0.1, 0.6)])
body = path.read_text()
check("SRT next to the clip, teaser line first, the clip shifted by the teaser", path.name == "clip_01.srt"
      and body.index("Climax line") < body.index("Pricing matters") and "00:00:01,100 --> " in body, body)

# ============================== 5. Calendar angle in the pitch, post and page ==============================
email = outreach.build_outreach_email("Acme", opening_at=95.0, key_point="pricing by outcome instead of hours",
                                      drive_link="https://clips.navstudiolab.com/acme-k2m4qx", greeting_name="Sarah",
                                      content_format="podcast", theme="AI pricing")
check("Pitch names their theme of the month", "They'd fit right in with your recent episodes on AI pricing.\n\n"
      "Want me to do the same for your next episode?" in email, email)
plain = outreach.build_outreach_email("Acme", opening_at=95.0, key_point="pricing by outcome instead of hours",
                                      drive_link="https://x.test/acme-k2m4qx", content_format="podcast")
check("No theme -> the Round 44 email, word for word", "fit right in" not in plain
      and "Free, and yours to keep either way.\n\nWant me to do the same" in plain)
check("A messy theme is dropped, not pasted", outreach.theme_line("a b c d e f g h") == "")
post = linkedin_post.build([W("Pricing", 0, 0.5), W("by", 0.55, 0.7), W("outcome", 0.75, 1.2), W("wins", 1.25, 1.6),
                            W("deals.", 1.65, 2.0)], title="t", theme="AI pricing", ask=lambda p, s: None)
check("LinkedIn post's closing question ties into the theme", post.question == "How is your team approaching AI pricing right now?")
import autonomous_worker as AW  # noqa: E402
check("Worker reads the lead's theme", AW._lead_theme({"calendar_angle": {"theme": "AI pricing"}}) == "AI pricing"
      and AW._lead_theme({}) == "")
worker_src = (ROOT / "autonomous_worker.py").read_text()
check("Worker greets the harvested decision-maker by name", 'set_greeting(email_body, clean_first_name(str(lead["contact_name"])' in worker_src)


class FakeClient:
    def __init__(self):
        self.objects: dict[str, object] = {}

    def upload_file(self, local, bucket, key, ExtraArgs=None):
        self.objects[key] = Path(local).name

    def put_object(self, Bucket, Key, Body, **_k):
        self.objects[Key] = json.loads(Body)


fake = FakeClient()
clip_pages._require = lambda: (type("S", (), {"bucket": "b", "public_base": "https://clips.test"})(), fake)
clip_pages._upload_poster = lambda *a, **k: 0.0
clip_pages.page_url = lambda name, settings=None: f"https://clips.test/{name}"
mp4 = TMP / "clip_01.mp4"
mp4.write_bytes(b"")
name, _url = clip_pages.publish_page("Acme", "Ep", [{"path": str(mp4), "start": 1, "end": 30}], theme="AI pricing")
info = fake.objects[f"{name}/page.json"]
check("Clip page: the theme and the SRT go up with the clip", info.get("theme") == "AI pricing"
      and info["clips"][0].get("srt") is True and f"{name}/captions_01.srt" in fake.objects)
if shutil.which("node"):
    (TMP / "w.mjs").write_text(js)
    (TMP / "r.mjs").write_text("""import worker from "./w.mjs";
const info = { channel_title: "Acme", video_title: "V", kind: "episode", theme: "AI pricing",
  clips: [{ start: 1, end: 30, srt: true }] };
const env = { CLIPS: { get: async (k) => k.endsWith("page.json") ? { json: async () => info } : null } };
console.log(await (await worker.fetch(new Request("https://x.test/acme-k2m4qx"), env)).text());""")
    page = subprocess.run(["node", str(TMP / "r.mjs")], capture_output=True, text=True, timeout=30).stdout
    check("Clip page shows 'Fits your theme' and the captions download",
          "Fits your theme</b> · AI pricing" in page and 'href="/acme-k2m4qx/captions_01.srt?dl=1"' in page)
    check("Worker serves captions_NN.srt", "(clip|poster|captions)_(0[1-9])\\.(mp4|jpg|srt)" in js)

# ============================== 6. The B2B harvester ==============================
recent_day = "Wed, 04 Nov 2026 10:00:00 GMT"
FEED = f"""<?xml version="1.0"?><rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
<title>Acme Revenue Show</title><link>https://www.acme.com</link><itunes:author>Acme</itunes:author>
<description>Founders and CFOs on B2B SaaS pricing and go-to-market.</description>
<itunes:owner><itunes:name>Dan Ortiz</itunes:name><itunes:email>podcast@acme.com</itunes:email></itunes:owner>
<item><title>AI pricing: what buyers pay for</title><pubDate>{recent_day}</pubDate>
<description>Watch: https://www.youtube.com/@acmehq and https://www.linkedin.com/company/acme-inc</description></item>
<item><title>AI pricing for usage-based SaaS</title><pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Hiring your first CFO</title><pubDate>Mon, 19 Oct 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""
HOME = """<html><a href="/about/team">Team</a><a href="mailto:hello@acme.com">Email</a>
<a href="https://www.linkedin.com/company/acme-inc/">LinkedIn</a></html>"""
TEAM = """<html><h3>Sarah Lee</h3><p>Head of Marketing</p><a href="mailto:sarah.lee@acme.com">Email Sarah</a>
<a href="https://www.linkedin.com/in/sarah-lee-acme">in</a><h3>Tom Hart</h3><p>Engineer</p></html>"""
SEARCH = {"results": [
    {"collectionName": "Acme Revenue Show", "artistName": "Acme", "feedUrl": "https://feeds.test/acme.xml",
     "genres": ["Business", "Podcasts"], "collectionViewUrl": "https://podcasts.apple.com/x"},
    {"collectionName": "Comedy Hour", "artistName": "X", "feedUrl": "https://feeds.test/c.xml", "genres": ["Comedy"]}]}
calls: list[str] = []


def fake_get(url, headers=None):
    calls.append(url)
    if "itunes.apple.com" in url:
        return json.dumps(SEARCH)
    if "cloudflare-dns.com" in url:
        domain = re.search(r"name=([^&]+)", url).group(1)
        return json.dumps({"Status": 0, "Answer": [{"type": 15}]} if domain == "acme.com" else {"Status": 3})
    return {"https://feeds.test/acme.xml": FEED, "https://www.acme.com": HOME,
            "https://www.acme.com/about/team": TEAM}.get(url)


LH._http_get = fake_get
LH._MX.clear()
yt = lambda urls: {"channel_id": "UCacme0000000000000000aa", "channel_title": "Acme", "subscriber_count": 4200,  # noqa: E731
                   "latest_video_url": "https://www.youtube.com/watch?v=abcdefghijk", "latest_video_title": "AI pricing",
                   "duration_seconds": 2400, "shorts_30d": 0, "long_30d": 2} if any("acmehq" in u for u in urls) else {}
shows = LH.search_podcasts("B2B SaaS podcast")
check("Apple Podcasts search keeps B2B genres only", [s["show"] for s in shows] == ["Acme Revenue Show"])
pr = LH.build_prospect(shows[0], youtube=yt)
contact = pr["contact"]
check("Decision-maker: the marketing lead from the team page, her own published email, MX-verified",
      (contact["name"], contact["role"], contact["email"], contact["status"])
      == ("Sarah Lee", "Head of Marketing", "sarah.lee@acme.com", "verified"), contact)
check("...with her LinkedIn", contact["linkedin"] == "https://www.linkedin.com/in/sarah-lee-acme")
check("The show owner and the shared inbox are kept as backups, ranked lower",
      [c["email"] for c in pr["contacts"]][:3] == ["sarah.lee@acme.com", "podcast@acme.com", "hello@acme.com"],
      [c["email"] for c in pr["contacts"]])
check("Company LinkedIn found", pr["linkedin_company"] == "https://www.linkedin.com/company/acme-inc")
check("Source media: the latest long YouTube episode", pr["latest_video_url"].endswith("abcdefghijk"))
check("Content-calendar angle from recent titles", pr["calendar_angle"]["theme"] == "AI pricing", pr["calendar_angle"])
check("LinkedIn Video Gap score: hot, with reasons", pr["score"] >= 70 and pr["tier"] == "hot"
      and any("no vertical video" in r for r in pr["reasons"]), (pr["score"], pr["reasons"]))
check("Guessed emails are never queueable", not LH.queueable({"status": "guess"}) and LH.queueable({"status": "verified"}))
check("Pattern guess for a named lead", LH.pattern_guess("Sarah Lee", "acme.com") == "sarah.lee@acme.com")
check("Hosting filter matches whole domains only", bool(LH._HOSTING.search("me.com")) and bool(LH._HOSTING.search("x.gmail.com"))
      and not LH._HOSTING.search("acme.com") and not LH._HOSTING.search("spotifyfans.io"))
check("A domain without MX is flagged", LH.email_status("ceo@nomail.test", True) == "bad_domain")
lead = LH.to_lead(pr)
check("Worker lead: verified email, name, theme, LinkedIn, ranks above YouTube leads",
      lead["contact_email"] == "sarah.lee@acme.com" and lead["contact_name"] == "Sarah Lee"
      and lead["calendar_angle"]["theme"] == "AI pricing" and lead["linkedin_url"].endswith("sarah-lee-acme")
      and lead["priority"] >= 8 and lead["latest_video_url"].endswith("=abcdefghijk"))
store = TMP / "prospects.json"
found = LH.harvest(["B2B SaaS podcast"], limit=5, youtube=yt, path=store)
check("Harvest: found, enriched, saved best first", len(found) == 1 and LH.load_prospects(store)[0]["show"] == "Acme Revenue Show")
check("A second harvest skips shows already found", LH.harvest(["B2B SaaS podcast"], limit=5, youtube=yt, path=store) == [])
before = LH.load_prospects(store)[0]["score"]
after = LH.set_check(pr["id"], "yes", store)["score"]
check("Manual check 'posts native video: yes' drops the score", after == before - 20, (before, after))
queued: list[dict] = []
added = LH.queue([pr["id"]], store, enqueue=lambda leads: queued.extend(leads) or len(leads))
check("Queue: the lead goes to the worker, the prospect is marked queued",
      added == 1 and queued[0]["source"] == "b2b_harvester" and LH.load_prospects(store)[0]["status"] == "queued")
noyt = dict(pr, id="rss-x", latest_video_url="", channel_id="", status="new")
LH.save_prospects([noyt], store)
check("No YouTube episode -> not queued (nothing to cut)", LH.queue(["rss-x"], store, enqueue=lambda l: len(l)) == 0)
check("CLI: harvest, list, queue, check", all(f'add_parser("{c}"' in (ROOT / "core" / "linkedin_harvester.py").read_text()
                                                for c in ("harvest", "list", "queue", "check"))
      and "--harvest-b2b" in (ROOT / "main.py").read_text())
check("App: B2B prospects panel in the pipeline tab", "_render_b2b_prospects()  # Round 46" in (ROOT / "app.py").read_text())
check("The harvester never reads LinkedIn itself", not [u for u in calls if "linkedin.com" in u])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(FAILED)} check(s) failed" if FAILED else "\nAll checks passed")
sys.exit(1 if FAILED else 0)
