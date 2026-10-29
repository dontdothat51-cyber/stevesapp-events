# -*- coding: utf-8 -*-
"""BRAND MASTER LISTS (Phase 4, 2026-10-28; docs/BRAND_LISTS_PHASE4_BRIEF_2026-10-28.md, plan MASTER_PLAN_V2 Phase 4).

What each brand door should hold: every title the service streams by SUBSCRIPTION in the US, from TMDB discover
(`with_watch_providers` + `watch_region=US` + `with_watch_monetization_types=flatrate`; the where-to-watch data TMDB gets
from JustWatch), movies and series apart, plus the company-id unions the plan keeps (Apple Studios, four Netflix ids) and
Marvel = Marvel Studios (company 420) on Disney+. Ids only -- no title text, no provider data, no category names.

Output cdn/brand_lists.json.gz =
  {schema:1, version, generatedAt, region:"US", graceDays:28,
   brands:[{id, door, name, provider_ids, companies, movies:[tmdb ids], series:[tmdb ids],
            seen:{movies:{"<id>":[first_seen,last_seen]}, series:{...}}}],
   credits:{tmdb, justwatch}}
+ cdn/brand_lists_manifest.json; published by tools/publish_events_http.py brand_lists (the publish door checks it again).

GRACE: a title stays on a list GRACE_DAYS (28) after the last run that saw it, so a door does not flicker with the weekly
rotation. The previous lists come from the publish door (GET /v1/brand_lists/state, same single-purpose key) or --prev FILE;
a door that answers anything but the pack or 404 (= first run) stops the run: publishing without the old dates would drop
every title in grace.

BUDGET: HARD CAP of TMDB calls per run (--max-calls, default 3,000 (10/28: Prime whole every week); every HTTP request counts, retries included). All page-1
probes run first; if the pages they announce would pass the cap, the run stops BEFORE fetching them and writes nothing.
Page caches live only in --cache-dir (default: a folder in the run's temp dir), never in the repo.

Key: env TMDB_API_KEY only (never printed, never written).
  python tools/build_brand_lists.py [--only apple] [--max-calls 2500] [--cache-dir DIR] [--prev FILE] [--no-prev] [--out DIR]
Exit 1 on any failure; nothing is written then.
"""
import argparse, datetime as dt, gzip, hashlib, json, os, sys, tempfile, time, urllib.error, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDN = os.path.join(ROOT, "cdn")
API = "https://api.themoviedb.org/3"
DOOR = os.environ.get("EVENTS_PUBLISH_URL", "https://stevesapp.tv/v1/events/publish").replace("/v1/events/publish", "")
GRACE_DAYS = 28
PAGE_CAP = 500            # TMDB discover answers at most 500 pages (10,000 titles) per query
REGION = "US"
CREDITS = {"tmdb": "This product uses the TMDB API but is not endorsed or certified by TMDB.",
           "justwatch": "Data provided by JustWatch"}

# Provider ids pinned 10/26 (docs/CARD_LISTS_DRYRUN_2026-10-26.md s.1); re-checked every run against /watch/providers.
# door = the brain's brand token (tools/brand_map.json `order`, tools/folder_codes_vocab.json brandTokens).
BRANDS = [
    {"id": "apple", "door": "APPLE", "name": "Apple TV+", "providers": [350], "companies": [194232]},
    {"id": "netflix", "door": "NETFLIX", "name": "Netflix", "providers": [8], "companies": [171251, 145174, 266997, 192478]},
    {"id": "disney", "door": "DISNEY", "name": "Disney+", "providers": [337], "companies": []},
    # Prime is the one list too big for the cap (10/28: 26,390 films + 5,564 series = ~1,600 pages of the ~2,830 a full pull needs):
    # `rotate` = when the whole plan would pass the cap, Prime's date slices are fetched on alternate runs (parity), and the
    # slices not fetched keep their previous entries through the 28-day grace (each slice is re-read every second run).
    {"id": "prime", "door": "AMAZON", "name": "Prime Video", "providers": [9], "companies": [], "rotate": True},
    {"id": "paramount", "door": "PARAMOUNT", "name": "Paramount+", "providers": [2303, 2616], "companies": []},
    {"id": "max", "door": "HBO", "name": "Max", "providers": [1899], "companies": []},
    {"id": "peacock", "door": "PEACOCK", "name": "Peacock", "providers": [386, 387], "companies": []},
    {"id": "crunchyroll", "door": "CRUNCHYROLL", "name": "Crunchyroll", "providers": [283], "companies": []},
    {"id": "discovery", "door": "DISCOVERY", "name": "Discovery+", "providers": [520], "companies": []},
    # Marvel is not a service: Marvel Studios (company 420) AND on Disney+ (337), movies only
    {"id": "marvel", "door": "MARVEL", "name": "Marvel", "providers": [337], "companies": [], "and_company": 420, "kinds": ["movie"]},
]
KINDS = {"movie": ("movies", "primary_release_date"), "tv": ("series", "first_air_date")}


def log(*a):
    print(*a, flush=True)


class CapReached(Exception):
    pass


class Tmdb:
    """Counted, paced TMDB GETs. Every HTTP request (retries too) counts against the cap; the cap is checked BEFORE each one."""

    def __init__(self, key, cap, cache_dir, pace=0.12):
        self.key, self.cap, self.cache_dir, self.pace = key, cap, cache_dir, pace
        self.calls = 0; self.cached = 0; self._last = 0.0
        os.makedirs(cache_dir, exist_ok=True)

    def _cache_path(self, path, params):
        q = urllib.parse.urlencode(sorted(params.items()))   # never holds the key
        return os.path.join(self.cache_dir, hashlib.sha256(f"{path}?{q}".encode()).hexdigest()[:32] + ".json")

    def get(self, path, params):
        cp = self._cache_path(path, params)
        if os.path.exists(cp) and time.time() - os.path.getmtime(cp) < 12 * 3600:
            try:
                self.cached += 1
                return json.load(open(cp, encoding="utf-8"))
            except ValueError:
                pass
        url = f"{API}{path}?" + urllib.parse.urlencode({**params, "api_key": self.key})
        for attempt in range(5):
            if self.calls >= self.cap:
                raise CapReached(f"TMDB call cap {self.cap} reached")
            wait = self.pace - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time(); self.calls += 1
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "stevesapp-brand-lists"}), timeout=30) as r:
                    data = json.loads(r.read().decode("utf-8"))
                json.dump(data, open(cp, "w", encoding="utf-8"), separators=(",", ":"))
                return data
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    ra = e.headers.get("retry-after")
                    time.sleep(min(30, float(ra) if ra and ra.isdigit() else 2 ** attempt)); continue
                raise RuntimeError(f"TMDB answered {e.code} on {path}") from None   # the URL (with the key) is never printed
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                time.sleep(2 ** attempt); last = type(e).__name__; continue
        raise RuntimeError(f"TMDB unreachable on {path} after 5 tries")


# ------------------------------------------------------------------------------------------------------------- pulling
def base_params(brand, kind):
    p = {"watch_region": REGION, "with_watch_monetization_types": "flatrate", "include_adult": "false",
         "with_watch_providers": "|".join(str(i) for i in brand["providers"]), "sort_by": f"{KINDS[kind][1]}.asc"}
    if kind == "movie":
        p["include_video"] = "false"
    if brand.get("and_company"):
        p["with_companies"] = str(brand["and_company"])
    return p


def company_params(companies, kind):
    return {"with_companies": "|".join(str(c) for c in companies), "include_adult": "false", "sort_by": f"{KINDS[kind][1]}.asc"}


def _date_rng(kind, lo, hi):
    f = KINDS[kind][1]
    return {f"{f}.gte": lo.isoformat(), f"{f}.lte": hi.isoformat()}


def probe(tm, kind, params, lo=None, hi=None, depth=0):
    """Page 1 of a query; a query over the 500-page cap is split by date range (halves) until every slice fits.
    Returns [(params_with_range, page1_answer)]. A split's over-cap probe is a spent call (counted)."""
    q = dict(params)
    if lo is not None:
        q.update(_date_rng(kind, lo, hi))
    a = tm.get(f"/discover/{kind}", {**q, "page": "1"})
    if int(a.get("total_pages") or 0) <= PAGE_CAP:
        return [(q, a)]
    if lo is None:
        log(f"  split by date: {kind} query of {a.get('total_results')} titles is over the {PAGE_CAP}-page cap (undated titles are lost to the split)")
    if depth > 14:
        raise RuntimeError("date split did not converge")
    if lo is None:   # first split: every dated title (undated titles cannot be reached past the cap -- logged)
        lo, hi = dt.date(1870, 1, 1), dt.date.today() + dt.timedelta(days=3 * 365)
    if (hi - lo).days < 2:
        raise RuntimeError(f"one {kind} day holds more than {PAGE_CAP} pages")
    mid = lo + dt.timedelta(days=(hi - lo).days // 2)
    return probe(tm, kind, params, lo, mid, depth + 1) + probe(tm, kind, params, mid + dt.timedelta(days=1), hi, depth + 1)


def plan_brand(tm, brand, only_kinds=None):
    """All page-1 probes of one brand -> list of jobs {brand, kind, src, params, first, pages, total}."""
    jobs = []
    for kind in brand.get("kinds", ["movie", "tv"]):
        if only_kinds and kind not in only_kinds:
            continue
        for src, params in [("list", base_params(brand, kind))] + ([("co", company_params(brand["companies"], kind))] if brand["companies"] else []):
            for q, a in probe(tm, kind, params):
                jobs.append({"brand": brand["id"], "kind": kind, "src": src, "params": q, "first": a,
                             "pages": min(int(a.get("total_pages") or 0), PAGE_CAP), "total": int(a.get("total_results") or 0)})
    return jobs


def fetch_jobs(tm, jobs):
    """Every page past the first. Returns {(brand, kind): set(ids)} and the per-job drift (ids got vs total announced)."""
    got, drift = {}, []
    for j in jobs:
        ids = {int(r["id"]) for r in j["first"].get("results") or [] if r.get("id")}
        for page in range(2, j["pages"] + 1):
            a = tm.get(f"/discover/{j['kind']}", {**j["params"], "page": str(page)})
            ids.update(int(r["id"]) for r in a.get("results") or [] if r.get("id"))
        got.setdefault((j["brand"], j["kind"]), set()).update(ids)
        if j["total"] and abs(len(ids) - j["total"]) > max(2, j["total"] // 200):
            drift.append(f"{j['brand']} {j['kind']} {j['src']}: {len(ids)} ids of {j['total']} announced")
    return got, drift


def check_providers(tm, brands):
    """The pinned ids must still exist in TMDB's US provider list (the 10/26 pass found Paramount+ 531 gone)."""
    have = set()
    for kind in ("movie", "tv"):
        a = tm.get(f"/watch/providers/{kind}", {"watch_region": REGION})
        have.update(int(r["provider_id"]) for r in a.get("results") or [] if r.get("provider_id") is not None)
    missing = sorted({p for b in brands for p in b["providers"]} - have)
    return missing


# --------------------------------------------------------------------------------------------------------------- grace
def merge_grace(prev_seen, fresh_ids, today, grace_days=GRACE_DAYS):
    """prev_seen {id: (first, last)} (ISO dates), fresh_ids = ids seen this run, today = ISO date ->
    {id: (first, last)}: every fresh id (first kept from before, last = today) + every previous id last seen within
    grace_days of today. Ids are ints."""
    t = dt.date.fromisoformat(today)
    out = {}
    for i in fresh_ids:
        f = prev_seen.get(i, (today, today))[0]
        out[int(i)] = (min(f, today), today)
    for i, (f, l) in prev_seen.items():
        if int(i) in out:
            continue
        if (t - dt.date.fromisoformat(l)).days <= grace_days:
            out[int(i)] = (f, l)
    return out


def prev_seen_of(prev, bid, plural):
    """{id: (first, last)} for one brand/kind from a previous pack (missing brand/kind = {})."""
    for b in (prev or {}).get("brands") or []:
        if b.get("id") == bid:
            s = ((b.get("seen") or {}).get(plural)) or {}
            return {int(k): (v[0], v[1]) for k, v in s.items()}
    return {}


def build_pack(brands, fresh, prev, now, version):
    """fresh {(brand id, kind): set(ids)} -> the pack dict + per-brand counts (with grace)."""
    today = now.date().isoformat()
    out = {"schema": 1, "version": version, "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "region": REGION,
           "graceDays": GRACE_DAYS, "brands": [], "credits": CREDITS}
    counts = {}
    for b in brands:
        e = {"id": b["id"], "door": b["door"], "name": b["name"], "provider_ids": b["providers"],
             "companies": b["companies"] + ([b["and_company"]] if b.get("and_company") else []), "seen": {}}
        c = {}
        for kind, (plural, _) in KINDS.items():
            merged = merge_grace(prev_seen_of(prev, b["id"], plural), fresh.get((b["id"], kind), set()), today)
            e[plural] = sorted(merged)
            e["seen"][plural] = {str(i): [merged[i][0], merged[i][1]] for i in sorted(merged)}
            c[plural] = len(merged)
            c[plural + "_fresh"] = len(fresh.get((b["id"], kind), set()))
        out["brands"].append(e); counts[b["id"]] = c
    return out, counts


def load_prev(a):
    """The previous pack: --prev FILE, else the publish door's state read (needs EVENTS_PUBLISH_KEY), else None (first run)."""
    if a.no_prev:
        return None
    if a.prev:
        with open(a.prev, "rb") as f:
            raw = f.read()
        return json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
    key = os.environ.get("EVENTS_PUBLISH_KEY", "").strip()
    if not key:
        log("::notice::no EVENTS_PUBLISH_KEY and no --prev: no previous lists, grace starts today")
        return None
    req = urllib.request.Request(f"{DOOR}/v1/brand_lists/state", headers={"x-events-key": key, "user-agent": "stevesapp-events-builder"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(gzip.decompress(r.read()))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log("previous lists: none published yet (404) -- grace starts today"); return None
        raise SystemExit(f"::error::previous lists unreadable (door answered {e.code}) -- not building without the grace dates")
    except Exception as e:
        raise SystemExit(f"::error::previous lists unreadable ({type(e).__name__}) -- not building without the grace dates")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma list of brand ids (dry runs); a partial run is never published")
    ap.add_argument("--max-calls", type=int, default=3000)
    ap.add_argument("--cache-dir", default=os.path.join(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir(), "brand_lists_pages"))
    ap.add_argument("--prev"); ap.add_argument("--no-prev", action="store_true")
    ap.add_argument("--out", default=CDN)
    ap.add_argument("--parity", default="auto", choices=("auto", "0", "1"), help="which half of a rotating list this run reads (auto = ISO week %% 2)")
    a = ap.parse_args()
    for n in ("brand_lists.json.gz", "brand_lists_manifest.json"):
        try:
            os.remove(os.path.join(a.out, n))
        except FileNotFoundError:
            pass
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if not key:
        log("::error::TMDB_API_KEY not set"); return 1
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::add-mask::{key}", flush=True)
    brands = [b for b in BRANDS if not a.only or b["id"] in a.only.split(",")]
    if not brands:
        log("::error::--only matched no brand"); return 1
    t0 = time.time(); now = dt.datetime.now(dt.timezone.utc)
    version = now.strftime("%Y%m%d%H%M")
    prev = load_prev(a)
    tm = Tmdb(key, a.max_calls, a.cache_dir)
    try:
        missing = check_providers(tm, brands)
        if missing:
            log(f"::error::pinned provider ids no longer in TMDB's {REGION} list: {missing} -- re-pin them (nothing written)"); return 1
        jobs = []
        for b in brands:
            jobs += plan_brand(tm, b)
        need = sum(max(0, j["pages"] - 1) for j in jobs)
        log(f"plan: {len(jobs)} queries, {tm.calls} calls spent on probes, {need} pages to go -> {tm.calls + need} of the {a.max_calls} cap")
        for j in jobs:
            rng = [v for k, v in sorted(j["params"].items()) if k.endswith((".gte", ".lte"))]
            log(f"  {j['brand']:12s} {j['kind']:5s} {j['src']:4s} total={j['total']:6d} pages={j['pages']:3d}" + (f" {rng[0]}..{rng[1]}" if rng else ""))
        rotated = []
        if tm.calls + need > a.max_calls:
            parity = (now.isocalendar()[1] % 2) if a.parity == "auto" else int(a.parity)
            rot = sorted((j for j in jobs if any(b["id"] == j["brand"] and b.get("rotate") for b in brands)),
                         key=lambda j: (j["kind"], min((v for k, v in j["params"].items() if k.endswith(".gte")), default="")))
            skip = [j for i, j in enumerate(rot) if i % 2 != parity]
            jobs = [j for j in jobs if all(j is not s_ for s_ in skip)]
            rotated = [f"{j['brand']} {j['kind']} " + "..".join(v for k, v in sorted(j["params"].items()) if k.endswith((".gte", ".lte"))) for j in skip]
            need = sum(max(0, j["pages"] - 1) for j in jobs)
            log(f"over the cap in full -> rotation parity {parity}: {len(skip)} slices deferred to the next run (kept by the grace): {rotated}"
                f" -> {tm.calls + need} of the {a.max_calls} cap")
        if tm.calls + need > a.max_calls:
            log(f"::error::the plan needs {tm.calls + need} TMDB calls, over the hard cap {a.max_calls} -- stopped before fetching (nothing written)")
            return 1
        fresh, drift = fetch_jobs(tm, jobs)
    except CapReached as e:
        log(f"::error::{e} mid-run -- stopped (nothing written)"); return 1
    except RuntimeError as e:
        log(f"::error::{e} (nothing written)"); return 1
    for d in drift:
        log(f"::warning::drift {d} (rotation while paging)")
    pack, counts = build_pack(brands, fresh, prev, now, version)   # fresh = the list ids UNION the company ids per brand/kind
    # week-over-week guard: a list whose FRESH count fell by more than half is loud (grace still holds the door 28 days)
    rot_brands = {r.split(" ")[0] for r in rotated}
    for b in brands:
        if b["id"] in rot_brands:
            continue
        for kind, (plural, _) in KINDS.items():
            pf = sum(1 for v in prev_seen_of(prev, b["id"], plural).values() if prev and v[1] == str(prev.get("generatedAt", ""))[:10])
            if pf >= 20 and counts[b["id"]][plural + "_fresh"] < pf * 0.5:
                log(f"::warning::{b['id']} {plural}: {counts[b['id']][plural + '_fresh']} this run vs {pf} last run (fell > 50%)")
    raw = json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    gz = gzip.compress(raw, compresslevel=9, mtime=0)
    os.makedirs(a.out, exist_ok=True)
    open(os.path.join(a.out, "brand_lists.json.gz"), "wb").write(gz)
    man = {"version": version, "file": "brand_lists.json.gz", "sha256": hashlib.sha256(gz).hexdigest(), "bytes": len(gz),
           "rawBytes": len(raw), "generatedAt": pack["generatedAt"], "schema": 1, "region": REGION, "partial": bool(a.only),
           "brands": len(brands), "calls": tm.calls, "rotated": rotated, "counts": {k: {"movies": v["movies"], "series": v["series"]} for k, v in counts.items()}}
    json.dump(man, open(os.path.join(a.out, "brand_lists_manifest.json"), "w", encoding="utf-8"), indent=1)
    line = "brand lists v" + version + ": " + " ".join(f"{k} movies={v['movies']} series={v['series']}" for k, v in counts.items())
    log(line)
    log(f"fresh this run: " + " ".join(f"{k} {v['movies_fresh']}/{v['series_fresh']}" for k, v in counts.items()))
    log(f"TMDB calls {tm.calls} of the {a.max_calls} cap ({tm.cached} page reads from the run cache), {len(gz) / 1e3:.0f} KB gz, {time.time() - t0:.0f}s")
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        open(summ, "a", encoding="utf-8").write(f"## Brand lists {version}\n\n- {line}\n- TMDB calls {tm.calls} / {a.max_calls}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
