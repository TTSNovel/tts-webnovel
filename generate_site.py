"""Render an epub's chapters as a small static site for testing in a browser.

Wraps each chapter in <article> and links chapters with rel="next" — the
exact selectors kokoro-tts-addon/content.js already auto-detects for content
extraction and auto-advance, so this doubles as a manual end-to-end test rig
for the extension without needing a real web novel site. Layout follows the
common webnovel-reader pattern (sticky "back to TOC" bar, prev/TOC/next
button row, centered reading column) instead of a bare list of links.

Usage: python3 generate_site.py path/to/book.epub [output_dir]
"""
import html
import re
import sys
from pathlib import Path

from epub_parser import extract_book

_CSS = """
:root {
  --bg: #fafafa; --surface: #ffffff; --text: #1a1a1a; --muted: #6b7280;
  --border: #e5e7eb; --accent: #4f46e5; --accent-text: #ffffff; --hover: #f3f4f6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17181c; --surface: #1e1f24; --text: #e4e4e7; --muted: #9ca3af;
    --border: #2e2f36; --accent: #818cf8; --accent-text: #17181c; --hover: #26272e;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
a { color: inherit; text-decoration: none; }
.topbar {
  position: sticky; top: 0; z-index: 10; display: flex; align-items: center;
  gap: .75rem; padding: .75rem 1.25rem; background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.topbar .back { color: var(--accent); font-weight: 600; white-space: nowrap; }
.topbar .book-title { color: var(--muted); font-size: .9rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wrap { max-width: 720px; margin: 0 auto; padding: 2rem 1.25rem 4rem; }

/* index page */
.book-header { text-align: center; margin-bottom: 2rem; }
.book-header h1 { font-size: 1.75rem; margin: 0 0 .4rem; }
.book-header .author { color: var(--muted); margin: 0 0 .2rem; }
.book-header .count { color: var(--muted); font-size: .85rem; }
.chapter-list { list-style: none; margin: 0; padding: 0; border: 1px solid var(--border); border-radius: 10px; overflow: hidden; background: var(--surface); }
.chapter-list li + li { border-top: 1px solid var(--border); }
.chapter-list a { display: flex; gap: .75rem; padding: .8rem 1rem; align-items: baseline; }
.chapter-list a:hover { background: var(--hover); }
.chapter-list .num { color: var(--muted); font-size: .85rem; min-width: 2ch; text-align: right; }
.chapter-list .title { flex: 1; }

/* chapter page */
article h1 { font-size: 1.4rem; margin: 0 0 1.5rem; }
article p { margin: 0 0 1.1em; font-size: 1.05rem; }
.chapter-nav { display: flex; gap: .6rem; margin-top: 2.5rem; }
.btn {
  flex: 1; text-align: center; padding: .7rem 1rem; border-radius: 8px;
  border: 1px solid var(--border); background: var(--surface); font-weight: 600;
}
.btn:hover { background: var(--hover); }
.btn.disabled { opacity: .4; pointer-events: none; }
.btn-primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }
"""

_CHAPTER_PAGE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>{chapter_title} — {book_title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{css}</style>
</head>
<body>
<div class="topbar">
  <a class="back" href="../index.html">‹ Mục lục</a>
  <span class="book-title">{book_title}</span>
</div>
<div class="wrap">
  <article>
    <h1>{chapter_title}</h1>
    {content}
  </article>
  <nav class="chapter-nav">
    <a class="btn {prev_disabled}" href="{prev_href}">‹ Chương trước</a>
    <a class="btn" href="../index.html">Mục lục</a>
    <a class="btn btn-primary {next_disabled}" rel="next" href="{next_href}">Chương tiếp theo ›</a>
  </nav>
</div>
</body>
</html>
"""

_INDEX_PAGE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>{book_title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{css}</style>
</head>
<body>
<div class="wrap">
  <header class="book-header">
    <h1>{book_title}</h1>
    {author_html}
    <p class="count">{n} chương</p>
  </header>
  <ol class="chapter-list">
{items}
  </ol>
</div>
</body>
</html>
"""


def _slugify(title: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug[:50] or "chapter"


def generate_site(epub_path: str, output_dir: str) -> int:
    metadata, chapters = extract_book(epub_path)
    book_title = html.escape(metadata["title"] or Path(epub_path).stem)
    author = html.escape(metadata["author"]) if metadata["author"] else None

    out = Path(output_dir)
    chapters_dir = out / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)

    filenames = [f"{ch.index:04d}_{_slugify(ch.title)}.html" for ch in chapters]

    for i, ch in enumerate(chapters):
        prev_href = filenames[i - 1] if i > 0 else "#"
        next_href = filenames[i + 1] if i + 1 < len(chapters) else "#"
        page = _CHAPTER_PAGE.format(
            css=_CSS,
            book_title=book_title,
            chapter_title=html.escape(ch.title),
            content=ch.html,
            prev_href=prev_href,
            prev_disabled="disabled" if i == 0 else "",
            next_href=next_href,
            next_disabled="disabled" if i + 1 >= len(chapters) else "",
        )
        (chapters_dir / filenames[i]).write_text(page, encoding="utf-8")

    items = "\n".join(
        f'    <li><a href="chapters/{fname}"><span class="num">{ch.index + 1}</span>'
        f'<span class="title">{html.escape(ch.title)}</span></a></li>'
        for ch, fname in zip(chapters, filenames)
    )
    author_html = f'<p class="author">Tác giả: {author}</p>' if author else ""
    index_html = _INDEX_PAGE.format(
        css=_CSS, book_title=book_title, author_html=author_html, n=len(chapters), items=items
    )
    (out / "index.html").write_text(index_html, encoding="utf-8")
    return len(chapters)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    epub_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "site_output"
    n = generate_site(epub_path, output_dir)
    print(f"Generated {n} chapter pages in {output_dir}/ (open {output_dir}/index.html)")
