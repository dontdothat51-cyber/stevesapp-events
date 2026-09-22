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
    key = os.environ.get("EVENTS_PUBLISH_KEY", "").strip()
    if not key:
        print("::error::EVENTS_PUBLISH_KEY not set"); sys.exit(1)
    man = json.load(open(os.path.join(CDN, "events_manifest.json"), encoding="utf-8"))
    body = open(os.path.join(CDN, man["file"]), "rb").read()
    # the same guard the private publisher applies: never ship a stale or broken pack
    gen = dt.datetime.strptime(man["generatedAt"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    age_min = (dt.datetime.now(dt.timezone.utc) - gen).total_seconds() / 60
    if age_min > 30:
        print(f"::error::events pack is {age_min:.0f} min old -- not publishing"); sys.exit(1)
    if man.get("boards") and len(man.get("failed") or []) * 2 > int(man["boards"]):
        print(f"::error::{len(man['failed'])}/{man['boards']} boards failed -- not publishing"); sys.exit(1)
    req = urllib.request.Request(DOOR, data=body, method="POST", headers={
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
        print(f"published events {out['published']} (replaced {out.get('replaced') or '-'}) -- {man['events']} events, {man['live']} live, failed {man['failed']}")
    else:
        print(f"skipped: {out.get('skipped')} (published {out.get('published')})")


if __name__ == "__main__":
    main()
