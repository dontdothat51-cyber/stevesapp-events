# -*- coding: utf-8 -*-
"""EVENTS PACK (Live Events Phase 1, 9/17; WIDER REFERENCE 9/18). The reference feed for the Live Now row: what is on,
normalized to one JSON the Stick reads through the Bouncer. Devices NEVER call any of these sources.

SOURCES (owner 9/18: "start with the data list"; free only):
  * ESPN scoreboards (site.api.espn.com), one call per Eastern day D-1/D/D+1 per board (team boards refuse ranges)
  * KHL: the league's own public feed (khl.api.webcaster.pro events_v2.json) -- teams, crests, state, start
  * Wikipedia (MediaWiki API): WWE / AEW premium events and boxing majors from the yearly pages' date lines
  * DAZN boxing schedule page (HTML) -- the promoter's own calendar, dates + "A vs B"
  * tools/data/broadcasters.json: rights holders per league by region (CANON network names) appended to every event,
    and the fixed WEEKLY wrestling shows (Raw/SmackDown/NXT/Dynamite/Collision) -- no board exists for them
Every source is fail-soft: a failed board is reported (ok=false) and the run still publishes; more than half of the
ESPN boards failing = exit 1 = no publish = the last good pack stays.

    cdn/events.json.gz            the pack (gzip JSON)
    cdn/events_manifest.json      {version yyyymmddHHMM UTC, file, sha256, bytes, generatedAt, events, live, boards}
    python tools/build_eventspack.py            # build
    python tools/publish_events_http.py events  # -> the server's publish door validates and stores it

Pack = {version, generatedAt, boards: [{league, ok, events}], events: [
  {id, league, sport, tier, name, shortName, start (ISO UTC), status: {state: pre|in|post, detail, completed},
   teams: [{key, id, abbr, name, home, keyed}], broadcasters: [raw source names], networks: [canon network names],
   source: espn|khl|wiki|dazn|weekly}]}
RULES: the pack alone NEVER creates a card (NO GHOST LIVES); status decides existence only, never a score; team.key ==
the team vault key (tools/data/team_keys.json) so a card always finds its crest; networks = ESPN's broadcasters mapped
to canon names + the table's rights holders for the league; the device attaches ONLY what its server carries.
"""
import datetime
import gzip, hashlib, io, json, os, re, sys, time, datetime as dt, html, urllib.request, urllib.parse, unicodedata, concurrent.futures as cf
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDN = os.path.join(ROOT, "cdn"); os.makedirs(CDN, exist_ok=True)
DATA = os.path.join(ROOT, "tools", "data")
def _load(name, required):
    """audit 9/18: a hand-edited data file must degrade with a clear message, never a bare traceback before main()."""
    try:
        return json.load(open(os.path.join(DATA, name), encoding="utf-8"))
    except Exception as e:
        if required: raise SystemExit(f"::error::{name} unreadable ({e}) -- not publishing")
        print(f"::warning::{name} unreadable ({e}) -- continuing without it"); return {}
KEYS = _load("team_keys.json", required=True)
TABLE = _load("broadcasters.json", required=False)
# 9/25 (Sye 1: "Cubs at Red Sox" listed only Marquee; his Marquee feed was dead while NESN sat unused): a team's regional
# network is a PER-GAME fact (that team is playing), unlike the league table -- it joins `networks`, never `hints`.
RSN = _load("rsn.json", required=False) or {}
if "leagues" not in TABLE: TABLE = {"leagues": {}, "weekly": []}

# league -> (sport, espn league, tier, extra query, has teams). Tier: 1 = headline (row order), 3 = filler.
BOARDS = {
    "nfl":        ("football", "nfl", 1, "", True),
    "ncaaf":      ("football", "college-football", 2, "&groups=80&limit=300", True),
    "nba":        ("basketball", "nba", 1, "", True),
    "wnba":       ("basketball", "wnba", 2, "", True),
    "ncaab":      ("basketball", "mens-college-basketball", 2, "&groups=50&limit=400", True),
    "mlb":        ("baseball", "mlb", 2, "", True),
    "nhl":        ("hockey", "nhl", 2, "", True),
    "mls":        ("soccer", "usa.1", 2, "", True),
    "epl":        ("soccer", "eng.1", 1, "", True),
    "laliga":     ("soccer", "esp.1", 2, "", True),
    "seriea":     ("soccer", "ita.1", 2, "", True),
    "bundesliga": ("soccer", "ger.1", 2, "", True),
    "ligue1":     ("soccer", "fra.1", 2, "", True),
    "ucl":        ("soccer", "uefa.champions", 1, "", True),
    "uel":        ("soccer", "uefa.europa", 2, "", True),
    "ligamx":     ("soccer", "mex.1", 2, "", True),
    # 9/24 NATIONS (owner: six Nations League games on the server, none on Live Now -- no international board existed).
    # Every league below keys its teams through the ONE national roster (team_keys.json["national"], crest pack
    # teams-national); keep this set in step with LogoVault.NATIONAL on the Stick and NATIONAL_SOURCES in build_teamvault.
    "uefa_nations":     ("soccer", "uefa.nations", 1, "", True),
    "wcq_uefa":         ("soccer", "fifa.worldq.uefa", 1, "", True),
    "wcq_conmebol":     ("soccer", "fifa.worldq.conmebol", 1, "", True),
    "wcq_concacaf":     ("soccer", "fifa.worldq.concacaf", 1, "", True),
    "wcq_afc":          ("soccer", "fifa.worldq.afc", 3, "", True),
    "wcq_caf":          ("soccer", "fifa.worldq.caf", 3, "", True),
    "concacaf_nations": ("soccer", "concacaf.nations.league", 2, "", True),
    "world_cup":        ("soccer", "fifa.world", 1, "", True),
    "euro":             ("soccer", "uefa.euro", 1, "", True),
    "copa_america":     ("soccer", "conmebol.america", 1, "", True),
    "gold_cup":         ("soccer", "concacaf.gold", 2, "", True),
    "afcon":            ("soccer", "caf.nations", 2, "", True),
    "asian_cup":        ("soccer", "afc.asian.cup", 3, "", True),
    "friendlies":       ("soccer", "fifa.friendly", 3, "", True),
    "libertadores": ("soccer", "conmebol.libertadores", 2, "", True),
    "brasileirao": ("soccer", "bra.1", 3, "", True),
    "argentina":  ("soccer", "arg.1", 3, "", True),
    "eredivisie": ("soccer", "ned.1", 3, "", True),
    "saudi":      ("soccer", "ksa.1", 3, "", True),
    "ufc":        ("mma", "ufc", 1, "", False),
    "pfl":        ("mma", "pfl", 3, "", False),
    "pga":        ("golf", "pga", 3, "", False),
    "atp":        ("tennis", "atp", 3, "", False),
    "wta":        ("tennis", "wta", 3, "", False),
    "f1":         ("racing", "f1", 2, "", False),
    "nascar":     ("racing", "nascar-premier", 3, "", False),
    "cricket_ipl":     ("cricket", "8048", 2, "", True),
    "cricket_bbl":     ("cricket", "8039", 3, "", True),
    "cricket_cpl":     ("cricket", "8676", 3, "", True),
    "cricket_hundred": ("cricket", "19301", 3, "", True),
    "rugby_prem":  ("rugby", "267979", 3, "", True),
    "super_rugby": ("rugby", "242041", 3, "", True),
    "urc":         ("rugby", "270557", 3, "", True),
    "nrl":         ("rugby-league", "3", 3, "", True),
    "euroleague":  ("basketball", "euroleague", 3, "", True),
}

# ESPN broadcaster name -> canon network name (CurationCanon.kt Sports/Premium rows). Unmapped names pass through
# unchanged (the device's canon matcher gets a second try); team-owned streams (Brewers.TV) map to nothing.
BROADCASTERS = {
    "ESPN": "ESPN", "ESPN2": "ESPN2", "ESPNU": "ESPNU", "ESPNEWS": "ESPNEWS", "ESPN Deportes": "ESPN Deportes",
    "ESPN+": "ESPN+", "ESPN Unlmtd": "ESPN+", "ESPN Unlimited": "ESPN+", "ABC": "ABC", "ACC Network": "ACC Network",
    "ACCN": "ACC Network", "SEC Network": "SEC Network", "SECN": "SEC Network", "Big Ten Network": "Big Ten Network",
    # 9/19 (owner: Tennessee-Kennesaw card played SEC Network TV, which had another game): the "+"/"X" overflow streams are
    # PLATFORMS -- a card exists only when the server names a slot for the game, never the network's TV channel
    "SECN+": "SEC Network+", "SEC Network+": "SEC Network+", "ACCNX": "ACC Network Extra", "ACC Network Extra": "ACC Network Extra",
    "BTN+": "BTN+", "ESPN3": "ESPN3", "Peacock Premium": "Peacock",
    "BTN": "Big Ten Network", "FOX": "FOX", "FS1": "FS1", "FS2": "FS2", "Fox Deportes": "Fox Deportes",
    "NBC": "NBC", "Peacock": "Peacock", "USA Net": "USA Network", "USA Network": "USA Network", "Golf Chnl": "Golf Channel",
    "Golf Channel": "Golf Channel", "CBS": "CBS", "CBSSN": "CBS Sports Network", "CBS Sports Network": "CBS Sports Network",
    "Paramount+": "Paramount+", "CBS Sports Golazo": "CBS Sports Golazo", "TNT": "TNT", "TBS": "TBS", "truTV": "truTV",
    "Max": "Max", "HBO Max": "Max", "NFL Network": "NFL Network", "NFL Net": "NFL Network", "NFL RedZone": "NFL RedZone",
    "NBA TV": "NBA TV", "MLB Network": "MLB Network", "MLB Net": "MLB Network", "NHL Network": "NHL Network",
    "Prime Video": "Prime Video", "Amazon Prime Video": "Prime Video", "Apple TV+": "Apple TV+", "Apple TV": "Apple TV+",
    "MLS Season Pass": "Apple TV+", "Netflix": "Netflix", "DAZN": "DAZN 1", "Tennis Channel": "Tennis Channel",
    "Univision": "Univision", "UniMas": "UniMas", "TUDN": "TUDN", "Telemundo": "Telemundo", "Tele": "Telemundo", "Universo": "Universo",
    "beIN SPORTS": "beIN Sports", "beIN Sports": "beIN Sports", "Sky Sports": "Sky Sports Main Event",
    "Sky Sports Premier League": "Sky Sports Premier League", "Sky Sports Football": "Sky Sports Football",
    "Sky Sports F1": "Sky Sports F1", "TNT Sports": "TNT Sports 1", "Premier Sports": "Premier Sports",
    "MSG": "MSG", "YES": "YES Network", "YES Network": "YES Network", "NESN": "NESN", "Marquee": "Marquee Sports Network",
    "Marquee Sports Network": "Marquee Sports Network", "CHSN": "Chicago Sports Network", "Root Sports": "Root Sports",
    "ROOT SPORTS": "Root Sports", "SportsNet LA": "Spectrum SportsNet", "Spectrum SportsNet": "Spectrum SportsNet",
    "NBC Sports CA": "NBC Sports California", "NBC Sports Bay Area": "NBC Sports Bay Area", "NBC Sports Boston": "NBC Sports Boston",
    "NBC Sports BO": "NBC Sports Boston", "NBC Sports Chicago": "NBC Sports Chicago", "NBC Sports Philadelphia": "NBC Sports Philadelphia",
    "Altitude": "Altitude Sports", "Monumental Sports Network": "Monumental Sports Network", "MNMT": "Monumental Sports Network",
    "FanDuel Sports Network": "FanDuel Sports", "FDSN": "FanDuel Sports", "Bally Sports": "Bally Sports", "ION": "ION",
    "Fandango": "Fandango at Home", "Willow": "Willow Cricket", "Willow TV": "Willow Cricket", "Star Sports": "Star Sports",
    "Scripps Sports": "Scripps Sports", "MLB.TV": None, "NBA League Pass": None, "WNBA League Pass": None, "NHL.TV": None, "fuboTV": None,
}

# 9/17 MEASURED from Cloudflare's edge and a GitHub runner (datacenter IPs): site.api.espn.com answers the custom
# "stevesapp-pipeline" agent, an empty agent and spoofed browser agents with Akamai "Access Denied" (403), but takes the
# known tool agents (python-requests / curl / okhttp) -- and the spoofed Chrome agent is refused even from a home IP.
UA = "python-requests/2.32.3"
def fetch(url, timeout=40, ua=UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    return urllib.request.urlopen(req, timeout=timeout).read()

def slug(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s.lower() if ch.isalnum() and ord(ch) < 128)

def now_utc(): return dt.datetime.now(dt.timezone.utc)
def iso(t): return t.strftime("%Y-%m-%dT%H:%M:%SZ")

def _is_edt(now):  # US DST: 2nd Sunday of March 07:00 UTC .. 1st Sunday of November 06:00 UTC
    y = now.year
    mar = dt.datetime(y, 3, 8, 7, tzinfo=dt.timezone.utc); mar += dt.timedelta(days=(6 - mar.weekday()) % 7)
    nov = dt.datetime(y, 11, 1, 6, tzinfo=dt.timezone.utc); nov += dt.timedelta(days=(6 - nov.weekday()) % 7)
    return mar <= now < nov
def et_offset(now): return dt.timedelta(hours=-4 if _is_edt(now) else -5)

def eastern_days():
    """ESPN's dates= is an Eastern-time day: D-1, D, D+1. ONE DAY PER CALL -- the team-sport boards answer a
    YYYYMMDD-YYYYMMDD range with HTTP 400 (only golf/tennis accept ranges; measured 9/17)."""
    d0 = (now_utc() + et_offset(now_utc())).date()
    return [(d0 + dt.timedelta(days=k)).strftime("%Y%m%d") for k in (-1, 0, 1)]

def table_networks(league):
    out = []
    for region in ("us", "uk", "intl"):
        for n in (TABLE["leagues"].get(league) or {}).get(region, []):
            if n not in out: out.append(n)
    return out

def networks_of(comp, league):
    raw, nets = [], []
    for b in comp.get("broadcasts") or []:
        for n in b.get("names") or []:
            if n and n not in raw: raw.append(n)
    for g in comp.get("geoBroadcasts") or []:
        n = ((g.get("media") or {}).get("shortName") or "").strip()
        if n and n not in raw: raw.append(n)
    for n in raw:
        m = BROADCASTERS.get(n, n)
        if m and m not in nets: nets.append(m)
    # 9/19 HOTFIX (owner: "Cardinals-Nationals showed MLB Network, but MLB Network was playing another game"): the
    # table's rights holders are NOT per-game facts -- appended to `networks` they attached the ONE MLB Network / ESPN
    # channel to EVERY game of the league = wrong game on the card. They now travel as `hints` (the app ignores them
    # until the "only live game of the league" rule ships); `networks` = ESPN's per-game broadcasters only.
    return raw, nets

def hints_of(league):
    return table_networks(league)

def status_of(st):
    return {"state": st.get("state") or "pre", "detail": st.get("shortDetail") or st.get("detail") or "", "completed": bool(st.get("completed"))}

# ---- ESPN -----------------------------------------------------------------------------------------------------
NATIONAL = {"uefa_nations", "wcq_uefa", "wcq_conmebol", "wcq_concacaf", "wcq_afc", "wcq_caf", "concacaf_nations",
            "world_cup", "euro", "copa_america", "gold_cup", "afcon", "asian_cup", "friendlies"}

def espn_board(league):
    sport, lg, tier, extra, has_teams = BOARDS[league]
    keys = KEYS.get("national" if league in NATIONAL else league, {})   # 9/24 NATIONS: one roster for every international board
    out, seen = [], set()
    raw_events = []
    for day in eastern_days():
        d = json.loads(fetch(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/scoreboard?dates={day}{extra}"))
        raw_events += d.get("events") or []
    for e in raw_events:
        if str(e.get("id")) in seen: continue  # a game can sit on two Eastern days' boards
        seen.add(str(e.get("id")))
        comps = e.get("competitions") or []
        if not comps: continue
        c = comps[0]
        st = (c.get("status") or e.get("status") or {}).get("type") or {}
        end_iso = ""
        if len(comps) > 1 and not has_teams:
            # 9/19 (owner: UFC 331 missing from Live Now): a fight card / golf event is ONE event holding many bouts --
            # comps[0] is the first early prelim, "Final" by 6 PM, and the whole card read as over. The event-level
            # status and date rule; the end = the last bout's slot + 3 h (the app's per-sport default is too short).
            st = (e.get("status") or {}).get("type") or st
            try:
                last = max(cc.get("date") or "" for cc in comps)
                lt = datetime.datetime.strptime(last[:16], "%Y-%m-%dT%H:%M") + datetime.timedelta(hours=3)
                end_iso = iso(lt)
            except Exception: end_iso = ""
        teams = []
        if has_teams:
            for cp in c.get("competitors") or []:
                t = cp.get("team") or {}
                tid = str(t.get("id") or "")
                teams.append({"key": keys.get(tid) or slug(t.get("displayName")), "id": tid, "abbr": t.get("abbreviation") or "",
                              "name": t.get("displayName") or "", "home": cp.get("homeAway") == "home", "keyed": tid in keys})
        raw, nets = networks_of(c, league)
        for t in teams:   # 9/25: both teams' regional networks (rsn.json), after the reference's own names
            for n in (RSN.get(league) or {}).get(t.get("key") or "", []):
                if n not in nets: nets.append(n)
        ev = {"id": "espn:" + str(e.get("id")), "league": league, "sport": sport, "tier": tier,
              "name": e.get("name") or "", "shortName": e.get("shortName") or "",
              "start": (e.get("date") if (len(comps) > 1 and not has_teams) else None) or c.get("date") or e.get("date") or "", "status": status_of(st),
              "teams": teams, "broadcasters": raw, "networks": nets, "hints": hints_of(league), "source": "espn"}
        if end_iso: ev["end"] = end_iso
        out.append(ev)
    return out

# ---- KHL (official public feed) ----------------------------------------------------------------------------------
def khl_board():
    d = json.loads(fetch("https://khl.api.webcaster.pro/api/khl_mobile/events_v2.json?locale=en", 40, "Mozilla/5.0"))
    items = d if isinstance(d, list) else d.get("events") or []
    keys = KEYS.get("khl", {})
    out = []
    # audit 9/18: this feed answered with NEXT SEASON's dates (March 2027) and ignores every date/stage parameter tried;
    # an empty window with items present is reported as a FAILED board, never as "ok, 0 events".
    def _within(w):
        e = w.get("event") or w; s = e.get("start_at")
        return s and abs(dt.datetime.fromtimestamp(int(s) / 1000, tz=dt.timezone.utc) - now_utc()) <= dt.timedelta(days=2)
    if items and not any(_within(w) for w in items):
        first = (items[0].get("event") or items[0]).get("start_at")
        raise RuntimeError(f"feed lists other dates only ({len(items)} items, first {dt.datetime.fromtimestamp(int(first) / 1000, tz=dt.timezone.utc):%Y-%m-%d})")
    for w in items:
        e = w.get("event") or w
        start = e.get("start_at")
        if not start: continue
        t = dt.datetime.fromtimestamp(int(start) / 1000, tz=dt.timezone.utc)
        if abs((t - now_utc()).total_seconds()) > 2 * 86400: continue
        gs = (e.get("game_state_key") or "").lower()
        state = "in" if gs in ("in_progress", "live", "started") else "post" if gs in ("finished", "ended") else "pre"
        teams = []
        for side, tm in (("a", e.get("team_a") or {}), ("b", e.get("team_b") or {})):
            tid = str(tm.get("id") or ""); nm = tm.get("name") or ""
            teams.append({"key": keys.get(tid) or slug(nm), "id": tid, "abbr": "", "name": nm, "home": side == "a", "keyed": tid in keys})
        out.append({"id": "khl:" + str(e.get("id")), "league": "khl", "sport": "hockey", "tier": 3, "name": e.get("name") or "",
                    "shortName": e.get("name") or "", "start": iso(t), "status": {"state": state, "detail": gs, "completed": state == "post"},
                    "teams": teams, "broadcasters": [], "networks": [], "hints": table_networks("khl"), "source": "khl"})
    return out

# ---- Weekly wrestling shows (fixed) -----------------------------------------------------------------------------
def weekly_shows():
    out = []
    now = now_utc(); off = et_offset(now)
    today_et = (now + off).date()
    for w in TABLE.get("weekly", []):
        for k in (-1, 0, 1):
            d = today_et + dt.timedelta(days=k)
            if d.isoweekday() != int(w["dowEt"]): continue
            hh, mm = w["startEt"].split(":")
            start = dt.datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=dt.timezone.utc) - off
            end = start + dt.timedelta(hours=float(w.get("hours", 2)))
            state = "in" if start <= now < end else "post" if now >= end else "pre"
            out.append({"id": "weekly:" + slug(w["name"]) + ":" + d.isoformat(), "league": w["league"], "sport": "wrestling", "tier": int(w.get("tier", 3)),
                        "name": w["name"], "shortName": w["name"], "start": iso(start), "end": iso(end), "status": {"state": state, "detail": "weekly", "completed": state == "post"},
                        "teams": [], "broadcasters": list(w.get("networks", [])), "networks": list(w.get("networks", [])),
                        # 0.14.10 (owner diag 9/22, NXT fronted by TNT Sports 1 then ESPN): a weekly show names its OWN channel only
                        # (NXT = The CW, Raw = Netflix); the league's rights table is a HINT, never an authoritative network
                        "hints": table_networks(w["league"]), "source": "weekly"})
    return out

# ---- Wikipedia: WWE / AEW premium events, boxing majors --------------------------------------------------------------
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}
DATE_RX = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b|\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b")

def wiki_text(title):
    u = "https://en.wikipedia.org/w/api.php?action=parse&prop=wikitext&format=json&formatversion=2&page=" + urllib.parse.quote(title)
    d = json.loads(fetch(u, 40, "stevesapp-events-builder/1.0 (contact via github dontdothat51-cyber)"))
    return (d.get("parse") or {}).get("wikitext") or ""

CELL_DATE_RX = re.compile(r"^\s*\|\s*(?:'{2,3})?(?:(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})|(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December))(?:,?\s*(\d{4}))?\s*(?:'{2,3})?\s*$", re.M)
YEAR_HDR_RX = re.compile(r"^=+\s*(\d{4})\s*=+\s*$", re.M)

def wiki_events(title, league, tier, default_start_et, hours, name_rx):
    """audit 9/18 rewrite. The yearly list pages write the DATE in its own table cell WITHOUT a year ("|January 24"), the
    year in the section header ("===2026==="), and citations (<ref>...</ref>) that carry unrelated dates. So: strip refs,
    track the section year, read the date from a whole cell, and take the event name as the DISPLAY half of the first
    [[link|display]] after it. Start = the promotion's usual slot in Eastern (no clock is written on these pages)."""
    out = []
    now = now_utc(); off = et_offset(now)
    text = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", wiki_text(title), flags=re.S)
    win = (now - dt.timedelta(days=1), now + dt.timedelta(days=2))
    year = now.year
    for chunk in re.split(r"\n\|-", text):
        for yh in YEAR_HDR_RX.findall(chunk): year = int(yh)   # a header inside the chunk applies to the rows after it
        m = CELL_DATE_RX.search(chunk)
        if not m: continue
        if m.group(1): mon, day = MONTHS[m.group(1)], int(m.group(2))
        else: mon, day = MONTHS[m.group(4)], int(m.group(3))
        y = int(m.group(5)) if m.group(5) else year
        try: d = dt.date(y, mon, day)
        except ValueError: continue
        hh, mm = default_start_et.split(":")
        start = dt.datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=dt.timezone.utc) - off
        if not (win[0] <= start <= win[1]): continue
        after = chunk[m.end():]
        nm = name_rx.search(after)
        raw_name = (nm.group(1) or (nm.group(2) if nm.lastindex and nm.lastindex >= 2 else "")) if nm else ""
        name = html.unescape(raw_name.split("|")[-1]).strip(" '|")   # "[[Royal Rumble (2026)|Royal Rumble]]" -> "Royal Rumble"
        if not name or len(name) < 3: continue
        end = start + dt.timedelta(hours=hours)
        state = "in" if start <= now < end else "post" if now >= end else "pre"
        out.append({"id": f"wiki:{league}:{slug(name)}:{d.isoformat()}", "league": league, "sport": "wrestling" if league in ("wwe", "aew") else "boxing",
                    "tier": tier, "name": name, "shortName": name, "start": iso(start), "status": {"state": state, "detail": "wikipedia", "completed": state == "post"},
                    "teams": [], "broadcasters": [], "networks": [], "hints": table_networks(league), "source": "wiki"})   # 0.14.10 audit: the table is a hint
    return out

def wiki_boards():
    y = now_utc().year
    link_rx = re.compile(r"\|\s*'{0,3}\[\[([^\]]+)\]\]")
    fight_rx = re.compile(r"\|\s*'{0,3}\[\[([^\]]+)\]\]|\|\s*([A-Z][A-Za-z.' -]+ vs\.? [A-Z][A-Za-z.' -]+)")
    jobs = [
        ("List of WWE pay-per-view and livestreaming supercards", "wwe", 1, "19:00", 4, link_rx),
        ("List of All Elite Wrestling pay-per-view events", "aew", 2, "20:00", 4, link_rx),
        (f"{y} in Zuffa Boxing", "boxing", 1, "20:00", 5, fight_rx),
        (f"{y} in Misfits Boxing and MF Pro", "boxing", 3, "20:00", 4, fight_rx),
    ]
    out, boards = [], []
    for title, league, tier, start_et, hours, rx in jobs:
        try:
            evs = wiki_events(title, league, tier, start_et, hours, rx); out += evs; boards.append({"league": f"wiki:{league}", "ok": True, "events": len(evs)})
        except Exception as e:
            boards.append({"league": f"wiki:{league}", "ok": False, "events": 0, "error": str(e)[:120]})
    return out, boards

# ---- DAZN boxing schedule page ------------------------------------------------------------------------------------
def dazn_boxing():
    page = fetch("https://www.dazn.com/en-US/news/boxing/boxing-schedule", 40, "Mozilla/5.0 (Windows NT 10.0; Win64; x64)").decode("utf-8", "replace")
    text = html.unescape(re.sub(r"<[^>]+>", "\n", page))
    now = now_utc(); off = et_offset(now)
    out, seen = [], set()
    cur_date = None
    for line in (l.strip() for l in text.splitlines()):
        if not line: continue
        m = DATE_RX.search(line)
        if m:
            if m.group(1): mon, day, year = MONTHS[m.group(1)], int(m.group(2)), int(m.group(3))
            else: mon, day, year = MONTHS[m.group(5)], int(m.group(4)), int(m.group(6))
            try: cur_date = dt.date(year, mon, day)
            except ValueError: cur_date = None
            continue
        f = re.search(r"([A-Z][A-Za-z.'\- ]{2,40}) vs\.? ([A-Z][A-Za-z.'\- ]{2,40})", line)
        if f and cur_date:
            name = f"{f.group(1).strip()} vs {f.group(2).strip()}"
            start = dt.datetime(cur_date.year, cur_date.month, cur_date.day, 20, 0, tzinfo=dt.timezone.utc) - off
            if abs((start - now).total_seconds()) > 2 * 86400: continue
            k = slug(name) + cur_date.isoformat()
            if k in seen: continue
            seen.add(k)
            end = start + dt.timedelta(hours=5)
            state = "in" if start <= now < end else "post" if now >= end else "pre"
            out.append({"id": "dazn:" + k, "league": "boxing", "sport": "boxing", "tier": 2, "name": name, "shortName": name, "start": iso(start),
                        "status": {"state": state, "detail": "dazn schedule", "completed": state == "post"}, "teams": [],
                        "broadcasters": ["DAZN"], "networks": ["DAZN 1", "DAZN Boxing"], "hints": table_networks("boxing"), "source": "dazn"})   # 0.14.10 audit
    return out

# ---- main ----------------------------------------------------------------------------------------------------------
def wait_for_network(host="site.api.espn.com", limit_s=120):
    """9/22: the laptop's scheduled task fires at wake while Wi-Fi is still reconnecting (every board failed on DNS at
    6:30 PM). Wait up to two minutes for a name lookup before touching any board; give up = the guard refuses the pass."""
    import socket
    t = time.time()
    while True:
        try:
            socket.getaddrinfo(host, 443); return True
        except OSError:
            if time.time() - t > limit_s:
                print(f"::warning::no network after {limit_s}s (DNS for {host} failed) -- continuing, the guard decides"); return False
            time.sleep(5)

def main():
    t0 = time.time(); boards = []; events = []
    wait_for_network()
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(espn_board, lg): lg for lg in BOARDS}
        for f in futs:
            lg = futs[f]
            try:
                evs = f.result(); events += evs; boards.append({"league": lg, "ok": True, "events": len(evs)})
            except Exception as e:
                boards.append({"league": lg, "ok": False, "events": 0, "error": str(e)[:120]})
    espn_failed = [b["league"] for b in boards if not b["ok"]]
    # audit 9/18: DAZN's schedule page is a JavaScript shell (no server-rendered text) -> the parser returned 0 forever; OFF
    # until DAZN's data endpoint is captured. Boxing = the Wikipedia Zuffa/Misfits pages + the broadcaster table.
    for name, fn in (("khl", khl_board), ("weekly", weekly_shows)):
        try:
            evs = fn(); events += evs; boards.append({"league": name, "ok": True, "events": len(evs)})
        except Exception as e:
            boards.append({"league": name, "ok": False, "events": 0, "error": str(e)[:120]})
    wevs, wboards = wiki_boards(); events += wevs; boards += wboards
    # dedupe boxing/wrestling by name+day across sources (DAZN page vs Wikipedia): first source wins
    seen = set(); uniq = []
    for e in events:
        k = (e["league"], slug(e["name"]), e["start"][:10]) if e["source"] in ("wiki", "dazn") else e["id"]
        if k in seen: continue
        seen.add(k); uniq.append(e)
    events = uniq
    boards.sort(key=lambda b: b["league"])
    events.sort(key=lambda e: (e["tier"], e["start"]))
    live = sum(1 for e in events if e["status"]["state"] == "in")
    unkeyed = sum(1 for e in events for t in e["teams"] if not t["keyed"])
    now = now_utc()
    ver = now.strftime("%Y%m%d%H%M")
    pack = {"version": ver, "generatedAt": iso(now), "boards": boards, "events": events}
    raw = json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    gz = gzip.compress(raw, mtime=0)
    failed = [b["league"] for b in boards if not b["ok"]]
    # 9/22 (owner check before 14.8): the laptop woke with no network, every ESPN board failed on DNS, and the guard
    # below stopped the task -- but the 3-event pack was ALREADY on disk, and a later publish step shipped it (Live Now
    # empty for every customer). Nothing is written until the pack passes; a refused pass leaves the last good file.
    if len(espn_failed) * 2 > len(BOARDS):
        for b in boards:
            if not b["ok"]: print(f"  {b['league']:16s} FAIL  {b.get('error','')}")
        raise SystemExit("::error::more than half the ESPN boards failed -- not writing or publishing (last good pack stays)")
    path = os.path.join(CDN, "events.json.gz"); open(path, "wb").write(gz)
    man = {"version": ver, "file": "events.json.gz", "sha256": hashlib.sha256(gz).hexdigest(), "bytes": len(gz),
           "generatedAt": pack["generatedAt"], "events": len(events), "live": live, "boards": len(boards), "failed": failed}
    json.dump(man, open(os.path.join(CDN, "events_manifest.json"), "w", encoding="utf-8"), indent=1)
    for b in boards:
        print(f"  {b['league']:16s} {'ok ' if b['ok'] else 'FAIL'} {b['events']:4d}" + (f"  {b.get('error','')}" if not b["ok"] else ""))
    by = {}
    for e in events: by[e["source"]] = by.get(e["source"], 0) + 1
    print(f"events pack {ver}: {len(events)} events ({live} live now) by source {by}, {len(gz)/1024:.0f} KB gz, unkeyed team refs {unkeyed}, "
          f"{len(failed)} boards failed, {time.time()-t0:.1f}s")

if __name__ == "__main__":
    main()
