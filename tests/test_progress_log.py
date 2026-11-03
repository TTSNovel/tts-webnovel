import json
import os

import pytest

import progress_log
from progress_log import LocalStore, PreconditionFailed, ProgressLog

for _key, _value in {
    "WEB_AUTH_USER": "u",
    "WEB_AUTH_PASS": "p",
    "TTS_API_URL": "http://tts.invalid",
    "TTS_API_SECRET": "s",
    "SESSION_SECRET": "test-secret",
}.items():
    os.environ.setdefault(_key, _value)

import server  # noqa: E402  (needs the env above at import time)

BOOK = 7
IPHONE = ("dev-iphone", "iPhone")
MAC = ("dev-mac", "MacBook")
FIREFOX = ("dev-firefox", "Firefox · Linux")


def make_log(site, **kwargs):
    return ProgressLog(LocalStore(site / "progress"), site, **kwargs)


def ev(event_id, chapter, base_rev, kind="select", book_id=BOOK):
    return {"id": event_id, "book_id": book_id, "chapter": chapter, "kind": kind, "base_rev": base_rev}


@pytest.fixture
def log(tmp_path):
    return make_log(tmp_path)


# --- the five rules ------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        ev("", 1, 0),
        ev("a", -1, 0),
        ev("a", True, 0),
        ev("a", 1, -1),
        ev("a", 1, None),
        ev("a", 1, 0, kind="legacy"),
        {**ev("a", 1, 0), "book_id": "7"},
        {**ev("a", 1, 0), "client_ts": 5},
    ],
)
def test_rule1_invalid_body_is_400(log, body):
    status, _ = log.post_event(body, IPHONE)
    assert status == 400
    assert log.history(BOOK)["events"] == []


def test_rule4_first_event_on_unread_book_applies_with_rev_0(log):
    status, body = log.post_event({**ev("e1", 3, 0), "client_ts": "2026-11-03T10:00:00Z"}, IPHONE)
    assert status == 200
    pos = body["position"]
    assert (pos["chapter"], pos["rev"], pos["sentence"]) == (3, 1, 0)
    assert (pos["device_id"], pos["device_name"]) == IPHONE
    assert pos["updated_at"].endswith("Z") and "." in pos["updated_at"]
    e = body["event"]
    assert e["applied"] is True and e["conflict"] is False and e["rev"] == 1
    assert e["prev_chapter"] is None and e["client_ts"] == "2026-11-03T10:00:00Z"
    assert set(e) == {
        "id", "book_id", "chapter", "prev_chapter", "kind", "base_rev", "device_id",
        "device_name", "client_ts", "server_ts", "applied", "conflict", "rev",
    }
    assert set(pos) == {"chapter", "sentence", "updated_at", "rev", "device_id", "device_name"}


def test_rule3_same_chapter_is_recorded_but_not_applied(log):
    log.post_event(ev("e1", 3, 0), IPHONE)
    # Even with a stale base_rev: being on the current chapter is never a conflict.
    status, body = log.post_event(ev("e2", 3, 0), MAC)
    assert status == 200
    assert body["position"]["rev"] == 1 and body["position"]["device_name"] == "iPhone"
    assert body["event"]["applied"] is False and body["event"]["conflict"] is False
    assert body["event"]["rev"] == 1 and body["event"]["prev_chapter"] == 3


def test_rule4_current_base_applies_backward_too(log):
    log.post_event(ev("e1", 10, 0), IPHONE)
    status, body = log.post_event(ev("e2", 4, 1, kind="restore"), MAC)
    assert status == 200
    assert body["position"]["chapter"] == 4 and body["position"]["rev"] == 2
    assert body["event"]["prev_chapter"] == 10 and body["event"]["kind"] == "restore"


def test_rule5_stale_base_is_409_and_recorded(log):
    log.post_event(ev("e1", 10, 0), IPHONE)
    log.post_event(ev("e2", 11, 1, kind="auto_next"), IPHONE)
    status, body = log.post_event(ev("e3", 12, 1), MAC)
    assert status == 409
    assert body["position"]["chapter"] == 11 and body["position"]["rev"] == 2
    assert body["event"]["applied"] is False and body["event"]["conflict"] is True
    assert body["event"]["rev"] == 2


@pytest.mark.parametrize("first_base, expected", [(1, 200), (5, 409)])
def test_rule2_retry_returns_same_status_without_writing(log, first_base, expected):
    log.post_event(ev("seed", 1, 0), IPHONE)  # rev 1
    status1, body1 = log.post_event(ev("dup", 2, first_base), MAC)
    # Position moves on in between; the retry must not be re-evaluated.
    log.post_event(ev("other", 9, log.history(BOOK)["position"]["rev"]), IPHONE)
    status2, body2 = log.post_event(ev("dup", 2, first_base), MAC)
    assert status1 == status2 == expected
    assert body2["event"] == body1["event"]
    assert [e["id"] for e in log.history(BOOK)["events"]].count("dup") == 1


# --- legacy POST -----------------------------------------------------------------


def test_legacy_apply_mode_moves_position_and_logs(tmp_path):
    log = make_log(tmp_path, legacy_mode="apply")
    status, pos = log.post_legacy({"book_id": BOOK, "chapter": 5, "sentence": 12}, FIREFOX)
    assert status == 200 and pos["chapter"] == 5 and pos["rev"] == 1 and pos["sentence"] == 0
    # Same chapter again (old clients re-post on every pause): no write.
    log.post_legacy({"book_id": BOOK, "chapter": 5, "sentence": 30}, FIREFOX)
    events = log.history(BOOK)["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "legacy" and events[0]["base_rev"] is None and events[0]["applied"] is True
    assert events[0]["device_name"] == "Firefox · Linux"


def test_legacy_log_mode_never_moves_position(tmp_path):
    log = make_log(tmp_path, legacy_mode="log")
    log.post_event(ev("e1", 441, 0), IPHONE)
    status, pos = log.post_legacy({"book_id": BOOK, "chapter": 297, "sentence": 0}, FIREFOX)
    assert status == 200 and pos["chapter"] == 441 and pos["rev"] == 1
    events = log.history(BOOK)["events"]
    assert events[0]["kind"] == "legacy" and events[0]["applied"] is False and events[0]["chapter"] == 297


def test_legacy_log_mode_unread_book_echoes_position_shape(tmp_path):
    log = make_log(tmp_path, legacy_mode="log")
    status, pos = log.post_legacy({"book_id": BOOK, "chapter": 2, "sentence": 0}, FIREFOX)
    assert status == 200 and pos["chapter"] == 2 and pos["rev"] == 0
    assert log.positions() == {}


def test_legacy_invalid_body_is_400(log):
    assert log.post_legacy({"book_id": BOOK, "chapter": 2}, FIREFOX)[0] == 400


def test_unknown_legacy_mode_rejected(tmp_path):
    with pytest.raises(ValueError):
        make_log(tmp_path, legacy_mode="ignore")


# --- compare-and-swap ------------------------------------------------------------


class RacingStore(LocalStore):
    """Runs `interleave` once, right before the first write — i.e. another
    instance commits between this writer's read and its write."""

    def __init__(self, root, interleave):
        super().__init__(root)
        self.interleave = interleave
        self.precondition_failures = 0

    def write(self, book_id, doc, if_generation):
        if self.interleave:
            hook, self.interleave = self.interleave, None
            hook()
        try:
            return super().write(book_id, doc, if_generation)
        except PreconditionFailed:
            self.precondition_failures += 1
            raise


def test_cas_conflict_reevaluates_and_stale_writer_gets_409(tmp_path):
    seed = make_log(tmp_path)
    seed.post_event(ev("e1", 296, 0), IPHONE)  # rev 1

    other = make_log(tmp_path)
    store = RacingStore(tmp_path / "progress", lambda: other.post_event(ev("iphone", 441, 1), IPHONE))
    racer = ProgressLog(store, tmp_path)
    # Both decided on rev 1; the iPhone commits first, so the Firefox write
    # hits a stale generation, re-reads, and is now a stale writer.
    status, body = racer.post_event(ev("firefox", 297, 1), FIREFOX)
    assert store.precondition_failures == 1
    assert status == 409
    assert body["position"]["chapter"] == 441
    ids = [e["id"] for e in racer.history(BOOK)["events"]]
    assert ids == ["firefox", "iphone", "e1"]


def test_cas_gives_up_with_unavailable(tmp_path):
    class AlwaysStale(LocalStore):
        def write(self, book_id, doc, if_generation):
            raise PreconditionFailed("stale")

    log = ProgressLog(AlwaysStale(tmp_path / "progress"), tmp_path)
    with pytest.raises(progress_log.Unavailable):
        log.post_event(ev("e1", 1, 0), IPHONE)


def test_local_store_generation_check(tmp_path):
    store = LocalStore(tmp_path)
    assert store.read(1) == (None, 0)
    assert store.write(1, {"position": None, "events": []}, 0) == 1
    with pytest.raises(PreconditionFailed):
        store.write(1, {"position": None, "events": []}, 0)
    doc, generation = store.read(1)
    assert generation == 1 and "_generation" not in doc


# --- migration -----------------------------------------------------------------------


def write_legacy(site, data):
    (site / "progress.json").write_text(json.dumps(data), encoding="utf-8")


def test_migration_seed_read_only_then_persisted_on_first_write(tmp_path):
    write_legacy(tmp_path, {str(BOOK): {"chapter": 120, "sentence": 4, "updated_at": "2026-11-01T08:00:00.5Z"}})
    log = make_log(tmp_path)

    pos = log.positions()[str(BOOK)]
    assert pos == {
        "chapter": 120, "sentence": 0, "updated_at": "2026-11-01T08:00:00.5Z",
        "rev": 1, "device_id": None, "device_name": "(migrated)",
    }
    hist = log.history(BOOK)
    assert [e["kind"] for e in hist["events"]] == ["legacy"]
    assert hist["events"][0]["applied"] is True and hist["events"][0]["device_name"] == "(migrated)"
    assert not (tmp_path / "progress" / f"{BOOK}.json").exists()  # GET never writes

    status, body = log.post_event(ev("e1", 121, 1, kind="auto_next"), IPHONE)
    assert status == 200 and body["position"]["rev"] == 2
    events = log.history(BOOK)["events"]
    assert [e["id"] for e in events] == ["e1", f"legacy-migrated-{BOOK}"]
    # v2 document wins over progress.json from now on.
    write_legacy(tmp_path, {str(BOOK): {"chapter": 1, "sentence": 0, "updated_at": "2026-11-02T00:00:00Z"}})
    assert log.positions()[str(BOOK)]["chapter"] == 121


def test_positions_merges_v2_and_legacy_only_books(tmp_path):
    write_legacy(tmp_path, {"1": {"chapter": 5, "sentence": 0, "updated_at": "2026-10-01T00:00:00Z"}, "junk": {}})
    log = make_log(tmp_path)
    log.post_event(ev("e1", 9, 0, book_id=2), IPHONE)
    result = log.positions()
    assert set(result) == {"1", "2"}
    assert result["1"]["device_name"] == "(migrated)" and result["2"]["chapter"] == 9


# --- history -------------------------------------------------------------------------


def test_history_newest_first_with_limit_and_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(progress_log, "EVENTS_CAP", 5)
    log = make_log(tmp_path)
    for i in range(8):
        log.post_event(ev(f"e{i}", i, i), IPHONE)
    full = log.history(BOOK, limit=500)
    assert [e["id"] for e in full["events"]] == ["e7", "e6", "e5", "e4", "e3"]
    assert full["position"]["chapter"] == 7 and full["position"]["rev"] == 8
    assert [e["id"] for e in log.history(BOOK, limit=2)["events"]] == ["e7", "e6"]
    assert len(log.history(BOOK, limit=0)["events"]) == 1  # clamped to >= 1


def test_history_of_unknown_book(log):
    assert log.history(999) == {"position": None, "events": []}


# --- the 2026-11-03 incident -------------------------------------------------------


def test_incident_replay_stale_tab_cannot_overwrite_but_going_back_is_allowed(log):
    log.post_event(ev("i1", 296, 0), IPHONE)
    firefox_base = log.positions()[str(BOOK)]["rev"]  # tab opened back then

    status, body = log.post_event(ev("i2", 441, firefox_base), IPHONE)
    assert status == 200 and body["position"]["chapter"] == 441
    current = body["position"]["rev"]

    status, body = log.post_event(ev("f1", 297, firefox_base), FIREFOX)
    assert status == 409
    assert body["position"]["chapter"] == 441 and body["position"]["device_name"] == "iPhone"
    assert log.positions()[str(BOOK)]["chapter"] == 441

    # The user on the iPhone (who has seen 441) deliberately goes back.
    status, body = log.post_event(ev("i3", 297, current), IPHONE)
    assert status == 200 and body["position"]["chapter"] == 297


# --- device identity ----------------------------------------------------------------


@pytest.mark.parametrize(
    "ua, expected",
    [
        ("Mozilla/5.0 (X11; Linux x86_64; rv:159.0) Gecko/20100101 Firefox/159.0", "Firefox · Linux"),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0 Safari/537.36",
            "Chrome · macOS",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.0 Mobile/15E148 Safari/604.1",
            "Safari · iOS",
        ),
        ("WebnovelReader/2611031200 CFNetwork/1568.100.1 Darwin/24.0.0", "WebnovelReader (iOS)"),
        ("curl/8.0", "curl/8.0"),
        ("x" * 100, "x" * 60),
        ("", None),
    ],
)
def test_device_name_from_user_agent(ua, expected):
    assert progress_log.device_name_from_user_agent(ua) == expected


def test_device_headers_are_percent_decoded():
    headers = {"X-Device-Id": " abc ", "X-Device-Name": "iPhone%20c%E1%BB%A7a%20Long", "User-Agent": "curl/8"}
    assert progress_log.device_from_headers(headers) == ("abc", "iPhone của Long")
    assert progress_log.device_from_headers({"User-Agent": "curl/8"}) == (None, "curl/8")


# --- HTTP routes ---------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROGRESS", make_log(tmp_path))
    c = server.app.test_client()
    with c.session_transaction() as s:
        s["authed"] = True
    return c


def test_routes_require_login(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROGRESS", make_log(tmp_path))
    c = server.app.test_client()
    assert c.get("/api/progress").status_code == 401
    assert c.post("/api/progress/events", json=ev("e", 1, 0)).status_code == 401
    assert c.get(f"/api/progress/{BOOK}/history").status_code == 401


def test_routes_end_to_end(client):
    headers = {"X-Device-Id": "web-1", "X-Device-Name": "Firefox%20%C2%B7%20Linux"}
    r = client.post("/api/progress/events", json=ev("e1", 3, 0), headers=headers)
    assert r.status_code == 200
    assert r.get_json()["position"]["device_name"] == "Firefox · Linux"

    r = client.post("/api/progress/events", json=ev("e2", 9, 0), headers=headers)
    assert r.status_code == 409 and r.get_json()["event"]["conflict"] is True

    assert client.post("/api/progress/events", data="nope").status_code == 400

    r = client.get("/api/progress")
    assert r.get_json()[str(BOOK)]["chapter"] == 3

    r = client.get(f"/api/progress/{BOOK}/history?limit=1")
    body = r.get_json()
    assert [e["id"] for e in body["events"]] == ["e2"] and body["position"]["rev"] == 1


def test_legacy_route_keeps_v1_response_shape(client):
    r = client.post(
        "/api/progress",
        json={"book_id": BOOK, "chapter": 4, "sentence": 2},
        headers={"User-Agent": "WebnovelReader/1 CFNetwork/1 Darwin/24"},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert {"chapter", "sentence", "updated_at"} <= set(body) and body["chapter"] == 4
    assert body["device_name"] == "WebnovelReader (iOS)"
    assert client.post("/api/progress", json={"book_id": BOOK}).status_code == 400


def test_storage_unavailable_is_503(client, monkeypatch):
    def boom(*a, **k):
        raise progress_log.Unavailable("down")

    monkeypatch.setattr(server.PROGRESS, "post_event", boom)
    assert client.post("/api/progress/events", json=ev("e1", 1, 0)).status_code == 503


# --- GCS backend (HTTP mocked) ---------------------------------------------------


class FakeResponse:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


def test_gcs_store_maps_generation_404_and_412(monkeypatch):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(("GET", url, params))
        if url.endswith("progress%2F1.json"):
            return FakeResponse(404)
        if url.endswith("/o"):
            return FakeResponse(200, {"items": [{"name": "progress/2.json"}, {"name": "progress/x.json"}]})
        return FakeResponse(200, {"position": None, "events": []}, {"x-goog-generation": "1699"})

    def fake_post(url, params=None, data=None, headers=None, timeout=None):
        calls.append(("POST", url, params))
        if params["ifGenerationMatch"] == "0":
            return FakeResponse(412, {"error": "precondition"})
        return FakeResponse(200, {"generation": "1700"})

    monkeypatch.setattr(progress_log.requests, "get", fake_get)
    monkeypatch.setattr(progress_log.requests, "post", fake_post)
    store = progress_log.GcsStore(lambda: ({"Authorization": "Bearer t"}, "proj"))

    assert store.read(1) == (None, 0)
    assert store.read(2) == ({"position": None, "events": []}, 1699)
    assert store.list_book_ids() == [2]
    with pytest.raises(PreconditionFailed):
        store.write(1, {"position": None, "events": []}, 0)
    assert store.write(2, {"position": None, "events": []}, 1699) == 1700
    assert calls[0][1] == "https://storage.googleapis.com/storage/v1/b/proj-novel-web-site/o/progress%2F1.json"
    assert calls[-1][1] == "https://storage.googleapis.com/upload/storage/v1/b/proj-novel-web-site/o"
    assert calls[-1][2] == {"uploadType": "media", "name": "progress/2.json", "ifGenerationMatch": "1699"}


def test_from_env_picks_gcs_on_cloud_run(tmp_path, monkeypatch):
    monkeypatch.setenv("K_SERVICE", "novel-web")
    monkeypatch.setenv("PROGRESS_BUCKET", "my-bucket")
    monkeypatch.setenv("PROGRESS_LEGACY_MODE", "log")
    log = progress_log.from_env(tmp_path, lambda: ({}, "proj"))
    assert isinstance(log.store, progress_log.GcsStore) and log.legacy_mode == "log"
    assert log.store._ctx()[1] == "my-bucket"
    monkeypatch.delenv("K_SERVICE")
    assert isinstance(progress_log.from_env(tmp_path, None).store, LocalStore)
