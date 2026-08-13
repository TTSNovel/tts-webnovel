"""Serves the novel site behind a real login page (session cookie, not HTTP
Basic Auth — browsers reliably offer to save/autofill a plain <form> login
but are inconsistent about saving Basic Auth popup credentials), proxies
TTS calls to tts-generate server-side so its shared secret never reaches
the browser, and handles /import for adding new books at runtime.

SITE_DIR is a GCS bucket mounted straight into the container (see
tts-pipeline-infra/environments/dev/main.tf) — not baked into the image —
specifically so /import's writes here are immediately visible to every
Cloud Run instance and survive redeploys. The bulk-load path
(build_library.py) writes to the same bucket via `gcloud storage rsync`.
"""
import functools
import hmac
import html
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Flask, Response, abort, redirect, request, send_from_directory, session

from book_renderer import _CSS
from build_library import _LEGACY_EXTENSIONS, add_book_to_site, write_index

_ALLOWED_IMPORT_EXTENSIONS = {".epub"} | _LEGACY_EXTENSIONS

SITE_DIR = os.environ.get("SITE_DIR", os.path.join(os.path.dirname(__file__), "..", "site"))
WEB_AUTH_USER = os.environ["WEB_AUTH_USER"]
WEB_AUTH_PASS = os.environ["WEB_AUTH_PASS"]
TTS_API_URL = os.environ["TTS_API_URL"]
TTS_API_SECRET = os.environ["TTS_API_SECRET"]
DEFAULT_TTS_MODEL = os.environ.get("DEFAULT_TTS_MODEL", "piper_vi")

app = Flask(__name__)
app.secret_key = os.environ["SESSION_SECRET"]
# "Remember me" by default — signed cookie, no server-side session store, so
# this works fine across Cloud Run scaling to multiple instances as long as
# SESSION_SECRET is the same on all of them (it is: same env var).
app.permanent_session_lifetime = timedelta(days=30)
# Defensive backstop — Cloud Run's own platform request-size limit (~32MB)
# is the real constraint for very large books (a couple of this library's
# source files run ~33MB); this just keeps an oversized upload from being
# buffered into memory in full before Flask even looks at it.
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024

_PWA_HEAD = """<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/assets/icons/icon-192.png" type="image/png">
<meta name="theme-color" content="#4f46e5">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Thư viện truyện">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/assets/icons/icon-180.png">"""

_LOGIN_PAGE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>Đăng nhập — Thư viện truyện</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
{pwa_head}
<style>
  :root {{ --bg:#fafafa; --surface:#fff; --text:#1a1a1a; --muted:#6b7280; --border:#e5e7eb; --accent:#4f46e5; --accent-text:#fff; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#17181c; --surface:#1e1f24; --text:#e4e4e7; --muted:#9ca3af; --border:#2e2f36; --accent:#818cf8; --accent-text:#17181c; }}
  }}
  * {{ box-sizing: border-box; }}
  html {{ background:var(--bg); overscroll-behavior-y: none; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; background:var(--bg); color:var(--text); font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; overscroll-behavior-y: none; }}
  form {{ width: min(90vw, 320px); background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:2rem 1.5rem; }}
  h1 {{ font-size:1.2rem; margin:0 0 1.2rem; text-align:center; }}
  label {{ display:block; font-size:.85rem; color:var(--muted); margin-bottom:.3rem; }}
  input {{ width:100%; padding:.6rem .7rem; margin-bottom:1rem; border:1px solid var(--border); border-radius:8px; background:var(--bg); color:var(--text); font-size:1rem; }}
  button {{ width:100%; padding:.7rem; border:none; border-radius:8px; background:var(--accent); color:var(--accent-text); font-size:1rem; font-weight:600; cursor:pointer; }}
  .error {{ color:#ef4444; font-size:.85rem; margin:-0.6rem 0 1rem; }}
</style>
</head>
<body>
<form method="post" action="/login">
  <h1>Thư viện truyện</h1>
  {error_html}
  <label for="username">Tài khoản</label>
  <input type="text" id="username" name="username" autocomplete="username" required autofocus>
  <label for="password">Mật khẩu</label>
  <input type="password" id="password" name="password" autocomplete="current-password" required>
  <input type="hidden" name="next" value="{next_path}">
  <button type="submit">Đăng nhập</button>
</form>
</body>
</html>
"""


def _safe_next(path):
    # Only ever redirect back within this site — an unchecked `next` value
    # (e.g. "//evil.example.com") would otherwise turn the login form into
    # an open redirect.
    if path and path.startswith("/") and not path.startswith("//"):
        return path
    return "/"


def require_auth(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            if request.path.startswith("/api/"):
                return Response("Unauthorized", 401)
            return redirect(f"/login?next={request.path}")
        return view(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET"])
def login_form():
    if session.get("authed"):
        return redirect(_safe_next(request.args.get("next")))
    return _LOGIN_PAGE.format(pwa_head=_PWA_HEAD, error_html="", next_path=_safe_next(request.args.get("next")))


@app.route("/login", methods=["POST"])
def login_submit():
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    next_path = _safe_next(request.form.get("next"))
    if hmac.compare_digest(username, WEB_AUTH_USER) and hmac.compare_digest(password, WEB_AUTH_PASS):
        session.permanent = True
        session["authed"] = True
        return redirect(next_path)
    return _LOGIN_PAGE.format(
        pwa_head=_PWA_HEAD, error_html='<div class="error">Sai tài khoản hoặc mật khẩu</div>', next_path=next_path
    ), 401


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


_IMPORT_PAGE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>Thêm truyện — Thư viện truyện</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
{pwa_head}
<style>{css}</style>
</head>
<body>
<div class="topbar">
  <a class="back" href="/">‹ Trang chủ</a>
</div>
<div class="import-form">
  <h1>Thêm truyện mới</h1>
  {msg_html}
  <form method="post" action="/import" enctype="multipart/form-data">
    <label for="ebook">File ebook</label>
    <input type="file" id="ebook" name="ebook" accept=".epub,.prc,.mobi,.azw3" required>
    <div class="hint">Hỗ trợ .epub, .prc, .mobi, .azw3 — thể loại được đoán tự động từ tiêu đề/mô tả.</div>
    <button type="submit">Tải lên &amp; thêm vào thư viện</button>
  </form>
</div>
</body>
</html>
"""


@app.route("/import", methods=["GET"])
@require_auth
def import_form():
    return _IMPORT_PAGE.format(pwa_head=_PWA_HEAD, css=_CSS, msg_html="")


@app.route("/import", methods=["POST"])
@require_auth
def import_submit():
    f = request.files.get("ebook")
    if not f or not f.filename:
        return _IMPORT_PAGE.format(
            pwa_head=_PWA_HEAD, css=_CSS, msg_html='<div class="msg err">Chưa chọn file</div>'
        )

    ext = Path(f.filename).suffix.lower()
    if ext not in _ALLOWED_IMPORT_EXTENSIONS:
        return _IMPORT_PAGE.format(
            pwa_head=_PWA_HEAD, css=_CSS,
            msg_html=f'<div class="msg err">Định dạng {html.escape(ext)} không được hỗ trợ</div>',
        )

    site_dir = Path(SITE_DIR)
    manifest_path = site_dir / "books.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    used_ids = {b["id"] for b in manifest}

    # Extract into a local tmp path first (not the GCS mount) — the
    # extraction libraries do plenty of small intermediate file I/O
    # (mobi.extract() especially), which is slow and unnecessary to run
    # over FUSE when only the final rendered pages need to land there.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / f.filename
        f.save(tmp_path)
        try:
            entry = add_book_to_site(site_dir, tmp_path, used_ids)
        except Exception as e:
            return _IMPORT_PAGE.format(
                pwa_head=_PWA_HEAD, css=_CSS, msg_html=f'<div class="msg err">Lỗi: {html.escape(str(e))}</div>'
            )

    manifest.append(entry)
    write_index(site_dir, manifest)

    msg = (
        f'<div class="msg ok">Đã thêm "{html.escape(entry["title"])}" '
        f'({entry["n"]} chương, thể loại {html.escape(entry["category"])}) — '
        f'<a href="/books/{entry["id"]}/index.html">Xem ngay</a></div>'
    )
    return _IMPORT_PAGE.format(pwa_head=_PWA_HEAD, css=_CSS, msg_html=msg)


_DOWNLOAD_PAGE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>Tải xuống — Thư viện truyện</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
{pwa_head}
<style>{css}</style>
</head>
<body>
<div class="topbar">
  <a class="back" href="/">‹ Trang chủ</a>
  <span class="book-title">Đã tải xuống</span>
</div>
<div class="wrap" id="download-app">
  <p class="dl-hint">Đang tải danh sách…</p>
</div>
<div class="wrap piper-offline-section">
  <h2 class="piper-offline-heading">🔊 Giọng đọc offline (Piper)</h2>
  <div class="piper-offline-card">
    <span id="piper-offline-status" class="piper-offline-status">Đang kiểm tra…</span>
    <button type="button" id="piper-offline-delete" class="dl-btn-large" hidden>Xoá model Piper offline</button>
  </div>
</div>
<script src="/assets/piper-offline.js"></script>
<script src="/assets/download.js"></script>
</body>
</html>
"""


@app.route("/download.html")
@require_auth
def download_page():
    return _DOWNLOAD_PAGE.format(pwa_head=_PWA_HEAD, css=_CSS)


def _progress_path(site_dir):
    return Path(site_dir) / "progress.json"


def _load_progress(site_dir):
    path = _progress_path(site_dir)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save_progress(site_dir, data):
    _progress_path(site_dir).write_text(json.dumps(data), encoding="utf-8")


@app.route("/api/progress", methods=["GET"])
@require_auth
def progress_list():
    return _load_progress(SITE_DIR)


@app.route("/api/progress", methods=["POST"])
@require_auth
def progress_update():
    body = request.get_json(force=True, silent=True) or {}
    book_id = body.get("book_id")
    chapter = body.get("chapter")
    sentence = body.get("sentence")
    if not isinstance(book_id, int) or not isinstance(chapter, int) or not isinstance(sentence, int):
        abort(400)

    data = _load_progress(SITE_DIR)
    # Server stamps its own clock rather than trusting the client's — every
    # timestamp compared during the app's last-write-wins merge (deciding
    # which device's progress is newer) is then issued by the same clock.
    record = {
        "chapter": chapter,
        "sentence": sentence,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    data[str(book_id)] = record
    _save_progress(SITE_DIR, data)
    return record


def _bug_reports_dir(site_dir):
    d = Path(site_dir) / "bug_reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


# iOS app's "Báo lỗi" (report bug) feature — sends a free-text description
# (optional — the description can be left blank when the attached log is
# the point of the report) plus (optionally) the app's own recent
# action/error timeline (EventLogStore, tts-novel-ios repo; it's the only
# logging the app does, so this already covers everything, not just user
# actions) so a report already comes with the context that led up to it.
# Each submission is its own timestamped file rather than one growing list
# (unlike progress.json) — reports are append-only and read individually,
# not merged/queried, so there's no need to load/rewrite the whole set on
# every submission.
@app.route("/api/bug-report", methods=["POST"])
@require_auth
def bug_report_submit():
    body = request.get_json(force=True, silent=True) or {}
    description = (body.get("description") or "").strip()
    events = body.get("events")
    events = events[-2000:] if isinstance(events, list) else []
    # Reject only a genuinely empty submission (no text AND no log) — the
    # client already lets the description be blank on its own.
    if not description and not events:
        abort(400)

    record = {
        "description": description,
        "device": body.get("device", ""),
        "os_version": body.get("os_version", ""),
        "app_version": body.get("app_version", ""),
        # Capped defensively server-side too, independent of whatever cap
        # the client already applies before sending.
        "events": events,
        "submitted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    filename = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.json"
    (_bug_reports_dir(SITE_DIR) / filename).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"ok": True}


@app.route("/api/tts", methods=["POST"])
@require_auth
def tts_proxy():
    body = request.get_json(force=True, silent=True) or {}
    text = body.get("text", "")
    if not text:
        abort(400)
    payload = {
        "text": text,
        "speed": body.get("speed", 1.0),
        "model": body.get("model") or DEFAULT_TTS_MODEL,
    }
    upstream = requests.post(
        f"{TTS_API_URL}/tts/generate",
        json=payload,
        headers={"x-tts-secret": TTS_API_SECRET},
        timeout=60,
    )
    return Response(
        upstream.content, status=upstream.status_code, content_type=upstream.headers.get("Content-Type", "audio/wav")
    )


@app.route("/")
@require_auth
def index():
    return send_from_directory(SITE_DIR, "index.html")


# App-shell pattern: every chapter URL serves this one static page instead
# of a physical per-chapter file — reader.js reads the URL itself client-
# side and fetches the matching data fragment from books/<id>/data/
# <chapter>.html. Keeps changing the reader UI a single-file edit instead
# of rewriting one of ~72,000 near-identical pages. Werkzeug prefers this
# route over the generic catch-all below for matching URLs regardless of
# definition order (literal segments rank higher than a <path:...> catch-
# all), but it's placed first for readability anyway.
@app.route("/books/<int:book_id>/chapters/<chapter>.html")
@require_auth
def chapter_shell(book_id, chapter):
    return send_from_directory(SITE_DIR, "chapter-shell.html")


# manifest.json / sw.js / icons must be fetchable without a session — iOS
# "Add to Home Screen" and Android's install-prompt machinery read these
# regardless of whether the browser currently has a valid login cookie.
# They're non-sensitive static assets, so that's fine to allow.
_PUBLIC_ASSET_PATHS = {"manifest.json", "sw.js"}

# A book's cover.<ext> is immutable once written (build_library.py re-encodes
# it once at import time and never touches that path again) — safe to let
# the browser skip revalidating it entirely instead of round-tripping an
# ETag check on every repeat view. That round trip was measured live eating
# a gunicorn worker/thread it didn't need to: a Home page with dozens of
# already-seen covers fires them all at once, and each one waiting on a
# free thread just to get told "304, unchanged" was crowding out the
# requests that actually needed a worker (see sw.js's ONLINE_FETCH_TIMEOUT_MS
# — a cover stuck behind that queue too long gets aborted client-side and
# shows up broken, even though the network and server were both fine).
_COVER_PATH_RE = re.compile(r"^books/\d+/cover\.[A-Za-z0-9]+$")


def _is_public_reading_path(path):
    # Book catalog + covers + chapter content + per-book titles — the exact
    # set the iOS app's APIClient reads (fetchBooks/fetchCoverData/
    # fetchChapter/fetchChapterTitles). Deliberately public since the app's
    # "chế độ khách" (guest mode) browses/reads without logging in first;
    # everything else under this catch-all (and every other route in this
    # file — /import, /api/tts, /api/progress, /api/bug-report) still
    # requires the shared login, so a guest can read but can't burn TTS
    # quota, push progress, or add books.
    return path == "books.json" or path.startswith("books/")


@app.route("/<path:path>")
def static_files(path):
    is_public = (
        path in _PUBLIC_ASSET_PATHS or path.startswith("assets/icons/") or _is_public_reading_path(path)
    )
    if not is_public and not session.get("authed"):
        return redirect(f"/login?next={request.path}")
    # sw.js and the app's own JS get no browser HTTP cache at all (not just
    # a short max_age) — without this, Flask leaves Cache-Control unset and
    # browsers fall back to their own heuristic caching, which has been
    # letting phones keep serving an old cached reader.js/sw.js well after
    # a new one was deployed (several "still buggy after fix" reports
    # traced back to this, not the fix itself). max_age=0 forces a
    # conditional revalidation (ETag/Last-Modified) on every request
    # instead of trusting a local copy blindly.
    max_age = 0 if path == "sw.js" or path.endswith(".js") else None
    resp = send_from_directory(SITE_DIR, path, max_age=max_age)
    if _COVER_PATH_RE.match(path):
        # Flask's own no-max_age default (max_age=None above) stamps
        # Cache-Control: no-cache — has to be cleared explicitly, or it
        # forces revalidation on every request regardless of max-age/
        # immutable below, defeating the whole point of this branch.
        resp.cache_control.no_cache = False
        resp.cache_control.max_age = 31536000
        resp.cache_control.public = True
        resp.cache_control.immutable = True
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
