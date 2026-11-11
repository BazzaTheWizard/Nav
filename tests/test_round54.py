"""Round 54: partner pitch, plans and guarantee, what gets replies, the new website, clean legacy tests.

1. Partner pitch: agencies (core.fit grade "partner") get a white-label email from the Prospects tab,
   their own bump and yes-reply.
2. Plans and guarantee: Founding $500 (3 spots), Executive $990, Team $2,500 (2 to 4 executives),
   "beat your last month of LinkedIn posts on impressions or your next month is free". The emails,
   the clip-page Worker's /offer page and the website all say the same.
3. What gets replies (core.reply_stats): every sent pitch by niche, fit, email, subject, contact, day.
4. The website (website/index.html): B2B positioning, the plans, the agency section, the form.
5. The three legacy suites (agent_worker, 28, 36) check today's behaviour, not wording from before.

Run: timeout 300 .venv/bin/python tests/test_round54.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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
from core import linkedin_harvester as LH  # noqa: E402
from core import outreach as O  # noqa: E402
from core import reply_stats as R  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="round54_"))
NOW = datetime.now(timezone.utc)


def use_storage(folder: Path) -> None:
    W.STORAGE_DIR = folder
    folder.mkdir(parents=True, exist_ok=True)
    for attr, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock"), ("LOW_FIT_PATH", "low_fit.json")):
        setattr(W, attr, folder / file)
    W.init_storage_files()


# ============================== 1. Partner pitch ==============================
agency = {"id": "p-hook", "show": "Agency Growth Mastermind Podcast", "website": "https://hookagency.com",
          "description": "Helping agency owners grow", "status": "new",
          "contact": {"name": "Mandelyn Miller", "role": "Chief Marketing Officer", "email": "mandelyn@hookagency.com",
                      "status": "verified"}}
body = O.build_partner_email(agency["show"], "Mandelyn")
words = len(body.split("Best,")[0].split())
check("Partner email: white-label, wholesale, a free sample from a client's episode, one question, under 110 words",
      body.startswith("Hi Mandelyn,") and "white-label" in body and "wholesale rate" in body
      and "one of your clients' recent episodes" in body and body.count("?") == 1 and words <= 110
      and "$" not in body, (words, body))
store = TMP / "prospects.json"
LH.save_prospects([agency], store)
use_storage(TMP / "partner")
added = LH.partner_pitch(["p-hook"], store)
record = W._pending_records()[0]
check("Partner pitch: in the Outbox as an email-only pitch, its own subject, the prospect marked",
      added == 1 and record["record_id"] == "partner-p-hook" and record["partner"] and record["kind"] == "intro"
      and record["subject"] == O.PARTNER_SUBJECT and record["contact_email"] == "mandelyn@hookagency.com"
      and LH.load_prospects(store)[0]["status"] == "partner")
check("...never added twice, and Direct pitch won't add it again", LH.partner_pitch(["p-hook"], store) == 0
      and LH.direct_pitch(["p-hook"], store) == 0)
check("...doesn't take one of the worker's Outbox slots", not W._takes_a_slot(record))
sent = {**record, "status": "sent", "sent_at": (NOW - timedelta(days=5)).isoformat()}
plan = W.next_follow_up(sent)
bump = W._with_follow_up(sent, NOW)
check("Partner follow-ups: a bump on day 4 about free samples for a client, then the last note",
      plan["step"] == "partner_bump" and plan["total"] == 2 and plan["due"]
      and "one of your clients' recent episodes" in bump["follow_up_body"])
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [{**sent, "replied_at": NOW.isoformat(), "reply": "yes"}])
yes = W.get_replies()[0]
check("Partner yes-reply: send a client recording, white-label files back in 24 h, then the wholesale rate",
      "one of your clients' recent recordings" in yes["yes_reply_body"] and "wholesale rate" in yes["yes_reply_body"]
      and "no Nav Studios branding" in yes["yes_reply_body"])

# ============================== 2. Plans and guarantee everywhere ==============================
js = (ROOT / "cloudflare" / "clip-pages-worker.js").read_text()
site = (ROOT / "website" / "index.html").read_text()
offer_js = lambda key: re.search(rf'{key}: "([^"]+)"', js).group(1)  # noqa: E731
check("Plans agree: emails and the clip-page Worker",
      O.EXECUTIVE_PRICE == f"{offer_js('executivePrice')}/month" == "$990/month"
      and O.TEAM_PRICE == f"{offer_js('teamPrice')}/month" == "$2,500/month"
      and O.TEAM_SIZE == offer_js("teamSize") and O.GUARANTEE == offer_js("guarantee"))
plans = json.loads(re.sub(r"(\w+):", r'"\1":', re.search(r"const PLANS = (\{[\s\S]*?\});", site).group(1)
                          .replace("\n", " ")).replace(",  }", "}").replace(", }", "}"))
check("...and the website's PLANS", plans["founding"] + "/month" == O.FOUNDING_PRICE and plans["spots"] == O.FOUNDING_SPOTS
      and plans["executive"] + "/month" == O.EXECUTIVE_PRICE and plans["team"] + "/month" == O.TEAM_PRICE
      and plans["teamSize"] == O.TEAM_SIZE and plans["guarantee"] == O.GUARANTEE and plans["clipsPerMonth"] == O.CLIPS_PER_MONTH
      and f"a {plans['trial']}" == O.TRIAL, plans)
visible = re.sub(r"<[^>]+>", " ", re.sub(r"<(style|script)[\s\S]*?</\1>", " ", site))
check("...and the website's visible plans say the same", all(x in visible for x in (
    "$500", "$990", "$2,500", "Founding clients: 3 spots", "2 to 4 executives", O.GUARANTEE, "2-week trial")))
offer = O.offer_text(3, "episode")
check("The yes-reply offer: founding price, then $990, the trial and the guarantee",
      "$500/month" in offer and "after that it's $990/month" in offer and "2-week trial" in offer
      and "next month is free" in offer)
check("The warm follow-up mentions the guarantee", "next month is free" in O.build_warm_follow_up("Ann", "", "https://x"))
if shutil.which("node"):
    (TMP / "worker.mjs").write_text(js)
    (TMP / "render.mjs").write_text("""import worker from "./worker.mjs";
const env = { CLIPS: { get: async () => null } };
const offer = await (await worker.fetch(new Request("https://x.test/offer"), env)).text();
console.log(JSON.stringify({ offer }));
""")
    out = subprocess.run(["node", str(TMP / "render.mjs")], capture_output=True, text=True, timeout=30)
    page = json.loads(out.stdout or "{}").get("offer", "")
    check("/offer: three plans, the guarantee and the agency question",
          all(x in page for x in ("$500", "$990", "$2,500", "Founding clients: 3 spots", "2 to 4 executives",
                                  "Our guarantee.", "Do you work with agencies?")), out.stderr[-300:])
    shown = re.sub(r"<[^>]+>", " ", re.sub(r"<(style|script)[\s\S]*?</\1>", " ", page))
    check("/offer: still no outside statistics on the page", not re.search(r"\d+\s?%", shown))
else:
    print("SKIP  /offer render (node not installed)")

# ============================== 3. What gets replies ==============================
def rec(i: int, **extra):
    return {"record_id": f"r{i}", "status": "sent", "sent_at": (NOW - timedelta(days=i)).isoformat(), "page_name": f"p{i}",
            "subject": "3 clips from Acme Show", "full_email_body": "Hi,\n\nI cut 3 LinkedIn-ready clips from ...",
            "channel_title": "Acme Revenue Show", "video_title": "B2B SaaS pricing", "contact_email": "ann@acme.com", **extra}


records = [rec(i) for i in range(10)] + [rec(10 + i, channel_title="Seattle Sports", video_title="NFL week 9",
                                              contact_email="https://youtube.com/channel/x/about") for i in range(10)]
records[0].update(replied_at=NOW.isoformat(), reply="yes")
records[1].update(replied_at=(NOW - timedelta(hours=1)).isoformat(), reply="no",
                  follow_ups=[(NOW - timedelta(days=1)).isoformat()])
activity = {"p0": {"opens": 2, "played": {1}}, "p11": {"opens": 1, "played": set()}}
by_niche = R.breakdown(records, activity, "niche")
check("Breakdown by niche: sent, opened, played, replied, best first",
      [(r["group"], r["sent"], r["opened"], r["played"], r["replied"]) for r in by_niche]
      == [("SaaS and tech", 10, 1, 1, 2), ("Sports", 10, 1, 0, 0)], by_niche)
check("...the best group named only with enough sends and a reply", R.best(by_niche)["group"] == "SaaS and tech"
      and R.best(R.breakdown(records[:5], activity, "niche")) is None)
check("By who it went to, and by email", {r["group"] for r in R.breakdown(records, activity, "contact")}
      == {"A named person's work email", "Contact form / About page"}
      and R.breakdown(records, activity, "email")[0]["group"] == "Executive B2B pitch (Round 49+)")
check("Replies by when they came: after the first email or a follow-up",
      R.when_replies_came(records) == [{"when": "After follow-up 1", "replies": 1}, {"when": "After the first email", "replies": 1}])
check("Every dimension works on old records with missing fields",
      all(R.breakdown([{"status": "sent"}], {}, d) for d in R.DIMENSIONS))

# ============================== 4. The website ==============================
check("Website: the B2B positioning, no creator-era copy, no Google Drive",
      "The LinkedIn video desk for B2B executives" in site and "high-retention Shorts" not in site
      and "Drive" not in visible)
check("Website: the free-clips form (Web3Forms key, honeypot, email fallback) and no phone number",
      'id="request-form"' in site and "web3formsKey" in site and 'name="botcheck"' in site and "showFallback" in site
      and not re.search(r"\b\d{3}[-. ]\d{3}[-. ]\d{4}\b", site))
check("Website: the agency section and request-a-call email", 'id="agencies"' in site and "White-label clips for our clients" in site
      and "A call about LinkedIn clips" in site)
check("Website: sources linked for every outside figure", visible.count("%") == 1
      and "ppc.land/linkedin-beats-youtube" in site and "news.linkedin.com/2026/keeping-conversations-real" in site)
check("Website: social preview image and deploy guide", (ROOT / "website" / "og-image.png").is_file()
      and (ROOT / "website" / "DEPLOY.md").is_file() and 'og:image' in site)

# ============================== 5. Legacy suites ==============================
agent = (ROOT / "tests" / "test_agent_worker.py").read_text()
r28 = (ROOT / "tests" / "test_round28.py").read_text()
r36 = (ROOT / "tests" / "test_round36.py").read_text()
check("Legacy: test_agent_worker checks today's first email (the clip page link), not round 25's",
      "round 44: the clip page link in the first email" in agent and "Want me to send them over?" not in agent)
check("Legacy: test_round28 tells the graphics director's prompt from the post and caption writers'",
      'prompt.startswith("Video title:")' in r28)
check("Legacy: test_round36 asks for the bold style it checks", 'accent="#FF5A1F", style="bold"' in r36)

# ============================== The app ==============================
from streamlit.testing.v1 import AppTest  # noqa: E402

app_store = TMP / "app_store"
use_storage(app_store)
W._write_json_atomic(W.PENDING_OUTREACH_PATH, [LH.partner_pitch_record(agency) | {"kind": "intro", "status": "pending",
                                                                                  "clips": []}] + records)
LH.save_prospects([{**agency, "id": "p-other", "tier": "hot", "score": 80}], TMP / "app_prospects.json")
wrapper = TMP / "app_wrapper.py"
wrapper.write_text(textwrap.dedent(f"""
    import sys; sys.path.insert(0, {str(ROOT)!r})
    from pathlib import Path
    import autonomous_worker as W
    W.STORAGE_DIR = Path({str(app_store)!r})
    for attr, file in (("PENDING_OUTREACH_PATH", "pending.json"), ("SEEN_CHANNELS_PATH", "seen.json"),
                       ("PROCESSED_CREATORS_PATH", "processed.json"), ("LEAD_QUEUE_PATH", "queue.json"),
                       ("SKIPPED_LEADS_PATH", "skipped.json"), ("LOCK_PATH", ".lock"), ("LOW_FIT_PATH", "low_fit.json")):
        setattr(W, attr, W.STORAGE_DIR / file)
    import core.linkedin_harvester as LH
    LH.PROSPECTS_PATH = Path({str(TMP / "app_prospects.json")!r})
    import runpy
    runpy.run_path({str(ROOT / "app.py")!r}, run_name="__main__")
"""))
at = AppTest.from_file(str(wrapper), default_timeout=180).run()
check("App loads", not at.exception and not at.error, str([e.value for e in at.exception][:1] + [e.value for e in at.error][:1]))
texts = [m.value for m in [*at.markdown, *at.caption]]
check("Outbox: the partner pitch with its badge", any("Partner pitch · white-label" in t for t in texts)
      and any((a.value or "").startswith("Hi Mandelyn,") for a in at.text_area))
check("Prospects: an agency gets 🤝 Partner pitch instead of Queue", "b2b_partner_p-other" in [b.key for b in at.button]
      and "b2b_queue_p-other" not in [b.key for b in at.button])
check("Sent: 📊 what gets replies", any("What gets replies" in (e.label or "") for e in at.expander))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(FAILED)} check(s) failed" if FAILED else "\nAll checks passed")
sys.exit(1 if FAILED else 0)
