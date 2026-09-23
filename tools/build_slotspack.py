"""
SLOTS PACK (2026-09-23, owner: "45 minutes is lagged for live events"): the provider's event-bucket channel NAMES,
mirrored every few minutes by the always-on builder so devices never poll the provider for names.

One line (env SLOTS_LINE = "server|user|pass") makes two catalog calls (get_live_categories, get_live_streams) -- list
reads, never a stream, so the account's single connection stays free. Event buckets = a category whose name carries
PPV/EVENT/REPLAY words, or whose names are mostly time-prefixed (the app's own rule, EventParser.isEventBucket).
Output: cdn/slots.json.gz  {version, generatedAt, buckets:[{id,name}], slots:[[streamId, categoryId, name], ...]}
        cdn/slots_manifest.json  {version, file, sha256, bytes, generatedAt, slots, buckets}
A provider error (auth, busy, 5xx) exits non-zero and writes NOTHING -- the last good pack stays; the builder never
retries in a loop (the next scheduled run is the retry).
"""
import datetime as dt, gzip, hashlib, json, os, re, sys, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDN = os.path.join(ROOT, "cdn"); os.makedirs(CDN, exist_ok=True)
BUCKET_WORDS = re.compile(r"(?i)\b(PPV|EVENT|EVENTS|REPLAY|REPLAYS|PAY PER VIEW)\b")
TIME_PREFIX = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s+(?=\S)")
UA = "Mozilla/5.0 (Linux; Android) StevesApp"


def api(base, user, pw, action):
    req = urllib.request.Request(f"{base}/player_api.php?username={user}&password={pw}&action={action}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    line = os.environ.get("SLOTS_LINE", "").strip()
    if line.count("|") != 2:
        print("::error::SLOTS_LINE not set (server|user|pass)"); sys.exit(1)
    base, user, pw = [x.strip().rstrip("/") for x in line.split("|")]
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
        timed = sum(1 for s in rows if TIME_PREFIX.search(str(s.get("name") or "")))
        if BUCKET_WORDS.search(name) or (len(rows) >= 8 and timed / len(rows) >= 0.5):
            buckets.append({"id": cid, "name": name})
    bucket_ids = {b["id"] for b in buckets}
    slots = []
    for s in streams:
        cid = str(s.get("category_id"))
        if cid not in bucket_ids: continue
        try: sid = int(s.get("stream_id"))
        except Exception: continue
        slots.append([sid, cid, str(s.get("name") or "")])
    slots.sort()
    now = dt.datetime.now(dt.timezone.utc)
    ver = now.strftime("%Y%m%d%H%M")
    pack = {"version": ver, "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "buckets": buckets, "slots": slots}
    gz = gzip.compress(json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)
    open(os.path.join(CDN, "slots.json.gz"), "wb").write(gz)
    man = {"version": ver, "file": "slots.json.gz", "sha256": hashlib.sha256(gz).hexdigest(), "bytes": len(gz),
           "generatedAt": pack["generatedAt"], "slots": len(slots), "buckets": len(buckets)}
    json.dump(man, open(os.path.join(CDN, "slots_manifest.json"), "w", encoding="utf-8"), indent=1)
    print(f"slots pack {ver}: {len(slots)} slots in {len(buckets)} buckets of {len(cats)} categories ({len(streams)} streams read), {len(gz)/1024:.0f} KB gz")


if __name__ == "__main__":
    main()
