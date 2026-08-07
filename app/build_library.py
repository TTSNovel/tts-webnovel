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
import json
import shutil
import sys
from pathlib import Path

from book_renderer import (
    render_book_meta,
    render_book_page,
    render_chapter_fragment,
    render_chapter_shell,
    render_index_page,
)
from classify import classify
from epub_parser import extract_book, extract_book_from_mobi

_LEGACY_EXTENSIONS = {".prc", ".mobi", ".azw3"}
_JUNK_TITLE_MARKERS = ("created with", "written by", "gettextfromhtml")


def _looks_like_real_title(title: str | None) -> bool:
    """Reject epub DC:title values seen in practice from scraped/converted
    books that are actually garbage: raw hex hashes, literal 'index', or a
    long dump of tags + converter-tool credits standing in for a title."""
    if not title:
        return False
    stripped = title.strip()
    if not stripped or stripped.lower() in {"index", "untitled", "cover", "book"}:
        return False
    import re
    if re.fullmatch(r"[0-9a-f]{20,}", stripped, re.IGNORECASE):
        return False
    if len(stripped) > 80:
        return False
    if any(marker in stripped.lower() for marker in _JUNK_TITLE_MARKERS):
        return False
    return True


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
    author = metadata["author"]
    category = classify(book_title, metadata.get("description", ""))

    book_id = max(used_ids, default=0) + 1
    used_ids.add(book_id)

    book_dir = site_dir / "books" / str(book_id)
    data_dir = book_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    for ch in chapters:
        fragment = render_chapter_fragment(ch)
        (data_dir / f"{ch.index:04d}.html").write_text(fragment, encoding="utf-8")

    (book_dir / "index.html").write_text(
        render_book_page(book_id=book_id, book_title=book_title, author=author, category=category, chapters=chapters),
        encoding="utf-8",
    )
    (book_dir / "meta.json").write_text(
        render_book_meta(title=book_title, author=author, category=category, n=len(chapters)),
        encoding="utf-8",
    )

    return {"id": book_id, "title": book_title, "author": author, "category": category, "n": len(chapters)}


def write_index(site_dir: Path, manifest: list[dict]) -> None:
    books_by_category: dict[str, list[dict]] = {}
    for entry in manifest:
        books_by_category.setdefault(entry["category"], []).append(entry)
    (site_dir / "index.html").write_text(render_index_page(books_by_category), encoding="utf-8")
    (site_dir / "books.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def build_library(inputs: list[str], output_dir: str) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    assets_dir = out / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    site_assets = Path(__file__).resolve().parent.parent / "site_assets"
    shutil.copy(site_assets / "reader.js", assets_dir / "reader.js")
    shutil.copy(site_assets / "download.js", assets_dir / "download.js")
    shutil.copy(site_assets / "piper-offline.js", assets_dir / "piper-offline.js")
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
