"""
C2 FAMILY CATEGORIES (2026-10-04, app 0.17.44 contract: docs/CLOUD_CONTRACT_0.17.44.md section C2).

Boxes used to ask their provider for the three category lists on every refresh, including every catalogue diff (trim
list T5: up to ~24 calls a day per box while nobody browses). Hourly, the family builder reads them ONCE on the builder
line (get_live_categories, get_vod_categories, get_series_categories -- list reads, 1 call / 2 s) and publishes, per kind,
a SIGNATURE and the category ids. A box re-reads a kind from its provider only when that kind's signature differs from the
one it stored at its last read.

NO NAMES: the 9/23 rule keeps the provider's category names out of our storage (the slots pack publishes bucket ids
only for the same reason). The signature covers the names, so a rename still changes it:
    sig(kind) = sha256( UTF-8 of "".join(f"{category_id}\t{parent_id}\t{category_name}\n" for each row IN ANSWER ORDER) )
where each field is the provider's raw value as a string (null/absent parent = ""), hex lower-case.
Output: cdn/cats.json.gz {schema:1, family, version, generatedAt, sig:{live,vod,series}, live:[{id,parent}], vod:[...],
        series:[...]} + cdn/cats_manifest.json; published by tools/publish_events_http.py cats. Our server keeps the old
        version (no box download) when every signature is unchanged and only moves checkedAt.
A provider error or an implausible answer writes nothing (exit 1). Prints counts only.
Args: --family k8 --line-env SLOTS_LINE
"""
import argparse, datetime as dt, gzip, hashlib, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from family_common import CDN, FAMILY_RX, Provider, ProviderError, clean_outputs, line_of, log, summary  # noqa: E402

KINDS = (("live", "get_live_categories"), ("vod", "get_vod_categories"), ("series", "get_series_categories"))


def s(v):
    return "" if v is None else str(v)


def sig_of(rows):
    return hashlib.sha256("".join(f"{s(r.get('category_id'))}\t{s(r.get('parent_id'))}\t{s(r.get('category_name'))}\n" for r in rows).encode("utf-8")).hexdigest()


def build(answers, family, now=None):
    """answers = {kind: provider list} -> (pack, manifest-without-file-fields)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    ver = now.strftime("%Y%m%d%H%M")
    pack = {"schema": 1, "family": family, "version": ver, "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "sig": {}}
    counts = {}
    for kind, _ in KINDS:
        rows = [r for r in answers[kind] if isinstance(r, dict)]
        pack["sig"][kind] = sig_of(rows)
        pack[kind] = [{"id": s(r.get("category_id")), "parent": s(r.get("parent_id"))} for r in rows if s(r.get("category_id")).isdigit()]
        counts[kind] = len(pack[kind])
    return pack, counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", required=True); ap.add_argument("--line-env", required=True); ap.add_argument("--pace", type=float, default=2.0)
    a = ap.parse_args()
    if not FAMILY_RX.match(a.family):
        raise SystemExit("::error::bad family")
    clean_outputs("cats.json.gz", "cats_manifest.json")
    line = line_of(a.line_env)
    if not line:
        log(f"::notice::{a.family}: no builder line ({a.line_env} not set) -- categories skipped")
        return 0
    prov = Provider(line, pace=a.pace)
    answers = {}
    for kind, action in KINDS:
        try:
            answers[kind] = prov.api(action)
        except ProviderError as e:
            log(f"::error::{a.family} {kind} categories: provider answered {e.code} -- nothing written"); return 1
        if not isinstance(answers[kind], list) or len(answers[kind]) < (5 if kind == "live" else 1):
            log(f"::error::{a.family} {kind} categories: implausible answer -- nothing written"); return 1
    pack, counts = build(answers, a.family)
    gz = gzip.compress(json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)
    os.makedirs(CDN, exist_ok=True)
    open(os.path.join(CDN, "cats.json.gz"), "wb").write(gz)
    man = {"version": pack["version"], "file": "cats.json.gz", "sha256": hashlib.sha256(gz).hexdigest(), "bytes": len(gz),
           "generatedAt": pack["generatedAt"], "family": a.family, "schema": 1, "sig": pack["sig"], "counts": counts}
    json.dump(man, open(os.path.join(CDN, "cats_manifest.json"), "w", encoding="utf-8"), indent=1)
    msg = f"cats {a.family}: {prov.calls} provider calls -> live {counts['live']}, vod {counts['vod']}, series {counts['series']} ids"
    log(msg); summary(f"- {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
