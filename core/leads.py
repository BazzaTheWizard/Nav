"""Round 64: the streamer lead finder. Who is live on a game right now, small enough to want a
clipper and big enough to pay for one, with a public VOD to cut pitch samples from.

  Helix          Twitch's API with an app access token (client credentials). The token is cached
                 until it expires, renewed once on a 401, and 429s wait for Ratelimit-Reset.
  resolve_game   a typed name -> a category (search/categories, then confirmed with /games)
  live_streams   /streams for a game, English, by viewers; stops once viewers fall below the range
  find           streams -> /users (type, description -> contact hints) -> /videos (latest archive)
  the database   storage/leads/leads.sqlite: streamers (with your status and notes) and one
                 snapshot per streamer per run, so avg_viewers builds up over repeated runs
  rows / csv     what tools/find_leads.py prints and saves, and what the GTA tab shows

Endpoints and fields were checked against dev.twitch.tv/docs/api/reference on 2026-11-16:
- POST id.twitch.tv/oauth2/token, form-encoded client_id, client_secret, grant_type ->
  access_token, expires_in, token_type.
- GET /streams (game_id, language, first <= 100, after): "in descending order by the number of
  viewers"; pagination is {} on the last page; a page can be short, or repeat a stream.
- GET /videos (user_id, type=archive, first): url, created_at, duration like "3h21m15s"
  ("leading zero units left out"), stream_id.
- GET /users (id repeated, 100 at most): broadcaster_type "partner" | "affiliate" | "", description.
  The email field is only ever the token owner's, so contact hints come from the description.
- GET /search/categories (query) and GET /games (id): id, name.
- Ratelimit-Reset: "a Unix epoch timestamp that identifies when your bucket is reset to full".

The client secret is only ever sent to id.twitch.tv in the token request's body. It is never
logged, printed, put in an error message or written to disk.
"""
from __future__ import annotations

import csv
import difflib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
LEADS_DIR = ROOT / "storage" / "leads"
# NAV_LEADS_DB: another database (the tests' own; like NAV_MADE_CLIPS for the Sent list)
DB_PATH = Path(os.getenv("NAV_LEADS_DB") or LEADS_DIR / "leads.sqlite")
TOKEN_CACHE = LEADS_DIR / "token.json"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX_URL = "https://api.twitch.tv/helix"

DEFAULT_GAMES = ("Grand Theft Auto VI", "Grand Theft Auto V")  # GTA RP streams are under V
MIN_VIEWERS, MAX_VIEWERS = 30, 400
TIER_SPLIT = 150  # below: "growing"; from here up: "established"
STATUSES = ("new", "contacted", "replied", "client", "skip")
HANDLED = ("replied", "contacted", "client", "skip")  # listed last, flagged, never dropped
EXPIRING_DAYS = 5.0  # an affiliate's VOD is deleted after about 7 days
PARTNER_VOD_DAYS = 60.0  # a partner's after 60: theirs is flagged in its last 5 days instead
PAGE = 100  # the most /streams, /videos and /users give per request
MAX_PAGES = 60  # 6,000 streams of one game: a stop for a cursor that never ends
TIMEOUT = 20
MAX_RATE_WAITS = 4
MAX_RATE_WAIT_SECONDS = 120.0
TOKEN_MARGIN = 60.0  # a cached token is dropped this long before it expires

CSV_COLUMNS = ("username", "live_viewers", "avg_viewers", "tier", "game", "stream_title", "latest_vod_url",
               "vod_duration", "vod_age_days", "channel_url", "contact_hints", "status")
EXTRA_COLUMNS = ("vod_note", "broadcaster_type", "snapshots", "notes")  # after the ones above


class HelixError(RuntimeError):
    """Twitch said no, in words safe to show (never the secret or a token)."""


# ------------------------------------------------------------------ Helix
class Helix:
    """GET requests to Helix with an app access token.

    ``http`` is anything with requests' ``request(method, url, params=, data=, headers=,
    timeout=)`` (tests pass a fake); ``sleep`` and ``now`` are injectable for the same reason."""

    def __init__(self, client_id: str | None = None, client_secret: str | None = None, *, http: Any = None,
                 token_cache: Path | None = TOKEN_CACHE, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.time) -> None:
        if client_id is None or client_secret is None:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env")
        self.client_id = (client_id if client_id is not None else os.getenv("TWITCH_CLIENT_ID", "")).strip()
        self._secret = (client_secret if client_secret is not None else os.getenv("TWITCH_CLIENT_SECRET", "")).strip()
        if not self.client_id or not self._secret:
            raise HelixError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET must both be set in .env "
                             "(register an app at https://dev.twitch.tv/console/apps).")
        if http is None:
            import requests

            http = requests.Session()
        self.http, self.token_cache, self.sleep, self.now = http, token_cache, sleep, now
        self._token: str = ""
        self._expires: float = 0.0
        self.requests = 0  # Helix calls made (shown at the end of a run)

    def __repr__(self) -> str:  # never the secret or the token
        return f"Helix(client_id={self.client_id[:4]}…)"

    # --- the token
    def _read_cache(self) -> None:
        if self.token_cache is None:
            return
        try:
            data = json.loads(Path(self.token_cache).read_text())
            if data.get("client_id") == self.client_id:
                self._token, self._expires = str(data["access_token"]), float(data["expires_at"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _write_cache(self) -> None:
        if self.token_cache is None:
            return
        try:
            path = Path(self.token_cache)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"client_id": self.client_id, "access_token": self._token,
                                       "expires_at": self._expires}))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("Could not save the Twitch token (%s); it will be fetched again next run.",
                           type(exc).__name__)

    def token(self, fresh: bool = False) -> str:
        """The app access token: the cached one while it lasts, else a new one."""
        if not fresh:
            if not self._token:
                self._read_cache()
            if self._token and self.now() < self._expires - TOKEN_MARGIN:
                return self._token
        try:
            resp = self.http.request("POST", TOKEN_URL, data={"client_id": self.client_id, "client_secret": self._secret,
                                                              "grant_type": "client_credentials"}, timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - only the kind of error: its text may hold the request
            raise HelixError(f"Could not reach Twitch for a token ({type(exc).__name__}).") from None
        if resp.status_code != 200:
            raise HelixError(f"Twitch refused the app's credentials (HTTP {resp.status_code}). Check "
                             "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env.")
        try:
            data = resp.json()
            self._token = str(data["access_token"])
            self._expires = self.now() + float(data.get("expires_in") or 0)
        except (ValueError, KeyError, TypeError):
            raise HelixError("Twitch's token answer could not be read.") from None
        self._write_cache()
        return self._token

    # --- requests
    def _rate_wait(self, resp: Any, attempt: int) -> float:
        headers = {str(k).lower(): v for k, v in dict(getattr(resp, "headers", {}) or {}).items()}
        try:
            wait = float(headers["ratelimit-reset"]) - self.now()
        except (KeyError, TypeError, ValueError):
            wait = 2.0 ** attempt
        return min(MAX_RATE_WAIT_SECONDS, max(1.0, wait + 0.5))

    def get(self, path: str, params: Iterable[tuple[str, Any]]) -> dict:
        """One Helix GET. A 401 gets a new token and one retry; a 429 waits for the bucket to
        refill (Ratelimit-Reset) and retries; a 503 is retried once."""
        params = list(params)
        renewed, waits, again = False, 0, False
        while True:
            headers = {"Authorization": f"Bearer {self.token()}", "Client-Id": self.client_id}
            try:
                resp = self.http.request("GET", HELIX_URL + path, params=params, headers=headers, timeout=TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                if again:
                    raise HelixError(f"Could not reach Twitch for {path} ({type(exc).__name__}).") from None
                again = True
                self.sleep(2.0)
                continue
            self.requests += 1
            code = resp.status_code
            if code == 401 and not renewed:
                renewed = True
                self.token(fresh=True)
                continue
            if code == 429 and waits < MAX_RATE_WAITS:
                waits += 1
                wait = self._rate_wait(resp, waits)
                logger.info("Twitch rate limit: waiting %.0f s.", wait)
                self.sleep(wait)
                continue
            if code == 503 and not again:
                again = True
                self.sleep(2.0)
                continue
            if code != 200:
                try:
                    message = str(resp.json().get("message") or "")
                except Exception:  # noqa: BLE001
                    message = ""
                raise HelixError(f"Twitch {path} answered HTTP {code}" + (f": {message[:200]}" if message else "."))
            try:
                return resp.json()
            except ValueError:
                raise HelixError(f"Twitch {path} sent something that is not JSON.") from None


# ------------------------------------------------------------------ small pure helpers
_DURATION = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_duration(text: str) -> int:
    """Twitch's video length ("3h21m15s", "3m21s", "1h0m5s", "45s") in seconds; 0 if unreadable."""
    m = _DURATION.match(str(text or "").strip().lower())
    if not m or not any(m.groups()):
        return 0
    d, h, mi, s = (int(g or 0) for g in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def clock(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


_EMAIL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
# "name [at] gmail [dot] com", "name(at)gmail(dot)com": only the bracketed spellings, so an
# ordinary sentence with " at " in it is never read as an address
_AT = re.compile(r"\s*[\[({]\s*at\s*[\])}]\s*", re.IGNORECASE)
_DOT = re.compile(r"\s*[\[({]\s*dot\s*[\])}]\s*", re.IGNORECASE)
_DISCORD = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]{2,})",
                      re.IGNORECASE)


def contact_hints(description: str) -> list[str]:
    """Email addresses and Discord invites written in a channel description, in order, once each.
    Nothing else is read: no About-page panels, no scraping."""
    text = _DOT.sub(".", _AT.sub("@", str(description or "")))
    found: list[str] = []
    for m in _EMAIL.finditer(text):
        hint = "email: " + m.group(0).rstrip(".").lower()
        if hint not in found:
            found.append(hint)
    for m in _DISCORD.finditer(text):
        hint = "discord: discord.gg/" + m.group(1)
        if hint not in found:
            found.append(hint)
    return found


def tier(viewers: float) -> str:
    return "growing" if viewers < TIER_SPLIT else "established"


def _when(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_days(created_at: str, now: float) -> float | None:
    made = _when(created_at)
    return None if made is None else max(0.0, (now - made.timestamp()) / 86400.0)


def expiring(age: float | None, broadcaster_type: str) -> bool:
    """A VOD about to be deleted. Twitch keeps past broadcasts about 7 days (affiliates and
    everyone else) or 60 days (partners)."""
    if age is None:
        return False
    return age > (PARTNER_VOD_DAYS - EXPIRING_DAYS if broadcaster_type == "partner" else EXPIRING_DAYS)


# ------------------------------------------------------------------ games
_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
_ALIASES = {"gta": "grand theft auto", "rdr": "red dead redemption", "cod": "call of duty"}
MATCH_MIN = 0.72


def _tokens(name: str) -> list[str]:
    out: list[str] = []
    for t in re.findall(r"[a-z0-9]+", name.lower()):
        out += _ALIASES.get(t, _ROMAN.get(t, t)).split()
    return out


def match_category(query: str, candidates: list[dict]) -> dict | None:
    """The category a typed name means, or None. The same words in the same order win; otherwise
    the closest name, but never one whose number differs: "Grand Theft Auto VI" is not
    "Grand Theft Auto V", however alike they are spelled."""
    want = _tokens(query)
    numbers = {t for t in want if t.isdigit()}
    best, best_score = None, 0.0
    for c in candidates:
        have = _tokens(str(c.get("name") or ""))
        if have == want:
            return c
        if {t for t in have if t.isdigit()} != numbers:
            continue
        score = difflib.SequenceMatcher(None, " ".join(want), " ".join(have)).ratio()
        if set(want) <= set(have):  # "red dead 2" -> "Red Dead Redemption 2"
            score = max(score, 0.8)
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= MATCH_MIN else None


@dataclass(frozen=True)
class Game:
    id: str
    name: str


def resolve_game(helix: Helix, name: str) -> Game | None:
    """A typed game name as a Twitch category: found with search/categories, then its id
    confirmed with /games. None when Twitch has no such category (yet)."""
    query = " ".join(_ALIASES.get(t.lower(), t) for t in name.split())
    found = helix.get("/search/categories", [("query", query), ("first", 50)]).get("data") or []
    picked = match_category(name, found)
    if picked is None:
        return None
    confirmed = helix.get("/games", [("id", str(picked["id"]))]).get("data") or []
    if not confirmed or str(confirmed[0].get("id")) != str(picked["id"]):
        return None
    return Game(str(confirmed[0]["id"]), str(confirmed[0].get("name") or picked.get("name") or name))


# ------------------------------------------------------------------ streams, users, videos
def live_streams(helix: Helix, game: Game, lo: int = MIN_VIEWERS, hi: int = MAX_VIEWERS, language: str = "en",
                 stats: dict | None = None) -> list[dict]:
    """The game's live streams with lo <= viewers <= hi. Twitch lists streams by viewers,
    highest first, so paging stops at the first page that reaches below ``lo``."""
    kept: dict[str, dict] = {}
    cursor, pages, seen = "", 0, 0
    while pages < MAX_PAGES:
        params: list[tuple[str, Any]] = [("game_id", game.id), ("first", PAGE)]
        if language:
            params.append(("language", language))
        if cursor:
            params.append(("after", cursor))
        body = helix.get("/streams", params)
        pages += 1
        page = body.get("data") or []
        seen += len(page)
        lowest = None
        for s in page:
            try:
                viewers = int(s.get("viewer_count") or 0)
            except (TypeError, ValueError):
                continue
            lowest = viewers if lowest is None else min(lowest, viewers)
            uid = str(s.get("user_id") or "")
            if uid and lo <= viewers <= hi and uid not in kept:  # paging can repeat a stream
                kept[uid] = {**s, "viewer_count": viewers, "game_name": s.get("game_name") or game.name}
        cursor = str((body.get("pagination") or {}).get("cursor") or "")
        if not cursor or (lowest is not None and lowest < lo):
            break
    if stats is not None:
        stats.update({"pages": stats.get("pages", 0) + pages, "streams_seen": stats.get("streams_seen", 0) + seen})
    return list(kept.values())


def users(helix: Helix, ids: list[str]) -> dict[str, dict]:
    """/users for these ids, 100 per request."""
    out: dict[str, dict] = {}
    for i in range(0, len(ids), PAGE):
        batch = ids[i:i + PAGE]
        for u in helix.get("/users", [("id", x) for x in batch]).get("data") or []:
            out[str(u.get("id"))] = u
    return out


def latest_vod(helix: Helix, user_id: str) -> dict | None:
    """The streamer's newest public past broadcast, or None when they keep none."""
    videos = helix.get("/videos", [("user_id", user_id), ("type", "archive"), ("first", 3)]).get("data") or []
    videos = [v for v in videos if v.get("url") and str(v.get("type") or "archive") == "archive"]
    if not videos:
        return None
    return max(videos, key=lambda v: str(v.get("created_at") or ""))


def find(helix: Helix, games: list[Game], lo: int = MIN_VIEWERS, hi: int = MAX_VIEWERS, language: str = "en",
         stats: dict | None = None, progress: Callable[[str], None] | None = None) -> list[dict]:
    """Everyone live on ``games`` in the viewer range who has a public past broadcast.
    One dict per streamer: the stream, the /users fields, the latest VOD."""
    say = progress or (lambda _line: None)
    stats = stats if stats is not None else {}
    streams: dict[str, dict] = {}
    for game in games:
        found = live_streams(helix, game, lo, hi, language, stats)
        say(f"{game.name}: {len(found)} live streams with {lo}-{hi} viewers")
        for s in found:
            streams.setdefault(str(s["user_id"]), s)
    profiles = users(helix, list(streams))
    leads: list[dict] = []
    no_vod: list[str] = []
    for n, (uid, s) in enumerate(sorted(streams.items(), key=lambda kv: -kv[1]["viewer_count"]), 1):
        vod = latest_vod(helix, uid)
        if n % 25 == 0:
            say(f"  checked {n} of {len(streams)} for past broadcasts")
        if vod is None:
            no_vod.append(str(s.get("user_login") or uid))
            continue
        profile = profiles.get(uid) or {}
        description = str(profile.get("description") or "")
        leads.append({
            "id": uid, "login": str(profile.get("login") or s.get("user_login") or ""),
            "display_name": str(profile.get("display_name") or s.get("user_name") or ""),
            "broadcaster_type": str(profile.get("broadcaster_type") or ""),
            "contact_hints": "; ".join(contact_hints(description)),
            "game": str(s.get("game_name") or ""), "viewers": int(s["viewer_count"]), "title": str(s.get("title") or ""),
            "vod_url": str(vod["url"]), "vod_seconds": parse_duration(vod.get("duration") or ""),
            "vod_created_at": str(vod.get("created_at") or ""),
            # the newest archive of someone who is live is the stream still being recorded
            "vod_live": bool(vod.get("stream_id")) and str(vod.get("stream_id")) == str(s.get("id")),
        })
    stats.update({"in_range": len(streams), "no_vod": no_vod, "leads": len(leads)})
    return leads


# ------------------------------------------------------------------ the database
SCHEMA = """
CREATE TABLE IF NOT EXISTS streamers (
  id TEXT PRIMARY KEY, login TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
  broadcaster_type TEXT NOT NULL DEFAULT '', contact_hints TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new', notes TEXT NOT NULL DEFAULT '',
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  vod_url TEXT NOT NULL DEFAULT '', vod_seconds INTEGER NOT NULL DEFAULT 0,
  vod_created_at TEXT NOT NULL DEFAULT '', vod_live INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS snapshots (
  streamer_id TEXT NOT NULL REFERENCES streamers(id), time TEXT NOT NULL, game TEXT NOT NULL DEFAULT '',
  viewers INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '', PRIMARY KEY (streamer_id, time));
CREATE INDEX IF NOT EXISTS snapshots_time ON snapshots(time);
"""


def connect(db: Path | str = DB_PATH) -> sqlite3.Connection:
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def save(con: sqlite3.Connection, leads: list[dict], when: str) -> None:
    """This run's streamers and one snapshot each. Your status and notes are never touched."""
    with con:
        for lead in leads:
            con.execute(
                "INSERT INTO streamers (id, login, display_name, broadcaster_type, contact_hints, first_seen, last_seen,"
                " vod_url, vod_seconds, vod_created_at, vod_live) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET login=excluded.login, display_name=excluded.display_name,"
                " broadcaster_type=excluded.broadcaster_type, last_seen=excluded.last_seen, vod_url=excluded.vod_url,"
                " vod_seconds=excluded.vod_seconds, vod_created_at=excluded.vod_created_at, vod_live=excluded.vod_live,"
                # a hint found once is kept even if the streamer later edits it out of the description
                " contact_hints=CASE WHEN excluded.contact_hints != '' THEN excluded.contact_hints"
                " ELSE streamers.contact_hints END",
                (lead["id"], lead["login"], lead["display_name"], lead["broadcaster_type"], lead["contact_hints"], when,
                 when, lead["vod_url"], int(lead["vod_seconds"]), lead["vod_created_at"], int(bool(lead.get("vod_live")))))
            con.execute("INSERT OR REPLACE INTO snapshots (streamer_id, time, game, viewers, title) VALUES (?,?,?,?,?)",
                        (lead["id"], when, lead["game"], int(lead["viewers"]), lead["title"]))


def set_status(con: sqlite3.Connection, login: str, status: str, notes: str | None = None) -> bool:
    """Mark a lead (by login). False when there is no such lead."""
    if status not in STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(STATUSES)}")
    with con:
        if notes is None:
            cur = con.execute("UPDATE streamers SET status=? WHERE lower(login)=lower(?)", (status, login.strip()))
        else:
            cur = con.execute("UPDATE streamers SET status=?, notes=? WHERE lower(login)=lower(?)",
                              (status, notes, login.strip()))
    return cur.rowcount > 0


def latest_run(con: sqlite3.Connection) -> str | None:
    return con.execute("SELECT MAX(time) FROM snapshots").fetchone()[0]


def rows(con: sqlite3.Connection, run: str | None = None, now: float | None = None) -> list[dict]:
    """Leads as the table and the CSV show them. ``run``: only streamers seen in that run (a
    snapshot time, see latest_run); None: every saved lead, each with its last snapshot.
    New leads first, by live viewers; ones you already handled (contacted, client, ...) last."""
    now = time.time() if now is None else now
    found = con.execute(
        "SELECT s.*, p.game, p.viewers, p.title, p.time AS seen,"
        " (SELECT AVG(viewers) FROM snapshots a WHERE a.streamer_id = s.id) AS avg_viewers,"
        " (SELECT COUNT(*) FROM snapshots a WHERE a.streamer_id = s.id) AS snapshots "
        "FROM streamers s JOIN snapshots p ON p.streamer_id = s.id AND p.time ="
        " (SELECT MAX(time) FROM snapshots x WHERE x.streamer_id = s.id) "
        "WHERE (? IS NULL OR p.time = ?)", (run, run)).fetchall()
    out: list[dict] = []
    for r in found:
        age = age_days(r["vod_created_at"], now)
        notes = []
        if r["vod_live"]:
            notes.append("live now: still recording")
        if expiring(age, r["broadcaster_type"]):
            notes.append("expiring")
        out.append({
            "username": r["login"], "live_viewers": int(r["viewers"]), "avg_viewers": round(float(r["avg_viewers"]), 1),
            "tier": tier(r["viewers"]), "game": r["game"], "stream_title": r["title"], "latest_vod_url": r["vod_url"],
            "vod_duration": clock(r["vod_seconds"]), "vod_age_days": "" if age is None else round(age, 1),
            "channel_url": f"https://twitch.tv/{r['login']}/about", "contact_hints": r["contact_hints"],
            "status": r["status"], "vod_note": ", ".join(notes), "broadcaster_type": r["broadcaster_type"],
            "snapshots": int(r["snapshots"]), "notes": r["notes"], "display_name": r["display_name"],
            "seen": r["seen"], "id": r["id"]})
    out.sort(key=lambda x: (x["status"] in HANDLED, -x["live_viewers"], x["username"]))
    return out


# ------------------------------------------------------------------ output
def write_csv(leads: list[dict], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[*CSV_COLUMNS, *EXTRA_COLUMNS], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(leads)
    return path


def csv_text(leads: list[dict]) -> str:
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=[*CSV_COLUMNS, *EXTRA_COLUMNS], extrasaction="ignore")
    writer.writeheader()
    writer.writerows(leads)
    return buffer.getvalue()


def _cut(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[:width - 1] + "…"


def table(leads: list[dict]) -> str:
    """The printed table. Leads you already handled come last, under their own heading."""
    if not leads:
        return "(no leads)"
    head = f"{'streamer':<22} {'live':>5} {'avg':>6}  {'tier':<11} {'game':<20} {'VOD':>8} {'age':>5}  {'contact / notes'}"
    lines = [head, "-" * len(head)]
    flagged = False
    for lead in leads:
        if lead["status"] in HANDLED and not flagged:
            flagged = True
            lines.append("-- already handled (listed, not dropped) " + "-" * (len(head) - 41))
        about = "; ".join(x for x in (lead["contact_hints"], lead["vod_note"]) if x)
        if lead["status"] in HANDLED:
            about = f"[{lead['status'].upper()}] " + about
        age = "" if lead["vod_age_days"] == "" else f"{lead['vod_age_days']:.1f}d"
        lines.append(f"{_cut(lead['username'], 22):<22} {lead['live_viewers']:>5} {lead['avg_viewers']:>6.1f}  "
                     f"{lead['tier']:<11} {_cut(lead['game'], 20):<20} {lead['vod_duration']:>8} {age:>5}  "
                     f"{_cut(about, 70)}".rstrip())
    return "\n".join(lines)
