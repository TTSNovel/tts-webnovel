"""Build a multi-book static reading site, grouped by category.

Layout (app-shell pattern — see book_renderer.py's _CHAPTER_SHELL docstring
for why chapter pages are split this way):
  site/index.html                       — home, books grouped by category
  site/books.json                       — manifest: [{id, title, author, category, n}]
  site/chapter-shell.html               — the ONE page served for every chapter URL
  site/books/<id>/index.html            — one book: chapter list
  site/books/<id>/meta.json             — {title, author, category, n} for reader.js
  site/books/<id>/data/NNNN.html        — chapter content fragment (no page chrome)
  site/assets/reader.js                 — shared read-aloud script (site_assets/reader.js)

Each book's <id> is a sequential integer, assigned in the order books are
added (bulk build or live /import) — never derived from the title, so a
book's URL never changes if it gets re-titled later.

Nothing lives at books/<id>/chapters/NNNN.html on disk — server.py routes
that URL pattern straight to chapter-shell.html instead, so pretty
per-chapter URLs still work without one physical file each.

books.json lets the live /import endpoint (server.py) add a new book
without having to re-scan every existing book's HTML to rebuild the home
page — it's the same rendering path (book_renderer.py) used here.

Usage: python3 app/build_library.py <epub_dir_or_file> [<epub_dir_or_file> ...] [-o site]
"""
import io
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from PIL import Image

from book_renderer import (
    render_book_meta,
    render_book_page,
    render_chapter_fragment,
    render_chapter_shell,
    render_chapter_titles,
    render_index_page,
)
from classify import classify
from epub_parser import extract_book, extract_book_from_mobi, extract_cover

_LEGACY_EXTENSIONS = {".prc", ".mobi", ".azw3"}
_JUNK_TITLE_MARKERS = ("created with", "written by", "gettextfromhtml")

# Covers embedded in epubs are sized for full-screen e-reader display
# (routinely 1000px+ on a side, hundreds of KB to ~1MB) — measured live on
# the deployed home page: 10 book-grid thumbnails alone totaled 2.25MB,
# with individual covers up to 809KB, for images shown at ~140px wide.
# Re-encoding to a bounded size cuts that by an order of magnitude with no
# visible quality loss at thumbnail size — this is the single biggest lever
# on this site's page-load weight, well ahead of anything server/network-side.
_COVER_MAX_DIM = 480
_COVER_JPEG_QUALITY = 82

# A handful of source epubs put a generic "image unavailable" icon (seen at
# 48x48) in the manifest's cover-image slot instead of real artwork — below
# this we treat it as no cover at all rather than rendering a broken-icon
# thumbnail on the book grid.
_COVER_MIN_DIM = 100


class _CoverTooSmall(Exception):
    pass


# site_dir is a Cloud Run GCS volume mount (Cloud Storage FUSE) in
# production, not a real disk — its directory creation isn't immediately
# durable. Writing the first file into a directory right after mkdir()-ing
# it can race and fail with FileNotFoundError even though mkdir() itself
# already returned success (observed directly: the placeholder object for a
# freshly mkdir'd book dir showed a GCS creation timestamp ~90s *after* the
# request that created it had already failed and returned to the browser).
# Re-issuing the write (re-creating the parent dir first, since that's the
# thing still catching up) clears it without adding real latency in the
# overwhelmingly common case where no race happens.
_WRITE_RETRY_ATTEMPTS = 6
_WRITE_RETRY_BASE_DELAY = 0.5


def _write_retrying(path: Path, write_fn) -> None:
    for attempt in range(_WRITE_RETRY_ATTEMPTS):
        try:
            write_fn(path)
            return
        except FileNotFoundError:
            if attempt == _WRITE_RETRY_ATTEMPTS - 1:
                raise
            path.parent.mkdir(parents=True, exist_ok=True)
            time.sleep(_WRITE_RETRY_BASE_DELAY * (attempt + 1))


def _write_text_retrying(path: Path, content: str) -> None:
    _write_retrying(path, lambda p: p.write_text(content, encoding="utf-8"))


def _write_bytes_retrying(path: Path, content: bytes) -> None:
    _write_retrying(path, lambda p: p.write_bytes(content))


def _resize_cover(cover_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(cover_bytes)) as img:
        if max(img.size) < _COVER_MIN_DIM:
            raise _CoverTooSmall(f"{img.size[0]}x{img.size[1]} looks like a placeholder icon, not a cover")
        img = img.convert("RGB")  # drop alpha/palette — JPEG has neither, and covers never need transparency
        if max(img.size) > _COVER_MAX_DIM:
            img.thumbnail((_COVER_MAX_DIM, _COVER_MAX_DIM), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_COVER_JPEG_QUALITY, optimize=True)
        return buf.getvalue()


def _looks_like_real_title(title: str | None) -> bool:
    """Reject epub DC:title values seen in practice from scraped/converted
    books that are actually garbage: raw hex hashes, literal 'index', or a
    long dump of tags + converter-tool credits standing in for a title."""
    if not title:
        return False
    stripped = title.strip()
    if not stripped or stripped.lower() in {"index", "untitled", "cover", "book"}:
        return False
    if re.fullmatch(r"[0-9a-f]{20,}", stripped, re.IGNORECASE):
        return False
    if len(stripped) > 80:
        return False
    if any(marker in stripped.lower() for marker in _JUNK_TITLE_MARKERS):
        return False
    return True


_TRAILING_TAG_RE = re.compile(r"\s*\[[^\[\]]{1,12}\]\s*$")


def _strip_title_tags(title: str) -> str:
    """Some source epubs bake a bracketed status/source tag onto the end of
    the title itself (seen in practice: "[C]" = complete, "[AI]" = AI-
    translated) — strip it so the same book doesn't show a different
    display title than an already-catalogued copy imported without the
    tag, or than a later copy imported with a differently-spelled one."""
    stripped = title
    while True:
        without_tag = _TRAILING_TAG_RE.sub("", stripped)
        if without_tag == stripped:
            return stripped
        stripped = without_tag


_GENRE_HINT_RE = re.compile(r"(?:th[eể]\s*lo[aạ]i|t[uừ]\s*kh[oó]a)\s*[:\-]?\s*([^\n]{1,100})", re.IGNORECASE)


def _genre_hint(chapters) -> str:
    """Pull a "Thể loại: ..." / "Từ khóa: ..." genre line out of the book's
    own intro text. epub DC:description is almost always empty for these
    scraped/converted sources, but a lot of them put the genre tags the
    source site scraped (e.g. "Thể loại: Mạt Thế, Khoa Huyễn") in plain
    text on the first page or two instead. Deliberately narrow — matching
    just that one labelled line instead of scanning the whole opening
    avoids false hits from ordinary narrative prose (e.g. a historical
    scene describing a dynasty's "diệt vong" isn't an apocalypse tag)."""
    for ch in chapters[:2]:
        match = _GENRE_HINT_RE.search(ch.text)
        if match:
            return match.group(1)
    return ""


def _iter_book_paths(inputs: list[str]) -> list[Path]:
    exts = {".epub"} | _LEGACY_EXTENSIONS
    paths = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            for ext in sorted(exts):
                paths.extend(sorted(p.glob(f"*{ext}")))
        elif p.suffix.lower() in exts:
            paths.append(p)
    return paths


def add_book_to_site(site_dir: Path, epub_path: Path, used_ids: set[int]) -> dict:
    """Extract + render a single book into site_dir/books/<id>/, returning
    its books.json entry. Shared by the bulk CLI builder and the live
    /import endpoint — the only difference is the caller's used_ids set
    (bulk building starts empty; live import loads it from books.json)."""
    if epub_path.suffix.lower() in _LEGACY_EXTENSIONS:
        metadata, chapters = extract_book_from_mobi(str(epub_path))
    else:
        metadata, chapters = extract_book(str(epub_path))
    if not chapters:
        raise ValueError("0 chapters extracted")

    book_title = metadata["title"] if _looks_like_real_title(metadata["title"]) else epub_path.stem
    book_title = _strip_title_tags(book_title)
    author = metadata["author"]
    category = classify(book_title, metadata.get("description", "") + " " + _genre_hint(chapters))

    book_id = max(used_ids, default=0) + 1
    used_ids.add(book_id)

    book_dir = site_dir / "books" / str(book_id)
    data_dir = book_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # If anything below fails, book_id never makes it into books.json — so
    # a half-written book_dir here would just sit there as dead debris that
    # the next import silently writes back into (same id, reused via
    # mkdir(exist_ok=True)). Wipe it on failure so a retry starts clean.
    try:
        cover_name = None
        if epub_path.suffix.lower() not in _LEGACY_EXTENSIONS:
            cover = extract_cover(str(epub_path))
            if cover is not None:
                cover_bytes, ext = cover
                try:
                    cover_bytes = _resize_cover(cover_bytes)
                    cover_name = "cover.jpg"
                except _CoverTooSmall as e:
                    print(f"  WARN: skipping cover ({e})")
                except Exception as e:
                    # Some embedded cover images are corrupt/in a format PIL
                    # can't decode — fall back to the original bytes as-is
                    # rather than losing the cover entirely over this.
                    print(f"  WARN: cover resize failed ({e}), using original")
                    cover_name = f"cover.{ext}"
                    _write_bytes_retrying(book_dir / cover_name, cover_bytes)
                else:
                    _write_bytes_retrying(book_dir / cover_name, cover_bytes)

        for ch in chapters:
            fragment = render_chapter_fragment(ch)
            _write_text_retrying(data_dir / f"{ch.index:04d}.html", fragment)

        _write_text_retrying(
            book_dir / "index.html",
            render_book_page(
                book_id=book_id, book_title=book_title, author=author, category=category,
                chapters=chapters, cover=cover_name,
            ),
        )
        _write_text_retrying(
            book_dir / "meta.json",
            render_book_meta(title=book_title, author=author, category=category, n=len(chapters), cover=cover_name),
        )
        _write_text_retrying(book_dir / "titles.json", render_chapter_titles(chapters))
    except Exception:
        shutil.rmtree(book_dir, ignore_errors=True)
        raise

    return {
        "id": book_id, "title": book_title, "author": author, "category": category,
        "n": len(chapters), "cover": cover_name,
    }


def write_index(site_dir: Path, manifest: list[dict]) -> None:
    books_by_category: dict[str, list[dict]] = {}
    for entry in manifest:
        books_by_category.setdefault(entry["category"], []).append(entry)
    # A fresh timestamp on every write_index() call (bulk build AND live
    # /import both go through this one function) — version-check.js polls
    # this and reloads the home page when it changes, so a client sees a
    # newly-imported book without needing a manual hard-refresh to bust
    # whatever cache is sitting between it and index.html.
    version = str(int(time.time()))
    (site_dir / "index.html").write_text(render_index_page(books_by_category, version=version), encoding="utf-8")
    (site_dir / "books.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (site_dir / "version.json").write_text(json.dumps({"v": version}), encoding="utf-8")


def _write_piper_offline_js(site_assets: Path, assets_dir: Path) -> None:
    """Copies piper-offline.js, substituting its GCS model base URL from
    PIPER_MODEL_BASE_URL — the bucket is a personal GCP resource, not
    committed as a literal in site_assets/ (see piper-offline.js)."""
    base_url = os.environ.get("PIPER_MODEL_BASE_URL", "")
    if not base_url:
        print("WARNING: PIPER_MODEL_BASE_URL not set — Piper offline voice download will not work", file=sys.stderr)
    content = (site_assets / "piper-offline.js").read_text(encoding="utf-8")
    content = content.replace("__PIPER_MODEL_BASE_URL__", base_url)
    (assets_dir / "piper-offline.js").write_text(content, encoding="utf-8")


def build_library(inputs: list[str], output_dir: str) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    assets_dir = out / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    site_assets = Path(__file__).resolve().parent.parent / "site_assets"
    shutil.copy(site_assets / "reader.js", assets_dir / "reader.js")
    shutil.copy(site_assets / "download.js", assets_dir / "download.js")
    _write_piper_offline_js(site_assets, assets_dir)
    shutil.copy(site_assets / "version-check.js", assets_dir / "version-check.js")
    shutil.copy(site_assets / "manifest.json", out / "manifest.json")
    shutil.copy(site_assets / "sw.js", out / "sw.js")
    shutil.copytree(site_assets / "icons", assets_dir / "icons", dirs_exist_ok=True)
    shutil.copytree(site_assets / "vendor", assets_dir / "vendor", dirs_exist_ok=True)
    (out / "chapter-shell.html").write_text(render_chapter_shell(), encoding="utf-8")

    book_paths = _iter_book_paths(inputs)
    print(f"Found {len(book_paths)} book files")

    manifest: list[dict] = []
    used_ids: set[int] = set()

    for path in book_paths:
        try:
            entry = add_book_to_site(out, path, used_ids)
        except Exception as e:
            print(f"  SKIP {path.name}: {e}")
            continue
        manifest.append(entry)
        print(f"  OK   {entry['title']} [{entry['category']}] — {entry['n']} chương -> books/{entry['id']}/")

    write_index(out, manifest)
    categories = {e["category"] for e in manifest}
    print(f"\nBuilt {len(manifest)} books across {len(categories)} categories in {output_dir}/")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "-o"]
    output_dir = "site"
    if "-o" in sys.argv:
        output_dir = sys.argv[sys.argv.index("-o") + 1]
        args = [a for a in args if a != output_dir]
    if not args:
        print(__doc__)
        sys.exit(1)
    build_library(args, output_dir)
