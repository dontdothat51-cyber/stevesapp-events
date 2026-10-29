"""
PUBLISH THE EVENTS PACK THROUGH THE BOUNCER'S PUBLISH DOOR (14.8, 2026-09-22).

The public builder never touches storage: it POSTs cdn/events.json.gz to https://stevesapp.tv/v1/events/publish with
one single-purpose key (env EVENTS_PUBLISH_KEY). The door validates the pack (version, age, boards, sha256, size,
gzip contents) and writes only the events files. An older-or-same version is skipped, never an error.

    EVENTS_PUBLISH_KEY=... python tools/publish_events_http.py
"""
import datetime as dt, json, os, sys, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDN = os.path.join(ROOT, "cdn")
DOOR = os.environ.get("EVENTS_PUBLISH_URL", "https://stevesapp.tv/v1/events/publish")


def main():
    # 9/23: slots + catalog ride the same door; 10/4 (0.17.44): the family builder's categories (cats) and channel guide (chguide);
    # 10/28 (Phase 4): the weekly brand lists (brand_lists, tools/build_brand_lists.py)
    kind = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("events", "slots", "catalog", "cats", "chguide", "brand_lists") else "events"
    # 10/28 SLOTS PER FAMILY: `slots --family flux|mega` publishes cdn/fam/<family>/ (tools/build_slotspack.py --family); k8 or no
    # flag = today's cdn/slots_manifest.json, unchanged. The manifest's own `family` field is what the door files it under.
    fam = sys.argv[sys.argv.index("--family") + 1] if "--family" in sys.argv[2:-1] else "k8"
    if (kind != "slots" and fam != "k8") or fam not in ("k8", "flux", "mega"):
        print("::error::--family is for slots only (k8, flux, mega)"); sys.exit(1)
    src = CDN if fam == "k8" else os.path.join(CDN, "fam", fam)
    # a family pack goes to its own door (/v1/slots/<fam>/publish): an older server has none and refuses it, so a family pack can
    # never land in the k8 keys the 0.17.82/83 boxes read
    door = DOOR.replace("/v1/events/publish", f"/v1/{kind}/publish" if fam == "k8" else f"/v1/slots/{fam}/publish")
    key = os.environ.get("EVENTS_PUBLISH_KEY", "").strip()
    if not key:
        print("::error::EVENTS_PUBLISH_KEY not set"); sys.exit(1)
    man = json.load(open(os.path.join(src, f"{kind}_manifest.json"), encoding="utf-8"))
    if kind == "slots" and man.get("family", "k8") != fam:
        print(f"::error::slots manifest is for {man.get('family', 'k8')}, not {fam} -- not publishing"); sys.exit(1)
    body = open(os.path.join(src, man["file"]), "rb").read()
    # the same guard the private publisher applies: never ship a stale or broken pack
    gen = dt.datetime.strptime(man["generatedAt"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    age_min = (dt.datetime.now(dt.timezone.utc) - gen).total_seconds() / 60
    if age_min > 30:
        print(f"::error::{kind} pack is {age_min:.0f} min old -- not publishing"); sys.exit(1)
    if man.get("boards") and len(man.get("failed") or []) * 2 > int(man["boards"]):
        print(f"::error::{len(man['failed'])}/{man['boards']} boards failed -- not publishing"); sys.exit(1)
    req = urllib.request.Request(door, data=body, method="POST", headers={
        "x-events-key": key,
        "x-events-manifest": json.dumps(man, separators=(",", ":")),
        "content-type": "application/gzip",
        "user-agent": "stevesapp-events-builder",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"::error::publish door answered {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"); sys.exit(1)
    except Exception as e:  # audit: DNS / timeout / refused -> a clean annotation, not a traceback
        print(f"::error::publish door unreachable: {type(e).__name__} {str(e)[:160]}"); sys.exit(1)
    if out.get("ok"):
        detail = (" ".join(f"{k} {c['movies']}/{c['series']}" for k, c in man['counts'].items()) + f" (TMDB calls {man.get('calls')})" if kind == "brand_lists"
                  else f"{man['events']} events, {man['live']} live, failed {man['failed']}" if kind == "events"
                  else f"{fam}: {man['slots']} slots in {man['buckets']} buckets" if kind == "slots"
                  else f"{man['family']}: " + ("unchanged (checked)" if out.get("unchanged") else "ids " + ", ".join(f"{k} {v}" for k, v in man['counts'].items())) if kind == "cats"
                  else f"{man['family']}: {man['keys']} guide ids, {man['streams']} channels, {man['progs']} programmes" if kind == "chguide"
                  else f"+{man['movies_added']}/-{man['movies_removed']} movies, +{man['series_added']}/-{man['series_removed']} series, {man['series_changed']} shows changed")
        print(f"published {kind} {out['published']} (replaced {out.get('replaced') or '-'}) -- {detail}")
    else:
        print(f"skipped: {out.get('skipped')} (published {out.get('published')})")


if __name__ == "__main__":
    main()
