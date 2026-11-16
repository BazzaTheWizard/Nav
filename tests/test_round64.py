"""Round 64: the streamer lead finder (core/leads.py, tools/find_leads.py) and the GTA tab's
🔎 Find Streamers mode. A fake Helix stands in for Twitch: no network, no real credentials.

Run: .venv/bin/python -u tests/test_round64.py
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="nav-r64-"))
os.environ["NAV_LEADS_DB"] = str(TMP / "app" / "leads.sqlite")  # the app check never opens your real leads
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail in ("", None) else f"\n      {detail!r}"))
    if not ok:
        FAILED.append(name)


from core import leads as LD  # noqa: E402

SECRET = "s3cr3t-never-shown-9f2b"
CLIENT = "client-abc"
NOW = 1_795_000_000.0  # a fixed "now" (Nov 2026)


# ---------------------------------------------------------------- the fake Helix
class Resp:
    def __init__(self, status: int, body: dict | None = None, headers: dict | None = None) -> None:
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def json(self) -> dict:
        return self._body


class FakeHelix:
    """Twitch, in memory: the token endpoint and the five Helix endpoints the lead finder uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list]] = []  # (method, path, params)
        self.token_posts: list[dict] = []
        self.valid: set[str] = set()
        self.minted = 0
        self.rate_limit: list[tuple[int, dict]] = []  # queued (status, headers) answers for the next GETs
        self.categories = [{"id": "32982", "name": "Grand Theft Auto V", "box_art_url": ""},
                           {"id": "29452", "name": "Grand Theft Auto: Vice City", "box_art_url": ""},
                           {"id": "493959", "name": "Red Dead Redemption 2", "box_art_url": ""}]
        self.streams: dict[str, list[dict]] = {}  # game id -> streams, most viewers first
        self.users: dict[str, dict] = {}
        self.videos: dict[str, list[dict]] = {}
        self.secret_seen_outside_token_body = False

    def expire_tokens(self) -> None:
        self.valid.clear()

    def request(self, method: str, url: str, params=None, data=None, headers=None, timeout=None) -> Resp:
        blob = json.dumps([url, params, headers], default=str)
        if SECRET in blob:
            self.secret_seen_outside_token_body = True
        if url == LD.TOKEN_URL:
            assert method == "POST"
            self.token_posts.append(dict(data or {}))
            if (data or {}).get("client_secret") != SECRET or data.get("grant_type") != "client_credentials":
                return Resp(400, {"status": 400, "message": "invalid client"})
            self.minted += 1
            token = f"tok-{self.minted}"
            self.valid = {token}
            return Resp(200, {"access_token": token, "expires_in": 5000, "token_type": "bearer"})
        path = url.replace(LD.HELIX_URL, "")
        params = list(params or [])
        self.calls.append((method, path, params))
        if self.rate_limit:
            status, hdrs = self.rate_limit.pop(0)
            return Resp(status, {"message": "Too Many Requests"}, hdrs)
        auth = (headers or {}).get("Authorization", "")
        if auth.removeprefix("Bearer ") not in self.valid or (headers or {}).get("Client-Id") != CLIENT:
            return Resp(401, {"status": 401, "message": "Invalid OAuth token"})
        get = lambda key: [v for k, v in params if k == key]  # noqa: E731
        if path == "/search/categories":
            query = str(get("query")[0]).lower()
            words = [w for w in query.split() if len(w) > 1]
            return Resp(200, {"data": [c for c in self.categories if any(w in c["name"].lower() for w in words)]})
        if path == "/games":
            return Resp(200, {"data": [c for c in self.categories if c["id"] in get("id")]})
        if path == "/streams":
            assert get("language") == ["en"], params
            every = self.streams.get(get("game_id")[0], [])
            first, start = int(get("first")[0]), int((get("after") or ["0"])[0])
            page = every[start:start + first]
            cursor = {"cursor": str(start + first)} if start + first < len(every) else {}
            return Resp(200, {"data": page, "pagination": cursor})
        if path == "/users":
            ids = get("id")
            assert len(ids) <= 100, len(ids)
            return Resp(200, {"data": [self.users[i] for i in ids if i in self.users]})
        if path == "/videos":
            assert get("type") == ["archive"] and get("first") == [3], params
            return Resp(200, {"data": self.videos.get(get("user_id")[0], [])[:3], "pagination": {}})
        return Resp(404, {"message": "no such endpoint"})


def stream(uid: int, viewers: int, game: str = "Grand Theft Auto V") -> dict:
    return {"id": f"live{uid}", "user_id": str(uid), "user_login": f"streamer{uid}", "user_name": f"Streamer{uid}",
            "game_id": "32982", "game_name": game, "type": "live", "title": f"stream of {uid}", "viewer_count": viewers,
            "started_at": LD.iso(NOW - 3600), "language": "en", "tags": None}


def video(uid: int, n: int, days_old: float, duration: str = "3h21m15s", stream_id: str | None = None) -> dict:
    return {"id": f"{uid}{n}", "stream_id": stream_id or f"old{uid}{n}", "user_id": str(uid), "type": "archive",
            "url": f"https://www.twitch.tv/videos/{uid}{n}", "created_at": LD.iso(NOW - days_old * 86400),
            "duration": duration, "title": "vod", "viewable": "public"}


def world() -> FakeHelix:
    """GTA V with 250 streams: 5 huge ones, then 400, 398, ... down to nothing. Page 1 (100
    streams) is all above 30 viewers, page 2 falls below it, page 3 must never be asked for."""
    fake = FakeHelix()
    viewers = [9000, 5000, 3000, 1200, 800] + [max(1, 400 - 3 * i) for i in range(245)]
    fake.streams["32982"] = [stream(1000 + i, v) for i, v in enumerate(viewers)]
    for i, v in enumerate(viewers):
        uid = 1000 + i
        fake.users[str(uid)] = {"id": str(uid), "login": f"streamer{uid}", "display_name": f"Streamer{uid}",
                                "broadcaster_type": "affiliate", "description": "", "type": ""}
        fake.videos[str(uid)] = [video(uid, 1, 1.0), video(uid, 2, 3.0)]
    fake.users["1005"]["description"] = "Business: Biz.Mail@Example.com | join discord.gg/abcDEF"
    fake.users["1005"]["broadcaster_type"] = "partner"
    fake.videos["1006"] = []  # no public past broadcasts: skipped
    fake.videos["1007"] = [video(1007, 1, 6.0, "45m")]  # an affiliate's 6-day-old VOD: expiring
    fake.videos["1005"] = [video(1005, 1, 6.0, "7h2m0s")]  # a partner's: kept 60 days, not expiring
    fake.videos["1008"] = [video(1008, 2, 2.0), video(1008, 1, 0.02, "1h0m5s", stream_id="live1008")]  # live now
    return fake


def helix(fake: FakeHelix, cache: Path | None = None, clock: list[float] | None = None, sleeps: list | None = None) -> LD.Helix:
    clock = clock if clock is not None else [NOW]
    return LD.Helix(CLIENT, SECRET, http=fake, token_cache=cache, sleep=(sleeps if sleeps is not None else []).append,
                    now=lambda: clock[0])


# ---------------------------------------------------------------- pure helpers
check("duration: 3h21m15s", LD.parse_duration("3h21m15s") == 3 * 3600 + 21 * 60 + 15)
check("duration: leading zero units left out (3m21s, 45s)", LD.parse_duration("3m21s") == 201 and LD.parse_duration("45s") == 45)
check("duration: 1h0m5s and a 2-day marathon", LD.parse_duration("1h0m5s") == 3605 and LD.parse_duration("48h0m0s") == 172800)
check("duration: junk is 0", LD.parse_duration("") == 0 and LD.parse_duration("soon") == 0 and LD.parse_duration(None) == 0)
check("clock", LD.clock(12075) == "3:21:15" and LD.clock(45) == "0:00:45")

hints = LD.contact_hints("Business inquiries: Biz.Mail@Example.com. Join https://discord.gg/abcDEF or "
                         "https://discord.com/invite/xyz-123 !! also biz.mail@example.com")
check("hints: email (once, lowercased) and both Discord invite forms",
      hints == ["email: biz.mail@example.com", "discord: discord.gg/abcDEF", "discord: discord.gg/xyz-123"], hints)
check("hints: [at] / (dot) spellings", LD.contact_hints("mail me: jo.smith [at] gmail (dot) com")
      == ["email: jo.smith@gmail.com"], LD.contact_hints("mail me: jo.smith [at] gmail (dot) com"))
check("hints: an ordinary sentence gives nothing",
      LD.contact_hints("Live at 8pm. Follow @me on twitter.com/me, discord in the panels, v1.2.3") == [],
      LD.contact_hints("Live at 8pm. Follow @me on twitter.com/me, discord in the panels, v1.2.3"))
check("hints: discordapp.com/invite and a subdomain email",
      LD.contact_hints("discordapp.com/invite/Qq9 team@mail.agency.co.uk")
      == ["email: team@mail.agency.co.uk", "discord: discord.gg/Qq9"])
check("tiers: 30-150 growing, 150-400 established", [LD.tier(v) for v in (30, 149, 150, 400)]
      == ["growing", "growing", "established", "established"])
check("expiring: an affiliate's VOD after 5 days, a partner's not", LD.expiring(5.5, "affiliate") and LD.expiring(6, "")
      and not LD.expiring(4.9, "affiliate") and not LD.expiring(6, "partner") and LD.expiring(56, "partner"))

cats = [{"id": "1", "name": "Grand Theft Auto V"}, {"id": "2", "name": "Grand Theft Auto: Vice City"},
        {"id": "3", "name": "Grand Theft Auto VI"}, {"id": "4", "name": "Red Dead Redemption 2"}]
check("match: VI is VI, V is V", LD.match_category("Grand Theft Auto VI", cats)["id"] == "3"
      and LD.match_category("grand theft auto v", cats)["id"] == "1")
check("match: no VI category -> None, never V or Vice City", LD.match_category("Grand Theft Auto VI", cats[:2]) is None)
check("match: gta 5 / GTA6 spellings", LD.match_category("gta 5", cats)["id"] == "1" and LD.match_category("GTA 6", cats)["id"] == "3")
check("match: a shortened name", LD.match_category("red dead 2", cats)["id"] == "4")
check("match: something else entirely -> None", LD.match_category("Minecraft", cats) is None)

# ---------------------------------------------------------------- the token
fake = world()
cache = TMP / "token.json"
clock = [NOW]
h = helix(fake, cache, clock)
check("resolve: GTA V found and confirmed with /games", LD.resolve_game(h, "Grand Theft Auto V") == LD.Game("32982", "Grand Theft Auto V")
      and [c[1] for c in fake.calls] == ["/search/categories", "/games"], fake.calls)
check("token: asked for once, with client credentials in the body", len(fake.token_posts) == 1 and fake.token_posts[0]
      == {"client_id": CLIENT, "client_secret": SECRET, "grant_type": "client_credentials"})
check("resolve: no GTA VI category yet -> None", LD.resolve_game(h, "Grand Theft Auto VI") is None)
check("token: reused for later requests", len(fake.token_posts) == 1)
saved = json.loads(cache.read_text())
check("token cache: token and expiry saved, readable only by you, without the secret",
      saved["access_token"] == "tok-1" and saved["expires_at"] == NOW + 5000 and SECRET not in cache.read_text()
      and (cache.stat().st_mode & 0o077) == 0, saved)
h2 = helix(fake, cache, clock)
h2.get("/games", [("id", "32982")])
check("token cache: a new run uses the saved token", len(fake.token_posts) == 1)
clock[0] = NOW + 5000 - 30  # inside the safety margin before expiry
h3 = helix(fake, cache, clock)
h3.get("/games", [("id", "32982")])
check("token cache: an expiring token is replaced", len(fake.token_posts) == 2 and fake.valid == {"tok-2"})
clock[0] = NOW

fake.expire_tokens()  # Twitch revoked it: the next request gets a 401
before = len(fake.calls)
data = h3.get("/games", [("id", "32982")])
check("401: a new token, then the request once more", data["data"][0]["name"] == "Grand Theft Auto V"
      and len(fake.token_posts) == 3 and len(fake.calls) - before == 2, (len(fake.token_posts), len(fake.calls) - before))


class Always401(FakeHelix):
    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        resp = super().request(method, url, params=params, data=data, headers=headers, timeout=timeout)
        return resp if url == LD.TOKEN_URL else Resp(401, {"message": "Invalid OAuth token"})


stuck = Always401()
try:
    helix(stuck).get("/games", [("id", "1")])
    check("401 twice: gives up", False)
except LD.HelixError as exc:
    check("401 twice: one retry only, then an error", len(stuck.calls) == 2 and len(stuck.token_posts) == 2
          and "401" in str(exc), (len(stuck.calls), str(exc)))

# 429: wait until Ratelimit-Reset, then go on
sleeps: list[float] = []
h4 = helix(fake, None, clock, sleeps)
fake.rate_limit = [(429, {"Ratelimit-Reset": str(int(NOW) + 7), "Ratelimit-Remaining": "0"}), (429, {})]
data = h4.get("/games", [("id", "32982")])
check("429: waits for Ratelimit-Reset, then backs off without the header", data["data"] and sleeps == [7.5, 4.5], sleeps)
fake.rate_limit = [(429, {"ratelimit-reset": str(int(NOW) + 9999)})] * (LD.MAX_RATE_WAITS + 1)
sleeps.clear()
try:
    h4.get("/games", [("id", "32982")])
    check("429 for ever: gives up", False)
except LD.HelixError as exc:
    check("429 for ever: waits are capped, then an error", sleeps == [LD.MAX_RATE_WAIT_SECONDS] * LD.MAX_RATE_WAITS
          and "429" in str(exc), (sleeps, str(exc)))
fake.rate_limit = []

try:
    LD.Helix(CLIENT, "wrong-secret-value", http=fake, token_cache=None).get("/games", [("id", "1")])
    check("bad credentials: an error", False)
except LD.HelixError as exc:
    check("bad credentials: a plain message without the secret", "TWITCH_CLIENT_SECRET" in str(exc)
          and "wrong-secret-value" not in str(exc), str(exc))
try:
    LD.Helix("", "", http=fake, token_cache=None)
    check("no credentials: an error", False)
except LD.HelixError as exc:
    check("no credentials: says which .env keys", "TWITCH_CLIENT_ID" in str(exc))
check("repr never shows the secret", SECRET not in repr(h) and SECRET not in str(vars(h).get("client_id")))

# ---------------------------------------------------------------- streams: paging and the range
fake = world()
h = helix(fake)
stats: dict = {}
gta5 = LD.Game("32982", "Grand Theft Auto V")
found = LD.live_streams(h, gta5, 30, 400, stats=stats)
stream_calls = [c for c in fake.calls if c[1] == "/streams"]
check("paging: stops at the page that falls below --min (2 of 3 pages)", len(stream_calls) == 2 and stats["pages"] == 2,
      len(stream_calls))
check("paging: the second request follows the cursor", ("after", "100") in stream_calls[1][2]
      and ("first", 100) in stream_calls[0][2] and ("game_id", "32982") in stream_calls[0][2])
check("range: everything kept has 30-400 viewers", found and all(30 <= s["viewer_count"] <= 400 for s in found)
      and max(s["viewer_count"] for s in found) == 400 and min(s["viewer_count"] for s in found) == 31,
      (len(found), [s["viewer_count"] for s in found][:3]))
check("range: the five big streams are left out", not {"1000", "1001", "1002", "1003", "1004"} & {s["user_id"] for s in found})
expected = sum(1 for s in fake.streams["32982"] if 30 <= s["viewer_count"] <= 400)
check("range: nobody in range is missed", len(found) == expected == 124, (len(found), expected))
fake.calls.clear()
check("paging: a higher --min stops after page 1", len(LD.live_streams(h, gta5, 200, 400)) == 67
      and len([c for c in fake.calls if c[1] == "/streams"]) == 1)
fake.calls.clear()
LD.live_streams(h, gta5, 0, 400)
check("paging: --min 0 reads to the last page (an empty cursor ends it)", len([c for c in fake.calls if c[1] == "/streams"]) == 3)

# ---------------------------------------------------------------- a whole run
sys.path.insert(0, str(ROOT / "tools"))
import find_leads as FL  # noqa: E402

fake = world()
h = helix(fake, TMP / "token2.json")
db = TMP / "run" / "leads.sqlite"
out = io.StringIO()
log = io.StringIO()
handler = logging.StreamHandler(log)
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.DEBUG)
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
    result = FL.run(h, games=list(LD.DEFAULT_GAMES), lo=30, hi=400, db=db, now=NOW)
    print(LD.table(result["leads"]))
leads = result["leads"]
by = {lead["username"]: lead for lead in leads}
printed = out.getvalue()
check("GTA VI has no category: said so, and the run went on with GTA V", result["missing"] == ["Grand Theft Auto VI"]
      and "No Twitch category for \"Grand Theft Auto VI\"" in printed and [g.name for g in result["games"]] == ["Grand Theft Auto V"])
check("no public VOD: skipped and counted", "streamer1006" not in by and result["stats"]["no_vod"] == ["streamer1006"]
      and len(leads) == 123, (len(leads), result["stats"]["no_vod"]))
check("leads are in the range, largest first", [lead["live_viewers"] for lead in leads] == sorted(
    (lead["live_viewers"] for lead in leads), reverse=True) and leads[0]["live_viewers"] == 400)
user_calls = [c for c in fake.calls if c[1] == "/users"]
check("/users in batches of 100", [len(c[2]) for c in user_calls] == [100, 24], [len(c[2]) for c in user_calls])
check("/videos once per streamer in range, none for the others", len([c for c in fake.calls if c[1] == "/videos"]) == 124)
first = by["streamer1005"]
check("a lead: type, hints, VOD, links", first["broadcaster_type"] == "partner" and first["tier"] == "established"
      and first["contact_hints"] == "email: biz.mail@example.com; discord: discord.gg/abcDEF"
      and first["latest_vod_url"] == "https://www.twitch.tv/videos/10051" and first["vod_duration"] == "7:02:00"
      and first["vod_age_days"] == 6.0 and first["channel_url"] == "https://twitch.tv/streamer1005/about"
      and first["status"] == "new" and first["vod_note"] == "", first)
check("the newest of the three archives is the one kept", by["streamer1010"]["latest_vod_url"].endswith("/10101")
      and by["streamer1010"]["vod_age_days"] == 1.0)
check("an affiliate's 6-day-old VOD is flagged expiring", by["streamer1007"]["vod_note"] == "expiring"
      and by["streamer1007"]["vod_duration"] == "0:45:00", by["streamer1007"])
check("a VOD still being recorded is marked", by["streamer1008"]["vod_note"] == "live now: still recording"
      and by["streamer1008"]["vod_duration"] == "1:00:05", by["streamer1008"])
check("tiers in the table", by["streamer1090"]["live_viewers"] == 145 and by["streamer1090"]["tier"] == "growing"
      and by["streamer1088"]["live_viewers"] == 151 and by["streamer1088"]["tier"] == "established")

with open(result["csv"], newline="", encoding="utf-8") as f:
    table = list(csv.DictReader(f))
    f.seek(0)
    header = f.readline().strip().split(",")
check("CSV: at storage-side leads_<date>.csv, the asked columns first, in order",
      result["csv"].parent == db.parent and result["csv"].name.startswith("leads_20") and result["csv"].suffix == ".csv"
      and header[:12] == ["username", "live_viewers", "avg_viewers", "tier", "game", "stream_title", "latest_vod_url",
                          "vod_duration", "vod_age_days", "channel_url", "contact_hints", "status"], header)
check("CSV: one row per lead", len(table) == 123 and table[0]["username"] == leads[0]["username"]
      and table[0]["channel_url"].startswith("https://twitch.tv/") and table[0]["channel_url"].endswith("/about"))

# ---------------------------------------------------------------- snapshots, status, a second run
con = LD.connect(db)
check("database: streamers and one snapshot each", con.execute("SELECT COUNT(*) FROM streamers").fetchone()[0] == 123
      and con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 123)
check("status: mark by login, with a note", LD.set_status(con, "Streamer1005", "contacted", "emailed 19 Nov")
      and LD.set_status(con, "streamer1009", "client") and LD.set_status(con, "streamer1011", "skip")
      and not LD.set_status(con, "nobody", "contacted"))
try:
    LD.set_status(con, "streamer1005", "maybe")
    check("status: only the five allowed", False)
except ValueError:
    check("status: only the five allowed", True)
con.close()

for s in fake.streams["32982"]:  # an hour later: everyone has 20 more viewers, one changed title
    s["viewer_count"] += 20
fake.streams["32982"] = sorted((s for s in fake.streams["32982"] if s["user_id"] != "1020"),  # and one went offline
                               key=lambda s: -s["viewer_count"])
fake.users["1005"]["description"] = ""  # the email was edited out: the saved hint stays
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
    again = FL.run(h, games=["Grand Theft Auto V"], lo=30, hi=450, db=db, now=NOW + 3600)
    shown = LD.table(again["leads"])
    print(shown)
by2 = {lead["username"]: lead for lead in again["leads"]}
lead = by2["streamer1012"]
check("snapshot average over the saved runs, beside the live number", lead["live_viewers"] == 399 and lead["avg_viewers"] == 389.0
      and lead["snapshots"] == 2, lead)
newcomer = [x for x in again["leads"] if x["snapshots"] == 1]
check("a streamer seen for the first time: avg = live", newcomer and all(x["avg_viewers"] == x["live_viewers"] for x in newcomer),
      len(newcomer))
names = [x["username"] for x in again["leads"]]
handled = [x["username"] for x in again["leads"] if x["status"] in LD.HANDLED]
check("contacted / client / skip: kept, with their status and note", by2["streamer1005"]["status"] == "contacted"
      and by2["streamer1005"]["notes"] == "emailed 19 Nov" and by2["streamer1009"]["status"] == "client"
      and by2["streamer1011"]["status"] == "skip", handled)
check("... and listed last, after every new lead", sorted(handled) == ["streamer1005", "streamer1009", "streamer1011"]
      and names[-3:] == handled and all(x["status"] == "new" for x in again["leads"][:-3]), names[-4:])
check("... and flagged in the printed table", "already handled (listed, not dropped)" in shown and "[CONTACTED]" in shown
      and "[CLIENT]" in shown and shown.index("already handled") < shown.index("[CONTACTED]"))
check("a contact hint found once is kept", by2["streamer1005"]["contact_hints"].startswith("email: biz.mail@example.com"))
with open(again["csv"], newline="", encoding="utf-8") as f:
    table = list(csv.DictReader(f))
check("CSV: handled leads are in it, last, with their status", [r["status"] for r in table[-3:]].count("new") == 0
      and len(table) == len(again["leads"]))
con = LD.connect(db)
everyone = LD.rows(con, now=NOW + 3600)
check("every saved lead: includes the one who went offline", len(everyone) == len(again["leads"]) + 1
      and "streamer1020" in {x["username"] for x in everyone} and "streamer1020" not in by2
      and LD.latest_run(con) == LD.iso(NOW + 3600), (len(everyone), len(again["leads"])))
con.close()

# ---------------------------------------------------------------- the secret stays secret
logging.getLogger().removeHandler(handler)
everything = out.getvalue() + log.getvalue() + (TMP / "token2.json").read_text() + result["csv"].read_text()
check("the secret is in no output, log, cache or CSV", SECRET not in everything and "tok-" not in out.getvalue() + log.getvalue())
check("the secret went to the token endpoint's body only", not fake.secret_seen_outside_token_body and fake.token_posts)
check("the database holds no secret or token", SECRET.encode() not in db.read_bytes() and b"tok-" not in db.read_bytes())
source = (ROOT / "core" / "leads.py").read_text() + (ROOT / "tools" / "find_leads.py").read_text()
check("the code never formats the secret into a message", "self._secret}" not in source and "_secret)" not in source.replace(
    '"client_secret": self._secret', ""), "")

# ---------------------------------------------------------------- the command line
run = subprocess.run([sys.executable, str(ROOT / "tools" / "find_leads.py"), "--db", str(db), "--mark", "streamer1013",
                      "replied", "--note", "wants a call"], capture_output=True, text=True, cwd=ROOT)
con = LD.connect(db)
row = con.execute("SELECT status, notes FROM streamers WHERE login='streamer1013'").fetchone()
con.close()
check("--mark sets a status from the terminal", run.returncode == 0 and tuple(row) == ("replied", "wants a call"),
      run.stdout + run.stderr)
run = subprocess.run([sys.executable, str(ROOT / "tools" / "find_leads.py"), "--db", str(db), "--min", "500", "--max", "100"],
                     capture_output=True, text=True, cwd=ROOT)
check("--min above --max is refused before any request", run.returncode != 0 and "--min" in run.stderr, run.stderr[-200:])
example = (ROOT / ".env.example").read_text()
check(".env.example names both Twitch keys, empty", "\nTWITCH_CLIENT_ID=\n" in example and "\nTWITCH_CLIENT_SECRET=\n" in example)
check("the leads folder is not committed", "storage/leads/" in (ROOT / ".gitignore").read_text())

# ---------------------------------------------------------------- the tab's helpers
import gta_tab as G  # noqa: E402

argv = G.leads_argv("Red Dead Redemption 2,  Fortnite\n", 50, 300)
check("leads argv: range and extra games", argv[1:] == [str(G.LEADS_TOOL), "--min", "50", "--max", "300", "--game",
                                                        "Red Dead Redemption 2", "--game", "Fortnite"], argv)
check("leads argv: defaults", G.leads_argv()[2:] == ["--min", "30", "--max", "400"]
      and "--no-default-games" in G.leads_argv("Fortnite", default_games=False))
for bad, why in (((("", 400, 30), {}), "range"), ((("",), {"default_games": False}), "no game")):
    try:
        G.leads_argv(*bad[0], **bad[1])
        check(f"leads argv refuses: {why}", False)
    except ValueError:
        check(f"leads argv refuses: {why}", True)
argv = G.sample_clips_argv("streamer1005", "https://www.twitch.tv/videos/10051", "Streamer1005", "Grand Theft Auto V")
check("sample clips: clip_stream on the VOD with --count 2 and the pitch campaign",
      argv[1:3] == [str(G.STREAM_TOOL), "https://www.twitch.tv/videos/10051"]
      and argv[argv.index("--campaign") + 1] == "Pitch sample: streamer1005" and argv[argv.index("--count") + 1] == "2"
      and argv[argv.index("--name") + 1] == "pitch-streamer1005" and argv[argv.index("--streamer") + 1] == "Streamer1005",
      argv)
try:
    G.sample_clips_argv("x", "")
    check("sample clips: no VOD is refused", False)
except ValueError:
    check("sample clips: no VOD is refused", True)
last = G.saved_leads(db)
check("saved leads: the last search, or all", len(last) == len(again["leads"]) and len(G.saved_leads(db, last_search=False))
      == len(everyone) and G.saved_leads(TMP / "none.sqlite") == [])
before_rows = [{"username": "a", "status": "new"}, {"username": "b", "status": "new"}]
after_rows = [{"username": "a", "status": "contacted"}, {"username": "b", "status": "new"}, {"username": "c", "status": "skip"}]
check("status edits: only real changes to known leads", G.status_edits(before_rows, after_rows) == {"a": "contacted"})
check("status edits are saved", G.save_statuses({"streamer1014": "contacted"}, db) == 1
      and {x["username"]: x["status"] for x in G.saved_leads(db)}["streamer1014"] == "contacted")

# ---------------------------------------------------------------- the app
try:
    import autonomous_worker  # noqa: E402

    for name, value in {"get_pending_outreach": lambda: [], "get_cached_leads": lambda: [],
                        "get_awaiting_reply": lambda: [], "get_lead_queue": lambda: [],
                        "get_recently_passed": lambda *a, **k: [], "pass_summary": lambda *a, **k: {},
                        "is_youtube_quota_exhausted": lambda: False}.items():
        setattr(autonomous_worker, name, value)
    from streamlit.testing.v1 import AppTest  # noqa: E402

    from core import tool_jobs as J  # noqa: E402

    check("the app's leads database is the test's own", LD.DB_PATH == TMP / "app" / "leads.sqlite")
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    radios = [r for r in at.radio if r.key == "gta_mode"]
    check("three GTA modes, Find Streamers third", len(radios) == 1 and list(radios[0].options) == list(G.MODES)
          and G.MODES[2] == "🔎 Find Streamers", radios and radios[0].options)
    radios[0].set_value("🔎 Find Streamers").run()
    check("Find Streamers renders (no leads yet)", not at.exception and any("No leads yet" in i.value for i in at.info),
          [e.value for e in at.exception])
    check("a game box, a viewer range, a Find button",
          any(t.label.startswith("Games") for t in at.text_input) and any(s.label == "Live viewers" and tuple(s.value) == (30, 400)
                                                                          for s in at.slider)
          and any("Find streamers" in str(b.label) for b in at.button), [b.label for b in at.button])
    # with leads saved: the table, the per-lead status box and the sample-clips button
    con = LD.connect(LD.DB_PATH)
    LD.save(con, [{"id": "7", "login": "vice_kid", "display_name": "ViceKid", "broadcaster_type": "affiliate",
                   "contact_hints": "email: v@k.gg", "game": "Grand Theft Auto VI", "viewers": 88, "title": "day one",
                   "vod_url": "https://www.twitch.tv/videos/777", "vod_seconds": 7200, "vod_created_at": LD.iso(NOW)},
                  {"id": "8", "login": "old_client", "display_name": "OldClient", "broadcaster_type": "partner",
                   "contact_hints": "", "game": "Grand Theft Auto V", "viewers": 300, "title": "rp",
                   "vod_url": "https://www.twitch.tv/videos/888", "vod_seconds": 600, "vod_created_at": LD.iso(NOW)}],
            LD.iso(NOW))
    LD.set_status(con, "old_client", "client")
    con.close()
    at.run()
    check("with leads: renders, counts them", not at.exception and any("2 leads" in s.value for s in at.subheader),
          [e.value for e in at.exception] or [s.value for s in at.subheader])
    picks = [s for s in at.selectbox if s.key == "lead_pick"]
    check("a lead picker, new leads before handled ones", picks and list(picks[0].options)[0].startswith("vice_kid")
          if picks and isinstance(picks[0].options[0], str) else bool(picks), picks and picks[0].options)
    statuses = [s for s in at.selectbox if s.key == "lead_status_vice_kid"]
    check("a status dropdown for the lead", statuses and list(statuses[0].options) == list(LD.STATUSES)
          and statuses[0].value == "new", statuses and statuses[0].options)
    if statuses:
        statuses[0].set_value("contacted")
        [b for b in at.button if b.key == "lead_save_vice_kid"][0].click().run()
        check("saving a status writes it to the database", not at.exception
              and {x["username"]: x["status"] for x in G.saved_leads(LD.DB_PATH)}["vice_kid"] == "contacted",
              [e.value for e in at.exception])
    sample = [b for b in at.button if b.key and str(b.key).startswith("lead_sample_")]
    check("a Make sample clips button per picked lead", len(sample) == 1 and "Make sample clips" in str(sample[0].label),
          [b.label for b in at.button])
    started: list = []
    real_start = J.start
    J.start = lambda kind, label, argv, **k: started.append((kind, label, argv)) or real_start(
        kind, label, [sys.executable, "-c", "print('stub')"], jobs_dir=TMP / "jobs")
    try:
        if sample and not J.running("stream"):
            sample[0].click().run()
            check("the button starts clip_stream as a stream job on that lead's VOD", len(started) == 1
                  and started[0][0] == "stream" and "--campaign" in started[0][2]
                  and started[0][2][started[0][2].index("--count") + 1] == "2", started)
    finally:
        J.start = real_start
except ImportError as exc:  # streamlit missing
    check("streamlit installed for the app check", False, exc)

print(f"\n{len(FAILED)} failed" if FAILED else "\nAll Round 64 checks passed.")
sys.exit(1 if FAILED else 0)
