"""Round 32 checks: semantic number sense, 1-2 Bento visuals in every clip (quantitative
first, qualitative fallback), a name card for every identified speaker, one editing profile
for Make clips and the worker, the Sent state in Make clips, and FFmpeg's -fps_mode.

The three clips: "Why It's Barbaric Not to Allow Autonomous Driving", "Ben Horowitz's Brutal
Advice on Marketing in Growth" and "Why Growth Investing Now Generates Venture Returns" (David
George on Uncapped, Ep. 58). Their word-timed transcripts weren't saved on this Mac (before
Round 32, Make clips didn't save a YouTube link's transcript), so CLIPS below are rebuilt
around the lines you quoted from them ("14 times safer than a human driver", "we don't need
a marketing strategy in growth", "venture firms didn't market themselves, then we did and it
worked", "20% of the S&P 500", "half between Seed and B, half from C+"). The AI's answers
are faked with the visuals Round 31's literal checks refused for these lines (a 1x
baseline, an 80% remainder, a "half and half" 50/50; checked against Round 31's director),
and with no chart for the marketing story, so these checks show what Round 32 now keeps.
Make clips now saves each video's transcript (storage/transcripts/<video id>.json): once you
run the episode again, its real words can replace CLIPS.

Temp files only: no AI, YouTube, R2 or network.
Run from the project root:  .venv/bin/python tests/test_round32.py .
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["NAV_NO_CLIP_PAGES"] = "1"
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


import core.card_checks as K  # noqa: E402
import core.graphics_director as G  # noqa: E402
import core.name_cards as N  # noqa: E402
import core.speaker_id as S  # noqa: E402
from core.edl import MODES, TEMPLATES, Card, CardRow, ClipEdit  # noqa: E402
from core.transcriber import Word  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="round32-"))


def words(text, start=100.0, step=0.38):
    out, t = [], start
    for token in text.split():
        out.append(Word(token, round(t, 2), round(t + step - 0.03, 2)))
        t += step + (0.25 if token.endswith((".", "?", "!")) else 0)
    return out


def said(text, *cards, start=100.0, clip=(95.0, 160.0)):
    w = words(text, start)
    return [K.check_card(card, w, *clip, 3.0) for card in cards]


def amounts(text):
    return [(n.value, n.scale) for n in K.spoken_numbers(words(text))]


# ---------- 1. semantic number sense ----------
check("A multiple is a unit: \"14 times\" and \"10x\" are 14x and 10x",
      amounts("it is 14 times safer and 10x cheaper") == [(14.0, "x"), (10.0, "x")], str(amounts("it is 14 times safer and 10x cheaper")))
safety = "and the data says these cars are 14 times safer than a human driver on the road today, which is barbaric"
ok, = said(safety, Card("metric_bars", 101.0, 106.0, rows=[CardRow("Human driver", "1x"), CardRow("Waymo", "14x", True)]))
check("Implicit baseline: 14x against a 1x nobody says (Human driver = 1x)", ok.ok, str(ok.errors))
bad, = said(safety, Card("metric_bars", 101.0, 106.0, rows=[CardRow("Robotaxi rival", "1x"), CardRow("Waymo", "14x", True)]))
check("...the baseline's label must be what the speaker compares against", not bad.ok and "baseline" in bad.errors[0],
      str(bad.errors))
alone, = said(safety, Card("metric_bars", 101.0, 106.0, rows=[CardRow("Human driver", "1x"), CardRow("Waymo", "9x")]))
check("...and the multiple itself must still be said", not alone.ok, str(alone.errors))
index = "the top ten tech companies are now about 20% of the S&P 500 which is wild if you think about it"
ok, = said(index, Card("share_split", 101.0, 106.0, rows=[CardRow("Top ten tech", "~20%", True),
                                                          CardRow("Rest of the S&P 500", "80%")]))
check("Complementary remainder: 20% said, the other 80% computed (100% - 20%)", ok.ok, str(ok.errors))
off, = said(index, Card("share_split", 101.0, 106.0, rows=[CardRow("Top ten tech", "~20%"), CardRow("Rest", "70%")]))
check("...only the exact remainder (70% is refused)", not off.ok and "80%" in off.errors[0], str(off.errors))
whole, = said(index, Card("metric_bars", 101.0, 106.0, rows=[CardRow("Top ten tech", "~20%"), CardRow("The S&P 500", "100%")]))
check("...and the whole (100%) as the bar it's a part of", whole.ok, str(whole.errors))
split = "we put half between Seed and B, and half from C+ on, so the money really moved later in the fund"
ok, = said(split, Card("diverging_bar", 100.5, 106.0, rows=[CardRow("Seed–Series B", "50%"), CardRow("Series C+", "50%")]))
check("Spoken fractions: \"half ... and half\" is 50% / 50%", ok.ok, str(ok.errors))
check("\"a third\" is 33.3% (33% matches), \"two thirds\" 66.7%, \"a quarter of\" 25%, \"three quarters of\" 75%",
      [round(v, 1) for v, _s in amounts("a third of it, two thirds of us, a quarter of revenue, three quarters of them")]
      == [33.3, 66.7, 25.0, 75.0] and said("so a third of the market is ours now and the rest is split",
                                           Card("kinetic_range", 100.2, 103.5, rows=[CardRow("", "33%")]))[0].ok)
check("Hyphenated and slashed splits: \"70-30\" and \"80/20\" are 70% & 30%, 80% & 20%",
      amounts("a 70-30 split and an 80/20 rule and fifty-fifty") == [(70.0, "%"), (30.0, "%"), (80.0, "%"), (20.0, "%"),
                                                                     (50.0, "%"), (50.0, "%")])
check("Verbal multipliers: doubled 2x (+100%), tripled 3x (+200%), tenfold and an order of magnitude 10x",
      amounts("it doubled then tripled, a tenfold jump, an order of magnitude") == [(2.0, "x"), (3.0, "x"), (10.0, "x"),
                                                                                  (10.0, "x")]
      and said("revenue doubled last year and that was the plan all along for us",
               Card("stat_pill", 100.5, 103.5, rows=[CardRow("revenue", "+100%")]))[0].ok)
bps = said("our margins went up 50 basis points which was the plan for the whole year here",
           Card("stat_pill", 101.0, 104.0, rows=[CardRow("margins", "+0.5%")]),
           Card("stat_pill", 101.0, 104.0, rows=[CardRow("margins", "50 bps")]))
check("Basis points: 50 bps is 0.5% (and \"went up\" isn't a hedge, \"up to\" is)", all(r.ok for r in bps),
      str([r.errors for r in bps]))
money = said("the market for this is about $5 trillion of value all around the world right now",
             Card("stat_pill", 100.2, 106.0, rows=[CardRow("the market", "~$5T")])) + said(
    "so we raised half a billion dollars for the fund from the same investors as before",
    Card("stat_pill", 100.2, 106.0, rows=[CardRow("the fund", "$500M")]))
check("Money shorthand: \"$5 trillion\" is $5T, \"half a billion\" is $500M", all(r.ok for r in money),
      str([r.errors for r in money]))
spread = "it is somewhere between 10 and 14 times safer than a human driver on these roads now"
top, middle, plain = said(spread, Card("metric_bars", 101.0, 106.0, rows=[CardRow("Human driver", "1x"),
                                                                        CardRow("Waymo · 10–14x", "14x", True)]),
                          Card("metric_bars", 101.0, 106.0, rows=[CardRow("Human driver", "1x"),
                                                                  CardRow("Waymo (10–14x)", "12x", True)]),
                          Card("metric_bars", 101.0, 106.0, rows=[CardRow("Human driver", "1x"), CardRow("Waymo", "14x")]))
check("Spoken ranges: the top (14x) or the middle (12x) with the exact range on the label", top.ok and middle.ok,
      str(top.errors + middle.errors))
check("...without the range shown, the hedge is still required", not plain.ok and "hedged" in plain.errors[0], str(plain.errors))
stages = said(split, Card("timeline", 100.5, 106.0, rows=[CardRow("", "Seed–Series B", detail="half"),
                                                          CardRow("", "Series C+", detail="half")]))[0]
check("Stage anchors on a timeline: \"Seed–Series B\" and \"Series C+\" (no 4-digit years)", stages.ok, str(stages.errors))
later = said("so today we have one fund and in five years from now we will have many more of them",
             Card("timeline", 100.2, 105.0, rows=[CardRow("", "Today", detail="one fund"), CardRow("", "+5 yrs", detail="many more")]))[0]
check("...and relative times (Today -> +5 yrs)", later.ok, str(later.errors))
unsaid = said(split, Card("timeline", 100.5, 106.0, rows=[CardRow("", "Seed–Series B"), CardRow("", "Series D")]))[0]
check("...a stage that wasn't said is still refused (Series D)", not unsaid.ok, str(unsaid.errors))

# the anti-force guardrail
passing = words("back in 2018 I was in Phoenix 3 or 4 months ago and I had 2 questions for them, six times seven")
tokens = [K._clean(w.text) for w in passing]
check("Incidental numbers: a year, a time ago, an everyday count, arithmetic", all(
    K.incidental(n, tokens) for n in K.spoken_numbers(passing)), str([(n.value, K.incidental(n, tokens)) for n in K.spoken_numbers(passing)]))
check("...while real metrics are chartable (14x, 20%, $5T, half)", [n.scale for n in K.chartable_numbers(
    words("14 times safer and 20% of it, $5 trillion in value, and half of it"))] == ["x", "%", "T", "%"])
fake_chart = Card("stat_pill", 101.0, 104.0, rows=[CardRow("in Phoenix", "3–4 months")])
check("...and the director never builds a chart on them", K.incidental_card(fake_chart, words(
    "so I was in Phoenix 3 or 4 months ago and took one every day", 100.0)))

# ---------- the qualitative Bento cards ----------
story = words("Venture firms didn't market themselves, that was the culture for decades. Then we did, and it worked.", 100.0)
comparison = Card("comparison_card", 100.0, story[-1].end + 0.2, headline="Marketing in growth",
                  rows=[CardRow("Venture firms", "Before", detail="didn't market themselves"),
                        CardRow("Then we did", "Now", True, detail="and it worked")])
res = K.check_card(comparison, story, 95.0, 130.0, 3.0)
check("Comparison card: two sides in the speaker's words, each on its words",
      res.ok and comparison.rows[0].reveal_at < comparison.rows[1].reveal_at, str(res.errors))
check("...made up words are refused", not K.check_card(Card("comparison_card", 100.0, 108.0, rows=[
    CardRow("Old school VCs"), CardRow("Modern brand building")]), story, 95.0, 130.0, 3.0).ok)
steps = words("First you find the market. Then you hire the team. Then you scale the brand.", 100.0)
sequence = Card("step_sequence", 100.0, steps[-1].end + 0.2, rows=[CardRow("Find the market"), CardRow("Hire the team"),
                                                                  CardRow("Scale the brand")])
res = K.check_card(sequence, steps, 95.0, 130.0, 3.0)
check("Step sequence: 2-4 steps in the order said", res.ok and [r.reveal_at for r in sequence.rows]
      == sorted(r.reveal_at for r in sequence.rows), str(res.errors))
check("Both are Bento cards (split) in the library, with the app's guide", all(
    t in TEMPLATES and MODES[t] == "split" for t in ("comparison_card", "step_sequence"))
    and all(f'"{t}": (' in (ROOT / "app.py").read_text() for t in ("comparison_card", "step_sequence")))
from core import cards as C  # noqa: E402
from core.edl import BrandKit  # noqa: E402

pages = {card.template: C.scene(card, BrandKit())[0] for card in (comparison, sequence)}
check("...drawn inside the Bento card (no chart library needed)",
      all('class="card"' in page and "plot.min.js" not in page for page in pages.values())
      and "Then we did" in pages["comparison_card"] and "Scale the brand" in pages["step_sequence"])
try:
    drawn = [C.render_card(card, BrandKit(), tmp / "cards") for card in (comparison, sequence)]
    check("...and they render (Playwright)", all(f.count > 0 for f in drawn))
except Exception as exc:  # noqa: BLE001
    check("...and they render (Playwright)", False, str(exc)[:160])

# ---------- 2. the three clips: 1-2 Bento visuals each, a name card for each speaker ----------
CLIPS = {
    "Why It's Barbaric Not to Allow Autonomous Driving": (1674.0, (
        "I think it's barbaric that we don't allow autonomous driving everywhere already. "
        "When I was in Phoenix 3 or 4 months ago, I took a Waymo every single day. "
        "It's not even close anymore. "
        "The data says these cars are 14 times safer than a human driver. "
        "Fourteen times safer, and we're still debating whether to let them on the road. "
        "Think about what that means for the people who get hurt in crashes every year. "
        "If you had a medicine that worked this well, you would approve it tomorrow. "
        "So the question isn't whether it's safe enough, it's why we're waiting. "
        "Regulators should be racing to get this into every city.")),
    "Ben Horowitz's Brutal Advice on Marketing in Growth": (2090.0, (
        "When we started the growth fund, I said we don't need a marketing strategy in growth. "
        "The companies know who we are, the numbers speak for themselves. "
        "And Ben looked at me and said that's exactly what every venture firm used to say. "
        "Venture firms didn't market themselves, that was the culture for decades. "
        "Then we did, and it worked. "
        "Founders started calling us before we called them. "
        "So his advice was brutal and simple: you are not special, go tell your story. "
        "Growth investors need a brand just as much as early stage investors do, maybe more. "
        "That changed how we built the whole team.")),
    "Why Growth Investing Now Generates Venture Returns": (2603.0, (
        "The biggest companies in the world are staying private for longer than ever. "
        "The top ten tech companies are now about 20% of the S&P 500. "
        "And most of that value was created after they were already big. "
        "So if you look at where venture returns come from today, it's split. "
        "Roughly half between Seed and B, and half from C+ on. "
        "That's why growth investing now generates venture returns. "
        "The power law is stronger than it has ever been, not weaker.")),
}
INFO = G.VideoInfo(title="David George on Growth Investing, AI, and Why the Power Law Is Stronger Than Ever | Ep. 58",
                   channel="Uncapped | Benchmark Breakfast",
                   description="Jack Altman sits down with David George, General Partner at Andreessen Horowitz.")


def idea(template, first, last, strength, items, headline=""):
    return {"template": template, "first_sentence": first, "last_sentence": last, "strength": strength,
            "headline": headline, "items": items, "reason": "test"}


SPEAKERS = [{"name": "David George", "role": "General Partner, Andreessen Horowitz", "kind": "guest"},
            {"name": "Jack Altman", "role": "Host", "kind": "host"}]
ANSWERS = list(map(json.dumps, [
    {"graphics": [idea("metric_bars", 4, 5, 9, [{"label": "Human driver", "value": "1x"},
                                                {"label": "Waymo", "value": "14x", "highlight": True}], "Safer than people"),
                  idea("stat_pill", 2, 2, 6, [{"label": "in Phoenix", "value": "3–4 months"}])],
     "fallback": idea("comparison_card", 4, 6, 7, [{"label": "Human driver", "detail": "we're still debating"},
                                                   {"label": "Waymo", "detail": "14 times safer", "highlight": True}]),
     "speakers": SPEAKERS},
    {"graphics": [], "fallback": idea("comparison_card", 4, 5, 8, [
        {"label": "Venture firms", "value": "Before", "detail": "didn't market themselves"},
        {"label": "Then we did", "value": "Now", "detail": "and it worked", "highlight": True}], "Marketing in growth"),
     "speakers": SPEAKERS},
    {"graphics": [idea("share_split", 2, 3, 8, [{"label": "Top ten tech", "value": "~20%", "highlight": True},
                                                {"label": "Rest of the S&P 500", "value": "80%"}], "The S&P 500"),
                  idea("diverging_bar", 5, 6, 8, [{"label": "Seed–Series B", "value": "50%"},
                                                  {"label": "Series C+", "value": "50%", "highlight": True}],
                       "Where returns come from")],
     "fallback": None, "speakers": SPEAKERS},
]))
EXPECTED = [{"metric_bars"}, {"comparison_card"}, {"share_split", "diverging_bar"}]
made = []
for (title, (start, text)), answer, expected in zip(CLIPS.items(), ANSWERS, EXPECTED):
    spoken = words(text, start)
    end = spoken[-1].end + 0.4
    G._GUESTS.clear()
    direction = G.direct(spoken, start, end, info=INFO, hook_seconds=4.0, ask=lambda p, s, a=answer: a)
    bento = [c for c in direction.edit.cards if c.template != "speaker_title"]
    names = [c.rows[0].label for c in direction.edit.cards if c.template == "speaker_title"]
    gaps = [b.start - a.start for a, b in zip(bento, bento[1:])]
    check(f"\"{title}\": 1-2 Bento visuals, {G.BENTO_SPACING:g}+ s apart ({', '.join(c.template for c in bento)})",
          1 <= len(bento) <= 2 and all(g >= G.BENTO_SPACING for g in gaps) and expected <= {c.template for c in bento},
          " | ".join(direction.notes))
    check("...a name card for each speaker, guest and host", sorted(names) == ["David George", "Jack Altman"], str(names))
    check("...and every graphic passes the renderer's own check", all(r.ok for r in K.check_edit(direction.edit, spoken, 4.0)))
    made.append(direction)
autonomy = next(c for c in made[0].edit.cards if c.template == "metric_bars")
check("Clip 1's chart is the 14x safety multiple against the human-driver baseline",
      [(r.label, r.value) for r in autonomy.rows] == [("Human driver", "1x"), ("Waymo", "14x")])
check("...and the incidental \"3 or 4 months ago\" never became a chart",
      any("said in passing" in n for n in made[0].notes), str(made[0].notes))
check("Clip 2 (no chartable metric): the speaker's contrast as a comparison card (Tier 2)",
      any(c.template == "comparison_card" for c in made[1].edit.cards), str(made[1].notes))
growth = {c.template: c for c in made[2].edit.cards}
check("Clip 3: the 20% share with its 80% remainder, and the 50/50 split with the hedge shown",
      [r.value for r in growth["share_split"].rows] == ["~20%", "80%"]
      and [r.value for r in growth["diverging_bar"].rows] == ["~50%", "~50%"], str(made[2].notes))
for (title, (start, text)) in CLIPS.items():
    spoken = words(text, start)
    G._GUESTS.clear()
    alone = G.direct(spoken, start, spoken[-1].end + 0.4, info=INFO, hook_seconds=4.0, ask=lambda p, s: None)
    bento = [c for c in alone.edit.cards if c.template != "speaker_title"]
    check(f"No AI: \"{title}\" still gets a Bento visual from its own words ({', '.join(c.template for c in bento)})",
          len(bento) == 1 and all(r.ok for r in K.check_edit(alone.edit, spoken, 4.0)), " | ".join(alone.notes))
empty_answer = json.dumps({"graphics": [], "fallback": None, "speakers": SPEAKERS})
start, text = CLIPS["Ben Horowitz's Brutal Advice on Marketing in Growth"]
spoken = words(text, start)
asked = []
G._GUESTS.clear()
second = G.direct(spoken, start, spoken[-1].end + 0.4, info=INFO, hook_seconds=4.0,
                  ask=lambda p, s: asked.append(p) or empty_answer)
check("The AI gives nothing usable: a second, qualitative-only request, then the transcript (never zero)",
      len(asked) == 2 and "SECOND LOOK" in asked[1] and any(c.template != "speaker_title" for c in second.edit.cards),
      str(second.notes))
prompt = G.build_prompt(G.sentences(spoken), start, 40.0, 4.0, INFO, [], spoken)
check("The prompt asks for 1-2 visuals 12 s apart, and says when the clip has no chartable metric",
      "At most 2 graphics, at least 1" in prompt and "No chartable metric" in prompt and "fallback" in G.SYSTEM_PROMPT)
start, text = CLIPS["Why It's Barbaric Not to Allow Autonomous Driving"]
prompt = G.build_prompt(G.sentences(words(text, start)), start, 40.0, 4.0, INFO, [], words(text, start))
check("...and lists the clip's real metrics for Tier 1 (14 times), not the incidental ones", "14 times" in prompt
      and "months ago" not in prompt.split("Chartable metrics")[1].split("\n")[0], prompt.split("\n")[5][:160])

# ---------- 3. name cards for every identified speaker ----------
check("Speakers from the description: the host and the guest, with the guest's role",
      [(s.name, s.kind) for s in G.names_from_text(INFO)] == [("Jack Altman", "host"), ("David George", "guest")]
      and G.names_from_text(INFO)[1].role == "General Partner at Andreessen Horowitz", str(G.names_from_text(INFO)))
check("...and from titles: \"– Neil Patel & Eric Siu\", \"| Brian Folmer, FirstLook VC\", \"CEO David Solomon on AI\"",
      [s.name for s in G.names_from_text(G.VideoInfo("Why Buying Companies Might Be A Terrible Idea – Neil Patel & Eric Siu"))]
      == ["Neil Patel", "Eric Siu"]
      and [(s.name, s.role) for s in G.names_from_text(G.VideoInfo(
          "What a Consumer Investor Actually Looks For in Celebrity Brands | Brian Folmer, FirstLook VC"))]
      == [("Brian Folmer", "FirstLook VC")]
      and [s.name for s in G.names_from_text(G.VideoInfo(
          "Goldman Sachs Chairman and CEO David Solomon on AI: Implementation, Accountability, and Jobs"))]
      == ["David Solomon"])
check("...never a show or a company (\"Uncapped | Benchmark Breakfast\", \"Founders Podcast\")",
      G.grounded_name({"name": "Benchmark Breakfast"}, INFO) is None
      and G.grounded_name({"name": "Founders Podcast"}, G.VideoInfo(channel="Founders Podcast")) is None)
check("...but the host whose name is the channel's is a speaker (\"Lex Fridman\")",
      G.grounded_name({"name": "Lex Fridman", "role": "Host", "kind": "host"}, G.VideoInfo(channel="Lex Fridman"))
      == ("Lex Fridman", "Host"))

GUEST_FACE, HOST_FACE = (1.0, 0.0), (0.0, 1.0)


def solo(start, end):
    return N.Option(start, end, "solo", (540, 850, 240), N.FULL_REGION, 1.0, 1150)


def panel(start, end):
    return N.Option(start, end, "panel", (515, 1392, 375), N.BOTTOM_PANEL, N.PANEL_SCALE, N.BOTTOM_PANEL[0])


def plan_cards(shots_by_face, split=False, people=(GUEST_FACE, HOST_FACE), taken=()):
    plan = SimpleNamespace(frame_width=1920, shots=[SimpleNamespace(start=100.0, end=140.0, group=False)],
                           split_view=lambda a, b: SimpleNamespace(windows=[(0.0, b - a, 0, 0, 0, 0)]) if split else None)
    real = (N.options, S.people_faces, S.video_duration)
    N.options = lambda source, plan_, a, b, guest, fallback_x=None: list(shots_by_face.get(guest, []))
    S.people_faces = lambda source, duration, width, most=2: list(people)[:most]
    S.video_duration = lambda source: 600.0
    try:
        return N.plan_name_cards("x.mp4", plan, 100.0, 140.0, 4.0, list(taken),
                                 [G.Speaker("Jack Altman", "Host", "host"), G.Speaker("David George", "General Partner")])
    finally:
        N.options, S.people_faces, S.video_duration = real


cards, notes = plan_cards({GUEST_FACE: [solo(100.0, 112.0)], HOST_FACE: [solo(114.0, 124.0)]})
when = {c.rows[0].label: round(c.start, 2) for c in cards}
check("A clip that opens on the guest's solo shot: their card right after the title (4.2 s)",
      when.get("David George") == 104.2, str(when))
check("...the host's on the first time the camera is on them alone (14.3 s)", when.get("Jack Altman") == 114.3, str(when))
cards, notes = plan_cards({GUEST_FACE: [solo(115.0, 125.0)], HOST_FACE: [panel(100.0, 115.0)]}, split=True)
when = {c.rows[0].label: round(c.start, 2) for c in cards}
check("The guest's first solo shot is later (15 s): their card then; the host, never alone, right after the title",
      when == {"David George": 115.3, "Jack Altman": 104.2}, f"{when} {notes}")
cards, notes = plan_cards({GUEST_FACE: [panel(100.0, 140.0)], HOST_FACE: [panel(100.0, 140.0)]}, split=True)
when = [(c.rows[0].label, round(c.start, 2), round(c.end, 2)) for c in cards]
check("A clip that stays in the split: the cards one after the other, right after the title (guest first)",
      when == [("David George", 104.2, 107.5), ("Jack Altman", 107.8, 111.1)], str(when))
check("...each in their own panel of the split", all(c.place and float(c.place.split(",")[2]) < 1 for c in cards),
      str([c.place for c in cards]))
check("Two speakers' cards back to back pass the checks (a 0.3 s gap between name cards)",
      all(r.ok for r in K.check_edit(ClipEdit(100.0, 140.0, cards=cards), [], 4.0)))
cards, notes = plan_cards({GUEST_FACE: [solo(100.0, 140.0)]})
check("A speaker whose face is known but who is never on screen in this clip gets no card",
      [c.rows[0].label for c in cards] == ["David George"] and any("aren't on screen" in n for n in notes), str(notes))
cards, notes = plan_cards({}, people=())
check("Faces can't be matched: every speaker still gets a card, right after the title",
      [c.rows[0].label for c in cards] == ["David George", "Jack Altman"], str(notes))
chart = Card("metric_bars", 104.0, 112.0, rows=[CardRow("a", "1"), CardRow("b", "2")])
cards, _ = plan_cards({GUEST_FACE: [panel(100.0, 140.0)], HOST_FACE: [panel(100.0, 140.0)]}, split=True, taken=[chart])
check("Name cards keep clear of a Bento visual (never on top of a chart)",
      all(c.start >= chart.end + K.MIN_GAP for c in cards), str([(c.rows[0].label, c.start) for c in cards]))
two = [Card("speaker_title", 104.2, 107.5, rows=[CardRow("David George")]),
       Card("speaker_title", 107.8, 111.1, rows=[CardRow("Jack Altman")]),
       Card("kinetic_range", 113.0, 116.0, rows=[CardRow("", "1")]), Card("kinetic_range", 126.0, 129.0, rows=[CardRow("", "2")])]
results = K.check_edit(ClipEdit(100.0, 140.0, cards=two), [], 4.0)
check("Name cards never count as Bento visuals: two speakers' cards and two visuals in one clip",
      not any("At most" in e for r in results for e in r.errors), str([r.errors for r in results]))
again = K.check_edit(ClipEdit(100.0, 140.0, cards=[two[0], Card("speaker_title", 120.0, 123.3, rows=[CardRow("David George")])]), [], 4.0)
check("...but each speaker's card appears once", any("per speaker" in e for e in again[1].errors), str(again[1].errors))

# ---------- 4. Make clips and the worker: one editing profile ----------
import autonomous_worker as W  # noqa: E402
from core.hook_selector import ClipResult  # noqa: E402
from core.pipeline import STANDARD_EDIT, EditProfile  # noqa: E402

seen = []
real_render = W.render_selected_clip
W.render_selected_clip = lambda *a, **k: seen.append(k) or SimpleNamespace(path=Path(a[4]), layout_mode="auto")
try:
    W._render_selected_clips(tmp / "x.mp4", [ClipResult(start_time=0, end_time=30, viral_title="t", caption="",
                                                        hashtags=[], virality_score=90)],
                             tmp, SimpleNamespace(), [], graphics=G.VideoInfo())
finally:
    W.render_selected_clip = real_render
check("The worker renders with Make clips' editing profile (layout, title card, explainer, graphics)",
      seen and all(seen[0].get(k) == v for k, v in STANDARD_EDIT.render_kwargs().items()) and "graphics" in seen[0],
      str(seen))
app_text = (ROOT / "app.py").read_text()
check("...and Make clips renders with the same profile (its defaults are STANDARD_EDIT)",
      "**profile.render_kwargs()" in app_text and "STANDARD_EDIT.max_seconds is not None" in app_text
      and "not STANDARD_EDIT.title_fade_in" in app_text and EditProfile() == STANDARD_EDIT)
long = [Word(f"w{k}.", k * 1.0, k * 1.0 + 0.8) for k in range(90)]
capped = W._cap_like_make_clips([ClipResult(start_time=0.0, end_time=80.0, viral_title="t", caption="", hashtags=[],
                                            virality_score=90)], long, {"pitch_facts": {"at": 0.0, "seconds": 80.0}})
check("The worker's clips are capped at 59 s like Make clips' (ending on a finished sentence)",
      capped[0].end_time <= 59.0, str(capped[0].end_time))
check("Make clips saves (and reuses) a YouTube video's transcript by its id, like the worker",
      "load_cached_transcript(video_id)" in app_text and "save_cached_transcript(video_id, words)" in app_text)

# ---------- 5. Sent, in Make clips ----------
from core import made_clips as M  # noqa: E402

M.BATCHES_PATH = tmp / "made_clips.json"
clip_files = []
for k in range(3):
    path = tmp / f"clip_{k}.mp4"
    path.write_bytes(b"x")
    clip_files.append(str(path))
batch = M.record_batch("David George on Growth Investing", "Uncapped", "https://www.youtube.com/watch?v=abc",
                       [{"path": p, "title": f"Clip {k}", "start": 10.0 * k, "end": 10.0 * k + 40} for k, p in enumerate(clip_files)])
check("A Make clips batch is kept (to send)", M.status(M.get(batch["batch_id"])) == "to send" and M.counts() == {"to send": 1, "sent": 0})
M.mark_sent(batch["batch_id"], [clip_files[0]])
check("...one clip marked sent: partly sent, still under To send", M.status(M.get(batch["batch_id"])) == "partly sent"
      and [b["batch_id"] for b in M.batches("to send")] == [batch["batch_id"]])
M.mark_sent(batch["batch_id"], sent_to="dana@example.com")
saved = json.loads(M.BATCHES_PATH.read_text())
check("...the whole batch marked sent: in the Sent view, with when and to whom, saved to disk (survives a restart)",
      M.status(saved[0]) == "sent" and saved[0]["sent_at"] and saved[0]["sent_to"] == "dana@example.com"
      and [b["batch_id"] for b in M.batches("sent")] == [batch["batch_id"]] and M.batches("to send") == [])
M.mark_unsent(batch["batch_id"])
check("...and moved back to To send", M.status(M.get(batch["batch_id"])) == "to send" and M.get(batch["batch_id"])["sent_at"] is None)
M.replace_clip(batch["batch_id"], clip_files[1], tmp / "trimmed.mp4", 12.0, 50.0)
M.remove_clip(batch["batch_id"], clip_files[2])
kept = M.get(batch["batch_id"])["clips"]
check("Trimming a clip or removing one updates the batch", [Path(c["path"]).name for c in kept] == ["clip_0.mp4", "trimmed.mp4"])
other = M.record_batch("Another video", "", "file.mp4", [{"path": clip_files[0], "title": "x", "start": 0, "end": 30}])
M.mark_sent(other["batch_id"])
from streamlit.testing.v1 import AppTest  # noqa: E402

W.get_pending_outreach = lambda: []
at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
check("App runs without errors", not at.exception, str([e.value for e in at.exception]))
check("Make clips lists the batches: 1 to send, 1 sent", any("Your batches" in s.value for s in at.subheader)
      and any(b.key == f"made_all_{batch['batch_id']}" for b in at.button), str([b.key for b in at.button][-8:]))
at.button(key=f"made_all_{batch['batch_id']}").click().run()
check("✅ Mark batch as Sent (like the Outbox's): it moves to 📨 Sent", M.status(M.get(batch["batch_id"])) == "sent"
      and M.counts() == {"to send": 0, "sent": 2})
at.session_state["made_view"] = "sent"
at.run()
check("The 📨 Sent view lists both, each with ↩ Move back", sorted(b.key for b in at.button if (b.key or "").startswith("made_back_"))
      == sorted([f"made_back_{batch['batch_id']}", f"made_back_{other['batch_id']}"]))

# ---------- 6. FFmpeg 7+: -fps_mode, not -vsync ----------
flag = "-" + "vsync"
uses = [str(p.relative_to(ROOT)) for folder in ("core", "tests") for p in (ROOT / folder).glob("*.py")
        if flag in p.read_text() and p.name != "test_round32.py"]
check("No -vsync left in core/ or tests/ (removed in FFmpeg 7+; -fps_mode passthrough instead)", not uses, str(uses))
check("test_round30 reads its frames with -fps_mode passthrough",
      '"-fps_mode", "passthrough"' in (ROOT / "tests" / "test_round30.py").read_text())
import core.dynamic_layout as D  # noqa: E402

check("The move into a chart reads and writes its frames as Rec. 709 (no colour shift on newer FFmpeg)",
      "in_color_matrix=bt709" in D.TO_RGB and "out_color_matrix=bt709" in D.FROM_RGB)

print("\nAll checks passed." if not fails else f"\n{fails} check(s) failed.")
sys.exit(1 if fails else 0)
