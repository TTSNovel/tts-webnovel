"""Shared HTML rendering for the novel site.

Used by both the local bulk generator (build_library.py) and the live
/import endpoint in novel-web/server.py — kept in sync manually between the
two repos (server.py already follows this same pattern), since there's no
shared package mechanism across the two separate git repos.
"""
from __future__ import annotations

import hashlib
import html
import json
import re

# Deterministic "cover" placeholder (no real cover art available) — a
# gradient + the title's first 1-2 characters, same idea as Spotify/Notion
# default icons. Picked to read fine as white text on top in both themes.
_COVER_PALETTE = [
    ("#f97316", "#db2777"), ("#6366f1", "#06b6d4"), ("#16a34a", "#0d9488"),
    ("#dc2626", "#ea580c"), ("#7c3aed", "#2563eb"), ("#0891b2", "#4f46e5"),
    ("#be123c", "#7c3aed"), ("#059669", "#65a30d"), ("#c026d3", "#e11d48"),
    ("#2563eb", "#0891b2"), ("#ea580c", "#ca8a04"), ("#4338ca", "#be185d"),
]


def cover_gradient(title: str) -> str:
    h = int(hashlib.md5(title.encode("utf-8")).hexdigest(), 16)
    c1, c2 = _COVER_PALETTE[h % len(_COVER_PALETTE)]
    return f"linear-gradient(135deg, {c1}, {c2})"


def cover_initial(title: str) -> str:
    for ch in title:
        if ch.isalnum():
            return ch.upper()
    return "?"


def slugify(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug[:60] or "book"


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
.wrap { max-width: 1100px; margin: 0 auto; padding: 2rem 1.25rem 6rem; }
.wrap.narrow { max-width: 720px; }

/* home page */
.site-topbar {
  position: sticky; top: 0; z-index: 10; display: flex; align-items: center; gap: 1rem;
  padding: .9rem 1.25rem; background: var(--surface); border-bottom: 1px solid var(--border);
}
.site-topbar h1 { font-size: 1.15rem; margin: 0; white-space: nowrap; }
.site-topbar .spacer { flex: 1; }
.site-search {
  flex: 1; max-width: 340px; padding: .5rem .8rem; border-radius: 999px;
  border: 1px solid var(--border); background: var(--bg); color: var(--text); font-size: .9rem;
}
.site-topbar .import-link {
  padding: .5rem 1rem; border-radius: 999px; background: var(--accent); color: var(--accent-text);
  font-size: .85rem; font-weight: 600; white-space: nowrap;
}
.category { margin: 2rem 0; }
.category h2 {
  font-size: 1.05rem; margin: 0 0 1rem; padding-left: .6rem;
  border-left: 4px solid var(--accent);
}
.book-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 1.1rem; }
.book-card { display: flex; flex-direction: column; gap: .4rem; }
.book-card .cover {
  aspect-ratio: 2 / 3; border-radius: 10px; display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 2.4rem; font-weight: 700; text-shadow: 0 2px 6px rgba(0,0,0,.25);
  box-shadow: 0 2px 10px rgba(0,0,0,.18); transition: transform .15s ease;
}
.book-card:hover .cover { transform: translateY(-3px); }
.book-card .title { font-weight: 600; font-size: .9rem; line-height: 1.35; }
.book-card .author { color: var(--muted); font-size: .78rem; }
.book-card .meta { color: var(--muted); font-size: .75rem; }
.book-card[hidden] { display: none; }

/* book page */
.book-header { display: flex; gap: 1.5rem; align-items: flex-start; margin-bottom: 2rem; }
.book-header .cover {
  width: 120px; aspect-ratio: 2 / 3; flex: none; border-radius: 12px; display: flex;
  align-items: center; justify-content: center; color: #fff; font-size: 3rem; font-weight: 700;
  text-shadow: 0 2px 6px rgba(0,0,0,.25); box-shadow: 0 3px 14px rgba(0,0,0,.2);
}
.book-header .info h1 { font-size: 1.5rem; margin: 0 0 .4rem; }
.book-header .info .author { color: var(--muted); margin: 0 0 .3rem; }
.book-header .info .count { color: var(--muted); font-size: .85rem; }
.book-header .info .category-badge {
  display: inline-block; margin-top: .5rem; padding: .2rem .6rem; border-radius: 999px;
  background: var(--hover); color: var(--muted); font-size: .75rem;
}
.chapter-list { list-style: none; margin: 0; padding: 0; border: 1px solid var(--border); border-radius: 10px; overflow: hidden; background: var(--surface); }
.chapter-list li + li { border-top: 1px solid var(--border); }
.chapter-list a { display: flex; gap: .75rem; padding: .8rem 1rem; align-items: baseline; }
.chapter-list a:hover { background: var(--hover); }
.chapter-list .num { color: var(--muted); font-size: .85rem; min-width: 3ch; text-align: right; }
.chapter-list .title { flex: 1; }

/* chapter page */
article h1 { font-size: 1.4rem; margin: 0 0 1.5rem; }
article p { margin: 0 0 1.1em; font-size: 1.05rem; }
[data-r-s].reading { background-color: rgba(255, 220, 50, 0.6); border-radius: 3px; outline: 2px solid rgba(255, 180, 0, 0.65); outline-offset: 1px; }
.chapter-nav { display: flex; gap: .6rem; margin-top: 2.5rem; }
.btn {
  flex: 1; text-align: center; padding: .7rem 1rem; border-radius: 8px;
  border: 1px solid var(--border); background: var(--surface); font-weight: 600;
}
.btn:hover { background: var(--hover); }
.btn.disabled { opacity: .4; pointer-events: none; }
.btn-primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }

/* reader control bar */
.reader-bar {
  position: fixed; left: 50%; bottom: 1rem; transform: translateX(-50%);
  display: flex; align-items: center; gap: .6rem; padding: .5rem .75rem;
  background: var(--surface); border: 1px solid var(--border); border-radius: 999px;
  box-shadow: 0 4px 16px rgba(0,0,0,.15); z-index: 20;
}
.reader-btn {
  width: 2.4rem; height: 2.4rem; flex: none; border-radius: 50%; border: none;
  background: var(--accent); color: var(--accent-text); font-size: 1rem; cursor: pointer;
}
.reader-btn-ghost { background: transparent; color: var(--muted); border: 1px solid var(--border); font-size: 1.1rem; }
.reader-progress { font-size: .85rem; color: var(--muted); min-width: 4.5rem; text-align: center; }
.reader-status { font-size: .8rem; color: var(--muted); max-width: 9rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.reader-settings {
  position: fixed; left: 50%; bottom: 4.5rem; transform: translateX(-50%);
  display: flex; flex-direction: column; gap: .7rem; width: min(90vw, 320px);
  padding: 1rem 1.1rem; background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.15); z-index: 20;
}
.reader-settings[hidden] { display: none; }
.reader-settings label { display: flex; flex-direction: column; gap: .3rem; font-size: .8rem; color: var(--muted); }
.reader-settings select, .reader-settings input[type="number"] {
  border: 1px solid var(--border); border-radius: 6px; background: var(--bg); color: var(--text);
  font-size: .85rem; padding: .4rem .5rem;
}
.reader-settings-checkbox { flex-direction: row !important; align-items: center; gap: .5rem !important; }
.reader-settings-checkbox input { width: auto; }

/* import page */
.import-form { max-width: 480px; margin: 3rem auto; padding: 2rem; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; }
.import-form h1 { font-size: 1.2rem; margin: 0 0 1.2rem; }
.import-form label { display: block; font-size: .85rem; color: var(--muted); margin-bottom: .3rem; }
.import-form input, .import-form select { width: 100%; padding: .6rem .7rem; margin-bottom: 1rem; border: 1px solid var(--border); border-radius: 8px; background: var(--bg); color: var(--text); font-size: .95rem; }
.import-form button { width: 100%; padding: .7rem; border: none; border-radius: 8px; background: var(--accent); color: var(--accent-text); font-size: 1rem; font-weight: 600; cursor: pointer; }
.import-form .hint { font-size: .78rem; color: var(--muted); margin: -.6rem 0 1rem; }
.import-form .msg { font-size: .85rem; margin-bottom: 1rem; padding: .6rem .8rem; border-radius: 8px; }
.import-form .msg.ok { background: rgba(22,163,74,.15); color: #16a34a; }
.import-form .msg.err { background: rgba(220,38,38,.15); color: #dc2626; }
"""

# iOS Safari's "Add to Home Screen" reads the apple-* meta/link tags
# directly, not the web manifest — the manifest link is included anyway for
# Android/Chrome's install prompt. Absolute paths (leading "/") so this
# works unchanged regardless of how deeply nested the page is (chapter
# pages sit 3 levels under site root).
_PWA_HEAD = """<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#4f46e5">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Thư viện truyện">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/assets/icons/icon-180.png">
<script>if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');</script>"""

# App-shell pattern: this ONE file is served for every "/books/<slug>/
# chapters/<n>.html" URL (see novel-web/server.py's chapter_shell route) —
# byte-identical regardless of which book/chapter, so changing the reader
# UI means rewriting this single file instead of one per chapter (was
# ~72,000 separate full pages, each with its own copy of this same CSS).
# reader.js parses the visited URL itself, fetches the matching fragment
# from books/<slug>/data/<n>.html, and fills in the topbar/nav — nothing
# here is chapter-specific.
_CHAPTER_SHELL = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>Đang tải… — Thư viện truyện</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
{pwa_head}
<style>{css}</style>
</head>
<body>
<div class="topbar">
  <a class="back" href="../index.html">‹ Mục lục</a>
  <span class="book-title"></span>
</div>
<div class="wrap narrow">
  <article></article>
  <nav class="chapter-nav">
    <a class="btn disabled" data-chapter-nav="prev" href="#">‹ Chương trước</a>
    <a class="btn" href="../index.html">Mục lục</a>
    <a class="btn btn-primary disabled" data-chapter-nav="next" rel="next" href="#">Chương tiếp theo ›</a>
  </nav>
</div>
<script src="../../../assets/reader.js"></script>
</body>
</html>
"""

_BOOK_PAGE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>{book_title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
{pwa_head}
<style>{css}</style>
</head>
<body>
<div class="topbar">
  <a class="back" href="../../index.html">‹ Trang chủ</a>
  <span class="book-title">{book_title}</span>
</div>
<div class="wrap">
  <header class="book-header">
    <div class="cover" style="background:{cover_gradient}">{cover_initial}</div>
    <div class="info">
      <h1>{book_title}</h1>
      {author_html}
      <p class="count">{n} chương</p>
      <span class="category-badge">{category}</span>
    </div>
  </header>
  <ol class="chapter-list">
{items}
  </ol>
</div>
</body>
</html>
"""

_INDEX_PAGE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>Thư viện truyện</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
{pwa_head}
<style>{css}</style>
</head>
<body>
<div class="site-topbar">
  <h1>📚 Thư viện truyện</h1>
  <input class="site-search" type="search" placeholder="Tìm truyện…" oninput="
    document.querySelectorAll('.book-card').forEach(function(el){{
      el.hidden = !el.dataset.title.includes(this.value.toLowerCase());
    }}, this);
    document.querySelectorAll('.category').forEach(function(cat){{
      cat.hidden = !cat.querySelector('.book-card:not([hidden])');
    }});
  ">
  <span class="spacer"></span>
  <a class="import-link" href="/import">+ Thêm truyện</a>
</div>
<div class="wrap">
{categories}
</div>
</body>
</html>
"""

_BOOK_CARD = """    <a class="book-card" href="books/{slug}/index.html" data-title="{title_lower}">
      <div class="cover" style="background:{cover_gradient}">{cover_initial}</div>
      <span class="title">{title}</span>
      {author_html}
      <span class="meta">{n} chương</span>
    </a>"""


def render_chapter_shell() -> str:
    """The one static page served for every chapter URL — see _CHAPTER_SHELL."""
    return _CHAPTER_SHELL.format(css=_CSS, pwa_head=_PWA_HEAD)


def render_chapter_fragment(chapter) -> str:
    """Just the chapter's own content — no page chrome, no CSS — fetched by
    reader.js and injected into the shell's <article> at runtime."""
    return f"<h1>{html.escape(chapter.title)}</h1>\n{chapter.html}"


def render_book_meta(title: str, author: str | None, category: str, n: int) -> str:
    """Small per-book manifest reader.js fetches once per chapter-page load
    to fill in the topbar title and compute prev/next-disabled state (needs
    the total chapter count) — far cheaper than re-deriving this from the
    book's full chapter-list page."""
    return json.dumps({"title": title, "author": author, "category": category, "n": n}, ensure_ascii=False)


def render_book_page(book_title: str, author: str | None, category: str, chapters) -> str:
    filenames = [f"{ch.index:04d}.html" for ch in chapters]
    items = "\n".join(
        f'    <li><a href="chapters/{fname}"><span class="num">{ch.index + 1}</span>'
        f'<span class="title">{html.escape(ch.title)}</span></a></li>'
        for ch, fname in zip(chapters, filenames)
    )
    author_html = f'<p class="author">Tác giả: {html.escape(author)}</p>' if author else ""
    return _BOOK_PAGE.format(
        css=_CSS,
        pwa_head=_PWA_HEAD,
        book_title=html.escape(book_title),
        cover_gradient=cover_gradient(book_title),
        cover_initial=html.escape(cover_initial(book_title)),
        author_html=author_html,
        n=len(chapters),
        category=html.escape(category),
        items=items,
    )


def render_index_page(books_by_category: dict[str, list[dict]]) -> str:
    category_blocks = []
    for category in sorted(books_by_category):
        books = sorted(books_by_category[category], key=lambda b: b["title"])
        cards = []
        for b in books:
            author_html = f'<span class="author">{html.escape(b["author"])}</span>' if b.get("author") else ""
            cards.append(_BOOK_CARD.format(
                slug=b["slug"],
                title=html.escape(b["title"]),
                title_lower=html.escape(b["title"].lower()),
                cover_gradient=cover_gradient(b["title"]),
                cover_initial=html.escape(cover_initial(b["title"])),
                author_html=author_html,
                n=b["n"],
            ))
        category_blocks.append(
            f'  <section class="category">\n    <h2>{html.escape(category)}</h2>\n'
            f'    <div class="book-grid">\n' + "\n".join(cards) + "\n    </div>\n  </section>"
        )
    return _INDEX_PAGE.format(css=_CSS, pwa_head=_PWA_HEAD, categories="\n".join(category_blocks))
