"""Round 58: cut clips to a client's brief (core.brief), render them, brand them (core.brand_outro).

    .venv/bin/python tools/brief_clips.py --brief storage/briefs/district_consulting.json --video workshop.mp4 --dry-run
    .venv/bin/python tools/brief_clips.py --brief storage/briefs/district_consulting.json --video workshop.mp4

--dry-run    pick and write the review report only (picks.md); nothing is rendered. Read it first.
--override   your own times for a theme, e.g. --override 2=812.4-845.0 (snapped to whole sentences
             that end on a complete statement); repeatable.
--hook       your own hook for a theme, e.g. --hook "1=AI won't replace your strategy"; repeatable.
--only       render just these themes, e.g. --only 2,3 (the others keep their last picks).

The transcript is cached in the output folder (transcript.json), so re-runs don't transcribe again.
Picks are saved (picks.json); a render run reuses them unless --repick or an --override changes a theme.
Output: <out>/clip_<n>_final.mp4 (+ .srt) for each theme, picks.md (what was picked and why).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import brief as B  # noqa: E402


def _times(items: list[str]) -> dict[int, tuple[float, float]]:
    out = {}
    for item in items:
        m = re.fullmatch(r"\s*(\d+)\s*=\s*([\d.:]+)\s*-\s*([\d.:]+)\s*", item)
        if not m:
            raise SystemExit(f"--override {item!r}: use THEME=START-END, e.g. 2=812.4-845 or 2=13:32.4-14:05")
        out[int(m.group(1)) - 1] = (_seconds(m.group(2)), _seconds(m.group(3)))
    return out


def _seconds(text: str) -> float:
    parts = [float(p) for p in text.split(":")]
    return sum(p * 60 ** k for k, p in enumerate(reversed(parts)))


def _stamp(seconds: float) -> str:
    return f"{int(seconds // 60)}:{seconds % 60:05.2f}"


def transcript(video: Path, out: Path) -> list:
    cache = out / "transcript.json"
    if cache.is_file():
        return B.load_words(cache)
    from config import load_settings
    from core.downloader import extract_audio_only
    from core.transcriber import transcribe_audio

    wav = out / "audio.wav"
    extract_audio_only(video, wav)
    words = transcribe_audio(wav, load_settings())
    B.save_words(words, cache)
    wav.unlink(missing_ok=True)
    return words


def report(brief: B.Brief, picks: list, out: Path, finals: dict[int, dict] | None = None) -> Path:
    lines = [f"# {brief.client}: brief clips", ""]
    for n, p in enumerate(picks, 1):
        lines.append(f"## Clip {n}: {brief.themes[n - 1].title}")
        if p is None:
            lines += ["**No valid clip found.** Try --override with times you choose.", ""]
            continue
        lines += [f"- **Hook (first {brief.hook_seconds:.0f} s):** {p.hook or '(write one: --hook)'}",
                  f"- **Source:** {_stamp(p.start)} to {_stamp(p.end)} ({p.seconds:.1f} s of speech"
                  f" + {brief.end_card_seconds:.0f} s end card)",
                  f"- **Ends on:** \"{p.last_sentence}\"",
                  f"- **Model's check:** on theme {p.on_theme}/10, standalone {p.standalone}/10",
                  f"- **Why:** {p.why}"]
        if finals and n in finals:
            f = finals[n]
            lines.append(f"- **Final file:** {f['file']} ({f['seconds']:.1f} s){'' if f['ok'] else ' — OUTSIDE ' + f'{brief.min_seconds:.0f}-{brief.max_seconds:.0f} s'}")
        lines += [f"- **Note:** {x}" for x in p.notes]
        lines += ["", f"> {p.text}", ""]
    path = out / "picks.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brief", required=True)
    ap.add_argument("--video", required=True, help="a local .mp4/.mov, or a video URL")
    ap.add_argument("--out", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repick", action="store_true")
    ap.add_argument("--override", action="append", default=[])
    ap.add_argument("--hook", action="append", default=[])
    ap.add_argument("--only", default="")
    ap.add_argument("--layout", default="auto")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    brief = B.Brief.load(args.brief)
    slug = re.sub(r"[^a-z0-9]+", "-", brief.client.lower()).strip("-")
    out = Path(args.out or ROOT / "storage" / "output" / "briefs" / slug).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.video.startswith(("http://", "https://")):
        from core.downloader import download_source_video

        video = Path(download_source_video(args.video, str(out / "source.mp4")))
    else:
        video = Path(args.video).expanduser().resolve(strict=True)
    words = transcript(video, out)
    overrides = _times(args.override)
    lo, hi = brief.speech_range()

    saved = out / "picks.json"
    picks: list = []
    if saved.is_file() and not args.repick:
        picks = [B.Pick(**p) if p else None for p in json.loads(saved.read_text())]
        if len(picks) != len(brief.themes):
            picks = []
    redo = sorted(set(overrides) | ({k for k, p in enumerate(picks) if p is None} if picks else set()))
    if not picks:
        picks = B.pick(words, brief.themes, min_s=lo, max_s=hi, overrides=overrides)
    elif redo:
        fresh = B.pick(words, brief.themes, min_s=lo, max_s=hi,
                       overrides={**{k: (p.start, p.end) for k, p in enumerate(picks) if p and k not in redo}, **overrides})
        picks = [fresh[k] if k in redo else p for k, p in enumerate(picks)]
    for item in args.hook:
        k, _, text = item.partition("=")
        hook = B.clean_hook(text)
        if not hook:
            raise SystemExit(f"--hook {item!r}: the hook must be {B.HOOK_WORDS[0]}-{B.HOOK_WORDS[1]} words")
        if picks[int(k) - 1] is not None:
            picks[int(k) - 1].hook, picks[int(k) - 1].notes = hook, []
    saved.write_text(json.dumps([dataclasses.asdict(p) if p else None for p in picks], indent=2))
    print(f"\nReview: {report(brief, picks, out)}")
    if args.dry_run:
        return

    from config import load_settings
    from core import brand_outro, executive
    from core.hook_selector import ClipResult
    from core.pipeline import render_selected_clip

    executive.TOPIC_SECONDS = brief.hook_seconds  # the hook header: first N seconds, then its fade
    settings = load_settings()
    only = {int(x) for x in args.only.split(",") if x.strip()} or set(range(1, len(picks) + 1))
    finals: dict[int, dict] = {}
    for n, p in enumerate(picks, 1):
        if p is None or n not in only:
            continue
        if not p.hook:
            print(f"Clip {n}: no hook yet; add one with --hook \"{n}=...\" and run again.")
            continue
        clip = ClipResult(start_time=p.start, end_time=p.end, viral_title=p.hook, caption=p.why,
                          hashtags=[], virality_score=90)
        made = render_selected_clip(video, clip, words, settings, out / f"clip_{n}.mp4", layout_mode=args.layout,
                                    work_dir=out / "work", title_fade_in=False, climax_hook_on=False)
        final = brand_outro.finish(made.path, brief.client, brief.cta, sub=brief.cta_sub, accent=brief.accent,
                                   logo=brief.logo, card_seconds=brief.end_card_seconds)
        seconds, _rate = brand_outro._probe(final)
        finals[n] = {"file": final.name, "seconds": seconds,
                     "ok": brief.min_seconds <= seconds <= brief.max_seconds}
        print(f"Clip {n}: {final} ({seconds:.1f} s)")
    print(f"\nReview: {report(brief, picks, out, finals)}")


if __name__ == "__main__":
    main()
