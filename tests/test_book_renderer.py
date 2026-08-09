import json

from book_renderer import render_chapter_titles
from epub_parser import Chapter


def test_render_chapter_titles_is_ordered_title_only_json():
    chapters = [
        Chapter(index=0, title="Chương 1: Khởi đầu", html="<p>a</p>", text="a"),
        Chapter(index=1, title="Chương 2: Tiếp theo", html="<p>b</p>", text="b"),
    ]
    titles = json.loads(render_chapter_titles(chapters))
    assert titles == ["Chương 1: Khởi đầu", "Chương 2: Tiếp theo"]
