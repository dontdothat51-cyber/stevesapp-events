"""
FAMILY BUILDERS, shared pieces (2026-10-04, app 0.17.44 contract: docs/CLOUD_CONTRACT_0.17.44.md).

Owner rule: every "soft check" a box used to make against its provider (categories, guide, episode lists) is made ONCE
PER SERVER FAMILY by this builder, on the family's BUILDER line -- k8 = env SLOTS_LINE (the k8 builder line), flux =
env FLUX_LINE once one exists -- never the owner's or a customer's line. List reads only (player_api.php actions), never
a stream. Pace: at most one provider call every 2 seconds per job (Provider.pace). The answers go to our server through
its key-gated doors (env EVENTS_PUBLISH_KEY), which validate and store them.

PUBLIC LOGS: this code runs in a public repository, so it prints COUNTS only -- never a line, a series id, a channel or
category name. Secrets are masked on Actions.
"""
import gzip, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

UA = "Mozilla/5.0 (Linux; Android) StevesApp"
BASE = os.environ.get("STEVESAPP_BASE", "https://stevesapp.tv").rstrip("/")
FAMILY_RX = re.compile(r"^[a-z0-9]{1,16}$")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDN = os.path.join(ROOT, "cdn")
STATE_DIR = os.path.join(ROOT, "cdn-build", "family")


def log(*a):
    print(*a, flush=True)


def mask(v):
    if v and os.environ.get("GITHUB_ACTIONS"):
        print(f"::add-mask::{v}", flush=True)


def line_of(env_name):
    """(base, user, pw) from "server|user|pass" in env `env_name`, masked; None when unset (the family is skipped)."""
    raw = os.environ.get(env_name, "").strip()
    if raw.count("|") != 2:
        return None
    base, user, pw = [x.strip().rstrip("/") for x in raw.split("|")]
    if not base.startswith("http"):
        base = "http://" + base
    for v in (base, user, pw, re.sub(r"^https?://", "", base).split(":")[0].split("/")[0]):
        mask(v)
    # T22 (REL 0.17.44 audit): the same fail-closed rule as tools/builder_line.py -- on a laptop that holds the dev build's
    # personal line (config/secrets.properties xtream.username) a builder line carrying that username is refused
    owner = None
    try:
        for ln in open(os.path.join(ROOT, "config", "secrets.properties"), encoding="utf-8"):
            if ln.startswith("xtream.username") and "=" in ln:
                owner = ln.split("=", 1)[1].strip() or None
    except OSError:
        pass
    if owner and user == owner:
        raise SystemExit(f"{env_name} carries the OWNER'S personal line -- refusing (T22)")
    return base, user, pw


class ProviderError(Exception):
    def __init__(self, code, what):
        super().__init__(f"{what} -> {code}")
        self.code = code


class Provider:
    """The builder line. One call at a time, at most one every `pace` seconds (the owner's 1 call / 2 s budget)."""

    def __init__(self, line, pace=2.0, timeout=60):
        self.base, self.user, self.pw = line
        self.pace = max(2.0, float(pace))
        self.timeout = timeout
        self.calls = 0
        self._last = 0.0

    def secrets(self):
        return (self.user, self.pw)

    def api(self, action, **params):
        wait = self._last + self.pace - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.calls += 1
        q = {"username": self.user, "password": self.pw, "action": action, **{k: str(v) for k, v in params.items()}}
        url = f"{self.base}/player_api.php?" + urllib.parse.urlencode(q)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise ProviderError(e.code, action)
        except Exception as e:  # DNS, refused, timeout
            raise ProviderError(0, f"{action} {type(e).__name__}")
        finally:
            self._last = time.monotonic()   # the 2 s count from the END of the answer: never two reads closer than that
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise ProviderError(-1, f"{action} not json")


def door(method, path, body=None, timeout=120):
    """Our server's builder doors (x-events-key). A gzip JSON body when `body` is given."""
    key = os.environ.get("EVENTS_PUBLISH_KEY", "").strip()
    if not key:
        raise SystemExit("::error::EVENTS_PUBLISH_KEY not set")
    mask(key)
    hdr = {"x-events-key": key, "user-agent": "stevesapp-family-builder", "accept": "application/json"}
    data = None
    if body is not None:
        data = gzip.compress(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
        hdr.update({"content-type": "application/json", "content-encoding": "gzip"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, headers=hdr, method=method), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise SystemExit(f"::error::door {method} {path.split('?')[0]} -> {e.code} {e.read()[:160]!r}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 2:
                raise SystemExit(f"::error::door {method} {path.split('?')[0]} unreachable ({type(e).__name__})")
        time.sleep(5 * (attempt + 1))


def safe_text(s, n=300):
    """One-line text for our storage: tabs/newlines out, URLs out, the provider's brand phrase out (the 9/23 rule)."""
    from build_slotspack import strip_brand   # the app's Brand.strip twin, byte for byte
    s = re.sub(r"https?://\S+", "", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return strip_brand(s)[:n].strip()


def summary(text):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        open(p, "a", encoding="utf-8").write(text + "\n")


def clean_outputs(*names):
    for n in names:
        try:
            os.remove(os.path.join(CDN, n))
        except FileNotFoundError:
            pass
