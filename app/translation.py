"""Google Cloud Translation (v3) with a shared glossary, plus a per-chapter
translation store on the site bucket so a sentence is only ever paid for
once — both the web reader and the iOS/macOS app go through /api/translate
(server.py) and share whatever the other already translated.

Store layout (under SITE_DIR, i.e. the GCS bucket mounted into Cloud Run):

  translations/glossary.json                          the glossary (edited via /api/glossary)
  translations/glossaries/<glossary id>.tsv           what's published to Google
  translations/<book_id>/<src>-<tgt>/<chapter>.json   translated segments

Glossary: one list of terms that applies to every book, plus optional terms
of a single book (e.g. "class" → "chức nghiệp" is right for a LitRPG, wrong
elsewhere). Google takes exactly one glossary per translate call, so what's
published is, per source language, a global glossary with the shared terms
and — for each book that has terms of its own — a book glossary with the
shared terms + the book's (the book's win). A book uses its own glossary
if it has one, else the global one. A term's source language is inferred
from its text (CJK → zh, otherwise en); the target is always Vietnamese.
Google only matches a glossary term exactly, so each English term is
published along with its other spellings (see term_variants) — "Dao Seed"
also covers "Dao Seeds", "class" also "Class"/"classes" — rather than
making the user enter every form.

A chapter file maps sha256(source segment) -> {"t": translation, "g":
glossary revision}. Keyed by the segment's own text rather than its
position, so it doesn't matter how a client splits a chapter (the app sends
TTS-sized sentences, the web reader sends whole paragraphs) or in what
order/chunks it asks — every request just fills in whatever's missing, and
a chapter can be partially translated for as long as nobody reads the rest.
An entry whose "g" doesn't match the current glossary revision (the Google
glossary's submitTime, new on every publish) is treated as missing, so
editing the glossary re-translates lazily on next read instead of needing a
cache purge.
"""
import hashlib
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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

# How long an instance trusts its last look at a glossary's revision. Kept
# short because a save on one Cloud Run instance only clears that
# instance's cache — others keep serving segments stored under the old
# glossary until this runs out.
_GLOSSARY_TTL_SECONDS = 30

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


GLOSSARY_TARGET = "vi"
MAX_GLOSSARY_TERMS = 5000
MAX_TERM_CHARS = 200
_CJK = re.compile(r"[㐀-鿿豈-﫿]")


def glossary_id(book_id, source, target=GLOSSARY_TARGET):
    """Google glossary id — a book's own, or the global one (book_id None)."""
    scope = "global" if book_id is None else f"book-{book_id}"
    return f"novel-{scope}-{source}-{target}".lower()


def term_language(text):
    return "zh" if _CJK.search(text) else "en"


_WORD_END = re.compile(r"[A-Za-z]+$")


def _other_number(word):
    """`word`'s plural, or its singular if it already looks plural."""
    lower = word.lower()
    if lower.endswith(("ss", "us", "is")):  # Class, Status, Axis: singular
        return word + "es"
    if lower.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if lower.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if lower.endswith("s"):
        return word[:-1]
    if lower.endswith(("ch", "sh", "x", "z")):
        return word + "es"
    if lower.endswith("y") and len(word) > 1 and lower[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"  # also for an acronym: "HP" → "HPs"


def term_variants(source):
    """The other spellings an English term also applies to: singular/plural
    of its last word ("Dao Seed" ↔ "Dao Seeds", "Ability" ↔ "Abilities")
    and, for each, the first letter capitalized ("class" → "Class", for the
    start of a sentence). Not the other way round — matching stays
    case-sensitive, "Strength" mustn't catch the ordinary word "strength" —
    and not all-caps, which prose rarely uses. CJK terms have neither."""
    if term_language(source) != "en":
        return []
    match = _WORD_END.search(source)
    forms = [source]
    if match and len(match.group()) > 1:
        forms.append(source[:match.start()] + _other_number(match.group()))
    forms += [f[0].upper() + f[1:] for f in forms if f[0].islower()]
    return [f for f in dict.fromkeys(forms) if f != source]


def _with_variants(entries):
    """`entries` plus every term's variants, translated the same — except a
    keep-original term's, which keep their own spelling ("Classes" stays
    "Classes", not "class"). A term entered as such wins over another's
    variant."""
    expanded = {}
    for source, target in sorted(entries.items()):
        for variant in term_variants(source):
            expanded.setdefault(variant, variant if target == source else target)
    return {**expanded, **entries}


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


def _published_revision(gid):
    """A Google glossary's submitTime (new on every publish), or None if it
    doesn't exist. Cached briefly — looked up on every /api/translate call;
    publish() clears the cache on the instance that published."""
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


def resolve_glossary(book_id, source, target):
    """(glossary id, revision) to translate `book_id` with — its own
    glossary if it has one, else the global one — or (None, None)."""
    if target != GLOSSARY_TARGET:
        return None, None
    for gid in (glossary_id(book_id, source), glossary_id(None, source)):
        revision = _published_revision(gid)
        if revision:
            return gid, revision
    return None, None


# ── Glossary storage + publishing ───────────────────────────────────────────


def _glossary_path(site_dir):
    return Path(site_dir) / "translations" / "glossary.json"


def load_glossary(site_dir):
    path = _glossary_path(site_dir)
    if not path.exists():
        return {"terms": [], "updated_at": "1970-01-01T00:00:00.000Z", "published": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _clean_term_text(value):
    if not isinstance(value, str):
        raise ValueError("term text must be a string")
    text = re.sub(r"\s+", " ", value).strip()
    if not text or len(text) > MAX_TERM_CHARS:
        raise ValueError(f"term text must be 1–{MAX_TERM_CHARS} characters")
    return text


def normalize_terms(raw_terms):
    """Validated copy of a client-submitted term list: trimmed text, an id
    on every term, and one term per (book, source text) — the last wins. A
    blank target means "keep as is" (e.g. "class" stays "class"), stored as
    the source text itself, which is how Google's glossary expresses it."""
    if not isinstance(raw_terms, list) or len(raw_terms) > MAX_GLOSSARY_TERMS:
        raise ValueError("terms must be a list of at most %d items" % MAX_GLOSSARY_TERMS)
    by_key = {}
    for raw in raw_terms:
        if not isinstance(raw, dict):
            raise ValueError("each term must be an object")
        book_id = raw.get("book_id")
        if book_id is not None and (not isinstance(book_id, int) or isinstance(book_id, bool)):
            raise ValueError("book_id must be an integer or null")
        source = _clean_term_text(raw.get("source"))
        target = raw.get("target")
        term = {
            "id": raw.get("id") if isinstance(raw.get("id"), str) and raw.get("id") else str(uuid.uuid4()),
            "source": source,
            "target": source if target is None or (isinstance(target, str) and not target.strip()) else _clean_term_text(target),
            "book_id": book_id,
        }
        by_key.pop((book_id, term["source"]), None)  # re-insert so order follows the last occurrence
        by_key[(book_id, term["source"])] = term
    return list(by_key.values())


def glossary_contents(terms):
    """{glossary id: TSV text} that should be published for `terms`."""
    shared = {}  # lang -> {source: target}
    per_book = {}  # (book_id, lang) -> {source: target}
    for term in terms:
        lang = term_language(term["source"])
        if term["book_id"] is None:
            shared.setdefault(lang, {})[term["source"]] = term["target"]
        else:
            per_book.setdefault((term["book_id"], lang), {})[term["source"]] = term["target"]

    def tsv(entries):
        return "".join(f"{s}\t{t}\n" for s, t in sorted(entries.items()))

    contents = {glossary_id(None, lang): tsv(_with_variants(entries)) for lang, entries in shared.items()}
    for (book_id, lang), entries in per_book.items():
        contents[glossary_id(book_id, lang)] = tsv({**_with_variants(shared.get(lang, {})), **_with_variants(entries)})
    return contents


def _content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _wait_for(op_name, headers, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        op = requests.get(f"{_API}/{op_name}", headers=headers, timeout=30).json()
        if op.get("done"):
            if "error" in op:
                raise TranslationError(f"{op_name} failed: {op['error']}")
            return
        time.sleep(1)
    raise TranslationError(f"{op_name} timed out")


class GoogleGlossaries:
    """The handful of Glossary API calls publish() needs."""

    def __init__(self, site_dir):
        self.site_dir = site_dir

    def list_ids(self):
        headers, project = _headers()
        ids, page = [], None
        while True:
            params = {"pageSize": 1000, **({"pageToken": page} if page else {})}
            resp = requests.get(f"{_API}/{_parent(project)}/glossaries", headers=headers, params=params, timeout=30)
            if not resp.ok:
                raise TranslationError(f"list glossaries failed: {resp.status_code} {resp.text[:200]}")
            data = resp.json()
            ids += [g["name"].rsplit("/", 1)[-1] for g in data.get("glossaries", [])]
            page = data.get("nextPageToken")
            if not page:
                return [i for i in ids if i.startswith("novel-")]

    def delete(self, gid):
        headers, project = _headers()
        resp = requests.delete(f"{_API}/{_parent(project)}/glossaries/{gid}", headers=headers, timeout=30)
        if resp.status_code == 404:
            return
        if not resp.ok:
            raise TranslationError(f"delete glossary {gid} failed: {resp.status_code} {resp.text[:200]}")
        _wait_for(resp.json()["name"], headers)

    def create(self, gid, tsv_text):
        headers, project = _headers()
        # Written through the bucket mount (SITE_DIR), which is where Google
        # reads glossary input from.
        rel = f"translations/glossaries/{gid}.tsv"
        path = Path(self.site_dir) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tsv_text, encoding="utf-8")
        bucket = os.environ.get("GLOSSARY_BUCKET") or f"{project}-novel-web-site"
        source = gid.split("-")[-2]
        body = {
            "name": f"{_parent(project)}/glossaries/{gid}",
            "languagePair": {"sourceLanguageCode": source, "targetLanguageCode": GLOSSARY_TARGET},
            "inputConfig": {"gcsSource": {"inputUri": f"gs://{bucket}/{rel}"}},
        }
        resp = requests.post(f"{_API}/{_parent(project)}/glossaries", json=body, headers=headers, timeout=30)
        if not resp.ok:
            raise TranslationError(f"create glossary {gid} failed: {resp.status_code} {resp.text[:200]}")
        _wait_for(resp.json()["name"], headers)


_publish_lock = threading.Lock()


def save_glossary(site_dir, raw_terms, google=None):
    """Stores the edited term list and publishes it to Google. Returns the
    stored glossary plus {"updated": [...], "deleted": [...]} glossary ids.
    Raises ValueError for an invalid list (nothing stored) and
    TranslationError if publishing failed (the list itself is stored; the
    next save retries publishing whatever's still out of date)."""
    terms = normalize_terms(raw_terms)
    with _publish_lock:
        data = load_glossary(site_dir)
        data["terms"] = terms
        data["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        path = _glossary_path(site_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            changes = _publish(site_dir, data, google or GoogleGlossaries(site_dir))
        finally:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data, changes


def _publish(site_dir, data, google):
    desired = glossary_contents(data["terms"])
    published = data.setdefault("published", {})
    existing = set(google.list_ids())
    to_update = [gid for gid, text in desired.items() if gid not in existing or published.get(gid) != _content_hash(text)]
    to_delete = sorted(existing - set(desired))

    def update(gid):
        if gid in existing:
            google.delete(gid)
        google.create(gid, desired[gid])
        return gid

    errors = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(update, gid) for gid in to_update] + [pool.submit(google.delete, gid) for gid in to_delete]
        for future in futures:
            try:
                future.result()
            except TranslationError as e:
                errors.append(str(e))
    for gid in to_update:
        published[gid] = _content_hash(desired[gid])
    for gid in to_delete:
        published.pop(gid, None)
    _glossary_cache.clear()
    if errors:
        # Forget hashes so the next save retries everything that may be stale.
        data["published"] = {}
        raise TranslationError("; ".join(errors))
    return {"updated": sorted(to_update), "deleted": to_delete}


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


def translate_segments(site_dir, book_id, chapter, texts, source, target, translate_fn=None, glossary_fn=None):
    """Translations for `texts` (aligned), serving whatever's already stored
    for this chapter and translating + storing only the rest. Returns
    (translations, {"cached": n, "translated": n, "glossary": revision})."""
    translate_fn = translate_fn or google_translate
    glossary_fn = glossary_fn or resolve_glossary
    glossary, revision = glossary_fn(book_id, source, target)
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
        try:
            translated = translate_fn([missing[k] for k in keys], source, target, glossary)
        except TranslationError:
            if not glossary:
                raise
            # The glossary may be mid-republish (deleted, not yet re-created)
            # — translate without it rather than fail. Stored with no
            # revision, so it's redone with the glossary on a later read.
            glossary, revision, rev_tag = None, None, ""
            translated = translate_fn([missing[k] for k in keys], source, target, None)
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
