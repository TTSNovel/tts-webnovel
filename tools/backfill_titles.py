"""Backfill books/<id>/titles.json for books imported before that file
existed (see book_renderer.render_chapter_titles). Extracts each title
straight from the already-rendered books/<id>/data/NNNN.html's <h1> instead
of re-importing the original epub — /import doesn't keep the source file
around after extraction, so that's not an option for books added live.

Usage: python3 tools/backfill_titles.py <site_dir> [--force]

<site_dir> is the site root (same layout build_library.py writes — the one
mounted at SITE_DIR in production, see tts-pipeline-infra). --force
regenerates titles.json even for books that already have one.
"""
import html
import json
import re
import sys
from pathlib import Path

_H1_RE = re.compile(r"<h1>(.*?)</h1>", re.IGNORECASE | re.DOTALL)


def _title_from_fragment(path: Path) -> str:
    match = _H1_RE.search(path.read_text(encoding="utf-8"))
    return html.unescape(match.group(1)).strip() if match else ""


def backfill(site_dir: str, force: bool = False) -> None:
    books_dir = Path(site_dir) / "books"
    if not books_dir.is_dir():
        print(f"No books/ under {site_dir}")
        return

    for book_dir in sorted(books_dir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0):
        if not book_dir.is_dir():
            continue
        titles_path = book_dir / "titles.json"
        if titles_path.exists() and not force:
            continue
        fragments = sorted((book_dir / "data").glob("*.html"))
        if not fragments:
            continue
        titles = [_title_from_fragment(f) for f in fragments]
        titles_path.write_text(json.dumps(titles, ensure_ascii=False), encoding="utf-8")
        print(f"  {book_dir.name}: {len(titles)} chương -> {titles_path}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--force"]
    if not args:
        print(__doc__)
        sys.exit(1)
    backfill(args[0], force="--force" in sys.argv)
