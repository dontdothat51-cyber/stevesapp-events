"""
SLOTS PACK (2026-09-23, owner: "45 minutes is lagged for live events"): the provider's event-bucket channel NAMES,
mirrored every few minutes by the always-on builder so devices never poll the provider for names.

One line (env SLOTS_LINE = "server|user|pass") makes two catalog calls (get_live_categories, get_live_streams) -- list
reads, never a stream, so the account's single connection stays free. Event buckets = a category whose name carries
PPV/EVENT/REPLAY words, or whose names are mostly time-prefixed (the app's own rule, EventParser.isEventBucket).
Output: cdn/slots.json.gz  {version, generatedAt, buckets:[{id}], slots:[[streamId, categoryId, name], ...]}
        (9/23 PHASE 3: the provider brand phrase is stripped from every published name -- strip_brand)
        (9/23 PHASE 1: bucket NAMES are not published -- the device never reads them; it takes its bucket ids from its own
        library -- so the provider's category names stay out of our storage)
        cdn/slots_manifest.json  {version, file, sha256, bytes, generatedAt, slots, buckets}
A provider error (auth, busy, 5xx) exits non-zero and writes NOTHING -- the last good pack stays; the builder never
retries in a loop (the next scheduled run is the retry).

10/28 PER FAMILY (docs/SLOTS_PER_FAMILY_BRIEF_2026-10-28.md in the private repo): `--family k8|flux|mega` reads that family's
builder line (k8 = SLOTS_LINE, flux = FLUX_LINE, mega = MEGA_LINE -- the owner's own line, LIST reads only, never a stream) with
the same two list calls. k8 is BYTE-FOR-BYTE today's run: same env, same bucket rule, same files (cdn/slots.json.gz +
cdn/slots_manifest.json), no `family` field -- the door files it under the legacy keys every 0.17.82/83 box reads. Any other
family writes cdn/fam/<family>/slots.json.gz + slots_manifest.json (manifest `family` field; `file` stays "slots.json.gz", the
door's name check) and picks its buckets with the BOX's content rule (EventParser.isEventBucket + looksLikeEvent, RefreshEngine.
bucketFlags): the bucket's words, OR >= 3 event-shaped names, OR >= 8 names of which half are event-shaped. The legacy rule only
knows a leading "HH:MM " clock, and the bracket-stamped slot names other families write ("... LIVE 03 [A vs. B (YYYY-MM-DD
HH:MM:SS)]") would never be read. A family whose line is not set exits 0 with a ::notice:: (nothing written, nothing published).
"""
import argparse, datetime as dt, gzip, hashlib, json, os, re, sys, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDN = os.path.join(ROOT, "cdn"); os.makedirs(CDN, exist_ok=True)
BUCKET_WORDS = re.compile(r"(?i)\b(PPV|EVENT|EVENTS|REPLAY|REPLAYS|PAY PER VIEW)\b")
TIME_PREFIX = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s+(?=\S)")
UA = "Mozilla/5.0 (Linux; Android) StevesApp"
LINE_ENV = {"k8": "SLOTS_LINE", "flux": "FLUX_LINE", "mega": "MEGA_LINE"}   # the Worker's EPS_LINE_ENV, same three families

# 10/28: the box's own "looks like a scheduled matchup" rule (core-parser EventParser.looksLikeEvent), ported regex for regex:
# a leading clock, OR an "A vs B" plus any clock shape (bracket stamp, bare date-time, HH:MM with am/pm/zone, a bare "7pm").
_ZONES = ["GMT", "BST", "UTC", "CET", "CEST", "EET", "EEST", "WET", "WEST", "ET", "EDT", "EST", "CT", "CDT", "CST", "MT", "MDT", "MST",
          "PT", "PDT", "PST", "AEST", "AEDT", "IST", "JST", "KST", "HKT", "SAST", "MSK", "BRT", "ART", "NDT", "NST", "ADT", "AST"]
_ZONE_ALT = "|".join(sorted(_ZONES, key=len, reverse=True))
_TS = re.compile(r"\((\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):\d{2}\)")
_TS_BARE = re.compile(r"(?:@\s*)?(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})(?::\d{2})?")
_TIME_ONLY = re.compile(r"(?i)(?<![\d:.])(\d{1,2})[:.](\d{2})\s*(am|pm)?(?:\s*\(?((?-i:" + _ZONE_ALT + r"))\)?(?![A-Za-z]))?(?![\d:.])")
_TIME_HOUR = re.compile(r"(?i)(?<![\d:.])(\d{1,2})\s*(am|pm)\b(?![\d:.])")
_VS = re.compile(r"(?i)\s+(?:vs\.?|v\.?|versus|@|at|x|-|\u2013|\u2014)\s+")


def looks_like_event(name):
    if TIME_PREFIX.search(name): return True
    if not _VS.search(name): return False
    return bool(_TS.search(name) or _TS_BARE.search(name) or _TIME_ONLY.search(name) or _TIME_HOUR.search(name))


def is_bucket(cat_name, rows, content):
    """k8 (content=False): the 9/23 rule, unchanged. Other families: the box's bucketFlags rule (see the header)."""
    if BUCKET_WORDS.search(cat_name): return True
    names = [str(s.get("name") or "") for s in rows]
    if not content:
        timed = sum(1 for n in names if TIME_PREFIX.search(n))
        return len(rows) >= 8 and timed / len(rows) >= 0.5
    ev = sum(1 for n in names if looks_like_event(n))
    return ev >= 3 or (len(names) >= 8 and ev / len(names) >= 0.5)


# 9/23 PHASE 3: the provider's brand phrase is not part of a channel's name, and it must not sit in our storage naming
# the provider. Stripped here, before publishing -- the SAME rule the app applies at every live-name write and on both
# sides of its pack compare (app-tv/.../Brand.kt, shipped in 0.15.2 / vc 139). Order mattered: the app half had to reach
# every device that reads this pack (vc >= 135) FIRST, or a stripped pack would fail their byte-for-byte name check and
# be refused for good. Proven on the owner's Stick 9/23: "8892 brand-stripped" on the first tick, 0 after, the pack
# still accepted at 98 % agreement. Segment-level only: a bare "8K" is a quality tag and "8KANAL" is a real channel.
_PHRASE = "8K EXCLUSIVE"
_INNER = re.compile(r"\s*-\s*8K\s+EXCLUSIVE(?=\s*-|\s*$)", re.I)
_LEAD = re.compile(r"^\s*8K\s+EXCLUSIVE\s*-\s*", re.I)


def strip_brand(name):
    """Byte-for-byte the same rule as Brand.strip in the app; a name without the phrase is returned unchanged."""
    if _PHRASE.lower() not in name.lower():
        return name
    touched = False
    parts = []
    for raw in name.split("|"):
        seg = raw.strip()
        if seg.lower() == _PHRASE.lower():
            touched = True
            continue
        t = _LEAD.sub("", _INNER.sub("", seg)).strip()
        if t != seg:
            touched = True
        if t:
            parts.append(t)
    if not touched:
        return name
    return " | ".join(parts) or name


def api(base, user, pw, action):
    req = urllib.request.Request(f"{base}/player_api.php?username={user}&password={pw}&action={action}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


FRESH_SKIP_MIN = 12   # audit M3: a family pack younger than this already landed in this window (the clock's dispatch + GitHub's cron)


def landed_this_window(fam):
    """audit M3: two triggers (the server clock's dispatch and GitHub's own cron) often both start a run inside the same 5-min
    family window, and each would read the family's full lists again. Ask the server (/v1/health, the publish key) how old the
    family's pack is; younger than FRESH_SKIP_MIN = this window's read already landed -> skip. Any error = read (fail open)."""
    key = os.environ.get("EVENTS_PUBLISH_KEY", "").strip()
    if not key: return False
    url = os.environ.get("EVENTS_PUBLISH_URL", "https://stevesapp.tv/v1/events/publish").replace("/v1/events/publish", "/v1/health")
    try:
        req = urllib.request.Request(url, headers={"x-events-key": key, "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            h = json.loads(r.read().decode("utf-8", "replace"))
        age = (((h or {}).get("slots") or {}).get(fam) or {}).get("ageMin")
        return isinstance(age, (int, float)) and 0 <= age < FRESH_SKIP_MIN
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", default="k8", choices=sorted(LINE_ENV))
    fam = ap.parse_args().family
    env_name = LINE_ENV[fam]
    line = os.environ.get(env_name, "").strip()
    if fam != "k8" and not line:
        print(f"::notice::{env_name} not set -- {fam} slots skipped (nothing written)"); sys.exit(0)
    if line.count("|") != 2:
        print(f"::error::{env_name} not set (server|user|pass)"); sys.exit(1)
    base, user, pw = [x.strip().rstrip("/") for x in line.split("|")]
    # audit M2: GitHub masks the whole secret, not its parts -- an error that quotes the request URL (a server typed without
    # its scheme, an odd port) would print the login in this PUBLIC repo's logs. Mask each part before any request.
    for part in (base, user, pw):
        if part: print(f"::add-mask::{part}")
    if fam != "k8" and landed_this_window(fam):
        print(f"::notice::{fam} slots pack already landed this window -- skipped (nothing read, nothing written)"); sys.exit(0)
    try:
        auth = api(base, user, pw, "")
        ui = (auth or {}).get("user_info") or {}
        if str(ui.get("auth")) != "1" or str(ui.get("status", "Active")).lower() != "active":
            print(f"::error::line refused: auth={ui.get('auth')} status={ui.get('status')}"); sys.exit(1)
        cats = api(base, user, pw, "get_live_categories")
        streams = api(base, user, pw, "get_live_streams")
    except urllib.error.HTTPError as e:
        print(f"::error::provider answered {e.code} -- nothing written"); sys.exit(1)
    except Exception as e:
        print(f"::error::provider unreachable: {type(e).__name__} {str(e)[:120]} -- nothing written"); sys.exit(1)
    if not isinstance(cats, list) or not isinstance(streams, list) or len(streams) < 1000:
        print(f"::error::implausible answer (cats={type(cats).__name__} streams={len(streams) if isinstance(streams, list) else '?'}) -- nothing written"); sys.exit(1)
    by_cat = {}
    for s in streams:
        by_cat.setdefault(str(s.get("category_id")), []).append(s)
    buckets = []
    for c in cats:
        cid, name = str(c.get("category_id")), str(c.get("category_name") or "")
        rows = by_cat.get(cid) or []
        if is_bucket(name, rows, content=(fam != "k8")):
            buckets.append({"id": cid, "name": name})
    bucket_ids = {b["id"] for b in buckets}
    slots = []
    for s in streams:
        cid = str(s.get("category_id"))
        if cid not in bucket_ids: continue
        try: sid = int(s.get("stream_id"))
        except Exception: continue
        slots.append([sid, cid, strip_brand(str(s.get("name") or ""))])   # 9/23 PHASE 3
    slots.sort()
    now = dt.datetime.now(dt.timezone.utc)
    ver = now.strftime("%Y%m%d%H%M")
    pack = {"version": ver, "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "buckets": [{"id": b["id"]} for b in buckets], "slots": slots}
    if fam != "k8" and not (100 <= len(slots) <= 30000):   # the door's own range (worker publishEvents): say so here, write nothing
        print(f"::error::{fam}: {len(slots)} slots in {len(buckets)} buckets is outside the door's 100..30000 -- nothing written"); sys.exit(1)
    gz = gzip.compress(json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)
    out = CDN if fam == "k8" else os.path.join(CDN, "fam", fam)
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "slots.json.gz"), "wb").write(gz)
    man = {"version": ver, "file": "slots.json.gz", "sha256": hashlib.sha256(gz).hexdigest(), "bytes": len(gz),
           "generatedAt": pack["generatedAt"], "slots": len(slots), "buckets": len(buckets)}
    if fam != "k8": man["family"] = fam   # k8 keeps today's manifest exactly (the door defaults to k8)
    json.dump(man, open(os.path.join(out, "slots_manifest.json"), "w", encoding="utf-8"), indent=1)
    print(f"{'' if fam == 'k8' else fam + ' '}slots pack {ver}: {len(slots)} slots in {len(buckets)} buckets of {len(cats)} categories ({len(streams)} streams read), {len(gz)/1024:.0f} KB gz")


if __name__ == "__main__":
    main()
