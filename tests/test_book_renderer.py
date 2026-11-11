import json
import re

from book_renderer import render_categories_page, render_chapter_titles, render_index_page
from build_library import refresh_index_if_stale
from epub_parser import Chapter


def test_render_chapter_titles_is_ordered_title_only_json():
    chapters = [
        Chapter(index=0, title="Chương 1: Khởi đầu", html="<p>a</p>", text="a"),
        Chapter(index=1, title="Chương 2: Tiếp theo", html="<p>b</p>", text="b"),
    ]
    titles = json.loads(render_chapter_titles(chapters))
    assert titles == ["Chương 1: Khởi đầu", "Chương 2: Tiếp theo"]


# books.json order = the order books were added
_BOOKS = [
    {"id": 3, "title": "Zeta", "author": None, "category": "Cultivation - Fantasy", "n": 10, "cover": None},
    {"id": 1, "title": "Alpha", "author": "A", "category": "Romance", "n": 5, "cover": None},
    {"id": 2, "title": "Beta", "author": None, "category": "Cultivation - Fantasy", "n": 7, "cover": None},
]


def _card_ids(page):
    return [int(i) for i in re.findall(r'class="book-card"[^>]*data-id="(\d+)"', page)]


def _section(page, category):
    start = page.index(f"<h2>{category}</h2>")
    return page[start:page.index("</section>", start)]


def test_index_page_is_one_flat_list_in_added_order():
    page = render_index_page(_BOOKS, version="1")
    assert _card_ids(page) == [3, 1, 2]
    assert page.count('class="book-grid"') == 1
    assert 'href="/categories.html"' in page


def test_categories_page_groups_by_category_in_added_order():
    page = render_categories_page(_BOOKS)
    assert _card_ids(_section(page, "Cultivation - Fantasy")) == [3, 2]
    assert _card_ids(_section(page, "Romance")) == [1]
    assert "Cultivation - Fantasy (2)" in page and "Romance (1)" in page


def test_refresh_index_if_stale_rewrites_only_when_layout_changed(tmp_path):
    (tmp_path / "books.json").write_text(json.dumps(_BOOKS), encoding="utf-8")
    (tmp_path / "index.html").write_text("old layout", encoding="utf-8")
    assert refresh_index_if_stale(tmp_path) is True
    assert (tmp_path / "categories.html").exists()
    assert refresh_index_if_stale(tmp_path) is False
