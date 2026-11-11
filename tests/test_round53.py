"""Round 53: reply rate. Buyer fit, the marketing contact, follow-ups that fit what they did.

1. Buyer fit (core.fit): a B2B company with a marketing lead ranks first; sports, schools and
   consumer money channels are "not a buyer"; agencies are partners; thin data is "weak", never
   "not a buyer". The worker (FIT_GATE) doesn't queue channels it found itself that can't buy.
2. The marketing contact (core.marketing_contact): found on team pages (JSON-LD or text), guests
   ignored, the company's own email pattern learned from its published addresses; the harvester
   reads it from pages it already fetched; the Outbox shows it with a ready email.
3. Follow-ups (core.follow_up_plan): warm for people who watched, a nudge for people who only
   opened, a new door (their marketing lead) or a resend for people who never opened, then the
   last note; at most three; never a word about the tracking.

Run: timeout 300 .venv/bin/python tests/test_round53.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for key in ("FIT_GATE", "ADAPTIVE_FOLLOW_UPS", "LEGACY_UI", "EXEC_EMAIL"):
    os.environ.pop(key, None)
os.environ["NAV_NO_CLIP_PAGES"] = "1"
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


import autonomous_worker as W  # noqa: E402
from core import fit as F  # noqa: E402
from core import follow_up_plan as FP  # noqa: E402
from core import linkedin_harvester as LH  # noqa: E402
from core import marketing_contact as M  # noqa: E402
from core import outreach as O  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="round53_"))
NOW = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


def use_storage(folder: Path) -> None:
    W.STORAGE_DIR = folder
    folder.mkdir(parents=True, exist_ok=True)
    for attr, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock"),
                       ("LOW_FIT_PATH", "low_fit.json")):
        setattr(W, attr, folder / file)
    W.init_storage_files()


# ============================== 1. Buyer fit ==============================
saas = {"show": "Streak", "description": "Streak turns Gmail into a CRM for sales teams and founders",
        "website": "https://streak.com", "linkedin_company": "https://www.linkedin.com/company/streak",
        "source": "b2b_harvester", "calendar_angle": {"recent_titles": ["Pipeline reviews that work", "B2B sales hiring"]},
        "contact": {"name": "Aleks Bass", "role": "CEO", "email": "aleks@streak.com"},
        "marketing_contact": {"name": "Brita Ulf", "role": "Head of Content", "email": "brita.ulf@streak.com"}}
sports = {"channel_title": "Seattle Sports", "latest_video_title": "Seahawks film breakdown", "subscriber_count": 90000,
          "contact_email": "https://www.youtube.com/channel/UCx/about", "business_offer": ["sponsor"]}
agency = {"show": "Agency Growth Podcast", "website": "https://evergrowmarketing.com",
          "description": "Helping B2B founders scale their agency", "contact": {"email": "jake@evergrowmarketing.com"}}
thin = {"channel_title": "Smart Circle International", "video_title": "Belief, Grit & Breakthrough with a National Consultant",
        "contact_email": "https://www.youtube.com/channel/UC0/about"}
money = {"channel_title": "Wealth Juice", "latest_video_title": "5 dividend stocks for passive income",
         "contact_email": "wealthjuice@gmail.com"}
giant = {**saas, "show": "The Big CEO Show", "subscriber_count": 21_700_000, "marketing_contact": {}}

grades = {name: F.buyer_fit(r) for name, r in
          (("saas", saas), ("sports", sports), ("agency", agency), ("thin", thin), ("money", money), ("giant", giant))}
check("Fit: a B2B company with a named marketing lead is a strong fit", grades["saas"]["grade"] == "strong",
      grades["saas"])
check("Fit: a sports channel is not a buyer, and says why",
      grades["sports"]["grade"] == "skip" and any("off-niche" in r for r in grades["sports"]["reasons"]))
check("Fit: a marketing agency is a partner, not a client", grades["agency"]["grade"] == "partner", grades["agency"])
check("Fit: thin data (old pitch, About page only) is weak, never 'not a buyer'", grades["thin"]["grade"] == "weak",
      grades["thin"])
check("Fit: consumer money tips with a gmail address is not a buyer", grades["money"]["grade"] == "skip",
      grades["money"])
check("Fit: a 21M-subscriber media brand scores far lower than the same company",
      grades["giant"]["points"] <= grades["saas"]["points"] - 30, (grades["giant"]["points"], grades["saas"]["points"]))
ranked = sorted([thin, agency, sports, saas], key=F.rank_key)
check("Fit: best buyer first (strong, partner, weak, not a buyer)",
      [r.get("show") or r.get("channel_title") for r in ranked]
      == ["Streak", "Agency Growth Podcast", "Smart Circle International", "Seattle Sports"])
check("Fit: prospects sort by fit and Video Gap together, partners last",
      F.priority({**saas, "score": 60}) > F.priority({**agency, "score": 95}))
rows = F.outcomes_by_grade([{**saas, "page_name": "a", "replied_at": "x"}, {**sports, "page_name": "b"}],
                           {"a": {"opens": 2, "played": {1}}, "b": {"opens": 1, "played": set()}})
check("Fit: results by grade count sends, opens, plays and replies",
      [(r["grade"], r["sent"], r["opened"], r["played"], r["replied"]) for r in rows]
      == [("strong", 1, 1, 1, 1), ("skip", 1, 1, 0, 0)], rows)

# The worker's gate
use_storage(TMP / "gate")
lead_sports = {**sports, "channel_id": "UCsports", "latest_video_url": "https://youtube.com/watch?v=aaaaaaaaaaa",
               "priority": 5}
lead_b2b = {"channel_id": "UCb2b", "channel_title": "Sales Pipeline Radio", "source": "b2b_harvester",
            "latest_video_url": "https://youtube.com/watch?v=bbbbbbbbbbb", "priority": 6, "website": "https://salespipelineradio.com",
            "description": "B2B sales and marketing leaders on pipeline and revenue", "contact_email": "matt@heinzmarketing.com",
            "contact_name": "Matt Heinz"}
lead_forced_sports = {**lead_sports, "channel_id": "UCsports2", "source": "b2b_harvester"}
added = W._enqueue_leads([lead_sports, lead_b2b, lead_forced_sports])
queue = W._lead_queue()
check("Worker: a channel it found that can't buy isn't queued; leads you chose always are",
      added == 2 and {l["channel_id"] for l in queue} == {"UCb2b", "UCsports2"}, [l["channel_id"] for l in queue])
check("Worker: ...the skipped one is on file with its reasons, and counted in the pass summary",
      W.low_fit_leads()[0]["channel_id"] == "UCsports" and W.pass_summary().get("auto_low_fit") == 1)
b2b_in_queue = next(l for l in queue if l["channel_id"] == "UCb2b")
check("Worker: a better fit moves a lead up (priority + fit)", b2b_in_queue["priority"] > 6
      and b2b_in_queue["fit"]["grade"] in {"strong", "good"}, b2b_in_queue.get("fit"))
os.environ["FIT_GATE"] = "0"
use_storage(TMP / "gate_off")
check("Worker: FIT_GATE=0 queues everything as before", W._enqueue_leads([lead_sports, lead_b2b]) == 2
      and "fit" not in W._lead_queue()[0])
os.environ.pop("FIT_GATE")

# ============================== 2. The marketing contact ==============================
team_page = """<html><head><script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Organization", "employee": [
  {"@type": "Person", "name": "Dana Reyes", "jobTitle": "VP of Marketing"}]}</script></head><body>
<div><h3>Jeb Blount</h3><p>CEO &amp; Founder</p></div>
<div><h3>Maria Lopez</h3><p>Content Marketing Manager</p></div>
<a href="https://www.linkedin.com/in/dana-reyes-12ab/">Dana on LinkedIn</a>
<p>Questions? jeb@salesgravy.com</p></body></html>"""
episode_page = "<p>Shay Keeler, VP Growth at Common Room</p><p>Kim Lee</p><p>Head of Marketing</p>"
home = '<a href="/about-us">About</a> <a href="/podcast/ep-12">Episode 12</a> <a href="/blog/x">Blog</a>'
people = M.people_on_page(team_page, "salesgravy.com")
check("Contact: names and marketing roles from structured data and from the page text",
      people == [("Dana Reyes", "VP of Marketing"), ("Maria Lopez", "Content Marketing Manager")], people)
check("Contact: a guest from another company is never their marketer",
      M.people_on_page("<p>Shay Keeler, VP Growth at Common Room</p>", "salesgravy.com") == [])
check("Contact: their email pattern from an address they publish (jeb@ for Jeb Blount: first@)",
      M.infer_pattern(["jeb@salesgravy.com"], ["Jeb Blount"], "salesgravy.com") == "first")
fetched: list[str] = []
pages = {"https://salesgravy.com/": home, "https://salesgravy.com/about-us": team_page,
         "https://salesgravy.com/podcast/ep-12": episode_page}


def fake_fetch(url: str) -> str | None:
    fetched.append(url)
    return pages.get(url)


found = M.find("https://salesgravy.com/podcast/ep-12", fetch=fake_fetch, known_names=["Jeb Blount"], mx=lambda d: True)
check("Contact: the best marketing role on the team page, with their email in the company's own pattern",
      (found.get("name"), found.get("role"), found.get("email"), found.get("confidence"))
      == ("Dana Reyes", "VP of Marketing", "dana@salesgravy.com", "site_pattern"), found)
check("Contact: ...their LinkedIn, and episode and blog pages never read",
      found.get("linkedin") == "https://www.linkedin.com/in/dana-reyes-12ab"
      and not any("/podcast/" in u or "/blog/" in u for u in fetched), fetched)
published = M.find("https://acme.com", bodies={"https://acme.com/team": "<p>Kim Lee, Head of Marketing</p>"
                                               "<a href='mailto:kim.lee@acme.com'>Email Kim</a>"})
check("Contact: an address published for them is verified; no fetch given = no requests",
      (published.get("email"), published.get("status")) == ("kim.lee@acme.com", "verified"), published)
check("Contact: nothing on the site = nothing made up", M.find("https://acme.com", bodies={"https://acme.com/": "<p>Hi</p>"}) == {})
check("Contact: the company's site from its email when the website is a podcast host",
      M.site_for({"website": "https://redcircle.com/shows/sales-gravy", "contact_email": "jeb@salesgravy.com"})
      == "https://salesgravy.com")
check("Contact: a new confidence badge for the company's own pattern", "site_pattern" in LH.EMAIL_CONFIDENCE)

# The harvester reads it from the pages it already fetched
LH._http_get = lambda url, headers=None: {"https://salesgravy.com": home.replace("/about-us", "https://salesgravy.com/about-us"),
                                          "https://salesgravy.com/about-us": team_page}.get(url.rstrip("/") if url.count("/") <= 3 else url)
site = LH.scan_website("https://salesgravy.com")
check("Harvester: keeps each page's HTML for the contact finder", "https://salesgravy.com/about-us" in site.bodies)
lead = LH.to_lead({"id": "p1", "show": "Sales Gravy", "channel_id": "UCsg", "latest_video_url": "https://y/watch?v=x",
                   "contact": {"email": "jeb@salesgravy.com", "status": "verified", "name": "Jeb Blount"},
                   "marketing_contact": found, "description": "B2B sales training",
                   "linkedin_company": "https://www.linkedin.com/company/sales-gravy"})
check("Harvester: the worker's lead carries the marketing contact, the description and the company page",
      lead.get("marketing_contact", {}).get("name") == "Dana Reyes" and lead.get("description")
      and lead.get("linkedin_company"))
backfilled = [{"id": "p2", "website": "https://salesgravy.com", "contact": {"email": "jeb@salesgravy.com", "name": "Jeb Blount"}},
              {"id": "p3", "website": "https://nothing-here.com"}]
check("Prospects: 👥 find marketing leads for ones harvested earlier (and remember who has none)",
      M.backfill(backfilled) == 1 and backfilled[0]["marketing_contact"]["name"] == "Dana Reyes"
      and backfilled[1]["marketing_contact"] == {"name": "", "looked_up": True}, backfilled)
LH._http_get = None

# The email for them
subject, body = O.build_marketer_email("Dana", "Jeb Blount", "Sales Gravy", "Cold calling is not dead",
                                       "https://clips.navstudiolab.com/sales-gravy-x", "why the first ten seconds of a cold call decide it",
                                       47.4, 3)
words = len(body.split("Best,")[0].split())
check("Marketer email: their executive's episode, the link, the standout moment, one question, under 110 words",
      subject == "Clips from Jeb's episode" and "Hi Dana," in body and "Jeb's episode \"Cold calling is not dead\"" in body
      and "https://clips.navstudiolab.com/sales-gravy-x" in body and "at 0:47" in body and body.count("?") == 1
      and words <= 110, (subject, words, body))

# ============================== 3. Follow-ups ==============================
sent_at = NOW - timedelta(days=4)
record = {"record_id": "r1", "status": "sent", "sent_at": sent_at.isoformat(), "page_name": "acme-x",
          "page_url": "https://clips.navstudiolab.com/acme-x", "channel_title": "Acme Revenue Show",
          "video_title": "Pricing by outcome", "subject": "3 clips from Acme Revenue Show", "greeting_name": "Ann",
          "contact_email": "ann@acme.com", "contact_name": "Ann Lee",
          "clips": [{"start": 10, "judge": 70, "point": "why buyers pay for results, not logins"},
                    {"start": 90, "judge": 82, "point": "how usage pricing follows the value they get"}],
          "pitch": {"format": "episode", "point": "how usage pricing follows the value they get", "at": 90.0},
          "full_email_body": "Hi Ann,\n\nI cut 3 LinkedIn-ready clips from ..."}
watched = {"opens": 2, "played": {1, 2}, "downloaded": set(), "copied": set(), "assets": set(), "offer": 0,
           "opened_at": [sent_at + timedelta(hours=3)], "last": sent_at + timedelta(hours=3)}
opened = {"opens": 1, "played": set(), "downloaded": set(), "copied": set(), "assets": set(), "offer": 0,
          "opened_at": [sent_at + timedelta(hours=5)], "last": sent_at + timedelta(hours=5)}
marketer = {"name": "Dana Reyes", "role": "VP of Marketing", "email": "dana@acme.com", "status": "guess"}

warm = FP.plan(record, watched, NOW)
check("Follow-up: watched the clips -> a warm note on day 2, in the same thread",
      warm["follow_up_step"] == "warm" and warm["follow_up_due"] and warm["follow_up_on"] == (sent_at + timedelta(days=2)).date().isoformat()
      and warm["follow_up_subject"] == "Re: 3 clips from Acme Revenue Show" and not warm["follow_up_new_thread"], warm)
check("Follow-up: ...it names the clip to post first and the founding offer, never the tracking",
      "usage pricing follows the value" in warm["follow_up_body"] and "founding clients" in warm["follow_up_body"]
      and not any(w in warm["follow_up_body"].lower() for w in ("saw you", "you watched", "noticed you", "viewed", "opened")))
early = FP.plan({**record, "sent_at": (NOW - timedelta(days=1)).isoformat()}, watched, NOW)
check("Follow-up: ...not before day 2, however fast they watched", early["follow_up_step"] == "warm" and not early["follow_up_due"])
nudge = FP.plan(record, opened, NOW)
check("Follow-up: opened but didn't play -> a bump that points at the best clip on day 3",
      nudge["follow_up_step"] == "nudge" and nudge["follow_up_due"] and "usage pricing" in nudge["follow_up_body"])
door = FP.plan({**record, "marketing_contact": marketer}, None, NOW)
check("Follow-up: never opened + a marketing lead -> a NEW email to them, written for them",
      door["follow_up_step"] == "new_door" and door["follow_up_to"] == "dana@acme.com" and door["follow_up_new_thread"]
      and door["follow_up_body"].startswith("Hi Dana,") and "Ann's episode" in door["follow_up_body"], door)
resend = FP.plan(record, None, NOW)
check("Follow-up: never opened, no marketing lead -> resend to them with a fresh subject",
      resend["follow_up_step"] == "resend" and resend["follow_up_new_thread"] and resend["follow_up_to"] == "ann@acme.com"
      and not resend["follow_up_subject"].startswith("Re:"))
after_warm = {**record, "follow_ups": [(sent_at + timedelta(days=2)).isoformat()],
              "follow_up_log": [{"step": "warm", "at": (sent_at + timedelta(days=2)).isoformat()}]}
last = FP.plan(after_warm, watched, NOW + timedelta(days=9))
check("Follow-up: after the warm note -> the last note on day 12", last["follow_up_step"] == "last"
      and last["follow_up_on"] == (sent_at + timedelta(days=12)).date().isoformat() and last["follow_up_due"])
late_watch = {**record, "follow_ups": [(sent_at + timedelta(days=3)).isoformat()],
              "follow_up_log": [{"step": "resend", "at": (sent_at + timedelta(days=3)).isoformat()}]}
check("Follow-up: watched after the resend -> a warm note before the last one",
      FP.plan(late_watch, {**watched, "opened_at": [NOW - timedelta(days=1)]}, NOW + timedelta(days=3))["follow_up_step"] == "warm")
old_done = {**record, "follow_ups": ["2026-11-01T00:00:00+00:00", "2026-11-05T00:00:00+00:00"]}
check("Follow-up: pitches that already had both old follow-ups are done",
      FP.plan(old_done, watched, NOW)["follow_up_step"] is None)
old_one = {**record, "follow_ups": [(sent_at + timedelta(days=3)).isoformat()]}
check("Follow-up: an old pitch with one bump sent, whose prospect watched -> warm next",
      FP.plan(old_one, watched, NOW + timedelta(days=4))["follow_up_step"] == "warm")
check("Follow-up: direct pitches keep their own plan", FP.plan({**record, "kind": "intro"}, None, NOW) is None)
os.environ["ADAPTIVE_FOLLOW_UPS"] = "0"
check("Follow-up: ADAPTIVE_FOLLOW_UPS=0 keeps the fixed follow-ups", FP.apply(record, watched, NOW) == record)
os.environ.pop("ADAPTIVE_FOLLOW_UPS")
check("Follow-up: the most interested first", [r["record_id"] for r in sorted(
    [FP.apply({**record, "record_id": "cold"}, opened, NOW), FP.apply({**record, "record_id": "hot"}, watched, NOW)],
    key=FP.urgency)] == ["hot", "cold"])

use_storage(TMP / "marks")
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [{**record, "marketing_contact": marketer}])
W.mark_follow_up("r1", "followed_up", step="new_door", to="dana@acme.com")
saved = W._pending_records()[0]
check("Worker: a follow-up is logged by name and who it went to; the new door marks the marketer as written to",
      saved["follow_up_log"][0]["step"] == "new_door" and saved["follow_up_log"][0]["to"] == "dana@acme.com"
      and saved.get("marketer_sent_at") and len(saved["follow_ups"]) == 1)
check("Follow-up: ...so the next one isn't the new door again",
      FP.plan(saved, None, NOW + timedelta(days=9))["follow_up_step"] == "last")
try:
    W.set_record_fields("r1", status="pending")
    refused = False
except KeyError:
    refused = True
check("Worker: set_record_fields only sets the marketing contact fields",
      refused and W.set_record_fields("r1", marketing_contact={"name": "X"}) and W._pending_records()[0]["marketing_contact"] == {"name": "X"})

# ============================== The app ==============================
from streamlit.testing.v1 import AppTest  # noqa: E402

app_store = TMP / "app_store"
use_storage(app_store)
pending = {**record, "record_id": "p1", "status": "pending", "sent_at": None, "marketing_contact": marketer,
           "full_email_body": "Hi Ann,\n\nI cut 2 LinkedIn-ready clips from \"Pricing by outcome\":\n\nhttps://clips.navstudiolab.com/acme-x\n\nWorth doing this for every episode?\n\nBest,\nBarry"}
weak_pending = {**sports, "record_id": "p2", "status": "pending", "clips": [{"start": 1, "judge": 70}],
                "full_email_body": "Hi there,\n\nI cut 1 clip", "subject": "1 clip", "channel_id": "UCsports"}
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [weak_pending, pending, {**record, "record_id": "s1"}])
wrapper = TMP / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys; sys.path.insert(0, {str(ROOT)!r})
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(app_store)!r})
    for attr, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock"), ("LOW_FIT_PATH", "low_fit.json")):
        setattr(W, attr, W.STORAGE_DIR / file)
    import core.linkedin_harvester as LH
    LH.PROSPECTS_PATH = Path({str(TMP / "prospects.json")!r})
    import core.clip_pages as CP
    sent = datetime.fromisoformat({sent_at.isoformat()!r})
    CP.configured = lambda: True
    CP.page_settings = lambda: type("S", (), {{"base_url": "https://clips.test", "bucket": "b"}})()
    CP.register_own_network = lambda *a, **k: "x"
    CP.manual_pages = lambda: []
    CP.views_count_from = lambda: None
    CP.views_seen_at = lambda: None
    CP.owner_link = lambda: ""
    CP.page_activity = lambda since=None: {{"acme-x": {{"opens": 2, "played": {{1, 2}}, "half": set(), "finished": set(),
        "downloaded": set(), "copied": set(), "assets": set(), "offer": 0, "opened_at": [sent + timedelta(hours=3)],
        "offer_at": [], "last": sent + timedelta(hours=3)}}}}
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
at = AppTest.from_file(str(wrapper), default_timeout=180).run()
check("App loads", not at.exception and not at.error, str([e.value for e in at.exception][:1] + [e.value for e in at.error][:1]))
texts = [m.value for m in [*at.markdown, *at.caption, *at.subheader]]
headers = [m.value for m in at.subheader]
check("Outbox: the best buyer first", headers.index("Acme Revenue Show") < headers.index("Seattle Sports"), headers)
check("Outbox: each pitch shows its buyer fit", any("Strong fit" in t or "Good fit" in t for t in texts)
      and any("Not a buyer" in t for t in texts))
check("Outbox: the marketing lead with a ready email",
      any("Dana Reyes" in t for t in texts) and any((a.value or "").startswith("Hi Dana,") for a in at.text_area)
      and "marketer_sent_p1" in [b.key for b in at.button])
check("Sent: 🔥 follow up today, with the warm note for the one who watched",
      any("Follow up today" in h for h in headers) and any("which clip to post first" in t for t in texts)
      and any("founding clients" in (a.value or "") for a in at.text_area))
check("Sent: replies broken down (by buyer fit among others; Round 54)", any("What gets replies" in (e.label or "") for e in at.expander))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(FAILED)} check(s) failed" if FAILED else "\nAll checks passed")
sys.exit(1 if FAILED else 0)
