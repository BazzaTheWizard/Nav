"""Round 64: find streamers to pitch. Who is live on GTA right now with 30-400 viewers and a
public past broadcast to cut sample clips from.

  python tools/find_leads.py
  python tools/find_leads.py --game "Red Dead Redemption 2" --min 50 --max 300
  python tools/find_leads.py --no-default-games --game "Fortnite"
  python tools/find_leads.py --mark some_login contacted --note "emailed 19 Nov"

What it does:
  1. finds each game's Twitch category (GTA VI and GTA V by default: GTA RP streams are under V;
     add more with --game). A game Twitch has no category for yet is reported and skipped;
  2. lists the live English streams of each, largest first, and stops once viewers fall below --min;
  3. for each streamer in the range: their newest public past broadcast (streamers who keep none
     are skipped, and counted), their channel type and any email or Discord invite written in
     their channel description (nothing is scraped);
  4. saves them to storage/leads/leads.sqlite with a snapshot of this run, so avg_viewers builds
     up over repeated runs, and your status and notes per streamer are kept;
  5. prints a table and writes storage/leads/leads_<date>.csv. Streamers you marked contacted,
     replied, client or skip are listed last and flagged, never left out.

Needs TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env (an app from dev.twitch.tv/console/apps).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import leads as LD  # noqa: E402


def run(helix: LD.Helix, *, games: list[str], lo: int, hi: int, language: str = "en", db: Path = LD.DB_PATH,
        out: Path | None = None, now: float | None = None, say=print) -> dict:
    """One search. Returns {"leads": rows, "csv": path, "missing": [games with no category], ...}."""
    now = time.time() if now is None else now
    when = LD.iso(now)
    found: list[LD.Game] = []
    missing: list[str] = []
    for name in games:
        game = LD.resolve_game(helix, name)
        if game is None:
            missing.append(name)
            say(f"No Twitch category for \"{name}\" yet: skipped, continuing with the rest.")
        elif game.id not in {g.id for g in found}:
            found.append(game)
            say(f"Game: {game.name} (category {game.id})")
    stats: dict = {}
    leads = LD.find(helix, found, lo, hi, language, stats, progress=say) if found else []
    con = LD.connect(db)
    try:
        LD.save(con, leads, when)
        shown = LD.rows(con, run=when, now=now) if leads else []
    finally:
        con.close()
    path = LD.write_csv(shown, out or (Path(db).parent / f"leads_{time.strftime('%Y-%m-%d', time.localtime(now))}.csv"))
    return {"leads": shown, "csv": path, "missing": missing, "games": found, "stats": stats}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", action="append", default=[], help="another game to search (repeatable)")
    ap.add_argument("--no-default-games", action="store_true", help="only the --game ones, not GTA VI and GTA V")
    ap.add_argument("--min", type=int, default=LD.MIN_VIEWERS, dest="lo", help="fewest live viewers (default 30)")
    ap.add_argument("--max", type=int, default=LD.MAX_VIEWERS, dest="hi", help="most live viewers (default 400)")
    ap.add_argument("--language", default="en", help='stream language (default en; "" for all)')
    ap.add_argument("--db", default=str(LD.DB_PATH))
    ap.add_argument("--out", default="", help="the CSV to write (default storage/leads/leads_<date>.csv)")
    ap.add_argument("--mark", nargs=2, metavar=("LOGIN", "STATUS"),
                    help=f"set a lead's status ({' | '.join(LD.STATUSES)}) and stop")
    ap.add_argument("--note", default=None, help="with --mark: the note to save")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.mark:
        con = LD.connect(args.db)
        try:
            ok = LD.set_status(con, args.mark[0], args.mark[1], args.note)
        except ValueError as exc:
            raise SystemExit(str(exc))
        finally:
            con.close()
        print(f"{args.mark[0]}: {args.mark[1]}" if ok else f"No saved lead called {args.mark[0]}.")
        raise SystemExit(0 if ok else 1)

    if args.lo > args.hi:
        raise SystemExit("--min must not be above --max.")
    games = ([] if args.no_default_games else list(LD.DEFAULT_GAMES)) + [g.strip() for g in args.game if g.strip()]
    if not games:
        raise SystemExit("No games to search: give --game, or drop --no-default-games.")
    try:
        helix = LD.Helix()
        result = run(helix, games=games, lo=args.lo, hi=args.hi, language=args.language, db=Path(args.db),
                     out=Path(args.out) if args.out else None)
    except LD.HelixError as exc:
        raise SystemExit(f"Twitch: {exc}")
    stats, leads = result["stats"], result["leads"]
    if not result["games"]:
        raise SystemExit("None of the games has a Twitch category. Nothing searched.")
    no_vod = stats.get("no_vod") or []
    print(f"\n{stats.get('streams_seen', 0)} streams listed over {stats.get('pages', 0)} pages, "
          f"{stats.get('in_range', 0)} with {args.lo}-{args.hi} viewers, {len(no_vod)} skipped for having no public "
          f"past broadcast, {len(leads)} leads. ({helix.requests} Twitch requests)")
    print()
    print(LD.table(leads))
    handled = sum(1 for lead in leads if lead["status"] in LD.HANDLED)
    expiring = sum(1 for lead in leads if "expiring" in lead["vod_note"])
    print(f"\n{len(leads) - handled} new, {handled} already handled (listed last)."
          + (f" {expiring} have a past broadcast that Twitch deletes soon: clip those first." if expiring else ""))
    print(f"CSV: {result['csv']}")


if __name__ == "__main__":
    main()
