"""Reading position per book as an append-only event log ("reading history
v2"), shared by the iOS/macOS/watchOS apps and the web reader through
/api/progress (see server.py).

Why not one "current chapter" value per book any more: that model applied
every POST blindly with the server's clock (last write wins), so a client
that merely re-announced a stale position — incident 2026-11-03, a Firefox
tab still on chapter 297 posting when it lost focus — overwrote the real
one (441) on every device, with nothing left to restore from.

Now each book is one document `progress/<book_id>.json`:
`{"position": Position|None, "events": [Event, oldest -> newest]}`.
Only deliberate user actions create events (select, auto_next, restore),
and an event only moves the position if the writer's `base_rev` equals the
current `rev`, i.e. it decided with the latest position in view. Direction
is irrelevant (re-reading an old chapter is legitimate); a stale writer
gets 409 and asks the user on that device. Nothing ever deletes or edits an
event, so any position can be restored from the history.

Documents are written with compare-and-swap on the storage generation: on
Cloud Run (up to 10 instances) two writers can read the same document, and
the loser has to re-read and re-evaluate the rules against what the winner
wrote — otherwise a stale writer could still slip past the rev check.
"""
import fcntl
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

import requests

EVENT_KINDS = ("select", "auto_next", "restore")
# ~ a few months of continuous listening per book; older events fall off.
EVENTS_CAP = 500
HISTORY_DEFAULT_LIMIT = 100
HISTORY_MAX_LIMIT = 500
# CAS attempts before giving up with 503 — contention is a handful of
# devices of one user, so more than a couple of retries means something
# else is wrong.
MAX_ATTEMPTS = 5
LEGACY_MODES = ("apply", "log")
MIGRATED_DEVICE_NAME = "(migrated)"
_MAX_HEADER_LEN = 100
_MAX_UA_NAME_LEN = 60


class PreconditionFailed(Exception):
    """The document changed since it was read (GCS 412)."""


class Unavailable(Exception):
    """Storage could not be read/written, or CAS kept losing — maps to 503."""


def now_iso():
    # Fractional seconds always present (isoformat drops them when the
    # microsecond happens to be 0) so clients can parse one fixed format.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# --- device identity ---------------------------------------------------------


def _browser_of(ua):
    for token, name in (
        ("Edg/", "Edge"),
        ("OPR/", "Opera"),
        ("Firefox/", "Firefox"),
        ("FxiOS/", "Firefox"),
        ("CriOS/", "Chrome"),
        ("Chrome/", "Chrome"),
        ("Safari/", "Safari"),
    ):
        if token in ua:
            return name
    return None


def _os_of(ua):
    for token, name in (
        ("iPhone", "iOS"),
        ("iPad", "iPadOS"),
        ("Android", "Android"),
        ("CrOS", "ChromeOS"),
        ("Windows", "Windows"),
        ("Mac OS X", "macOS"),
        ("Macintosh", "macOS"),
        ("Linux", "Linux"),
        ("X11", "Linux"),
    ):
        if token in ua:
            return name
    return None


def device_name_from_user_agent(ua):
    """Human label for clients that don't send X-Device-Name (old app builds,
    old cached web reader) — enough to tell "the Firefox tab" from "the
    phone" in the history, which is what the incident was missing."""
    ua = (ua or "").strip()
    if not ua:
        return None
    # The apps use URLSession's default UA: "<App>/<build> CFNetwork/... Darwin/...".
    if "CFNetwork" in ua or "Darwin/" in ua:
        app_name = unquote(ua.split("/", 1)[0]).strip()
        if app_name:
            return f"{app_name} (iOS)"
    browser, os_name = _browser_of(ua), _os_of(ua)
    if browser and os_name:
        return f"{browser} · {os_name}"
    if browser:
        return browser
    return ua[:_MAX_UA_NAME_LEN]


def device_from_headers(headers):
    """(device_id, device_name) for a request. X-Device-Name is
    percent-encoded UTF-8 since HTTP header values are Latin-1 only."""
    device_id = (headers.get("X-Device-Id") or "").strip()[:_MAX_HEADER_LEN] or None
    raw_name = (headers.get("X-Device-Name") or "").strip()
    device_name = unquote(raw_name).strip()[:_MAX_HEADER_LEN] if raw_name else ""
    if not device_name:
        device_name = device_name_from_user_agent(headers.get("User-Agent"))
    return device_id, device_name


# --- storage backends ----------------------------------------------------------


class LocalStore:
    """Directory-backed store for tests and local dev. Mirrors the GCS
    contract: read() returns the generation (0 = missing), write() only
    succeeds if the generation still matches. The generation lives inside
    the file and the check+write runs under a process lock (threading) plus
    a file lock (several gunicorn workers locally)."""

    _GEN_KEY = "_generation"

    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.Lock()

    def _path(self, book_id):
        return self.root / f"{int(book_id)}.json"

    def _read_raw(self, book_id):
        path = self._path(book_id)
        if not path.exists():
            return None, 0
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw, int(raw.pop(self._GEN_KEY, 1))

    def read(self, book_id):
        return self._read_raw(book_id)

    def write(self, book_id, doc, if_generation):
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self.root / ".lock", "a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            _, current = self._read_raw(book_id)
            if current != if_generation:
                raise PreconditionFailed(f"book {book_id}: generation {current} != {if_generation}")
            generation = current + 1
            path = self._path(book_id)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({**doc, self._GEN_KEY: generation}), encoding="utf-8")
            os.replace(tmp, path)
            return generation

    def list_book_ids(self):
        if not self.root.is_dir():
            return []
        return sorted(int(p.stem) for p in self.root.glob("*.json") if p.stem.isdigit())


class GcsStore:
    """GCS JSON API straight through `requests` + ADC (same approach as
    translation.py's glossary calls), NOT the FUSE mount: the mount caches
    reads and can't express "write only if unchanged", which is exactly the
    lost-update race this module exists to close."""

    _API = "https://storage.googleapis.com/storage/v1"
    _UPLOAD = "https://storage.googleapis.com/upload/storage/v1"
    _PREFIX = "progress/"

    def __init__(self, headers_fn, bucket=None):
        # headers_fn() -> (auth headers, project); the bucket defaults to the
        # site bucket, which needs the project id that only ADC knows.
        self._headers_fn = headers_fn
        self._bucket = bucket

    def _ctx(self):
        headers, project = self._headers_fn()
        # translation._headers adds x-goog-user-project for the Translation
        # API; on GCS it makes the call need serviceusage.services.use, which
        # the Cloud Run service account doesn't have (every request 403s).
        # The bucket lives in the same project, so it isn't needed here.
        headers = {k: v for k, v in headers.items() if k.lower() != "x-goog-user-project"}
        if not self._bucket:
            self._bucket = f"{project}-novel-web-site"
        return headers, self._bucket

    def _name(self, book_id):
        return f"{self._PREFIX}{int(book_id)}.json"

    def read(self, book_id):
        headers, bucket = self._ctx()
        url = f"{self._API}/b/{bucket}/o/{quote(self._name(book_id), safe='')}"
        try:
            resp = requests.get(url, params={"alt": "media"}, headers=headers, timeout=15)
        except requests.RequestException as e:
            raise Unavailable(f"read {self._name(book_id)}: {e}") from e
        if resp.status_code == 404:
            return None, 0
        if resp.status_code != 200:
            raise Unavailable(f"read {self._name(book_id)}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json(), int(resp.headers["x-goog-generation"])

    def write(self, book_id, doc, if_generation):
        headers, bucket = self._ctx()
        params = {"uploadType": "media", "name": self._name(book_id), "ifGenerationMatch": str(if_generation)}
        try:
            resp = requests.post(
                f"{self._UPLOAD}/b/{bucket}/o",
                params=params,
                data=json.dumps(doc).encode("utf-8"),
                headers={**headers, "Content-Type": "application/json"},
                timeout=15,
            )
        except requests.RequestException as e:
            raise Unavailable(f"write {self._name(book_id)}: {e}") from e
        if resp.status_code == 412:
            raise PreconditionFailed(f"{self._name(book_id)}: generation {if_generation} is stale")
        if resp.status_code != 200:
            raise Unavailable(f"write {self._name(book_id)}: HTTP {resp.status_code} {resp.text[:200]}")
        return int(resp.json()["generation"])

    def list_book_ids(self):
        headers, bucket = self._ctx()
        ids, page_token = [], None
        while True:
            params = {"prefix": self._PREFIX, "fields": "items(name),nextPageToken"}
            if page_token:
                params["pageToken"] = page_token
            try:
                resp = requests.get(f"{self._API}/b/{bucket}/o", params=params, headers=headers, timeout=15)
            except requests.RequestException as e:
                raise Unavailable(f"list {self._PREFIX}: {e}") from e
            if resp.status_code != 200:
                raise Unavailable(f"list {self._PREFIX}: HTTP {resp.status_code} {resp.text[:200]}")
            body = resp.json()
            for item in body.get("items", []):
                stem = item["name"][len(self._PREFIX):].removesuffix(".json")
                if stem.isdigit():
                    ids.append(int(stem))
            page_token = body.get("nextPageToken")
            if not page_token:
                return sorted(ids)


# --- legacy progress.json ------------------------------------------------------


def load_legacy(site_dir):
    """The v1 `{book_id: {chapter, sentence, updated_at}}` file. Read only
    (to seed books that have no v2 document yet); never written again."""
    path = Path(site_dir) / "progress.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _legacy_seed(book_id, entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("chapter"), int):
        return None
    updated_at = entry.get("updated_at") or now_iso()
    position = {
        "chapter": entry["chapter"],
        "sentence": 0,
        "updated_at": updated_at,
        "rev": 1,
        "device_id": None,
        "device_name": MIGRATED_DEVICE_NAME,
    }
    event = {
        # Deterministic so a read-only seed (GET before any write) shows the
        # same id as the one later persisted.
        "id": f"legacy-migrated-{int(book_id)}",
        "book_id": int(book_id),
        "chapter": entry["chapter"],
        "prev_chapter": None,
        "kind": "legacy",
        "base_rev": None,
        "device_id": None,
        "device_name": MIGRATED_DEVICE_NAME,
        "client_ts": None,
        "server_ts": updated_at,
        "applied": True,
        "conflict": False,
        "rev": 1,
    }
    return {"position": position, "events": [event]}


# --- the log itself -------------------------------------------------------------


def _is_int(value):
    # bool is an int subclass — `true` must not pass as chapter 1.
    return isinstance(value, int) and not isinstance(value, bool)


def parse_event_body(body):
    """Validated POST /api/progress/events body, or None (→ 400)."""
    if not isinstance(body, dict):
        return None
    event_id, client_ts = body.get("id"), body.get("client_ts")
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > _MAX_HEADER_LEN:
        return None
    if not _is_int(body.get("book_id")) or body["book_id"] < 0:
        return None
    if not _is_int(body.get("chapter")) or body["chapter"] < 0:
        return None
    if body.get("kind") not in EVENT_KINDS:
        return None
    if not _is_int(body.get("base_rev")) or body["base_rev"] < 0:
        return None
    if client_ts is not None and not isinstance(client_ts, str):
        return None
    return {
        "id": event_id,
        "book_id": body["book_id"],
        "chapter": body["chapter"],
        "kind": body["kind"],
        "base_rev": body["base_rev"],
        "client_ts": client_ts[:64] if client_ts else None,
    }


class ProgressLog:
    def __init__(self, store, site_dir, legacy_mode="apply", now=now_iso, new_id=None, log=None):
        if legacy_mode not in LEGACY_MODES:
            raise ValueError(f"PROGRESS_LEGACY_MODE must be one of {LEGACY_MODES}, got {legacy_mode!r}")
        self.store = store
        self.site_dir = site_dir
        self.legacy_mode = legacy_mode
        self._now = now
        self._new_id = new_id or (lambda: str(uuid.uuid4()))
        self._log = log or (lambda record: None)

    # -- reads --

    def _load(self, book_id):
        """(doc, generation) — a missing v2 document is seeded from legacy
        progress.json in memory (generation 0, so the first write creates it
        with exactly this seed underneath the new event)."""
        doc, generation = self.store.read(book_id)
        if doc is None:
            doc = _legacy_seed(book_id, load_legacy(self.site_dir).get(str(book_id))) or {
                "position": None,
                "events": [],
            }
        return doc, generation

    def positions(self):
        ids = self.store.list_book_ids()
        # One object per book means one read per book; parallel so the
        # Library page's single GET doesn't pay N sequential round trips.
        with ThreadPoolExecutor(max_workers=8) as pool:
            docs = list(pool.map(lambda book_id: self.store.read(book_id)[0], ids))
        result = {str(book_id): doc["position"] for book_id, doc in zip(ids, docs) if doc and doc.get("position")}
        # Books only ever written by v1: merged in read-only, with the same
        # rev (1) their seed will have once written, so a client's base_rev
        # from this GET matches on its first event.
        for key, entry in load_legacy(self.site_dir).items():
            if key in result or not key.isdigit():
                continue
            seed = _legacy_seed(int(key), entry)
            if seed:
                result[key] = seed["position"]
        return result

    def history(self, book_id, limit=HISTORY_DEFAULT_LIMIT):
        limit = max(1, min(int(limit), HISTORY_MAX_LIMIT))
        doc, _ = self._load(book_id)
        return {"position": doc.get("position"), "events": list(reversed(doc.get("events", [])[-limit:]))}

    # -- writes --

    def _transact(self, book_id, decide):
        """Read → decide → CAS write, re-deciding from scratch on every lost
        race. decide(doc) returns (result, new_doc or None for no write)."""
        for _ in range(MAX_ATTEMPTS):
            doc, generation = self._load(book_id)
            result, new_doc = decide(doc)
            if new_doc is None:
                return result
            new_doc["events"] = new_doc["events"][-EVENTS_CAP:]
            try:
                self.store.write(book_id, new_doc, generation)
            except PreconditionFailed:
                continue
            return result
        raise Unavailable(f"book {book_id}: gave up after {MAX_ATTEMPTS} conflicting writes")

    def _make_event(self, *, event_id, book_id, chapter, position, kind, base_rev, device, client_ts):
        device_id, device_name = device
        return {
            "id": event_id,
            "book_id": book_id,
            "chapter": chapter,
            "prev_chapter": position["chapter"] if position else None,
            "kind": kind,
            "base_rev": base_rev,
            "device_id": device_id,
            "device_name": device_name,
            "client_ts": client_ts,
            "server_ts": self._now(),
            "applied": False,
            "conflict": False,
            "rev": position["rev"] if position else 0,
        }

    def _moved_position(self, chapter, position, event, device):
        device_id, device_name = device
        return {
            "chapter": chapter,
            "sentence": 0,
            "updated_at": event["server_ts"],
            "rev": (position["rev"] if position else 0) + 1,
            "device_id": device_id,
            "device_name": device_name,
        }

    def post_event(self, body, device):
        """POST /api/progress/events → (status, payload)."""
        req = parse_event_body(body)
        if req is None:
            return 400, {"error": "invalid event"}
        book_id = req["book_id"]

        def decide(doc):
            position, events = doc.get("position"), doc.get("events", [])
            # Rule 2: a retry of an event already processed gets the same
            # answer again, never a second evaluation.
            for stored in events:
                if stored.get("id") == req["id"]:
                    status = 409 if stored.get("conflict") else 200
                    return (status, {"position": position, "event": stored}, False), None

            event = self._make_event(
                event_id=req["id"],
                book_id=book_id,
                chapter=req["chapter"],
                position=position,
                kind=req["kind"],
                base_rev=req["base_rev"],
                device=device,
                client_ts=req["client_ts"],
            )
            current_rev = position["rev"] if position else 0
            status = 200
            if position and req["chapter"] == position["chapter"]:
                pass  # rule 3: already there — recorded, nothing moves
            elif req["base_rev"] == current_rev:
                # Rule 4: the writer saw the latest position — apply, forward
                # or backward alike.
                position = self._moved_position(req["chapter"], position, event, device)
                event["applied"] = True
                event["rev"] = position["rev"]
            else:
                # Rule 5: decided on an outdated position — record it, but
                # let the user on that device choose (409).
                event["conflict"] = True
                status = 409
            new_doc = {"position": position, "events": events + [event]}
            return (status, {"position": position, "event": event}, True), new_doc

        status, payload, fresh = self._transact(book_id, decide)
        if fresh:
            self._log_event(payload["event"])
        return status, payload

    def post_legacy(self, body, device):
        """POST /api/progress (v1 clients: old app builds, cached web
        reader) → (status, payload). No base_rev, so it can't prove it saw
        the latest position: in "apply" mode (rollout) it still moves the
        position as v1 did, but now leaves a trace to restore from; in "log"
        mode it is history only."""
        body = body if isinstance(body, dict) else {}
        book_id, chapter, sentence = body.get("book_id"), body.get("chapter"), body.get("sentence")
        if not _is_int(book_id) or not _is_int(chapter) or not _is_int(sentence) or book_id < 0 or chapter < 0:
            return 400, {"error": "invalid progress"}

        def decide(doc):
            position, events = doc.get("position"), doc.get("events", [])
            if position and chapter == position["chapter"]:
                # Old clients re-post the chapter they are on at every
                # pause/tab switch; logging each one would push real history
                # out of the capped event list for no information.
                return (position, None), None
            event = self._make_event(
                event_id=self._new_id(),
                book_id=book_id,
                chapter=chapter,
                position=position,
                kind="legacy",
                base_rev=None,
                device=device,
                client_ts=None,
            )
            if self.legacy_mode == "apply":
                position = self._moved_position(chapter, position, event, device)
                event["applied"] = True
                event["rev"] = position["rev"]
            new_doc = {"position": position, "events": events + [event]}
            return (position, event), new_doc

        position, event = self._transact(book_id, decide)
        if event:
            self._log_event(event)
        if position is None:
            # log mode, never-read book: echo the request in Position shape
            # so v1 clients still decode a response.
            device_id, device_name = device
            position = {
                "chapter": chapter,
                "sentence": 0,
                "updated_at": self._now(),
                "rev": 0,
                "device_id": device_id,
                "device_name": device_name,
            }
        return 200, position

    def _log_event(self, event):
        self._log(
            {
                "message": "progress event",
                "book_id": event["book_id"],
                "chapter": event["chapter"],
                "prev_chapter": event["prev_chapter"],
                "kind": event["kind"],
                "base_rev": event["base_rev"],
                "rev": event["rev"],
                "device_id": event["device_id"],
                "device_name": event["device_name"],
                "applied": event["applied"],
                "conflict": event["conflict"],
                "legacy_mode": self.legacy_mode if event["kind"] == "legacy" else None,
            }
        )


def from_env(site_dir, headers_fn):
    """GCS on Cloud Run (K_SERVICE is set there), local directory otherwise."""
    if os.environ.get("K_SERVICE"):
        store = GcsStore(headers_fn, bucket=os.environ.get("PROGRESS_BUCKET") or None)
    else:
        store = LocalStore(Path(site_dir) / "progress")

    def log(record):
        # One JSON line on stdout = one structured Cloud Logging entry, so a
        # jump like the 2026-11-03 one can be traced by book/device directly.
        print(json.dumps(record, ensure_ascii=False), flush=True)

    legacy_mode = os.environ.get("PROGRESS_LEGACY_MODE", "apply").strip() or "apply"
    return ProgressLog(store, site_dir, legacy_mode=legacy_mode, log=log)
