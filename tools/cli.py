"""Dump every chapter of a real .epub file to output_dir/NNN_slug.html for manual inspection.

Usage: python3 tools/cli.py path/to/book.epub [output_dir]
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from epub_parser import extract_chapters


def _slugify(title: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug[:50] or "chapter"


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    epub_path = sys.argv[1]
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)

    chapters = extract_chapters(epub_path)
    print(f"Extracted {len(chapters)} chapters from {epub_path}")

    for ch in chapters:
        fname = output_dir / f"{ch.index:03d}_{_slugify(ch.title)}.html"
        fname.write_text(f"<h1>{ch.title}</h1>\n{ch.html}", encoding="utf-8")
        print(f"  [{ch.index:03d}] {ch.title!r} — {len(ch.text)} chars -> {fname}")


if __name__ == "__main__":
    main()
