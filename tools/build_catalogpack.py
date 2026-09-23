"""
CATALOG PACK (2026-09-23, owner: "can the refresh be hands off?"): what changed in the provider's movie and series
catalogue since the last run, mirrored every few minutes by the always-on builder so devices never pull the full lists.

One line (env SLOTS_LINE = "server|user|pass") makes two catalog calls (get_vod_streams, get_series). The previous
run's state (ids + series last_modified) is restored by the workflow's cache into cdn-build/catalog_state.json.gz;
the first run with no state publishes nothing and only saves state.
Output: cdn/catalog.json.gz {version, generatedAt, base, movies_added:[[id, name, cat, ext, added]...],
        movies_removed:[id...], series_added:[[id, name, cat, cover, last_modified]...], series_removed:[id...],
        series_changed:[[id, last_modified]...]}   (rows in the provider's own field order the app already parses)
        cdn/catalog_manifest.json {version, file, sha256, bytes, generatedAt, base, counts...}
Posture: a provider error, an implausible answer, or an absurd diff (a lost state would make everything "added")
exits non-zero and publishes nothing; the state is saved only after a good read.
"""
import datetime as dt, gzip, hashlib, json, os, sys, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDN = os.path.join(ROOT, "cdn"); os.makedirs(CDN, exist_ok=True)
STATE_DIR = os.path.join(ROOT, "cdn-build"); os.makedirs(STATE_DIR, exist_ok=True)
STATE = os.path.join(STATE_DIR, "catalog_state.json.gz")
UA = "Mozilla/5.0 (Linux; Android) StevesApp"
MAX_ADDED = 20000   # more than this in one diff = a lost state or a provider swap, never a real 5-minute change


def api(base, user, pw, action):
    req = urllib.request.Request(f"{base}/player_api.php?username={user}&password={pw}&action={action}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=240) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    line = os.environ.get("SLOTS_LINE", "").strip()
    if line.count("|") != 2:
        print("::error::SLOTS_LINE not set (server|user|pass)"); sys.exit(1)
    base, user, pw = [x.strip().rstrip("/") for x in line.split("|")]
    try:
        vod = api(base, user, pw, "get_vod_streams")
        ser = api(base, user, pw, "get_series")
    except urllib.error.HTTPError as e:
        print(f"::error::provider answered {e.code} -- nothing written"); sys.exit(1)
    except Exception as e:
        print(f"::error::provider unreachable: {type(e).__name__} {str(e)[:120]} -- nothing written"); sys.exit(1)
    if not isinstance(vod, list) or not isinstance(ser, list) or len(vod) < 1000 or len(ser) < 100:
        print(f"::error::implausible answer (vod={len(vod) if isinstance(vod, list) else '?'} series={len(ser) if isinstance(ser, list) else '?'}) -- nothing written"); sys.exit(1)

    movies = {}
    for s in vod:
        try: sid = int(s.get("stream_id"))
        except Exception: continue
        movies[sid] = [sid, str(s.get("name") or ""), str(s.get("category_id") or ""), str(s.get("container_extension") or ""), int(s.get("added") or 0)]
    series = {}
    for s in ser:
        try: sid = int(s.get("series_id"))
        except Exception: continue
        series[sid] = [sid, str(s.get("name") or ""), str(s.get("category_id") or ""), str(s.get("cover") or ""), int(s.get("last_modified") or 0)]

    prev = None
    if os.path.exists(STATE):
        try: prev = json.load(gzip.open(STATE, "rt", encoding="utf-8"))
        except Exception as e: print(f"::warning::state unreadable ({e}) -- treated as first run"); prev = None
    now = dt.datetime.now(dt.timezone.utc)
    ver = now.strftime("%Y%m%d%H%M")
    state = {"version": ver, "movies": sorted(movies.keys()), "series": {str(k): v[4] for k, v in series.items()}}

    def save_state():
        with gzip.open(STATE, "wt", encoding="utf-8") as f: json.dump(state, f, separators=(",", ":"))

    if prev is None:
        save_state()
        print(f"catalog state seeded ({len(movies)} movies, {len(series)} series) -- no pack this run"); return

    pm = set(prev.get("movies") or []); ps = {int(k): v for k, v in (prev.get("series") or {}).items()}
    movies_added = [movies[i] for i in movies if i not in pm]
    movies_removed = sorted(i for i in pm if i not in movies)
    series_added = [series[i] for i in series if i not in ps]
    series_removed = sorted(i for i in ps if i not in series)
    series_changed = [[i, series[i][4]] for i in series if i in ps and series[i][4] != ps[i]]
    if len(movies_added) > MAX_ADDED or len(series_added) > MAX_ADDED:
        print(f"::error::absurd diff (+{len(movies_added)} movies, +{len(series_added)} series) -- state kept, nothing published"); sys.exit(1)
    pack = {"version": ver, "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "base": prev.get("version"),
            "movies_added": movies_added, "movies_removed": movies_removed,
            "series_added": series_added, "series_removed": series_removed, "series_changed": series_changed}
    gz = gzip.compress(json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)
    open(os.path.join(CDN, "catalog.json.gz"), "wb").write(gz)
    man = {"version": ver, "file": "catalog.json.gz", "sha256": hashlib.sha256(gz).hexdigest(), "bytes": len(gz),
           "generatedAt": pack["generatedAt"], "base": pack["base"], "movies_added": len(movies_added), "movies_removed": len(movies_removed),
           "series_added": len(series_added), "series_removed": len(series_removed), "series_changed": len(series_changed),
           "movies": len(movies), "series": len(series)}
    json.dump(man, open(os.path.join(CDN, "catalog_manifest.json"), "w", encoding="utf-8"), indent=1)
    save_state()
    print(f"catalog pack {ver} (base {man['base']}): +{len(movies_added)}/-{len(movies_removed)} movies, +{len(series_added)}/-{len(series_removed)} series, "
          f"{len(series_changed)} shows changed, {len(gz)/1024:.0f} KB gz ({len(movies)} movies, {len(series)} series read)")


if __name__ == "__main__":
    main()
