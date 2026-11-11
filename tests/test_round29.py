"""Round 29 checks: one speaker never split into two panels (dissolves found, the two-face
gate), the guest's name card in every clip (face-matched, placed around the face, in a
split panel when needed, never counted toward the graphics limit), the 24/76 move (no
cross-fade), the fall-and-recovery curve, and the hook's pull-in zoom (jitter-free).
Temp files and fakes only: no AI, YouTube, R2 or real faces.

Run from the project root:  .venv/bin/python tests/test_round29.py .
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(ROOT))
os.environ["NAV_NO_CLIP_PAGES"] = "1"
fails = 0


def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


import core.face_tracker as F  # noqa: E402
from core.transcriber import Word  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="round29-"))
RATE = 25

# ---------- a call recording: one person alone, a 0.5 s cross-dissolve, then two side-by-side tiles ----------
W_, H_ = 1920, 1080
YELLOW, GREEN, MAGENTA, DARK = (230, 220, 40), (40, 220, 60), (230, 40, 220), (30, 30, 30)


def solo():
    img = np.full((H_, W_, 3), DARK, np.uint8)
    img[:, 700:1220] = YELLOW
    return img


def tiles():
    img = np.full((H_, W_, 3), DARK, np.uint8)
    img[:, :640] = GREEN
    img[:, 1280:] = MAGENTA
    return img


def write(path, frames):
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W_}x{H_}",
                             "-r", str(RATE), "-i", "-", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-shortest",
                             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-g", "12",
                             str(path)], stdin=subprocess.PIPE)
    for frame in frames:
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    proc.wait()


DISSOLVE_FROM, DISSOLVE_FRAMES = 100, 12  # frame 100 to 111: the two layouts blend
frames = []
for n in range(200):
    if n < DISSOLVE_FROM:
        frames.append(solo())
    elif n < DISSOLVE_FROM + DISSOLVE_FRAMES:
        mix = (n - DISSOLVE_FROM + 1) / (DISSOLVE_FRAMES + 1)
        frames.append((solo() * (1 - mix) + tiles() * mix).astype(np.uint8))
    else:
        frames.append(tiles())
call = tmp / "call.mp4"
write(call, frames)


class FakeDetector:
    """A "face" on each coloured block: yellow in the middle, green left, magenta right."""
    available = True

    def detect(self, frame):
        h, w = frame.shape[:2]
        found = []
        middle = frame[:, int(w * 0.40):int(w * 0.60)]
        if middle[:, :, 2].mean() > 150 and middle[:, :, 1].mean() > 150 and middle[:, :, 0].mean() < 110:  # yellow (BGR)
            found.append((w * 0.5, h * 0.4, w * 0.12, h * 0.2, 0.9))
        if frame[:, : w // 3, 1].mean() > 150 and frame[:, : w // 3, 2].mean() < 110:  # green
            found.append((w * 0.17, h * 0.4, w * 0.11, h * 0.2, 0.9))
        if frame[:, 2 * w // 3:, 0].mean() > 150 and frame[:, 2 * w // 3:, 2].mean() > 150:  # magenta
            found.append((w * 0.83, h * 0.4, w * 0.10, h * 0.2, 0.9))
        return found


F._face_detector = lambda: FakeDetector()
plan = F.analyze_crop_plan(call, 0.0, 8.0)
middle = (DISSOLVE_FROM + DISSOLVE_FRAMES / 2) / RATE
check("A cross-dissolve between layouts is a shot boundary, inside the dissolve",
      len(plan.shots) == 2 and DISSOLVE_FROM / RATE - 0.05 <= plan.shots[0].end <= (DISSOLVE_FROM + DISSOLVE_FRAMES) / RATE + 0.05,
      f"{[round(s.end, 2) for s in plan.shots]} vs the dissolve {DISSOLVE_FROM / RATE:.2f}-{(DISSOLVE_FROM + DISSOLVE_FRAMES) / RATE:.2f}")
check("The one person alone is a single crop, never the two-person split",
      len(plan.shots) == 2 and plan.shots[0].speakers is None and not plan.shots[0].group, str(plan.shots[:1]))
check("The two tiles after it are the split", len(plan.shots) == 2 and plan.shots[1].speakers is not None)
split = plan.split_view(0.0, 8.0)
check("Split view only where both people are on screen", split is not None and split.spans[0][0] > middle - 0.3,
      str(split.spans if split else None))
check("The crop follows: the solo part is framed on its person",
      abs(plan.shots[0].x + plan.crop_width / 2 - W_ / 2) < 40, str(plan.shots[0].x))

# the two-face gate on its own
one_then_other = [[(500.0, 1.0, 400.0, 300.0)]] * 5 + [[(1400.0, 1.0, 400.0, 300.0)]] * 5
clusters = [[(500.0, 1.0, 400.0, 300.0)] * 5, [(1400.0, 1.0, 400.0, 300.0)] * 5]
check("Two-face gate: two places at different times are never a pair (the same person moving, or two layouts)",
      F._speaker_pair(clusters, one_then_other, 345.6, 545.0) is None)
together = [[(500.0, 1.0, 400.0, 300.0), (1400.0, 1.0, 420.0, 290.0)]] * 10
check("Two-face gate: both in the same frames, far apart, similar size -> a pair",
      F._speaker_pair([[f[0] for f in together], [f[1] for f in together]], together, 345.6, 545.0) is not None)
tiny = [[(500.0, 1.0, 400.0, 300.0), (1400.0, 1.0, 420.0, 80.0)]] * 10
check("Two-face gate: a far smaller second face (a poster, a reflection) is not a speaker",
      F._speaker_pair([[f[0] for f in tiny], [f[1] for f in tiny]], tiny, 345.6, 545.0) is None)
close = [[(900.0, 1.0, 400.0, 300.0), (1100.0, 1.0, 420.0, 290.0)]] * 10
check("Two-face gate: two faces closer than a crop width are not two panels",
      F._speaker_pair([[f[0] for f in close], [f[1] for f in close]], close, 345.6, 545.0) is None)

# ---------- the guest: face matching (SFace features faked) ----------
from core import speaker_id as S  # noqa: E402

rng = np.random.default_rng(1)
guest_vec, host_vec = rng.normal(size=128), rng.normal(size=128)


def feature(base):
    v = base + rng.normal(scale=0.25, size=128)
    return tuple((v / np.linalg.norm(v)).tolist())


def seen(cx, base):
    return S.SeenFace(cx, 540.0, 360.0, feature(base))


fake_frames = [[seen(960, guest_vec)]] * 20 + [[seen(960, host_vec)]] * 8 + [[seen(500, host_vec), seen(1400, guest_vec)]] * 8
real_faces_at = S.faces_at
S.faces_at = lambda source, times, width: fake_frames[: len(times)]
S._cache.clear()
found_guest = S.guest_face(call, 600.0, 1920)
S.faces_at = real_faces_at
g_unit = guest_vec / np.linalg.norm(guest_vec)
check("The guest is whoever is alone on screen most across the video",
      found_guest is not None and float(np.dot(found_guest, g_unit)) > 0.9)
pair = [seen(500, host_vec), seen(1400, guest_vec)]
match = S.best_match(pair, found_guest) if found_guest else None
check("In a split, the guest's own face is found (the host never gets the name)", match is not None and match.cx == 1400)
check("A stranger isn't the guest", S.best_match([seen(700, rng.normal(size=128))], found_guest) is None)

# ---------- name card placement ----------
from core import name_cards as N  # noqa: E402
from core.cards import name_card_size, parse_place  # noqa: E402
from core.edl import Card, CardRow, ClipEdit  # noqa: E402


def core_box(face):
    cx, cy, fw = face
    return (cx - N.FACE_SIDE * fw, cy - N.FACE_UP * fw, cx + N.FACE_SIDE * fw, cy + N.FACE_DOWN * fw)


def card_box(place, name="Jonathan Bragdon", role="Founder, Capacity Capital"):
    x, y, scale, align = parse_place(place)
    w, h = name_card_size(name, role, scale)
    left = x - w / 2 if align == "center" else x if align == "left" else x - w
    return (left, y, left + w, y + h)


def overlaps(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


studio = N.Option(0, 10, "solo", (540, 850, 240), N.FULL_REGION, 1.0, N.FULL_REGION and 1150)
spot = N.place("Jonathan Bragdon", "Founder, Capacity Capital", studio)
check("A studio shot: the usual lower-middle spot", spot == "540,1150,1.00,center", spot)
webcam = N.Option(0, 10, "solo", (501, 974, 654), N.FULL_REGION, 1.0, 1150)
spot = N.place("Jonathan Bragdon", "Founder, Capacity Capital", webcam)
check("A webcam close-up (face fills the frame): above the head, never on the chin or mouth",
      spot is not None and parse_place(spot)[1] < 400 and not overlaps(card_box(spot), core_box(webcam.face)), spot)
low = N.Option(0, 10, "solo", (540, 1000, 330), N.FULL_REGION, 1.0, 1150)
spot = N.place("Jonathan Bragdon", "Founder, Capacity Capital", low)
check("A face low in the frame: the card moves below the chin, clear of the caption line",
      spot is not None and not overlaps(card_box(spot), core_box(low.face))
      and not overlaps(card_box(spot), (0, N.CALM_CAPTIONS[0], 1080, N.CALM_CAPTIONS[1])), spot)
panel = N.Option(0, 10, "panel", (515, 1392, 375), N.BOTTOM_PANEL, N.PANEL_SCALE, N.BOTTOM_PANEL[0])
spot = N.place("Jonathan Bragdon", "Founder, Capacity Capital", panel)
box = card_box(spot) if spot else None
check("In a split: a compact card inside the guest's own panel, off the face and the seam captions",
      spot is not None and parse_place(spot)[2] < 1 and N.BOTTOM_PANEL[0] <= box[1] and box[3] <= N.BOTTOM_PANEL[1]
      and not overlaps(box, core_box(panel.face)), spot)

taken = [Card("kinetic_range", 104.0, 107.0, rows=[CardRow("", "96 hrs")])]
when = N.slot(N.Option(100.0, 130.0, "solo", (540, 850, 240), N.FULL_REGION, 1.0, 1150), taken, 104.2, 128.0)
check("The name card never overlaps a data graphic (and leaves a gap)", when is not None and when >= 107.0 + 1.5, str(when))
talks = N.Option(100.0, 130.0, "panel", (515, 1392, 375), N.BOTTOM_PANEL, N.PANEL_SCALE, 1068,
                 lambda t0, t1: 0.9 if t0 >= 112 else 0.2)
check("In a panel, it comes in while the guest is the one talking", 112.0 <= N.slot(talks, [], 104.2, 128.0) < 112.3)

# ---------- the director: every clip, never counted, makes room when it must ----------
from core import graphics_director as G  # noqa: E402

words = [Word(f"w{i}", 100.0 + i * 0.3, 100.28 + i * 0.3) for i in range(130)]
data = [Card("kinetic_range", 108.0, 111.0, rows=[CardRow("", "1")]), Card("kinetic_range", 118.0, 121.0, rows=[CardRow("", "2")])]
direction = G.Direction(ClipEdit(100.0, 139.0, cards=list(data)))
calls = []


def fake_name_card(source, plan_, a, b, hook, taken_, name, role, fallback_x=None):
    calls.append(len(taken_))
    if len(taken_) == 2:
        return None, "no free moment while the guest is on screen"
    return Card("speaker_title", 106.0, 109.3, rows=[CardRow(name, detail=role)], place="540,150,1.00,center"), "on their solo shot"


real_name_card = N.name_card
N.name_card = fake_name_card
G._add_name_card(direction, ("Jonathan Bragdon", "Founder, Capacity Capital"), words, 100.0, 139.0, 4.0,
                 plan=object(), source="x.mp4", strengths={id(data[0]): 9, id(data[1]): 7})
N.name_card = real_name_card
kinds = [c.template for c in direction.edit.cards]
check("No free moment: the weakest data graphic makes room for the guest's name card",
      kinds.count("speaker_title") == 1 and data[0] in direction.edit.cards and data[1] not in direction.edit.cards,
      str(kinds) + str(direction.notes))

free = G.Direction(ClipEdit(100.0, 139.0, cards=[Card("kinetic_range", 112.0, 115.0, rows=[CardRow("", "1")]),
                                                    Card("kinetic_range", 125.0, 128.0, rows=[CardRow("", "2")])]))
N.name_card = lambda *a, **k: (Card("speaker_title", 104.2, 107.5, rows=[CardRow("Jonathan Bragdon")]), "on their solo shot")
G._add_name_card(free, ("Jonathan Bragdon", ""), words, 100.0, 139.0, 4.0, plan=object(), source="x.mp4", strengths={})
N.name_card = real_name_card
check("The name card doesn't count toward the 1-2 graphics: two data graphics and the name card",
      [c.template for c in free.edit.cards].count("kinetic_range") == 2 and len(free.edit.cards) == 3, str(free.notes))

INFO = G.VideoInfo(title="Top Decile Podcast: Jonathan Bragdon of Capacity Capital", channel="Decile Group",
                   description="Connor Sattely talks with Jonathan Bragdon, founder of Capacity Capital.")
G._GUESTS.clear()
answers = iter([{"graphics": [], "speaker": {"name": "Jonathan Bragdon", "role": "Founder, Capacity Capital"}},
                {"graphics": [], "speaker": None}])
first = G.direct(words, 100.0, 139.0, info=INFO, hook_seconds=4.0, ask=lambda p, s: json.dumps(next(answers)))
second = G.direct(words, 100.0, 139.0, info=INFO, hook_seconds=4.0, ask=lambda p, s: json.dumps(next(answers)))
check("Every clip of the video keeps the guest's name, even when the AI leaves it out for one clip",
      "Jonathan Bragdon" in [s.name for s in G._GUESTS.get((INFO.title, INFO.channel), [])]
      and any("no face track" in n for n in second.notes), str(second.notes))
check("...and never the host's", G.grounded_name({"name": "Decile Group", "role": ""}, INFO) is None)

# ---------- calm captions sit on the seam in a two-person split ----------
from core.dynamic_layout import calm_captions  # noqa: E402

talk = [Word(t, 105.0 + k * 0.3, 105.28 + k * 0.3) for k, t in enumerate("this is said over the number here".split())]
overlay = ClipEdit(100.0, 130.0, cards=[Card("kinetic_range", 105.0, 108.0, rows=[CardRow("", "1")])])
seam = calm_captions(talk, overlay, tmp / "seam.ass", seams=[(0.0, 30.0)]).read_text()
plain = calm_captions(talk, overlay, tmp / "plain.ass").read_text()
check("During a graphic in a two-person split, the calm captions sit on the seam (not on a face)",
      "\\an5\\pos(540,960)" in seam and "\\pos(" not in plain)

# ---------- a name card clear of the bold captions leaves them as they are ----------
import core.dynamic_layout as D  # noqa: E402


def name_at(place, start=104.2):
    return Card("speaker_title", start, start + 3.3, rows=[CardRow("Jonathan Bragdon", detail="Founder, Capacity Capital")],
                place=place)


clip_edit = ClipEdit(100.0, 140.0)
check("Name card above the head: the bold captions stay (no switch to small captions in the hook)",
      D.keeps_bold_captions(name_at("540,150,1.00,center"), clip_edit))
check("...in the standard lower spot (on the captions' line): the small captions, as before",
      not D.keeps_bold_captions(name_at(""), clip_edit)
      and not D.keeps_bold_captions(name_at("540,1150,1.00,center"), clip_edit))
check("...in the guest's split panel, clear of the seam captions: the bold captions stay",
      D.keeps_bold_captions(name_at("540,1100,0.78,center"), clip_edit, seams=[(0.0, 40.0)])
      and not D.keeps_bold_captions(name_at("540,900,0.78,center"), clip_edit, seams=[(0.0, 40.0)]))
check("...a data graphic always gets the small captions",
      not D.keeps_bold_captions(Card("kinetic_range", 105.0, 108.0, rows=[CardRow("", "1")]), clip_edit))
spoken = [Word(t, 103.0 + k * 0.3, 103.28 + k * 0.3) for k, t in enumerate(
    "so the talent is here in the Southeast and it is spread out all over the place".split())]
seen = {}


def fake_compose(base, source, edit, frames, words, *args, **kwargs):
    seen["calm"] = list(words)  # the words that get the small captions
    return base


def fake_base(kept):
    seen["bold"] = list(kept)  # the words that keep their bold captions
    return tmp / "base.mp4"


real_compose = D.compose_cards
D.compose_cards = fake_compose
try:
    for label, place in (("top", "540,150,1.00,center"), ("standard", "")):
        D.render_with_cards(tmp / "nosource.mp4", ClipEdit(100.0, 140.0, cards=[name_at(place)]), spoken, tmp / "x.mp4",
                            tmp / "nc_work", fake_base, hook_seconds=4.0)
        seen[label] = (len(seen.pop("bold", [])), len(seen.pop("calm", [])))
finally:
    D.compose_cards = real_compose
check("Rendering: a top name card keeps every bold caption; a lower one swaps them for small ones",
      seen["top"] == (len(spoken), 0) and seen["standard"][0] < len(spoken) and seen["standard"][1] > 0, str(seen))

# ---------- the fall-and-recovery curve ----------
from core import card_checks as K  # noqa: E402
from core.edl import BrandKit, MODES, TEMPLATES  # noqa: E402

said = [Word(t, 200.0 + k * 0.3, 200.28 + k * 0.3) for k, t in enumerate(
    "So we can say what if it turns out the stock market crashes by 20%? Given the last hundred years of history, "
    "when the markets go down, eventually they go back up again.".split())]
curve = Card("drawdown_curve", 200.0, said[-1].end + 1.6, rows=[CardRow("", "−20%"), CardRow("they go back up", "")])
result = K.check_card(curve, said, 195.0, 240.0)
check("Fall and recovery: passes when the fall is said and the recovery is in the speaker's words",
      result.ok and curve.rows[0].reveal_at < curve.rows[1].reveal_at, str(result.errors))
check("...as a chart (24/76) in the library", "drawdown_curve" in TEMPLATES and MODES["drawdown_curve"] == "split")
for name, rows, needle in [
    ("needs the minus", [CardRow("", "20%"), CardRow("they go back up", "")], "minus"),
    ("an unsaid fall is stopped", [CardRow("", "−30%"), CardRow("they go back up", "")], "isn't said"),
    ("the recovery must be the speaker's words", [CardRow("", "−20%"), CardRow("a quick V-shaped rebound", "")], "speaker"),
]:
    bad = K.check_card(Card("drawdown_curve", 200.0, said[-1].end + 1.6, rows=rows), said, 195.0, 240.0)
    check(f"Fall and recovery: {name}", any(needle in e for e in bad.errors), str(bad.errors))
G_prompt = G.SYSTEM_PROMPT
check("The director is told to show, not tell (a fall and recovery is a curve, not a printed number)",
      "Show, don't tell" in G_prompt and "drawdown_curve" in G_prompt)
app_text = (ROOT / "app.py").read_text()
guide_keys = set(re.findall(r'^    "(\w+)": \(', app_text[app_text.index("CARD_GUIDE"):app_text.index("CARD_ROWS")], re.M))
check("Make clips' manual list offers every graphic", set(TEMPLATES) <= guide_keys, str(set(TEMPLATES) - guide_keys))
check("A name card's spot is saved with the clip's edit",
      ClipEdit.from_json(ClipEdit(0, 10, cards=[Card("speaker_title", 4, 7.3, rows=[CardRow("A B")],
                                                   place="540,150,1.00,center")]).to_json()).cards[0].place
      == "540,150,1.00,center")

from core import cards as C  # noqa: E402

frames_curve = C.render_card(curve, BrandKit(), tmp / "cards")
before = np.array(__import__("PIL.Image", fromlist=["Image"]).open(frames_curve.pattern % int((curve.rows[0].reveal_at - curve.start - 0.3) * 30)))
after = np.array(__import__("PIL.Image", fromlist=["Image"]).open(frames_curve.pattern % int((curve.rows[1].reveal_at - curve.start + 1.6) * 30)))


def reds(img):
    return int(((img[:, :, 0] > 180) & (img[:, :, 1] < 140) & (img[:, :, 3] > 200)).sum())


def greens(img):
    return int(((img[:, :, 1] > 150) & (img[:, :, 0] < 120) & (img[:, :, 3] > 200)).sum())


check("The fall draws in as it's said (red appears on the word), then the recovery (green)",
      reds(before) < 50 and reds(after) > 500 and greens(before) < 50 and greens(after) > 500,
      f"red {reds(before)}->{reds(after)}, green {greens(before)}->{greens(after)}")

# ---------- the hook's pull-in zoom ----------
from core.face_tracker import CropBox, CropPlan, Shot, pull_in_scale  # noqa: E402
from core.video_renderer import render_clip  # noqa: E402

dots = np.full((H_, W_, 3), 40, np.uint8)
for x, y in [(700, 200), (1220, 200), (960, 900)]:
    dots[y - 5:y + 6, x - 5:x + 6] = 255
still = tmp / "dots.mp4"
write(still, [dots] * 150)
ass = tmp / "empty.ass"
ass.write_text("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n"
               "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
               "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
               "MarginR, MarginV, Encoding\nStyle: Default,Arial,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,"
               "100,100,0,0,1,1,0,2,10,10,10,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
               "MarginV, Effect, Text\n")
crop = CropBox(656, 0, 608, 1080)
zoomed = render_clip(still, ass, crop, 0.0, 6.0, tmp / "zoom.mp4", layout_mode="smart_crop", pull_in_until=4.5)
raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(zoomed), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                     capture_output=True).stdout
video = np.frombuffer(raw, np.uint8).reshape(-1, 1920, 1080).astype(np.float32)


def dot(img, x, y):
    win = np.clip(img[y - 45:y + 46, x - 45:x + 46] - 90, 0, None)
    yy, xx = np.mgrid[-45:46, -45:46]
    return x + (win * xx).sum() / win.sum(), y + (win * yy).sum() / win.sum()


track = np.array([dot(f, 1002, 355) for f in video])  # the dot at (1220, 200) in the source, top right of the crop
moves = np.diff(track[:, 0])
during = moves[: int(3.0 * RATE)]
check("Pull-in: the frame eases in (moves outward, slowing, never reversing)",
      during[0] > during[-1] >= -0.02 and (during > -0.05).all(), str(np.round(during[::10], 3)))
frames_n = np.arange(len(track))
eased = 1 - (1 - np.minimum(1, frames_n / (3.2 * RATE))) ** 3
gain = ((track[:, 0] - track[0, 0]) * eased)[: int(4.4 * RATE)].sum() / (eased[: int(4.4 * RATE)] ** 2).sum()
wobble = (track[:, 0] - track[0, 0] - gain * eased)[: int(4.4 * RATE)]
check("Pull-in: no sub-pixel jitter (the dot follows the eased path to a fraction of a pixel)",
      np.abs(wobble).max() < 0.3 and wobble.std() < 0.12, f"max {np.abs(wobble).max():.3f} px, std {wobble.std():.3f} px")
total = track[int(4.0 * RATE)] - track[0]
expected = (1220 - 960) * (1080 / 608) * 0.05
check("Pull-in: about 5% in (1.00x to 1.05x)", abs(total[0] - expected) < expected * 0.15, f"{total[0]:.1f} vs {expected:.1f}")
check("Pull-in: back to 1.00x at the first cut (hidden by it), never a second zoom motion",
      abs(track[int(5.0 * RATE)][0] - track[0][0]) < 1.0 and pull_in_scale(4.6, 4.5) == 1.0)
split_first = CropPlan(1920, 1080, 608, 1080, 0, (Shot(0, 3, 600, True, True, ((400.0, 300.0, 200.0), (1500.0, 300.0, 200.0))),
                                                 Shot(3, 10, 600, True, False)), cuts=(3.0,))
plain = CropPlan(1920, 1080, 608, 1080, 0, (Shot(0, 6, 600, True, False), Shot(6, 10, 650, True, False)), cuts=(6.0,))
check("Pull-in only on a plain shot of one person: never on a split or the whole frame",
      split_first.pull_in_until(0, 10) == 0.0 and plain.pull_in_until(0, 10) == 6.0,
      f"{split_first.pull_in_until(0, 10)}, {plain.pull_in_until(0, 10)}")

# ---------- the move into a split view starts from exactly the picture on screen ----------
from core.dynamic_layout import _move_frame, band_rect  # noqa: E402

gradient = np.zeros((H_, W_, 3), np.uint8)
gradient[:, :, 0] = np.linspace(0, 255, W_)[None, :]
gradient[:, :, 1] = np.linspace(0, 255, H_)[:, None]
base_rect = (656.0, 0.0, 608.0, 1080.0)
import cv2  # noqa: E402

base_frame = cv2.resize(gradient[:, 656:1264], (1080, 1920), interpolation=cv2.INTER_LANCZOS4)
start = _move_frame(gradient, base_frame, None, 0.0, base_rect)
end = _move_frame(gradient, base_frame, None, 1.0, base_rect)
check("The move's first frame is the full-screen picture itself (no jump into the move)",
      float(np.abs(start[:1800].astype(int) - base_frame[:1800].astype(int)).mean()) < 2.0)
sx, sy, sw, sh = band_rect(W_, H_, base_rect[0] + base_rect[2] / 2)  # Round 39: the face-centred 1080 x 1040 viewport
band = cv2.resize(gradient[int(sy):int(sy + sh), int(sx):int(sx + sw)], (1080, 1040), interpolation=cv2.INTER_AREA)
check("...and its last frame is the speaker's viewport (top 54%, around the face) over the panel",
      float(np.abs(end[:1030].astype(int) - band[:1030].astype(int)).mean()) < 3.0
      and tuple(int(v) for v in end[1800, 540]) == tuple(int(__import__("core.cards", fromlist=["CANVAS"]).CANVAS[k:k + 2], 16) for k in (1, 3, 5)))

# ---------- frame times survive a decoder message cutting into FFmpeg's log ----------
true_times = [round(k * 0.04, 2) for k in range(40)]
picked_true = [t for k, t in enumerate(true_times) if k % 13 == 0]  # every 13th frame at 25 fps (the 0.499 s step)
log_lines = ["[Parsed_showinfo_1 @ 0x5567] config in time_base: 1/12800, frame_rate: 25/1",
             "[Parsed_showinfo_5 @ 0x5568] config in time_base: 1/12800, frame_rate: 25/1"]
for k, t in enumerate(true_times):
    info = f"n:{k:4d} pts:{int(t * 12800):7d} pts_time:{t:<7} duration:    512 duration_time:0.04    fmt:yuv420p"
    # frame 7 of the cut branch: an h264 warning without a line end took the line's start (and its prefix)
    log_lines.append(f"[h264 @ 0x55aa] Late SEI{info}" if k == 7 else f"[Parsed_showinfo_1 @ 0x5567] {info}")
    log_lines.append("[Parsed_showinfo_1 @ 0x5567] checksum:DE85FDC1 plane_checksum:[281B713E] mean:[151 127 132]")
    if k % 13 == 0:
        sample = f"n:{k // 13:4d} pts:{int(t * 12800):7d} pts_time:{t:<7} duration:    512 duration_time:0.04"
        log_lines.append(f"[h264 @ 0x55aa] Late SEI{sample}" if k == 26 else f"[Parsed_showinfo_5 @ 0x5568] {sample}")
garbled = "\n".join(log_lines)
check("Frame times: a decoder warning cutting into a showinfo line no longer throws the timing away",
      F._frame_times(garbled, 0) == true_times and F._frame_times(garbled, 1) == picked_true,
      f"{len(F._frame_times(garbled, 0))} cut times, {F._frame_times(garbled, 1)}")
check("...and if the times are ever lost, the video's own frame rate is used (25 fps here, not 30)",
      F._log_frame_rate(garbled) == 25.0)
check("...the face samples' times come from the same pick the select filter makes",
      F._selected_times(true_times) == picked_true and F._selected_times([k / 30 for k in range(31)]) == [0.0, 0.5, 1.0])

# ---------- the steady split runs at the camera's own frame rate (25 fps: no repeated frames) ----------
from core.dynamic_layout import compose_cards  # noqa: E402

moving = tmp / "moving25.mp4"
plain25 = tmp / "base25.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=214",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(moving)], check=True)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xC04040:s=1080x1920:r=25:d=18",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(plain25)], check=True)
made25 = compose_cards(plain25, moving, ClipEdit(195.0, 213.0, cards=[curve]), [frames_curve], [], tmp / "split25.mp4")
steady_from = curve.start - 195.0 + 0.6
steady_to = curve.end - 195.0 - 0.6
raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{steady_from:.2f}", "-t", f"{steady_to - steady_from:.2f}",
                      "-i", str(made25), "-vf", "crop=1080:440:0:0,scale=270:110", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                     capture_output=True, check=True).stdout
strip = np.frombuffer(raw, np.uint8).reshape(-1, 110, 270).astype(int)
repeats = int((np.abs(np.diff(strip, axis=0)).mean(axis=(1, 2)) < 0.05).sum())
check("Steady split at 25 fps: the speaker's picture never repeats a frame (no judder)",
      len(strip) > 100 and repeats == 0, f"{repeats} repeated of {len(strip)}")

print(f"\n{'All' if not fails else fails} {'checks passed' if not fails else 'check(s) failed'}")
sys.exit(1 if fails else 0)
