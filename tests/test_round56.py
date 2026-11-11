"""Round 56: hardening from the trending-repo review (Nov 2026).

1. Naughty strings (from the "big list of naughty strings"): channel names, titles, transcripts and
   speaker names go into .ass caption files, .srt files, email subjects and clip-page names. Markup
   and caption-tag injection were already blocked; control characters (NUL, escape codes) and
   Unicode direction overrides now are too (core.plain_text).
2. The clip-page Worker (cloudflare/clip-pages-worker.js), audited:
   - "social" (copied the Shorts & Reels caption) and "call" (Request a call) events were sent by
     the page and read by the app since Round 50 but dropped by the Worker: now recorded.
   - Every HTML page refuses framing (clickjacking) and MIME sniffing.
   - Beacons posted from another site (faked prospect activity) are dropped.
3. The offer page describes clips the way Round 55 cuts them (35 s to 3 min, idea-complete).

Run: timeout 300 .venv/bin/python tests/test_round56.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["NAV_NO_CLIP_PAGES"] = "1"
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


from core import clip_pages, executive, outreach, srt_export, title_card  # noqa: E402
from core.plain_text import strip_controls  # noqa: E402
from core.transcriber import Word  # noqa: E402

NAUGHTY = ["", " ", "null", "${7*7}", "{{7*7}}", "%s%n", "{\\b1}bold{\\b0}", "{\\pos(0,0)\\fs300}HUGE", "\\N\\h",
           "Dialogue: 0,0:00:00.00,9:00:00.00,Exec,,0,0,0,,x", "line1\nline2", "a\r\nb", "nul\x00byte", "\x1b[31mred",
           "\x0bv\x0cf", "<script>alert(1)</script>", "\"><img src=x onerror=alert(1)>", "../../etc/passwd", "$(touch /tmp/x)",
           "a:b'c\\d;e[f]g", "‮evil‬", "﻿bom", "zero​width", "田中さんにあげて下さい", "مرحبا", "👨‍👩‍👧‍👦🇨🇦",
           "A" * 3000, "1\n00:00:00,000 --> 00:00:09,000\ninjected cue"]
BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f‪-‮⁦-⁩﻿]")

# ============================== 1. Naughty strings ==============================
check("strip_controls: control characters and direction overrides out, joiners and scripts kept",
      strip_controls("a\x00b\x1b[1m‮c‬") == "a b [1mc" and strip_controls("👨‍👩‍👧") == "👨‍👩‍👧"
      and strip_controls("مرحبا") == "مرحبا" and strip_controls("x\ny") == "x y"
      and strip_controls("x\r\ny", keep_newlines=True) == "x\ny")

tmp = Path(tempfile.mkdtemp(prefix="round56_"))
ass_bad, srt_bad, injected, subject_bad, names_bad, crashes = [], [], [], [], [], []
for k, s in enumerate(NAUGHTY):
    words = [Word(text="Start.", start=0.1, end=0.5), Word(text=s, start=0.6, end=1.2), Word(text="end.", start=1.3, end=1.8)]
    try:
        ass = executive.write_ass(words, tmp / f"c{k}.ass", 3.0, hook_title=s, hook_seconds=2.0,
                                  lower_third=executive.LowerThird(name=s or "Jane Doe", role=s, start=0.0, end=2.5))
        body = ass.read_text(encoding="utf-8")
        srt = srt_export.to_srt(srt_export.cues(words))
        subject = outreach.build_subject(outreach.clean_short_topic(s), "episode", 3, show=s, business=True)
        email = outreach.build_outreach_email(s, video_title=s, clip_count=3, business=True)
        page = clip_pages.make_page_name(s)
        title_card.clean_title(s)
    except Exception as exc:  # noqa: BLE001
        crashes.append((s[:20], repr(exc)))
        continue
    if BAD.search(body):
        ass_bad.append(s[:20])
    events = [line for line in body.split("\n") if line.startswith("Dialogue:")]
    if any("\\fs300" in line or "}bold{" in line for line in events) or any(
            line.startswith("Dialogue: 0,0:00:00.00,9:00:00.00") for line in body.split("\n")):
        injected.append(s[:20])
    if BAD.search(srt) or any(not block.split("\n")[0].isdigit() for block in srt.strip().split("\n\n") if block):
        srt_bad.append(s[:20])
    if BAD.search(subject) or "\n" in subject or BAD.search(email.replace("\n", "")):
        subject_bad.append(s[:20])
    if not re.fullmatch(r"[a-z0-9-]{6,80}", page):
        names_bad.append((s[:20], page))
check("No text crashes a caption file, .srt, title, page name or email", not crashes, crashes)
check("Caption files (.ass) carry no control characters or direction overrides", not ass_bad, ass_bad)
check("...and no caption tag or extra caption line can be injected", not injected, injected)
check(".srt files: every cue well-formed, no control characters (no injected cues)", not srt_bad, srt_bad)
check("Email subjects and bodies: no control characters or line breaks in the subject", not subject_bad, subject_bad)
check("Clip-page names always match the Worker's pattern", not names_bad, names_bad)
if shutil.which("ffmpeg"):
    sample = [k for k, s in enumerate(NAUGHTY) if s in ("nul\x00byte", "‮evil‬", "{\\pos(0,0)\\fs300}HUGE", "👨‍👩‍👧‍👦🇨🇦")]
    rendered = [subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=1", "-vf",
                                f"subtitles={tmp / f'c{k}.ass'}", "-frames:v", "1", "-f", "null", "-"],
                               capture_output=True, timeout=60).returncode == 0 for k in sample]
    check("...and libass renders them", all(rendered), rendered)
shutil.rmtree(tmp, ignore_errors=True)

# ============================== 2. The clip-page Worker ==============================
worker = (ROOT / "cloudflare" / "clip-pages-worker.js").read_text()
events = set(re.findall(r'"(\w+)"', re.search(r"const EVENTS = new Set\(\[([^\]]+)\]\)", worker).group(1)))
check("Worker records every event the app reads (social and call were dropped)",
      events == set(clip_pages.EVENT_TYPES), sorted(events ^ set(clip_pages.EVENT_TYPES)))
check("Every event the page sends is accepted", all(t in events for t in
      set(re.findall(r'send\("(\w+)"', worker)) | set(re.findall(r'data-event="(\w+)"', worker)) | {"srt", "cover"}))
check("HTML pages refuse framing and MIME sniffing (clip page, offer, not found, owner)",
      worker.count("...SECURITY_HEADERS") >= 4 and "frame-ancestors 'none'" in worker and '"x-frame-options": "DENY"' in worker)
check("Files are served with nosniff", 'headers.set("x-content-type-options", "nosniff")' in worker)
check("Beacons from another site are dropped", "origin !== new URL(request.url).origin" in worker)
check("Offer page: clips run 35 seconds to 3 minutes and end when the point lands",
      "35 seconds to 3 minutes" in worker and "starts and ends on a complete sentence" not in worker)

node = shutil.which("node")
if node:
    work = Path(tempfile.mkdtemp(prefix="round56_worker_"))
    (work / "worker.mjs").write_text(worker)
    (work / "run.mjs").write_text("""
import worker from "./worker.mjs";
const store = new Map([["acme-show-k7m2qx/page.json", JSON.stringify({ channel_title: "Acme <Show>", video_title: "x",
  clips: [{ start: 10, end: 120, post: "Hook <b>x</b>" }] })]]);
const puts = [];
const env = { CLIPS: {
  async get(k) { const v = store.get(k); return v === undefined ? null : { json: async () => JSON.parse(v) }; },
  async head(k) { return store.has(k) ? {} : null; },
  async put(k) { puts.push(k.split("/")[2].split("_")[1]); } } };
const R = (p, i = {}) => worker.fetch(new Request("https://clips.navstudiolab.com" + p, i), env);
const page = await R("/acme-show-k7m2qx"), html = await page.text();
const ua = { "user-agent": "Mozilla/5.0 Safari", "cf-connecting-ip": "203.0.113.9" };
const post = (t, extra = {}) => R("/e", { method: "POST", headers: { ...ua, ...extra }, body: JSON.stringify({ s: "acme-show-k7m2qx", t, c: 1 }) });
await post("call", { origin: "https://clips.navstudiolab.com" }); await post("social"); await post("open", { origin: "https://evil.example" });
await post("bogus");
console.log(JSON.stringify({ xfo: page.headers.get("x-frame-options"), sniff: page.headers.get("x-content-type-options"),
  escaped: html.includes("Acme &lt;Show&gt;") && !html.includes("<b>x</b>"), puts }));
""")
    out = subprocess.run([node, "run.mjs"], cwd=work, capture_output=True, text=True, timeout=60)
    import json
    try:
        got = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        got = {}
    check("Worker (run in node): call and social recorded; another site's beacon and unknown events dropped",
          got.get("puts") == ["call", "social"], out.stdout[-300:] + out.stderr[-300:])
    check("...the page is served with DENY / nosniff and still escapes the channel name and posts",
          got.get("xfo") == "DENY" and got.get("sniff") == "nosniff" and got.get("escaped"), got)
    shutil.rmtree(work, ignore_errors=True)
else:
    print("SKIP  node not installed: the Worker run checks need it")

print(f"\n{len(FAILED)} check(s) failed" if FAILED else "\nAll checks passed")
sys.exit(1 if FAILED else 0)
