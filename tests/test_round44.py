"""Round 44: the first email shows the clips (link in the first email), tighter AI
wording for the moment it names, acronyms fixed, a short bump with the link.

Run: timeout 60 .venv/bin/python tests/test_round44.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or not detail else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


from core import outreach as O  # noqa: E402
from core import agent_brain as B  # noqa: E402
import autonomous_worker as W  # noqa: E402

PAGE = "https://clips.navstudiolab.com/coach-dana-k7m2qx"
POINT = "pricing retainers by outcome instead of hours"
check("Round 44 is on by default", O.LINK_IN_FIRST_EMAIL is True)

# ---------------- 1. The first email ----------------
email = O.build_outreach_email(
    "Dana Growth Co", "Ep 41", "pricing agency retainers", PAGE,
    opening_at=872.4, key_point=POINT, greeting_name="dana", content_format="podcast", business=True,
)
expected = (
    "Hi Dana,\n\n"
    f"Your point at 14:32 about {POINT} makes a great short, so I cut it, plus 2 more from the same episode:\n\n"
    f"{PAGE}\n\n"
    "Vertical, captioned and ready for LinkedIn and Shorts. Free, and yours to keep either way.\n\n"
    "Want me to do the same for your next episode?\n\n" + O.SIGNATURE
)
check("Show-the-clips email, word for word (B2B creator)", email == expected, email)
body = email.rsplit("\n\nBest,", 1)[0]
check("Under 70 words, one question, no price", len(body.split()) < 70 and body.count("?") == 1 and "$" not in email,
      str(len(body.split())))
check("The link is on its own line", f"\n\n{PAGE}\n\n" in email)
no_link = O.build_outreach_email("C", opening_at=5, key_point=POINT)
check("Before the page exists: the placeholder, which the worker fills",
      O.DRIVE_LINK_PLACEHOLDER in no_link and O.fill_drive_link(no_link, PAGE).count(PAGE) == 1)
check("Not a B2B creator -> Shorts and Reels", "ready for Shorts and Reels." in no_link)
plain = O.build_outreach_email("Chan", "Ep 41", "pricing agency retainers", PAGE)
check("No moment -> 'I cut 3 vertical clips from your recent video on ...'",
      plain.startswith("Hi there,\n\nI cut 3 vertical clips from your recent video on pricing agency retainers:\n\n" + PAGE),
      plain)
check("The topic still reads back from the plain email", O.topic_from_email(plain) == "pricing agency retainers")

two, ok = O.set_clip_count(email, 2)
check("Removing a clip: 'plus 1 more'", ok and "so I cut it, plus 1 more from the same episode:" in two, two)
one, ok = O.set_clip_count(two, 1)
check("...down to 1: 'so I cut it:'", ok and "makes a great short, so I cut it:\n\n" in one, one)
p2, ok = O.set_clip_count(plain, 1)
check("Plain email: 'I cut 1 vertical clip'", ok and "I cut 1 vertical clip from your recent" in p2, p2)

check("Greeting and name still work", O.greeting_name_of(email) == "Dana"
      and O.set_greeting(plain, "Sam").startswith("Hi Sam,\n\nI cut"))
check("The new email isn't an 'old template'", not O.is_old_template(email) and not O.is_old_template(plain))
O.LINK_IN_FIRST_EMAIL = False
ask = O.build_outreach_email("C", "T", "t", PAGE, opening_at=872.4, key_point=POINT, greeting_name="Dana")
O.LINK_IN_FIRST_EMAIL = True
check("An unsent round 25 ask-first draft counts as old...", O.is_old_template(ask))
facts = {"at": 872.4, "point": POINT, "topic": "pricing agency retainers", "format": "episode", "business": True}
check("...and is rewritten with the link and the saved moment",
      O.rewrite_old_draft(O.set_greeting(ask, "Dana"), "Dana Growth Co", PAGE, clip_count=3, facts=facts) == email)
check("Flag off -> round 25's ask-first email (no link)", "://" not in ask.rsplit("\n\nBest,", 1)[0]
      and "Want me to send them over?" in ask)

# ---------------- 2. Follow-ups ----------------
record = {"full_email_body": email, "page_url": PAGE, "sent_at": "2026-11-01T12:00:00+00:00", "greeting_name": "Dana"}
check("A pitch with the link gets the bump with the link, then the last note",
      W.next_follow_up(record)["step"] == "bump_link" and W.next_follow_up(record)["total"] == 2)
bump = O.build_follow_up_step("bump_link", "Dana", PAGE, 3)
check("Bump: short, the link, an easy offer",
      bump.startswith(f"Hi Dana, quick bump in case this got buried: {PAGE}\n\n")
      and "raw MP4s" in bump and len(bump.split()) < 45, bump)
check("...one clip: 'test it'", "test it on your feed" in O.build_follow_up_step("bump_link", "Dana", PAGE, 1))

# ---------------- 3. Wording from the AI ----------------
check("Acronyms in the subject", O.build_subject("ai sales reps", "episode") == "3 shorts from your AI sales reps episode")
check("...and in the point", O.clean_key_point("a 6-month cac payback for saas founders") ==
      "a 6-month CAC payback for SaaS founders")
check("Everyday words left alone ('us', 'safe')", O.clean_key_point("why it is safe for us to wait") ==
      "why it is safe for us to wait")
check("Words inside other words left alone", O.clean_short_topic("aid policy") == "aid policy")
prompt = B._SYSTEM_PROMPT
check("Prompt: the point completes the new email line", "makes a great short" in prompt and "stuck with me" not in prompt)
check("Prompt: concrete, their own terms, never flip the claim",
      "their own terms" in prompt and "Never flip or sharpen" in prompt and "5–12 words" in prompt)
check("Prompt: subject safe for a work inbox, acronyms kept", "safe for a work inbox" in prompt and '"AI sales reps"' in prompt)

print(f"\n{len(FAILED)} check(s) failed" if FAILED else "\nAll checks passed")
sys.exit(1 if FAILED else 0)
