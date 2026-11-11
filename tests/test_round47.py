"""Round 47: the "Found 0 new prospects" fix (certifi, big feeds, scheme-less websites), the
multi-source B2B discovery engine (core.b2b_sources) with a persistent cursor, seen-list,
backlog and funnel report, email-confidence badges, key-free YouTube matching, budget signals,
and the B2B-only app (legacy creator controls behind VISUAL_OVERLAYS=full / LEGACY_UI=1).

Run: timeout 120 .venv/bin/python tests/test_round47.py
Everything is offline: the harvester's HTTP is replaced by a fake internet below.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for key in ("VISUAL_OVERLAYS", "CAPTION_STYLE", "MIDCLIP_TRIM", "LEGACY_UI", "PODCASTINDEX_API_KEY",
            "PODCASTINDEX_API_SECRET"):
    os.environ.pop(key, None)
os.environ["B2B_YOUTUBE_SEARCHES"] = "0"  # never spend real quota from a test
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


from core import b2b_sources as S  # noqa: E402
from core import linkedin_harvester as LH  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="round47_"))
NOW = datetime.now(timezone.utc)
day = lambda n: (NOW - timedelta(days=n)).strftime("%a, %d %b %Y %H:%M:%S GMT")  # noqa: E731
iso = lambda n: (NOW - timedelta(days=n)).isoformat()  # noqa: E731


# ============================== the fake internet ==============================
def feed_xml(title: str, site: str, owner: str = "", email: str = "", ages=(3, 10, 17), extra: str = "",
             description: str = "Founders and CFOs on B2B SaaS revenue and go-to-market.") -> str:
    items = "".join(f"<item><title>Episode {n}: pricing for B2B SaaS</title><pubDate>{day(a)}</pubDate>"
                    f"<description>{extra if n == 0 else ''}</description></item>" for n, a in enumerate(ages))
    owner_xml = f"<itunes:owner><itunes:name>{owner}</itunes:name><itunes:email>{email}</itunes:email></itunes:owner>" if email else ""
    return (f'<?xml version="1.0"?><rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>'
            f"<title>{title}</title><link>{site}</link><itunes:author>{owner}</itunes:author>"
            f"<description>{description}</description>{owner_xml}{items}</channel></rss>")


MX_DOMAINS = {"acme.com", "beta.io", "gamma.co", "delta.com", "omega.com", "yc-one.com"}
PAGES: dict[str, str] = {}
CALLS: list[str] = []


def apple_result(name: str, feed: str, apple_id: str = "1") -> dict:
    return {"collectionName": name, "artistName": name, "feedUrl": feed, "genres": ["Business", "Podcasts"],
            "collectionViewUrl": f"https://podcasts.apple.com/us/podcast/x/id{apple_id}", "kind": "podcast",
            "wrapperType": "track", "trackCount": 40}


def fake_get(url: str, headers=None):
    CALLS.append(url)
    if "cloudflare-dns.com" in url:
        domain = re.search(r"name=([^&]+)", url).group(1)
        return json.dumps({"Status": 0, "Answer": [{"type": 15}]} if domain in MX_DOMAINS else {"Status": 3})
    if "itunes.apple.com/search" in url:
        term = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["term"][0]
        slug = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")
        return json.dumps({"results": [apple_result(f"Show {term}", f"https://feeds.test/{slug}.xml")]})
    if "itunes.apple.com/lookup" in url:
        ids = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["id"][0].split(",")
        return json.dumps({"results": [apple_result(f"Listed show {i}", f"https://feeds.test/listed-{i}.xml", i) for i in ids]})
    if "itunes.apple.com/us/rss/toppodcasts" in url:
        return json.dumps({"feed": {"entry": [{"id": {"attributes": {"im:id": str(900 + n)}}} for n in range(5)]}})
    if url.startswith("https://feeds.test/"):
        slug = url.rsplit("/", 1)[-1].removesuffix(".xml")
        return PAGES.get(url) or feed_xml(f"Show {slug}", "https://www.acme.com" if "acme" in slug else "",
                                          "Dan Ortiz", "dan@acme.com" if "acme" in slug else "")
    return PAGES.get(url)


LH._http_get = fake_get
LH._MX.clear()
no_yt = lambda urls: {}  # noqa: E731

# ============================== 1. Why it found 0 ==============================
src = (ROOT / "core" / "linkedin_harvester.py").read_text()
check("fetch() uses certifi's certificates (python.org Python on macOS has none)",
      "certifi.where()" in src and "context=_ssl_context()" in src and LH._ssl_context() is LH._ssl_context())
check("...and says so loudly when a certificate check fails", "Certificates.command" in src
      and 'FETCH_STATS["ssl"]' in src and "logger.warning(\"HTTPS certificate check failed" in src)
long_feed = feed_xml("Big Show", "https://www.acme.com", ages=list(range(1, 60)))
cut = long_feed[: long_feed.index("Episode 30")]  # a feed read only partly: cut mid-episode
parsed = LH.parse_feed(cut)
check("A feed cut mid-episode (over the read limit) still parses, up to its last whole episode",
      parsed.title == "Big Show" and len(parsed.episodes) == 30, (parsed.title, len(parsed.episodes)))
messy = feed_xml("Fish &amp; Chips &nbsp;Show", "https://www.acme.com").replace("Founders and", "Founders & CFOs and")
check("HTML entities (&nbsp;) and a bare '&' no longer drop the feed", LH.parse_feed(messy).title.startswith("Fish & Chips"),
      LH.parse_feed(messy).title)
check("Websites without https:// are kept", LH.normalize_site("trailmerge.com") == "https://trailmerge.com"
      and LH.normalize_site("//acme.com/x") == "https://acme.com/x" and LH.normalize_site("not a site") == ""
      and LH.parse_feed(feed_xml("T", "trailmerge.com")).website == "https://trailmerge.com")
check("Feeds are read up to 3 MB", LH.FEED_MAX_BYTES >= 3_000_000 and "max_bytes=FEED_MAX_BYTES" in src)

# ============================== 2. The keyword matrix, niches, sources ==============================
matrix = S.keyword_matrix()
check("60+ search phrases across the B2B niches", len(matrix) >= 60 and len(S.NICHES) >= 20, len(matrix))
check("...interleaved: the first phrases cover many niches", len({p.split()[0] for p in matrix[:24]}) >= 15, matrix[:6])
check("A niche narrows the matrix", all(any(t in p for t in S.NICHES["fintech"]) for p in S.keyword_matrix("fintech")))
try:
    S.keyword_matrix("crypto-memes")
    check("An unknown niche is refused", False)
except ValueError:
    check("An unknown niche is refused", True)
check("Sources: all, a list, or refused", S.parse_sources("all") == list(S.SOURCES)
      and S.parse_sources("apple_search, yc") == ["apple_search", "yc"] and len(S.SOURCES) >= 7)
try:
    S.parse_sources("linkedin")
    check("LinkedIn is not a source (no public API, scraping breaks its terms)", False)
except ValueError:
    check("LinkedIn is not a source (no public API, scraping breaks its terms)", "linkedin" not in S.SOURCES)

# ============================== 3. Persistent cursor, seen-list, funnel ==============================
store = TMP / "prospects.json"
report: dict = {}
first = LH.harvest(limit=3, ask=None, youtube=no_yt, path=store, sources="apple_search", report=report)
state = S.load_state(S.state_path_for(store))
terms_1 = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query)["term"][0] for u in CALLS if "itunes.apple.com/search" in u]
check("A harvest saves real prospects (> 0), no more than the limit", 0 < len(first) <= 3, len(first))
check("The cursor moved past the phrases it used", state["cursors"].get("apple_search:all") == len(terms_1) > 0,
      (state["cursors"], len(terms_1)))
CALLS.clear()
second = LH.harvest(limit=3, ask=None, youtube=no_yt, path=store, sources="apple_search")
terms_2 = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query)["term"][0] for u in CALLS if "itunes.apple.com/search" in u]
check("The next run starts from the backlog, then fresh phrases (never the same top results again)",
      len(second) > 0 and not set(terms_2) & set(terms_1) and LH.LAST_REPORT["backlog_in"] > 0,
      (terms_1[:3], terms_2[:3], LH.LAST_REPORT.get("backlog_in")))
saved_feeds = {p["feed_url"] for p in first}
check("Shows already found are never fetched again", not [u for u in CALLS if u in saved_feeds])
check("Every show looked at is remembered with why", all(v.get("why") for v in S.load_state(S.state_path_for(store))["seen"].values()))
text = LH.format_report(report)
check("Funnel: raw_fetched -> deduplicated -> enriched -> active_b2b -> saved, printed",
      "[raw_fetched" in text and "-> deduplicated" in text and "-> enriched" in text and "-> active_b2b" in text
      and "-> saved" in text and report["saved"] == len(first) and report["raw_fetched"] >= report["deduplicated"], text)
check("...with the email badges, drops and sources", "with an email" in text and "by source: apple_search" in text, text)
check("The funnel of each run is kept", len(S.load_state(S.state_path_for(store))["runs"]) == 2)
check("Round 46's way still works: exact queries", len(LH.harvest(["exact phrase"], limit=2, youtube=no_yt, path=TMP / "q.json")) == 1)

# ============================== 4. Emails: badges, never dropped for one ==============================
PAGES["https://feeds.test/beta.xml"] = feed_xml("Beta Revenue Podcast", "https://www.beta.io", "Ana Silva", "podcast@beta.io")
PAGES["https://www.beta.io"] = "<html><a href='/contact'>Contact</a></html>"
PAGES["https://www.beta.io/contact"] = "<html><form action='/send'><input name=email></form></html>"
beta, why = LH.enrich({"feed_url": "https://feeds.test/beta.xml", "source": "apple_search"}, youtube=no_yt)
emails = {c["email"]: c["confidence"] for c in beta["contacts"]}
check("A shared show inbox is kept and badged shared_inbox (queueable)",
      why == "saved" and emails.get("podcast@beta.io") == "shared_inbox" and LH.queueable(beta["contacts"][0]), emails)
check("The host's first.last@ at their MX domain is kept, badged domain_mx_pattern, with alternates",
      emails.get("ana.silva@beta.io") == "domain_mx_pattern"
      and next(c for c in beta["contacts"] if c["email"] == "ana.silva@beta.io")["alternates"] == ["ana@beta.io", "asilva@beta.io"])
check("Their contact form is found", beta["contact_form"] == "https://www.beta.io/contact")
PAGES["https://feeds.test/gamma.xml"] = feed_xml("Gamma CFO Show", "https://gamma.co", "Gamma Media", "lee@gamma.co")
gamma, _ = LH.enrich({"feed_url": "https://feeds.test/gamma.xml"}, youtube=no_yt)
check("A personal email in the feed at an MX domain: verified_rss", gamma["email_confidence"] == "verified_rss", gamma["contact"])
PAGES["https://feeds.test/none.xml"] = feed_xml("Revenue Leaders Show", "")
nomail, why = LH.enrich({"feed_url": "https://feeds.test/none.xml"}, youtube=no_yt)
check("No email at all: still saved (badge 'none'), never dropped for it", why == "saved" and nomail["email_confidence"] == "none")
guess_first = dict(beta, contact=next(c for c in beta["contacts"] if c["confidence"] == "domain_mx_pattern"),
                   contacts=sorted(beta["contacts"], key=lambda c: c["confidence"] != "domain_mx_pattern"))
lead = LH.to_lead(guess_first)
check("Queueing: the guess isn't emailed; the published shared inbox is, greeting the named host",
      lead["contact_email"] == "podcast@beta.io" and lead["contact_name"] == "Ana Silva"
      and lead["contact_confidence"] == "shared_inbox", lead)
check("...unless you allow guesses", LH.to_lead(guess_first, allow_guess=True)["contact_email"] == "ana.silva@beta.io")
check("Badges for every case", set(LH.EMAIL_CONFIDENCE) >= {"verified_rss", "verified_site", "domain_mx_pattern", "shared_inbox"})
check("Team-page headings aren't people", LH.person_name("Marketing Futures") == "" and LH.person_name("Meet The Team") == ""
      and LH.person_name("Sarah Lee") == "Sarah Lee" and LH.person_name("Acme Media") == "")

# ============================== 5. The sources ==============================
PAGES["https://lists.test/best"] = ('<a href="https://podcasts.apple.com/us/podcast/a/id1234567">A</a>'
                                    '<a href="https://podcasts.apple.com/us/podcast/b/id7654321?i=1">B</a>'
                                    '<a href="https://feeds.transistor.fm/delta">feed</a>'
                                    '<a href="https://www.youtube.com/@omegahq">YT</a><a href="/style.css">x</a>')
links = S.links_on_page(PAGES["https://lists.test/best"])
check("Seed pages: Apple ids, RSS feeds and YouTube channels on a page",
      links["apple_ids"] == ["1234567", "7654321"] and links["feeds"] == ["https://feeds.transistor.fm/delta"]
      and links["youtube"] == ["https://www.youtube.com/@omegahq"], links)
seeds_file = TMP / "seeds.txt"
seeds_file.write_text("# comment\nhttps://lists.test/best\n")
st_ = S.load_state(TMP / "s.json")
seeded = S.source_seed_pages(st_, None, seeds_file)
check("...looked up into feeds, each page read once a month",
      {s.get("feed_url") for s in seeded} >= {"https://feeds.test/listed-1234567.xml", "https://feeds.transistor.fm/delta"}
      and any(s.get("youtube_url") for s in seeded) and S.source_seed_pages(st_, None, seeds_file) == [])
charts = S.source_apple_charts(S.load_state(TMP / "c.json"), None)
check("Apple charts: looked up, with their chart rank", len(charts) == 5 and charts[0]["chart_rank"] == 1
      and charts[0]["source"] == "apple_charts", charts[:1])
PAGES[S.YC_URL] = json.dumps([{"id": n, "name": f"Co{n}", "status": "Active", "team_size": 50, "website": f"https://co{n}.test",
                               "subindustry": "B2B -> Finance and Accounting", "tags": ["Fintech"], "one_liner": "payments"}
                              for n in range(45)] + [{"id": 99, "name": "Dead", "status": "Inactive", "team_size": 50, "website": "x"}])
yc_state = S.load_state(TMP / "yc.json")
batch_1, batch_2 = S.source_yc(yc_state, None), S.source_yc(yc_state, None)
check("YC: active B2B companies in batches, the cursor moving on",
      len(batch_1) == S.YC_BATCH and batch_1[0]["show"] == "Co0" and batch_2[0]["show"] == f"Co{S.YC_BATCH}"
      and all(s["website"] for s in batch_1))
check("...a niche narrows it", len(S.source_yc(S.load_state(TMP / "yc2.json"), "fintech")) == S.YC_BATCH)
check("Podcast Index only with a key; YouTube search obeys B2B_YOUTUBE_SEARCHES",
      S.source_podcastindex(S.load_state(TMP / "pi.json"), None) == [] and S.steps_for("youtube") == 0)

# ============================== 6. Company-first and YouTube-first seeds ==============================
PAGES["https://www.delta.com"] = ('<html><link rel="alternate" type="application/rss+xml" href="https://feeds.test/delta.xml">'
                                  '<script src="https://fast.wistia.net/assets/external/E-v1.js"></script>'
                                  '<p>Podcast production by Sweet Fish Media</p></html>')
PAGES["https://feeds.test/delta.xml"] = feed_xml("Delta Supply Chain Leaders", "https://www.delta.com", "Kim Park", "kim@delta.com")
delta, why = LH.enrich({"source": "yc", "website": "https://www.delta.com", "show": "Delta"}, youtube=no_yt)
check("A company site with a show: its feed found, enriched", why == "saved" and delta["feed_url"] == "https://feeds.test/delta.xml")
check("...its webinar/video stack and podcast agency are read as signals",
      delta["video_stack"] == ["Wistia"] and delta["agency"] == "Sweet Fish Media"
      and any("+5 records video" in r for r in delta["reasons"]) and any("Sweet Fish" in r for r in delta["reasons"]))
PAGES["https://www.noshow.test"] = "<html><p>We sell software.</p></html>"
check("A company without a show is dropped as no_show", LH.enrich({"source": "yc", "website": "https://www.noshow.test"}, youtube=no_yt)
      == (None, "no_show"))
about = lambda cid, url: {"title": "Omega GTM", "description": "B2B sales leadership interviews. hello@omega.com https://omega.com",  # noqa: E731
                          "links": ["https://omega.com"]}
yt_omega = lambda urls: {"channel_id": "UComega00000000000000000", "channel_title": "Omega GTM",  # noqa: E731
                         "latest_video_url": "https://www.youtube.com/watch?v=omegaomega1", "shorts_30d": 0, "long_30d": 3,
                         "titles": ["Enterprise sales in 2026"]} if urls else {}
PAGES["https://omega.com"] = "<html>Omega</html>"
omega, why = LH.enrich({"source": "youtube", "channel_id": "UComega00000000000000000"}, youtube=yt_omega, about=about)
check("A YouTube channel first: website and email from its description",
      why == "saved" and omega["website"] == "https://omega.com" and omega["contact"]["email"] == "hello@omega.com"
      and omega["id"] == "UComega00000000000000000", (why, omega and omega.get("contact")))

# ============================== 7. YouTube without quota ==============================
ATOM = f"""<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom"><title>Acme Revenue Show</title>
<entry><yt:videoId>short000001</yt:videoId><title>clip</title><link rel="alternate" href="https://www.youtube.com/shorts/short000001"/><published>{iso(2)}</published></entry>
<entry><yt:videoId>longvideo01</yt:videoId><title>AI pricing with a CFO</title><link rel="alternate" href="https://www.youtube.com/watch?v=longvideo01"/><published>{iso(5)}</published></entry>
<entry><yt:videoId>old00000001</yt:videoId><title>Old</title><link rel="alternate" href="https://www.youtube.com/watch?v=old00000001"/><published>{iso(90)}</published></entry></feed>"""
CID = "UCacme0000000000000000aa"
PAGES[f"https://www.youtube.com/feeds/videos.xml?channel_id={CID}"] = ATOM
public = LH.public_youtube([f"https://www.youtube.com/channel/{CID}"])
check("Public channel feed: the Shorts gap and the latest long episode, no API key",
      public["shorts_30d"] == 1 and public["long_30d"] == 1 and public["latest_video_url"].endswith("longvideo01")
      and public["youtube_via"] == "public feed", public)
PAGES["https://www.youtube.com/@acmerevenueshow"] = (f'<meta property="og:title" content="Acme Revenue Show">'
                                                     f'"externalId":"{CID}"')
PAGES["https://www.youtube.com/@wrongshow"] = '<meta property="og:title" content="Cooking with Bob">"externalId":"UCbob00000000000000000000"'
check("@handle guessed from the show name, confirmed by the channel's title",
      LH.guess_youtube("Acme Revenue Show") == [f"https://www.youtube.com/channel/{CID}"] and LH.guess_youtube("Wrong Show") == [])
check("...and a channel whose title doesn't match is never used", not LH.titles_match("Cooking with Bob", "Wrong Show"))

# ============================== 8. Scoring signals, drops ==============================
check("Podcast hosts: paid vs free", LH.host_platform("https://feeds.transistor.fm/x")["paid"]
      and not LH.host_platform("https://anchor.fm/s/abc/podcast/rss")["paid"] and LH.host_platform("https://acme.com/feed") == {})
base = {"show": "B2B SaaS revenue", "contact": {}, "linkedin_company": "x"}
check("Media-company shows and top-100 chart shows score lower (in-house producers)",
      LH.score({**base, "media_network": "npr.org"})[0] == LH.score(base)[0] - 25
      and LH.score({**base, "chart_rank": 7})[0] == LH.score(base)[0] - 10
      and LH.media_network("podcasts@npr.org") == "npr.org" and LH.media_network("dan@acme.com") == "")
check("Paid hosting adds 5", LH.score({**base, "host_platform": {"name": "Transistor", "paid": True}})[0] == LH.score(base)[0] + 5)
PAGES["https://feeds.test/stale.xml"] = feed_xml("Old Revenue Podcast", "", ages=(200, 210))
check("A show with no episode in 120 days is dropped as inactive",
      LH.enrich({"feed_url": "https://feeds.test/stale.xml"}, youtube=no_yt) == (None, "inactive"))
PAGES["https://feeds.test/garden.xml"] = feed_xml("Garden Hour", "", description="Tomatoes and roses.").replace(
    "pricing for B2B SaaS", "planting bulbs")
check("A show with no B2B signal is dropped as not_b2b", LH.enrich({"feed_url": "https://feeds.test/garden.xml"}, youtube=no_yt)
      == (None, "not_b2b"))
check("The harvester never reads LinkedIn itself", not [u for u in CALLS if "linkedin.com" in u])

# ============================== 9. CLI, worker, app ==============================
check("CLI: --niche, --source, --minutes, --allow-guess, sources, funnel",
      all(x in src for x in ('"--niche"', '"--source"', '"--minutes"', '"--allow-guess"', 'add_parser("sources"',
                             'add_parser("funnel"', 'print(format_report())')))
check("The worker carries the contact's name, role, badge and website into the Outbox",
      '"contact_confidence", "website", "prospect_id"' in (ROOT / "autonomous_worker.py").read_text())
app_src = (ROOT / "app.py").read_text()
check("App: legacy creator controls only with VISUAL_OVERLAYS=full or LEGACY_UI=1",
      "def _legacy_ui()" in app_src and "if _legacy_ui():  # Round 47: the creator graphics" in app_src
      and "_render_creator_contact(record)" in app_src)
check("App: prospects first in their tab, open, with niche / sources / funnel / guesses",
      '"🔎 Prospects"' in app_src and 'key="b2b_niche"' in app_src and 'key="b2b_sources"' in app_src
      and "LH.format_report(report)" in app_src and "allow_guess=allow_guess" in app_src
      and app_src.index("_render_b2b_prospects()  # Round 46") < app_src.index("    _render_lead_queue()\n"))
check("App: settings show the real caption style and the B2B modes", "caption_style_name()" in app_src
      and '"VISUAL_OVERLAYS"' in app_src and '"B2B harvester"' in app_src)
check("Seed pages ship with real lists", len(S.seed_pages()) >= 4)
try:
    from streamlit.testing.v1 import AppTest
except Exception:  # noqa: BLE001
    AppTest = None
    print("SKIP  the app run (Streamlit isn't installed here)")
if AppTest is not None:
    os.environ.pop("VISUAL_OVERLAYS", None)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    labels = [t.label for t in at.tabs]
    expanders = [e.label for e in at.expander]
    check("App runs in B2B mode: Prospects tab, prospects panel open, no About-page button",
          not at.exception and "🔎 Prospects" in labels and any(l.startswith("🔎 B2B prospects") for l in expanders)
          and not any("About page" in str(getattr(b, "label", "")) for b in at.get("link_button")),
          (at.exception, labels))
    check("...with the niche and source pickers", any(s.label == "Niche" for s in at.selectbox)
          and any(m.label == "Sources" for m in at.multiselect))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(FAILED)} check(s) failed" if FAILED else "\nAll checks passed")
sys.exit(1 if FAILED else 0)
