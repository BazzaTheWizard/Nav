"""Round 25 checks: ask-first email, 3-step follow-ups, replies (yes/no/other),
LinkedIn messages, shared-inbox warning, the named-clip removal, and the B2B lead
profile.

Run from the project root:  .venv/bin/python tests/test_round25.py .
Uses temporary storage and a simulated YouTube. It never calls YouTube, an AI or
R2, and never touches your real storage files.
"""
import json
import os
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ.pop("LEAD_PROFILE", None)
for name in ("LEAD_MIN_SUBSCRIBERS", "LEAD_MAX_SUBSCRIBERS"):
    os.environ.pop(name, None)

fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


from core import outreach as O  # noqa: E402
import autonomous_worker as W  # noqa: E402
import core.lead_harvester as H  # noqa: E402

O.LINK_IN_FIRST_EMAIL = False  # this file tests round 25's ask-first email (round 44 has its own test)

PAGE = "https://clips.navstudiolab.com/coach-dana-k7m2qx"
POINT = "pricing retainers by outcome instead of hours"

# =========================== 1. The first email ===========================
email = O.build_outreach_email(
    "Dana Growth Co", "Ep 41", "pricing agency retainers", PAGE,
    opening_at=872.4, key_point=POINT, greeting_name="dana", content_format="podcast", business=True,
)
expected = (
    "Hi Dana,\n\n"
    f"Your point at 14:32 about {POINT} would make a strong vertical clip, on LinkedIn as much as Shorts.\n\n"
    "I've already cut it, plus 2 more from the same episode, captioned and ready to post. "
    "Free, and yours to keep either way.\n\n"
    "Want me to send them over?\n\n" + O.SIGNATURE
)
check("Ask-first email, word for word (B2B creator)", email == expected, email)
body = email.rsplit("\n\nBest,", 1)[0]
check("No link at all in the first email", "://" not in email and PAGE not in email)
check("Under 80 words, one question", len(body.split()) < 80 and body.count("?") == 1, str(len(body.split())))
check("No price in the first email", "$" not in email and O.FOUNDING_PRICE not in email)
plain = O.build_outreach_email("Chan", "Ep 41", "pricing agency retainers", business=True)
check("No moment saved -> 'I watched your recent video on ...'",
      plain.startswith("Hi there,\n\nI watched your recent video on pricing agency retainers and cut 3 vertical clips "
                       "from it for LinkedIn and Shorts, captioned"), plain)
check("Not a B2B creator -> Shorts and Reels",
      "for Shorts and Reels." in O.build_outreach_email("C", opening_at=5, key_point=POINT))
check("A negative time is ignored", "Your point at" not in O.build_outreach_email("C", opening_at=-1, key_point=POINT))

two, found = O.set_clip_count(email, 2)
check("Removing a clip: 'plus 1 more'", found and "plus 1 more from the same episode" in two and "send them over" in two)
one, found = O.set_clip_count(two, 1)
check("...down to 1: 'I've already cut it, captioned' and 'send it over'",
      found and "I've already cut it, captioned" in one and "Want me to send it over?" in one, one)
check("...and the rest of the email is unchanged", one.split("\n\n")[1] == email.split("\n\n")[1])
p2, found = O.set_clip_count(plain, 2)
check("Plain email: 'cut 2 vertical clips'", found and "cut 2 vertical clips" in p2 and "send them over" in p2)
r18 = "Dana — I cut 3 highlight moments from your recent X upload into vertical 9:16 clips with captions:"
check("Round 18 wording still counts down", "I cut 2 highlight moments" in O.set_clip_count(r18, 2)[0])
check("'Hi there,' is no name; 'Hi Dana,' is", O.greeting_name_of(plain) == "" and O.greeting_name_of(email) == "Dana")
check("Typing a name: 'Hi there,' -> 'Hi Sam,'", O.set_greeting(plain, "Sam").startswith("Hi Sam,\n\nI watched"))
check("The topic reads back from the email", O.topic_from_email(plain) == "pricing agency retainers")
check("Round 18 drafts count as old; the new email doesn't",
      O.is_old_template("I’d love to produce your clips for the next week completely free to help you drive more reach.")
      and not O.is_old_template(email) and not O.is_old_template(plain))
facts = {"at": 872.4, "point": POINT, "topic": "pricing agency retainers", "short_topic": "agency pricing",
         "format": "episode", "business": True, "seconds": 37.6}
check("Rewriting an old draft uses the saved moment",
      O.rewrite_old_draft("Hi Dana,\n\nWorth exploring?", "Chan", PAGE, facts=facts) == email)

# =========================== 2. Follow-ups, the yes reply, LinkedIn ===========================
bump = O.build_follow_up_step("bump", "Dana", PAGE, 3, "episode")
check("Day 3 bump: short, no link", bump.startswith("Hi Dana, quick bump on this.") and "://" not in bump
      and "if three feels like a lot" in bump, bump)
check("...one clip: 'it's yours if you want it'", "it's yours if you want it" in O.build_follow_up_step("bump", "Dana", PAGE, 1))
show = O.build_follow_up_step("show", "", PAGE, 3, "episode")
check("Day 7: the link so they can just look", show.startswith("Hi there,\n\nEasier to just show you: " + PAGE)
      and "3 clips from your episode" in show, show)
last = O.build_follow_up_step("last", "Dana")
check("Day 12: last note, no link", "Last note from me." in last and "://" not in last)
check("Old pitches (link already sent): bump with the link, then the last note",
      O.follow_up_steps(True) == ("bump_link", "last") and O.follow_up_steps(False) == ("bump", "show", "last")
      and PAGE in O.build_follow_up_step("bump_link", "Dana", PAGE))
check("Days: 3, 7, 12", (O.FOLLOW_UP_DAYS["bump"], O.FOLLOW_UP_DAYS["show"], O.FOLLOW_UP_DAYS["last"]) == (3, 7, 12))
check("A channel name never becomes the greeting", O.build_follow_up("Leighton Grey - Grey Matter", link := PAGE).startswith("Hi there,"))
yes = O.build_yes_reply("Dana", PAGE, 3, "episode")
check("Yes reply: the link, then the offer with price and trial",
      yes.startswith(f"Hi Dana,\n\nHere they are: {PAGE}") and O.FOUNDING_PRICE in yes and O.TRIAL in yes
      and f"{O.FOUNDING_SPOTS} founding clients" in yes and "every new episode" in yes, yes)
li = O.build_linkedin_messages("Dana", short_topic="agency pricing", key_point=POINT, clip_seconds=38.4,
                               content_format="episode", business=True)
check("LinkedIn note names the topic and fits 200 characters",
      li["connect"] == "Dana, enjoyed your episode on agency pricing. I make short clips from B2B podcasts and would be glad to connect."
      and len(li["connect"]) <= O.LINKEDIN_NOTE_MAX, li["connect"])
check("...after they accept: the point and the clip length", f"Your point about {POINT}" in li["after_accept"]
      and "(38 seconds)" in li["after_accept"] and "Want me to send it over?" in li["after_accept"])
check("...the close carries the offer", O.FOUNDING_PRICE in li["close"] and li["close"].startswith("Here it is."))
long_li = O.build_linkedin_messages("Bartholomew", short_topic="supply chain", content_format="video")
check("No point or B2B flag: still sensible, still under 200",
      len(long_li["connect"]) <= 200 and "long-form videos" in long_li["connect"]
      and "I cut a few vertical clips" in long_li["after_accept"])
check("No name: no empty greeting", O.build_linkedin_messages("")["connect"].startswith("Enjoyed your latest video")
      and O.build_linkedin_messages("")["after_accept"].startswith("Thanks for connecting. "))
check("Shared inboxes are flagged; the host's own isn't",
      O.is_shared_inbox("Info@acme.com") and O.is_shared_inbox("hello@studio.co") and not O.is_shared_inbox("dana@acme.com")
      and not O.is_shared_inbox("business@creator.com") and not O.is_shared_inbox("https://x.com/about"))

# =========================== 3. The worker: schedule, replies, removing the named clip ===========================
tmp = Path(tempfile.mkdtemp(prefix="round25-"))


def use_storage(folder):
    W.STORAGE_DIR = folder
    folder.mkdir(parents=True, exist_ok=True)
    for attr, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, attr, folder / file)
    W.init_storage_files()


use_storage(tmp / "worker")
now = datetime.now(timezone.utc)


def sent(rid, days, body=email, **extra):
    return {"record_id": rid, "channel_title": "Dana Growth Co", "greeting_name": "Dana", "page_url": PAGE,
            "full_email_body": body, "subject": "3 shorts from your agency pricing episode", "status": "sent",
            "sent_at": (now - timedelta(days=days)).isoformat(), "pitch": dict(facts),
            "clips": [{"start": 872.4, "end": 910.0}, {"start": 300.0, "end": 340.0}, {"start": 1500.0, "end": 1540.0}],
            **extra}


old_style = "Dana — I cut 3 highlight moments ... clips with captions:\n\n" + PAGE + "\n\nWorth exploring?"
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [
    sent("d2", 2), sent("d3", 3.1), sent("old", 4, body=old_style), sent("legacy", 5, body=old_style, followed_up_at=now.isoformat()),
])
by_id = {item["record_id"]: item for item in W.get_awaiting_reply()}
check("Day 2: nothing due; the bump is set for day 3",
      not by_id["d2"]["follow_up_due"] and by_id["d2"]["follow_up_step"] == "bump"
      and by_id["d2"]["follow_up_on"] == (now - timedelta(days=2) + timedelta(days=3)).date().isoformat())
check("Day 3: follow-up 1 of 3 due, the short bump, as a reply",
      by_id["d3"]["follow_up_due"] and (by_id["d3"]["follow_up_number"], by_id["d3"]["follow_up_total"]) == (1, 3)
      and by_id["d3"]["follow_up_subject"] == "Re: 3 shorts from your agency pricing episode"
      and by_id["d3"]["follow_up_body"].startswith("Hi Dana, quick bump"))
check("Old pitch (link in the first email): 1 of 2, the bump with the link",
      by_id["old"]["follow_up_step"] == "bump_link" and by_id["old"]["follow_up_total"] == 2 and PAGE in by_id["old"]["follow_up_body"])
check("Followed up before round 25: next is the last note on day 12",
      by_id["legacy"]["follow_up_step"] == "last" and not by_id["legacy"]["follow_up_due"] and by_id["legacy"]["follow_up_number"] == 2)
check("get_follow_ups_due lists only what's due", sorted(i["record_id"] for i in W.get_follow_ups_due()) == ["d3", "old"])

W.mark_follow_up("d3", "followed_up")
item = next(i for i in W.get_awaiting_reply() if i["record_id"] == "d3")
check("After the bump: the link is next, on day 7", item["follow_up_step"] == "show" and not item["follow_up_due"]
      and item["follow_ups_sent"] == 1)
check("...due on day 8", next(i for i in W.get_awaiting_reply(now + timedelta(days=5)) if i["record_id"] == "d3")["follow_up_due"])
W.mark_follow_up("d3", "followed_up")
W.mark_follow_up("d3", "followed_up")
item = next(i for i in W.get_awaiting_reply(now + timedelta(days=12)) if i["record_id"] == "d3")
check("All 3 sent: still waiting, nothing more due", item["follow_up_step"] is None and not item["follow_up_due"]
      and "follow_up_body" not in item)

W.mark_follow_up("d2", "yes")
W.mark_follow_up("old", "no")
W.mark_follow_up("legacy", "replied")
replies = {r["record_id"]: r for r in W.get_replies()}
check("Replies: yes / no / other ('They replied' from before is 'other')",
      (replies["d2"]["reply"], replies["old"]["reply"], replies["legacy"]["reply"]) == ("yes", "no", "other"))
check("A yes comes with your reply: the link, then the offer",
      PAGE in replies["d2"]["yes_reply_body"] and O.FOUNDING_PRICE in replies["d2"]["yes_reply_body"]
      and replies["d2"]["yes_reply_subject"].startswith("Re: ") and "yes_reply_body" not in replies["old"])
check("Replied pitches leave the waiting list", {i["record_id"] for i in W.get_awaiting_reply()} == {"d3"})
W.mark_follow_up("d2", "client")
check("🤝 marks the yes as a trial or client", bool(next(r for r in W.get_replies() if r["record_id"] == "d2").get("client_at")))
W.undo_reply("old")
check("↩ Undo puts a reply back in the waiting list", "old" in {i["record_id"] for i in W.get_awaiting_reply()}
      and "old" not in {r["record_id"] for r in W.get_replies()})
try:
    W.mark_follow_up("d3", "maybe")
    check("Unknown outcomes are refused", False)
except KeyError:
    check("Unknown outcomes are refused", True)

# Removing the clip the email names
pitches = tmp / "pitches"
W._pitches_dir = lambda: pitches
W.clip_pages.remove_clip = lambda name, index, count: count - 1


def outbox(rid, body=None):
    clips = [{"start": 872.4, "end": 910.0, "judge": 90, "point": POINT},
             {"start": 300.0, "end": 340.0, "judge": 70, "point": "why fewer clients made more money"},
             {"start": 1500.0, "end": 1540.0, "judge": 80, "point": "the one question that qualifies a lead"}]
    first = O.build_outreach_email("Dana Growth Co", opening_at=872.4, key_point=POINT, greeting_name="Dana",
                                   content_format="episode", business=True)
    return {"record_id": rid, "channel_title": "Dana Growth Co", "page_name": "dana-x", "page_url": PAGE,
            "full_email_body": body or first, "subject": "3 shorts from your agency pricing episode",
            "status": "pending", "clips": clips, "pitch": dict(facts)}


W._write_json_atomic(W.PENDING_OUTREACH_PATH, [outbox("n1")])
result = W.remove_pitch_clip("n1", 1)
rec = json.loads(W.PENDING_OUTREACH_PATH.read_text())[0]
check("Removing the named clip: the email now names the best clip left (by AI score)",
      result["email_updated"] and not result["named_clip_removed"]
      and rec["full_email_body"].startswith("Hi Dana,\n\nYour point at 25:00 about the one question that qualifies a lead")
      and "plus 1 more" in rec["full_email_body"] and rec["pitch"]["at"] == 1500.0, rec["full_email_body"][:140])
edited = outbox("n2")["full_email_body"].replace("Want me to send them over?", "Want them?")
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [outbox("n2", edited)])
result = W.remove_pitch_clip("n2", 1)
rec = json.loads(W.PENDING_OUTREACH_PATH.read_text())[0]
check("...an email you edited is kept, and the app is told it names the removed clip",
      result["named_clip_removed"] and "Your point at 14:32" in rec["full_email_body"] and "plus 1 more" in rec["full_email_body"])
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [outbox("n3")])
result = W.remove_pitch_clip("n3", 2)
rec = json.loads(W.PENDING_OUTREACH_PATH.read_text())[0]
check("Removing another clip only changes the count", result["email_updated"] and not result["named_clip_removed"]
      and "Your point at 14:32" in rec["full_email_body"] and "plus 1 more" in rec["full_email_body"])

# The pipeline saves the moment
from core import pipeline as P  # noqa: E402
from core import agent_brain as A  # noqa: E402
from core.transcriber import Word  # noqa: E402

A.load_dotenv = lambda *a, **k: None
os.environ["OPENAI_API_KEY"] = "test"
os.environ.pop("GEMINI_API_KEY", None)
words, t = [], 0.0
for i in range(300):
    for j in range(10):
        words.append(Word(f"w{i}{'.' if j == 9 else ''}", t, t + 0.3, None, None))
        t += 0.32
    t += 0.5
picks = [{"start_time": s, "end_time": s + 40, "hook_title": f"h{k}", "reason": "r", "point": f"a clear point number {k} here"}
         for k, s in enumerate((100, 400, 700))]
A._request_openai = lambda key, prompt, system_prompt=None: json.dumps(
    {"clips": picks, "short_topic": "agency pricing", "format": "podcast", "host_first_name": "dana", "video_topic": "pricing"})
report = {}
clips, mail, subject = P.pick_clips_with_agent(words, t, 3, "Chan", "Title", report=report, business=True)
pf = report.get("pitch_facts") or {}
check("Clip picking saves the moment the email names",
      pf.get("point", "").startswith("a clear point number") and pf.get("short_topic") == "agency pricing"
      and pf.get("format") == "episode" and pf.get("business") is True and f"about {pf.get('point')}" in mail
      and isinstance(pf.get("seconds"), float), str(pf))
check("...and every clip's point", len(report.get("clip_points", [])) == len(clips) and all(report["clip_points"]))
check("B2B: the email names LinkedIn", "on LinkedIn as much as Shorts" in mail)

# =========================== 4. The B2B lead profile ===========================
for name in ("LEAD_PROFILE", "LEAD_MIN_SUBSCRIBERS", "LEAD_MAX_SUBSCRIBERS"):
    os.environ.pop(name, None)  # in case something above read your .env
check("Default profile is b2b; LEAD_PROFILE=wide goes back", H.lead_profile() == "b2b")
check("B2B default size: 1,000–100,000", H.subscriber_range() == (1_000, 100_000))
os.environ["LEAD_PROFILE"] = "wide"
check("Wide: 3,000–250,000 as before", H.lead_profile() == "wide" and H.subscriber_range() == (3_000, 250_000))
os.environ.pop("LEAD_PROFILE")
offer = H.business_offer("Book a free strategy call: https://calendly.com/dana/30min", ["Work with me → link"])
check("Business offer: booking link and phrases", offer == ["calendly.com", "book a free strategy call", "work with me"], str(offer))
check("...'book a 30-minute call' too", H.business_offer("Book a 30-minute call with our team") == ["book a 30-minute call"])
check("No offer in a hobby channel", H.business_offer("Weekly vlogs about my cats.", ["Subscribe for more!"]) == [])
check("Institutions and news are spotted", H.institution_match("Purdue University", "") == "university"
      and H.institution_match("Daily Market News", "") == "news"
      and H.institution_match("Coach Dana", "The official channel of the Riverside School District.") == "official channel")
check("...a coaching 'Academy' isn't an institution", H.institution_match("Growth Academy", "We help founders scale.") == "")
gaming = [{"snippet": {"categoryId": "20"}}, {"snippet": {"categoryId": "20"}}, {"snippet": {"categoryId": "22"}}]
check("Off-topic category share", abs(H.off_topic_share(gaming) - 2 / 3) < 1e-9 and H.off_topic_share([]) == 0.0)
check("LinkedIn: a person's page before a company's",
      H.linkedin_url(["https://linkedin.com/company/acme", "https://www.linkedin.com/in/dana-r/."]) == "https://www.linkedin.com/in/dana-r/")

(tmp / "harvest" / "storage").mkdir(parents=True)
H.PROJECT_ROOT = tmp / "harvest"
H.SEEN_CHANNELS_PATH = tmp / "harvest" / "storage" / "seen_channels.json"
H.CHANNEL_CHECKS_PATH = tmp / "harvest" / "storage" / "channel_checks.json"
recent = (now - timedelta(days=5)).isoformat().replace("+00:00", "Z")
older = (now - timedelta(days=12)).isoformat().replace("+00:00", "Z")
CHANNELS = {
    "UCb2b": ("Dana Growth Co", "Agency growth podcast. Book a call: https://calendly.com/dana · https://www.linkedin.com/in/dana-r", "22"),
    "UCuni": ("Riverside University", "Lectures and events.", "27"),
    "UCgame": ("Speedrun Talk", "Long streams. Work with me on sponsorships.", "20"),
    "UChobby": ("Weekend Woodworking", "Shop talk every week.", "26"),
}


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeYouTube:
    def channels(self):
        return self

    def videos(self):
        return self

    def list(self, part, **kw):
        ids = kw["id"].split(",")
        if part.startswith("snippet,statistics"):
            return Req(lambda: {"items": [
                {"id": cid, "snippet": {"title": CHANNELS[cid][0], "description": CHANNELS[cid][1]},
                 "statistics": {"subscriberCount": "8000"}, "contentDetails": {"relatedPlaylists": {"uploads": f"UU{cid}"}}}
                for cid in ids if cid in CHANNELS]})
        return Req(lambda: {"items": [
            {"id": vid, "snippet": {"title": f"Episode {vid}", "publishedAt": recent if vid.endswith("1") else older,
                                    "description": "", "categoryId": CHANNELS[vid[:-2]][2]},
             "contentDetails": {"duration": "PT35M"}, "statistics": {"viewCount": "900"}}
            for vid in ids]})


H._youtube_client = lambda: FakeYouTube()
H._search_candidates = lambda youtube, query, duration="any": [
    {"channel_id": cid, "video_id": f"{cid}-1", "channel_title": cid, "video_title": "t"} for cid in CHANNELS]
H._channel_uploads = lambda cid, playlist: H._ChannelUploads([f"{cid}-1", f"{cid}-2"], False, [], 0)
leads = H.discover_channels(["agency podcast"], max_results=10)
checks = H._read_channel_checks()
check("B2B: only the expert who sells a service qualifies", [lead["channel_id"] for lead in leads] == ["UCb2b"],
      str({cid: c["result"] for cid, c in checks.items()}))
check("...with what they sell and their LinkedIn",
      leads and leads[0]["business_offer"][:2] == ["calendly.com", "book a call"]
      and leads[0]["linkedin_url"] == "https://www.linkedin.com/in/dana-r")
check("...the university is skipped for good, the others can be re-checked later",
      checks["UCuni"]["result"] == "institution_or_news" and checks["UCuni"]["soft"] is False
      and checks["UCgame"]["result"] == "off_topic_category" and checks["UChobby"]["result"] == "no_business_offer"
      and checks["UChobby"]["soft"] is True)

H._write_channel_checks({})
H._write_seen_channel_ids(set())
os.environ["LEAD_PROFILE"] = "wide"
wide = H.discover_channels(["agency podcast"], max_results=10)
check("Wide: every niche qualifies again (business_offer still noted)",
      sorted(lead["channel_id"] for lead in wide) == sorted(CHANNELS)
      and next(lead for lead in wide if lead["channel_id"] == "UChobby")["business_offer"] == [])
os.environ.pop("LEAD_PROFILE")

searched = []
W.generate_search_queries = lambda category="All", count=10: searched.append(category) or ["q"]
W.discover_channels = lambda queries, max_results=10: []
W._discover_leads(5)
os.environ["LEAD_PROFILE"] = "wide"
W._discover_leads(5)
os.environ.pop("LEAD_PROFILE")
check("The worker searches B2B phrases unless LEAD_PROFILE=wide", searched == ["B2B", "All"] and "B2B" in H._CATEGORY_GUIDANCE,
      str(searched))

# =========================== 5. The app ===========================
from streamlit.testing.v1 import AppTest  # noqa: E402

use_storage(tmp / "app")
outbox_record = outbox("p1")
outbox_record.update(contact_email="info@danagrowth.co", greeting_name="", linkedin_url="https://www.linkedin.com/in/dana-r",
                     full_email_body=O.build_outreach_email("Dana Growth Co", opening_at=872.4, key_point=POINT,
                                                            content_format="episode", business=True))
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [
    outbox_record,
    {**sent("s1", 3.5), "page_name": "dana-s1"},
    {**sent("s2", 1), "page_name": "dana-s2", "channel_title": "Quiet Pod"},
    {**sent("s3", 9, replied_at=now.isoformat()), "reply": "yes", "channel_title": "Yes Pod"},
])
wrapper = tmp / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys; sys.path.insert(0, {str(ROOT)!r})
    from pathlib import Path
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(W.STORAGE_DIR)!r})
    for name, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock")):
        setattr(W, name, W.STORAGE_DIR / file)
    W.get_worker_status = lambda: {{"running": False}}
    from core import clip_pages as C
    C.configured = lambda: True
    from types import SimpleNamespace
    C.page_settings = lambda: SimpleNamespace(base_url="https://clips.navstudiolab.com")
    C.page_activity = lambda since=None: {{"dana-s1": {{"opens": 2, "played": {{1}}, "half": set(), "finished": set(),
                                              "downloaded": set(), "last": None}}}}
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
at = AppTest.from_file(str(wrapper), default_timeout=120).run()
check("App runs", not at.exception, str([e.value for e in at.exception][:1]))
captions = [str(c.value) for c in at.caption]
check("Outbox: no first name -> a warning", any("No first name yet" in c for c in captions))
check("Outbox: info@ -> shared inbox warning", any("A shared inbox" in c for c in captions))
codes = [str(c.value) for c in at.code]
check("Outbox: LinkedIn messages ready to copy",
      "Enjoyed your episode on agency pricing. I make short clips from B2B podcasts and would be glad to connect." in codes
      and any("(38 seconds)" in c for c in codes) and any(O.FOUNDING_PRICE in c for c in codes)
      and any(e.label.startswith("💼 LinkedIn messages") for e in at.expander), str(codes[:2]))
metrics = {m.label: m.value for m in at.metric}
check("Sent: the numbers (sent, followed up, opened, played, replies)",
      metrics.get("Sent") == "3" and metrics.get("Played a clip") == "1 / 2" and metrics.get("Opened their page") == "1 / 2"
      and metrics.get("Replies") == "1", str(metrics))
heads = [str(m.value) for m in at.markdown]
check("Sent: 'follow-up 1 of 3 due' and the next date for the others",
      any("Dana Growth Co" in h and "follow-up 1 of 3 due" in h for h in heads)
      and any("Quiet Pod" in h and "follow-up 1 of 3 on " in h for h in heads), str([h for h in heads if "sent " in h]))
check("Sent: the yes reply is ready to copy", any(O.FOUNDING_PRICE in str(t.value) for t in at.text_area if (t.key or "").startswith("yes_body_")))
at.toggle(key="sent_played_only").set_value(True).run()
heads = [str(m.value) for m in at.markdown]
check("'Only show who played a clip' hides the others",
      any("Dana Growth Co" in h and "follow-up" in h for h in heads) and not any("Quiet Pod" in h for h in heads))
at.toggle(key="sent_played_only").set_value(False).run()
at.button(key="reply_no_s2").click().run()
saved = {r["record_id"]: r for r in json.loads(W.PENDING_OUTREACH_PATH.read_text())}
check("👎 Not interested is saved", saved["s2"].get("reply") == "no" and saved["s2"].get("replied_at"))
at.button(key="client_s3").click().run()
saved = {r["record_id"]: r for r in json.loads(W.PENDING_OUTREACH_PATH.read_text())}
check("🤝 marks the yes as a trial or client", bool(saved["s3"].get("client_at")))
at.button(key="undo_reply_s2").click().run()
saved = {r["record_id"]: r for r in json.loads(W.PENDING_OUTREACH_PATH.read_text())}
check("↩ Undo from the app", "replied_at" not in saved["s2"] and "reply" not in saved["s2"])
at.button(key="followed_s1").click().run()
saved = {r["record_id"]: r for r in json.loads(W.PENDING_OUTREACH_PATH.read_text())}
check("'Follow-up sent' counts it", len(saved["s1"].get("follow_ups", [])) == 1)
check("System tab shows the lead profile", any("LEAD_PROFILE" in str(df.value.to_dict()) for df in at.dataframe)
      or any("LEAD_PROFILE" in str(t.value) for t in at.table), "")

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
