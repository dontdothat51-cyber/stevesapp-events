"""
C1 FAMILY CHANNEL GUIDE (2026-10-04, app 0.17.44 contract: docs/CLOUD_CONTRACT_0.17.44.md section C1).

Boxes used to sweep their provider's guide themselves (trim list T6: ~102 get_simple_data_table calls in 15-22 s per
full refresh, the biggest burst a box made) and to ask again at every tune (T7). This builder reads the family's guide
ONCE, for every box, on the family's builder line, as a slow trickle:
  * universe: the family's own channel list (get_live_streams, 1 call, refreshed every --universe-h hours): every
    distinct epg_channel_id and the stream ids that carry it (k8: ~4,400 ids over ~8,300 channels, 9/17 copy);
  * each run (every 10 min, family.yml) reads up to --max-calls (90) ids that are DUE, one call every 2 s:
    due = never read, or read more than --refresh-h (12) hours ago -- so every id is read twice a day; an id the
    provider answered EMPTY twice in a row sleeps 7 days (the k8 panel answers {"epg_listings":[]} for most ids -- 9/23 receipt),
    and the next try uses the next stream id that carries it (the box's old "sibling" call, done once in the cloud);
    order = ids that had listings last time, then US/CA/UK channels, then the rest, oldest read first;
  * clock: the panel's data-table times may be in its own zone; once a day the builder compares one id against
    get_short_epg (UTC-true) and applies the offset at output (the box's calibrate(), once in the cloud);
  * publish: twice a day (--publish-hours, UTC 09 and 21) the guide is written for ~--window-h (40) hours:
      cdn/changuide.tsv.gz   "#changuide\t<fam>\t<version>\t1"
                             "E\t<epg id>\t<start>\t<stop>\t<title>"  (true UTC epoch seconds, start-ordered per id)
                             "S\t<stream id>\t<epg id>"               (every stream id that carries a published id)
      cdn/chguide_manifest.json  {version, file, sha256, bytes, generatedAt, family, schema:1, keys, streams, progs,
                                  calls, calOffset, from, to}
    and handed to our server by tools/publish_events_http.py chguide.
BLANK BEATS WRONG: an id ships only with listings the provider itself gave for that id; titles lose URLs, tabs and the
provider's brand phrase. State (cdn-build/family/guide_<fam>.json.gz, kept between runs by the Actions cache): the
universe, per-id clocks, the last listings (trimmed to -6 h .. +48 h), the clock offset, the last publish hour.
Prints counts only (public logs). Unset line = the family is skipped (exit 0).
Args: --family k8 --line-env SLOTS_LINE [--max-calls 90] [--budget-s 200] [--refresh-h 12] [--window-h 40]
      [--universe-h 6] [--publish-hours 9,21] [--force-publish]
"""
import argparse, base64, datetime as dt, gzip, hashlib, json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from family_common import CDN, FAMILY_RX, STATE_DIR, Provider, ProviderError, clean_outputs, line_of, log, safe_text, summary  # noqa: E402

PRI = re.compile(r"^\s*(US|USA|CA|UK|GB)\s*[:|]", re.I)
KEY_OK = re.compile(r"^[^\t\r\n]{1,120}$")
SLEEP_S = 7 * 86400
CAL_EVERY_S = 86400


def load_state(path):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path, st):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, path)


def universe_of(streams):
    """{epg id: [stream ids...]} + the priority set, from a get_live_streams answer."""
    keys, pri = {}, set()
    for s in streams:
        if not isinstance(s, dict):
            continue
        epg = str(s.get("epg_channel_id") or "").strip()
        try:
            sid = int(s.get("stream_id"))
        except Exception:
            continue
        if not epg or not KEY_OK.match(epg) or "http" in epg.lower():
            continue
        keys.setdefault(epg, []).append(sid)
        if PRI.match(str(s.get("name") or "")):
            pri.add(epg)
    for k in keys:
        keys[k] = sorted(set(keys[k]))
    return keys, sorted(pri)


def listings(raw):
    """get_simple_data_table / get_short_epg answer -> [[start, stop, title]] (panel clock, uncorrected)."""
    out = []
    arr = raw.get("epg_listings") if isinstance(raw, dict) else None
    for e in arr or []:
        if not isinstance(e, dict):
            continue
        try:
            st, sp = int(str(e.get("start_timestamp"))), int(str(e.get("stop_timestamp")))
        except Exception:
            continue
        try:
            title = base64.b64decode(str(e.get("title") or ""), validate=False).decode("utf-8", "replace")
        except Exception:
            title = ""
        title = safe_text(title, 300)
        if title and sp > st > 0:
            out.append([st, sp, title])
    out.sort()
    dedup, last = [], None
    for r in out:
        if last is None or r[0] != last[0]:
            dedup.append(r)
        last = r
    return dedup


def offset_of(table, short):
    """The panel's data-table clock offset (seconds) against get_short_epg, by title; None when not provable."""
    offs = set()
    for st, _, title in short:
        hit = [r for r in table if r[2] == title]
        if len(hit) == 1:
            offs.add(hit[0][0] - st)
    if len(offs) != 1:
        return None
    off = offs.pop()
    return off if abs(off) <= 14 * 3600 and off % 900 == 0 else None


def render(fam, ver, progs, universe, cal, now, window_h):
    lo, hi = now - 3 * 3600, now + window_h * 3600
    lines, keys, streams, n, t_min, t_max = [f"#changuide\t{fam}\t{ver}\t1"], 0, 0, 0, None, None
    for epg in sorted(progs):
        rows = [(s - cal, e - cal, t) for s, e, t in progs[epg] if e - cal > lo and s - cal < hi]
        if not rows:
            continue
        keys += 1
        for s, e, t in rows:
            lines.append(f"E\t{epg}\t{s}\t{e}\t{t}"); n += 1
            t_min = s if t_min is None else min(t_min, s); t_max = e if t_max is None else max(t_max, e)
        for sid in universe.get(epg, []):
            lines.append(f"S\t{sid}\t{epg}"); streams += 1
    return "\n".join(lines) + "\n", keys, streams, n, t_min or 0, t_max or 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", required=True); ap.add_argument("--line-env", required=True)
    ap.add_argument("--max-calls", type=int, default=90); ap.add_argument("--pace", type=float, default=2.0)
    ap.add_argument("--budget-s", type=float, default=200.0); ap.add_argument("--refresh-h", type=float, default=12.0)
    ap.add_argument("--window-h", type=float, default=40.0); ap.add_argument("--universe-h", type=float, default=6.0)
    ap.add_argument("--publish-hours", default="9,21"); ap.add_argument("--force-publish", action="store_true")
    a = ap.parse_args()
    if not FAMILY_RX.match(a.family):
        raise SystemExit("::error::bad family")
    clean_outputs("changuide.tsv.gz", "chguide_manifest.json")
    line = line_of(a.line_env)
    if not line:
        log(f"::notice::{a.family}: no builder line ({a.line_env} not set) -- guide skipped")
        return 0
    t0 = time.monotonic()
    now = int(time.time())
    sp = os.path.join(STATE_DIR, f"guide_{a.family}.json.gz")
    st = load_state(sp)
    uni = st.get("universe") or {}
    clocks = st.get("keys") or {}
    progs = {k: [tuple(r) for r in v] for k, v in (st.get("progs") or {}).items()}
    cal = st.get("cal") or {"off": 0, "at": 0}
    prov = Provider(line, pace=a.pace)
    stopped = None

    # 1. universe
    if not uni.get("keys") or now - int(uni.get("at") or 0) > a.universe_h * 3600:
        try:
            streams = prov.api("get_live_streams")
        except ProviderError as e:
            log(f"::error::{a.family} guide: channel list -> provider {e.code}; state kept"); return 1
        if not isinstance(streams, list) or len(streams) < 100:
            log(f"::error::{a.family} guide: implausible channel list; state kept"); return 1
        keys, pri = universe_of(streams)
        uni = {"at": now, "keys": keys, "pri": pri}
        for k in list(progs):
            if k not in keys:
                del progs[k]
        log(f"guide {a.family}: universe {len(keys)} guide ids over {sum(len(v) for v in keys.values())} channels ({len(pri)} US/CA/UK)")
    keys, pri = uni["keys"], set(uni.get("pri") or [])

    # 2. due ids, in order
    due = []
    for epg in keys:
        c = clocks.get(epg) or {}
        t, e = int(c.get("t") or 0), int(c.get("e") or 0)
        if e >= 2 and now - t < SLEEP_S:
            continue
        if t and now - t < a.refresh_h * 3600:
            continue
        due.append((0 if progs.get(epg) else 1, 0 if epg in pri else 1, t, epg))
    due.sort()
    calls_left = max(0, a.max_calls - prov.calls)
    ok = emp = 0
    for _, _, _, epg in due:
        if calls_left <= 0 or time.monotonic() - t0 > a.budget_s:
            break
        c = clocks.setdefault(epg, {})
        sids = keys[epg]
        rep = sids[int(c.get("e") or 0) % len(sids)]   # an empty answer -> the next channel carrying this id next time
        try:
            got = listings(prov.api("get_simple_data_table", stream_id=rep))
        except ProviderError as e:
            if e.code in (0, 401, 403, 429, 460) or e.code >= 500:
                stopped = e.code; break
            got = None
        calls_left -= 1
        c["t"] = now
        if got:
            c["e"] = 0; c["ok"] = now; ok += 1
            progs[epg] = [r for r in got if r[1] > now - 6 * 3600 and r[0] < now + 48 * 3600] or got[-5:]
            # 3. clock, once a day, on the first id with real listings
            if now - int(cal.get("at") or 0) > CAL_EVERY_S and len(got) >= 3 and calls_left > 0 and stopped is None:
                try:
                    short = listings(prov.api("get_short_epg", stream_id=rep, limit=4)); calls_left -= 1
                    off = offset_of(got, short)
                    cal = {"off": off if off is not None else int(cal.get("off") or 0), "at": now, "proved": off is not None}
                except ProviderError:
                    pass
        else:
            c["e"] = int(c.get("e") or 0) + 1; emp += 1
            if c["e"] >= 2:
                progs.pop(epg, None)   # blank beats wrong: the provider no longer lists it
    for k in list(progs):   # keep the state small
        progs[k] = [r for r in progs[k] if r[1] > now - 6 * 3600]
        if not progs[k]:
            del progs[k]
    sleeping = sum(1 for k in keys if int((clocks.get(k) or {}).get("e") or 0) >= 2)
    msg = (f"guide {a.family}: {prov.calls} provider calls in {time.monotonic() - t0:.0f}s -> {ok} ids with listings, {emp} empty; "
           f"due left {max(0, len(due) - ok - emp)}; holding {len(progs)} ids; sleeping {sleeping}; clock offset {int(cal.get('off') or 0)}s"
           f"{'' if stopped is None else f'; STOPPED on provider answer {stopped}'}")
    log(msg); summary(f"- {msg}")

    # 4. publish twice a day
    hour = dt.datetime.now(dt.timezone.utc)
    hk = hour.strftime("%Y%m%d%H")
    want = {int(x) for x in a.publish_hours.split(",") if x.strip().isdigit()}
    publish = a.force_publish or (hour.hour in want and st.get("pub") != hk) or (not st.get("pub") and progs)
    if publish and progs:
        ver = hour.strftime("%Y%m%d%H%M")
        text, nk, ns, n, t_min, t_max = render(a.family, ver, progs, keys, int(cal.get("off") or 0), now, a.window_h)
        if n:
            gz = gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)
            os.makedirs(CDN, exist_ok=True)
            open(os.path.join(CDN, "changuide.tsv.gz"), "wb").write(gz)
            man = {"version": ver, "file": "changuide.tsv.gz", "sha256": hashlib.sha256(gz).hexdigest(), "bytes": len(gz),
                   "generatedAt": hour.strftime("%Y-%m-%dT%H:%M:%SZ"), "family": a.family, "schema": 1, "keys": nk, "streams": ns,
                   "progs": n, "calls": prov.calls, "calOffset": int(cal.get("off") or 0), "from": t_min, "to": t_max,
                   # coverage: guide ids in the family's list / read in the last 7 days -- the box knows when a missing id
                   # means "the provider has nothing" (tried == universe) rather than "not reached yet" (first ~2 days)
                   "universe": len(keys), "tried": sum(1 for k in keys if now - int((clocks.get(k) or {}).get("t") or 0) < SLEEP_S)}
            json.dump(man, open(os.path.join(CDN, "chguide_manifest.json"), "w", encoding="utf-8"), indent=1)
            st["pub"] = hk
            log(f"guide {a.family}: pack {ver} written -- {nk} ids, {ns} channels, {n} programmes, {len(gz) / 1024:.0f} KB")
    st.update({"universe": uni, "keys": clocks, "progs": {k: [list(r) for r in v] for k, v in progs.items()}, "cal": cal})
    save_state(sp, st)
    return 1 if stopped is not None else 0


if __name__ == "__main__":
    sys.exit(main())
