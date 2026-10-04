"""
C3 FAMILY EPISODE LISTS (2026-10-04, app 0.17.44 contract: docs/CLOUD_CONTRACT_0.17.44.md section C3).

Owner: "the episode list of a show is read ONCE per server family by our cloud, for every box." Every 10 minutes
(family.yml in the public builder repo):
  1. ask our server's queue door which series the family needs -- (0) series a box opened and the cloud lacked,
     (1) series the catalogue pack says changed while we hold a copy, (2) added/changed series we have never read --
     in that order (GET /v1/eps/queue; the server folds each new catalogue diff into the queue itself);
  2. read each one's episode list ONCE on the family's builder line (get_series_info -- a list read, never a stream),
     one call every 2 s, at most --max-calls (150) per run and --budget-s seconds (the backlog rolls to the next run);
  3. hand the reduced lists to the server's door (POST /v1/eps/publish), which stores them for every box of the family.
A series whose answer has no episodes is reported "empty" (the server lets it sleep 7 days). A provider refusal stops
the run after what was already read is handed over (the next run is the retry; never a retry loop).

The reduced list keeps the provider's own shape -- {"episodes": {"<season>": [{id, episode_num, season, title,
container_extension, info:{duration_secs, movie_image, air_date, rating}}]}} -- so the box's existing parser reads it
unchanged; both provider shapes (object or array of seasons) come out as the object shape.

Args: --family k8 --line-env SLOTS_LINE [--max-calls 150] [--pace 2] [--budget-s 330] [--dry]
Env:  SLOTS_LINE / FLUX_LINE ("server|user|pass"; unset = the family is skipped, exit 0), EVENTS_PUBLISH_KEY.
Prints counts only (public logs).
"""
import argparse, datetime as dt, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from family_common import FAMILY_RX, Provider, ProviderError, door, line_of, log, safe_text, summary  # noqa: E402

FLUSH = 25
STOP_CODES = {401, 403, 429, 460, 0}   # auth, rate, data-centre refusal, unreachable: stop the run


def _int(v, d=0):
    try:
        return int(str(v).strip())
    except Exception:
        return d


def _float(v):
    try:
        return float(str(v).strip())
    except Exception:
        return 0.0


def safe_image(url, secrets):
    u = str(url or "").strip()
    if not u.startswith("http") or len(u) > 400 or re.search(r"player_api|username=|password=", u, re.I):
        return None
    if any(s and s in u for s in secrets):
        return None
    return u


def reduce_series_info(raw, sid, fam, lm=None, secrets=()):
    """get_series_info -> the stored list (None = not a series answer). Mirrors EpisodeFetcher.kt's parse field for field."""
    if not isinstance(raw, dict):
        return None
    eps = raw.get("episodes")
    if isinstance(eps, dict):
        groups = [(str(k), v) for k, v in eps.items() if isinstance(v, list)]
    elif isinstance(eps, list):
        groups = [(str(i), v) for i, v in enumerate(eps) if isinstance(v, list)]
    else:
        groups = []
    out, n = {}, 0
    for key, arr in groups:
        for i, o in enumerate(arr):
            if not isinstance(o, dict):
                continue
            eid = str(o.get("id") or "").strip()
            if not eid.isdigit():
                continue
            season = _int(o.get("season"), _int(key, 1))
            e = {"id": eid, "episode_num": _int(o.get("episode_num"), i + 1), "season": season,
                 "title": safe_text(o.get("title"), 300), "container_extension": re.sub(r"[^A-Za-z0-9]", "", str(o.get("container_extension") or ""))[:8]}
            info = o.get("info") if isinstance(o.get("info"), dict) else {}
            inf = {}
            secs = _int(info.get("duration_secs"), 0)
            if secs > 0:
                inf["duration_secs"] = secs
            img = safe_image(info.get("movie_image"), secrets)
            if img:
                inf["movie_image"] = img
            ad = str(info.get("air_date") or info.get("releasedate") or "").strip()
            if len(ad) >= 8:
                inf["air_date"] = ad[:10]
            rt = _float(info.get("rating"))
            if rt > 0:
                inf["rating"] = round(rt, 2)
            if inf:
                e["info"] = inf
            out.setdefault(str(season), []).append(e)
            n += 1
    for k in out:
        out[k].sort(key=lambda e: (e["episode_num"], e["id"]))
    seasons = []
    for s in raw.get("seasons") or []:
        if isinstance(s, dict) and str(s.get("season_number", "")).strip().lstrip("-").isdigit():
            row = {"season_number": _int(s.get("season_number")), "episode_count": _int(s.get("episode_count"), 0)}
            if str(s.get("air_date") or "")[:4].isdigit():
                row["air_date"] = str(s["air_date"])[:10]
            seasons.append(row)
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    lm2 = _int(lm, 0) or _int(info.get("last_modified"), 0)
    return {"schema": 1, "fam": fam, "sid": int(sid), "lm": lm2, "fetchedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "n": n, "episodes": out, "seasons": seasons}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", required=True); ap.add_argument("--line-env", required=True)
    ap.add_argument("--max-calls", type=int, default=150); ap.add_argument("--pace", type=float, default=2.0)
    ap.add_argument("--budget-s", type=float, default=330.0); ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if not FAMILY_RX.match(a.family):
        raise SystemExit("::error::bad family")
    line = line_of(a.line_env)
    if not line:
        log(f"::notice::{a.family}: no builder line ({a.line_env} not set) -- family skipped; its boxes keep asking their provider")
        return 0
    t0 = time.monotonic()
    prov = Provider(line, pace=a.pace)
    q = door("GET", f"/v1/eps/queue?fam={a.family}&limit={max(1, min(500, a.max_calls))}")
    items = q.get("items") or []
    c = q.get("counts") or {}
    cat = q.get("catalog") or {}
    log(f"eps {a.family}: queue {len(items)} this run (wanted {c.get('wanted', 0)}: asked {c.get('asked', 0)}, stale {c.get('stale', 0)}, "
        f"backfill {c.get('backfill', 0)}; sleeping {c.get('sleeping', 0)}; held {c.get('held', 0)}); catalogue folded {len(cat.get('versions') or [])} version(s)")
    done, empty, pending = 0, [], []
    errs, stopped = 0, None
    sent = {"written": 0, "empty": 0, "refused": 0}

    def flush(final=False):
        nonlocal pending, empty
        if not pending and not (final and empty):
            return
        body = {"schema": 1, "fam": a.family, "items": pending, "empty": empty if final else []}
        if a.dry:
            log(f"DRY: would publish {len(pending)} lists{', ' + str(len(body['empty'])) + ' empty' if final else ''}")
        else:
            r = door("POST", "/v1/eps/publish", body, timeout=180)
            for k in sent:
                sent[k] += int(r.get(k) or 0)
        pending = []
        if final:
            empty = []

    for it in items:
        if time.monotonic() - t0 > a.budget_s:
            log(f"eps {a.family}: time budget {a.budget_s:.0f}s reached -- the rest rolls to the next run")
            break
        sid = _int(it.get("sid"), 0)
        if sid <= 0:
            continue
        try:
            raw = prov.api("get_series_info", series_id=sid)
        except ProviderError as e:
            errs += 1
            if e.code in STOP_CODES or e.code >= 500 or errs >= 3:
                stopped = e.code
                break
            continue
        red = reduce_series_info(raw, sid, a.family, it.get("lm"), prov.secrets())
        if red is None or red["n"] == 0:
            empty.append(sid)
        else:
            pending.append({"sid": sid, "lm": red["lm"], "data": red})
            done += 1
            if len(pending) >= FLUSH:
                flush()
    flush(final=True)
    msg = (f"eps {a.family}: {prov.calls} provider calls in {time.monotonic() - t0:.0f}s -> {done} lists, {sent['empty'] or 0} empty, "
           f"{sent['refused']} refused by the door{'' if stopped is None else f'; STOPPED on provider answer {stopped}'}")
    log(msg)
    summary(f"- {msg}")
    return 1 if stopped is not None else 0


if __name__ == "__main__":
    sys.exit(main())
