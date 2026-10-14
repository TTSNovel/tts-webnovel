"""Google Cloud Translation (v3) with per-book glossaries, plus a per-chapter
translation store on the site bucket so a sentence is only ever paid for
once — both the web reader and the iOS/macOS app go through /api/translate
(server.py) and share whatever the other already translated.

Store layout (under SITE_DIR, i.e. the GCS bucket mounted into Cloud Run):

  translations/glossaries/<book_id>.<src>-<tgt>.tsv   glossary source of truth
  translations/<book_id>/<src>-<tgt>/<chapter>.json   translated segments

A chapter file maps sha256(source segment) -> {"t": translation, "g":
glossary revision}. Keyed by the segment's own text rather than its
position, so it doesn't matter how a client splits a chapter (the app sends
TTS-sized sentences, the web reader sends whole paragraphs) or in what
order/chunks it asks — every request just fills in whatever's missing, and
a chapter can be partially translated for as long as nobody reads the rest.
An entry whose "g" doesn't match the book's current glossary revision is
treated as missing, so re-pushing a glossary (tools/glossary.py push)
re-translates lazily on next read instead of needing a cache purge.
"""
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

GLOSSARY_LOCATION = "us-central1"  # v3 glossaries only work against a regional endpoint
_API = "https://translation.googleapis.com/v3"
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# translateText's documented per-request ceilings (1024 items, 30k code points
# recommended) — callers are held to well under both.
MAX_SEGMENTS_PER_REQUEST = 200
MAX_CHARS_PER_REQUEST = 25000

_GLOSSARY_TTL_SECONDS = 300

# Google's output sometimes has a stray space just inside quotes/brackets —
# after an opener directly preceding a glossary term ("“ Thiên Đạo",
# "[ Nút Nexus") or between closing punctuation and a closing quote
# ("“Menu, ” Zac") or before punctuation right after a glossary term
# ("Nhanh Nhẹn ."). Never wanted; the source never has one there. Applied
# when serving (not only when storing) so already-stored entries get it too.
_SPACE_AFTER_OPENER = re.compile(r"([\[(“‘«]) +")
_SPACE_BEFORE_CLOSER = re.compile(r"([,.!?…]) +([”’»])")
_SPACE_BEFORE_PUNCT = re.compile(r"(\w) +([,.!?;:…])")


def _tidy(text):
    text = _SPACE_AFTER_OPENER.sub(r"\1", text)
    text = _SPACE_BEFORE_CLOSER.sub(r"\1\2", text)
    return _SPACE_BEFORE_PUNCT.sub(r"\1\2", text)

_lock = threading.Lock()
_credentials = None
_project = None
_glossary_cache = {}  # glossary id -> (fetched_at, revision or None)


class TranslationError(Exception):
    pass


def glossary_id(book_id, source, target):
    return f"novel-book-{book_id}-{source}-{target}".lower()


def segment_key(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _auth():
    """Application Default Credentials — the Cloud Run service account in
    production, `gcloud auth application-default login` locally."""
    global _credentials, _project
    import google.auth
    import google.auth.transport.requests

    with _lock:
        if _credentials is None:
            _credentials, _project = google.auth.default(scopes=_SCOPES)
        if not _credentials.valid:
            _credentials.refresh(google.auth.transport.requests.Request())
        return _credentials.token, _project


def _headers():
    token, project = _auth()
    # x-goog-user-project bills the call to this project when running on a
    # developer's user credentials (no-op for a service account).
    return {"Authorization": f"Bearer {token}", "x-goog-user-project": project}, project


def _parent(project):
    return f"projects/{project}/locations/{GLOSSARY_LOCATION}"


def glossary_revision(book_id, source, target):
    """The book's glossary revision — the Google glossary's submitTime,
    which changes every time tools/glossary.py re-creates it — or None if
    the book has no glossary. Cached briefly — this is checked on every
    /api/translate call."""
    gid = glossary_id(book_id, source, target)
    cached = _glossary_cache.get(gid)
    if cached and time.time() - cached[0] < _GLOSSARY_TTL_SECONDS:
        return cached[1]
    headers, project = _headers()
    resp = requests.get(f"{_API}/{_parent(project)}/glossaries/{gid}", headers=headers, timeout=15)
    if resp.status_code == 404:
        revision = None
    elif resp.ok:
        revision = resp.json().get("submitTime") or "unversioned"
    else:
        raise TranslationError(f"glossary lookup failed: {resp.status_code} {resp.text[:200]}")
    _glossary_cache[gid] = (time.time(), revision)
    return revision


def google_translate(texts, source, target, glossary=None):
    """One translateText call. Returns translations aligned with `texts`,
    glossary-applied when `glossary` (a glossary id) is given."""
    headers, project = _headers()
    body = {
        "contents": texts,
        "sourceLanguageCode": source,
        "targetLanguageCode": target,
        "mimeType": "text/plain",
    }
    if glossary:
        # Case-sensitive on purpose: the glossary's terms are the capitalized
        # system/cultivation names ("Strength", "Dao"), which also occur as
        # ordinary lowercase words in prose ("strength") that should stay
        # ordinary translations.
        body["glossaryConfig"] = {"glossary": f"{_parent(project)}/glossaries/{glossary}", "ignoreCase": False}
    resp = requests.post(f"{_API}/{_parent(project)}:translateText", json=body, headers=headers, timeout=60)
    if not resp.ok:
        raise TranslationError(f"translateText failed: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    items = data.get("glossaryTranslations") if glossary else data.get("translations")
    if not items or len(items) != len(texts):
        raise TranslationError("translateText returned an unexpected number of translations")
    return [item.get("translatedText", "") for item in items]


def _chapter_path(site_dir, book_id, chapter, source, target):
    return Path(site_dir) / "translations" / str(book_id) / f"{source}-{target}" / f"{chapter:04d}.json"


def _load_chapter(path):
    if not path.exists():
        return {"entries": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"entries": {}}


def translate_segments(site_dir, book_id, chapter, texts, source, target, translate_fn=None, revision_fn=None):
    """Translations for `texts` (aligned), serving whatever's already stored
    for this chapter and translating + storing only the rest. Returns
    (translations, {"cached": n, "translated": n, "glossary": revision})."""
    translate_fn = translate_fn or google_translate
    revision_fn = revision_fn or glossary_revision
    revision = revision_fn(book_id, source, target)
    glossary = glossary_id(book_id, source, target) if revision else None
    rev_tag = revision or ""

    path = _chapter_path(site_dir, book_id, chapter, source, target)
    stored = _load_chapter(path)
    entries = stored.get("entries", {})

    results = [None] * len(texts)
    missing = {}  # key -> source text (dedupes repeats within one request)
    for i, text in enumerate(texts):
        if not text.strip():
            results[i] = text
            continue
        entry = entries.get(segment_key(text))
        if entry and entry.get("g", "") == rev_tag:
            results[i] = entry["t"]
        else:
            missing.setdefault(segment_key(text), text)

    if missing:
        keys = list(missing)
        translated = translate_fn([missing[k] for k in keys], source, target, glossary)
        # Re-read right before writing: another request (e.g. the next chunk
        # of the same chapter) may have stored entries since we loaded.
        stored = _load_chapter(path)
        entries = stored.setdefault("entries", {})
        for key, text in zip(keys, translated):
            entries[key] = {"t": text, "g": rev_tag}
        stored.update(
            {
                "book_id": book_id,
                "chapter": chapter,
                "source": source,
                "target": target,
                "engine": "google-v3-nmt",
                "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        by_key = dict(zip(keys, translated))
        for i, text in enumerate(texts):
            if results[i] is None:
                results[i] = by_key[segment_key(text)]

    cached = sum(1 for t in texts if t.strip()) - len(missing)
    return [_tidy(r) for r in results], {"cached": cached, "translated": len(missing), "glossary": revision}


def list_stored(site_dir, book_id):
    """Per-chapter summary of what's stored for a book."""
    root = Path(site_dir) / "translations" / str(book_id)
    chapters = []
    if root.is_dir():
        for pair_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for f in sorted(pair_dir.glob("*.json")):
                data = _load_chapter(f)
                chapters.append(
                    {
                        "pair": pair_dir.name,
                        "chapter": int(f.stem),
                        "segments": len(data.get("entries", {})),
                        "bytes": f.stat().st_size,
                        "updated_at": data.get("updated_at"),
                    }
                )
    return chapters


def delete_stored(site_dir, book_id, chapter=None):
    """Removes a book's stored translations (or one chapter's, every
    language pair). Returns how many chapter files were deleted."""
    root = Path(site_dir) / "translations" / str(book_id)
    if not root.is_dir():
        return 0
    pattern = f"*/{chapter:04d}.json" if chapter is not None else "*/*.json"
    removed = 0
    for f in root.glob(pattern):
        f.unlink()
        removed += 1
    return removed
