"""Round 59: Narrate mode. Your script + your voiceover + bought gameplay -> finished vertical clips.

Steps (each is one command):

  1. Add footage to the library (once per Fiverr delivery):
       python tools/narrate.py ingest ~/Footage/gta6
     Folder and file names become tags, so name folders by what's in them
     (beach_sunset/, city_night_driving/). GEMINI_API_KEY in .env adds AI scene tags.

  2. Write a script (or write your own .txt in the format of core/narrate/script.py):
       python tools/narrate.py script --topic "Don't sell cars at the first garage" --format tips \
           --brief facts.txt --out scripts/garage.txt

  3. Record yourself reading it (scripts/garage.txt, the lines without the [tags]) as a .wav/.m4a/.mp3.

  4. Render:
       python tools/narrate.py render scripts/garage.txt --voice takes/garage.m4a
     Several at once:  python tools/narrate.py render scripts/*.txt --voice-dir takes/
     (each script's voiceover is the file in takes/ with the same name).

  Library check:  python tools/narrate.py library
Finished clips land in storage/output/narrate/<name>_final.mp4.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Narrate clips are not B2B podcast clips: no LinkedIn copy, no data cards, no lower third,
# no climax teaser (the hook is written), no mid-clip trimming of your voiceover's pauses.
# Creator captions (bold), not the LinkedIn executive style.
for key, value in {"LINKEDIN_POSTS": "0", "AUTO_GRAPHICS": "0", "VISUAL_OVERLAYS": "none",
                   "ENABLE_CLIMAX_HOOK": "0", "MIDCLIP_TRIM": "0", "NAV_NO_CLIP_PAGES": "1",
                   "CAPTION_STYLE": "bold"}.items():
    os.environ.setdefault(key, value)

LIBRARY_DB = ROOT / "storage" / "narrate" / "library.sqlite"
OUT_DIR = ROOT / "storage" / "output" / "narrate"
AUDIO_EXTENSIONS = (".wav", ".m4a", ".mp3", ".aac", ".flac", ".ogg")
MAX_REUSE = 0.4  # refuse a render when more than this share of its shots were in the last 10 videos


def cmd_ingest(args) -> None:
    from core.narrate import tagger
    from core.narrate.library import Library

    lib = Library(LIBRARY_DB)
    tag = None if args.no_ai else tagger.gemini_tagger()
    print("AI tags: " + ("on (Gemini)" if tag else "off (names, brightness and motion only)"))
    stats = lib.ingest(args.folder, tagger=tag)
    print(f"Added {stats['shots']} shots from {stats['files']} files "
          f"({stats['static']} static pieces dropped, {stats['skipped_files']} files already in the library).")


def cmd_library(_args) -> None:
    from collections import Counter

    from core.narrate.library import Library

    shots = Library(LIBRARY_DB).shots()
    seconds = sum(s.duration for s in shots)
    print(f"{len(shots)} shots, {seconds / 60:.1f} minutes of footage")
    for tag, n in Counter(t for s in shots for t in s.tags).most_common(30):
        print(f"  {tag:<14} {n}")


def cmd_script(args) -> None:
    from core.narrate import script as S

    brief = Path(args.brief).read_text() if args.brief else ""
    made = S.write(args.topic, args.format, brief)
    text = "\n".join(("HOOK: " if l.hook else "") + "".join(f"[{', '.join(l.tags)}] " if l.tags else "")
                     + "".join(f"[{m.kind}{': ' + chr(34) + m.value + chr(34) if m.value else ''}] " for m in l.markers)
                     + l.text for l in made.lines)
    text += "".join(f"\nSOURCE: {s}" for s in made.sources)
    out = Path(args.out) if args.out else None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n")
        print(f"Saved {out}")
    print(text)
    for problem in S.check(made):
        print("CHECK: " + problem)


def _voice_for(script_path: Path, args) -> Path:
    if args.voice:
        return Path(args.voice)
    folder = Path(args.voice_dir or script_path.parent)
    for ext in AUDIO_EXTENSIONS:
        candidate = folder / (script_path.stem + ext)
        if candidate.is_file():
            return candidate
    raise SystemExit(f"No voiceover for {script_path.name}: expected {folder}/{script_path.stem}.wav (or .m4a/.mp3)")


def render_one(script_path: Path, voice: Path, args) -> Path:
    from config import load_settings
    from core.hook_selector import ClipResult
    from core.narrate import align as A
    from core.narrate import script as S
    from core.narrate.assemble import assemble
    from core.narrate.finish import finish
    from core.narrate.library import Library
    from core.pipeline import render_selected_clip

    settings = load_settings()
    script = S.parse(script_path.read_text())
    for problem in S.check(script):
        print(f"  note: {problem}")
    name = script_path.stem
    work = OUT_DIR / "work" / name
    work.mkdir(parents=True, exist_ok=True)
    words = A.align(voice, script, settings)
    spans = A.line_spans(script, words)
    lib = Library(LIBRARY_DB)
    made = assemble(script, spans, lib, voice, work / f"{name}_assembled.mp4", work,
                    game_audio_db=None if args.no_game_audio else args.game_db)
    share = lib.reuse_share(made.shot_ids)
    if share > MAX_REUSE and not args.allow_reuse:
        raise SystemExit(f"{name}: {share:.0%} of its shots were in the last 10 videos. Add footage, or pass "
                         "--allow-reuse.")
    clip = ClipResult(start_time=0.0, end_time=made.duration, viral_title=script.hook, caption=script.hook,
                      hashtags=["GTA6", "GTAVI"], virality_score=80)
    rendered = render_selected_clip(made.video, clip, words, settings, OUT_DIR / f"{name}.mp4",
                                    layout_mode=args.layout, work_dir=work, title_fade_in=False,
                                    climax_hook_on=False)
    final = finish(rendered.path, made.cues, rendered.output_time, OUT_DIR / f"{name}_final.mp4")
    lib.record_render(name, made.shot_ids)
    return final


def cmd_render(args) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    done, failed = [], []
    for path in map(Path, args.scripts):
        try:
            final = render_one(path, _voice_for(path, args), args)
            done.append(final)
            print(f"OK   {path.name} -> {final}")
        except SystemExit as exc:
            failed.append(path.name)
            print(f"SKIP {exc}")
        except Exception as exc:  # noqa: BLE001 - one bad script never stops the batch
            logging.exception("Render failed for %s", path.name)
            failed.append(path.name)
            print(f"FAIL {path.name}: {exc}")
    print(f"\n{len(done)} clips made" + (f", {len(failed)} failed: {', '.join(failed)}" if failed else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="add a footage folder (or file) to the library")
    p.add_argument("folder")
    p.add_argument("--no-ai", action="store_true", help="skip Gemini scene tags")
    p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("library", help="what's in the library")
    p.set_defaults(fn=cmd_library)
    p = sub.add_parser("script", help="write a script with the model")
    p.add_argument("--topic", required=True)
    p.add_argument("--format", default="tips", choices=["tips", "news", "details", "comparison"])
    p.add_argument("--brief", default="", help="a .txt of facts and source links the script may use")
    p.add_argument("--out", default="")
    p.set_defaults(fn=cmd_script)
    p = sub.add_parser("render", help="script(s) + voiceover(s) -> finished clips")
    p.add_argument("scripts", nargs="+")
    p.add_argument("--voice", default="", help="the voiceover (one script only)")
    p.add_argument("--voice-dir", default="", help="folder of voiceovers named like the scripts")
    p.add_argument("--layout", default="blurred_background", choices=["blurred_background", "wide_blurred_background",
                                                                       "smart_crop"])
    p.add_argument("--game-db", type=float, default=-20.0, help="game sound under your voice, in dB")
    p.add_argument("--no-game-audio", action="store_true")
    p.add_argument("--allow-reuse", action="store_true")
    p.set_defaults(fn=cmd_render)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.cmd == "render" and args.voice and len(args.scripts) > 1:
        raise SystemExit("--voice is for one script; use --voice-dir for several.")
    args.fn(args)


if __name__ == "__main__":
    main()
