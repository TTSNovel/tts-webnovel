import tempfile
from pathlib import Path

from ebooklib import epub

from epub_parser import extract_chapters


def _build_sample_epub(path: str) -> None:
    book = epub.EpubBook()
    book.set_identifier("test-id-123")
    book.set_title("Sample Novel")
    book.set_language("vi")

    c1 = epub.EpubHtml(title="Chương 1: Khởi đầu", file_name="chap_01.xhtml", lang="vi")
    c1.content = "<html><body><h1>Chương 1: Khởi đầu</h1><p>Nội dung chương một.</p></body></html>"

    c2 = epub.EpubHtml(title="Chương 2: Tiếp theo", file_name="chap_02.xhtml", lang="vi")
    c2.content = "<html><body><h1>Chương 2: Tiếp theo</h1><p>Nội dung chương hai.</p></body></html>"

    blank = epub.EpubHtml(title="Separator", file_name="blank.xhtml", lang="vi")
    blank.content = "<html><body><p></p></body></html>"

    for item in (c1, c2, blank):
        book.add_item(item)

    book.toc = (epub.Link("chap_01.xhtml", "Chương 1: Khởi đầu", "c1"),
                epub.Link("chap_02.xhtml", "Chương 2: Tiếp theo", "c2"))
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", c1, c2, blank]

    epub.write_epub(path, book)


def test_extract_chapters_order_titles_and_text():
    with tempfile.TemporaryDirectory() as tmp:
        epub_path = str(Path(tmp) / "sample.epub")
        _build_sample_epub(epub_path)

        chapters = extract_chapters(epub_path)

        # Blank separator page must be dropped, only the 2 real chapters remain.
        assert len(chapters) == 2

        assert chapters[0].index == 0
        assert chapters[0].title == "Chương 1: Khởi đầu"
        assert "Nội dung chương một." in chapters[0].text
        assert "<p>" in chapters[0].html

        assert chapters[1].index == 1
        assert chapters[1].title == "Chương 2: Tiếp theo"
        assert "Nội dung chương hai." in chapters[1].text


def test_strips_duplicate_title_and_credit_line_from_body():
    """Real-world .txt-converted web novels often repeat the chapter title
    as the first line of the body, followed by a typesetting-group credit
    line (e.g. a Zalo contact) — both are redundant boilerplate since
    render_chapter_fragment already renders the title as its own <h1>."""
    with tempfile.TemporaryDirectory() as tmp:
        epub_path = str(Path(tmp) / "boilerplate.epub")
        book = epub.EpubBook()
        book.set_identifier("boilerplate-id")
        book.set_title("Boilerplate Novel")
        book.set_language("vi")

        c1 = epub.EpubHtml(title="Chương 1: Hạ Chí đã tới (1)", file_name="chap_01.xhtml", lang="vi")
        c1.content = (
            "<html><body>"
            "<h1>Chương 1: Hạ Chí đã tới (1)</h1>"
            "<p>Zalo người làm sách: 0945 787 018</p>"
            "<p>Tháng tám, trong vùng núi sâu ở tây nam bộ Trung Quốc.</p>"
            "</body></html>"
        )
        book.add_item(c1)
        book.toc = (epub.Link("chap_01.xhtml", "Chương 1: Hạ Chí đã tới (1)", "c1"),)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["nav", c1]

        epub.write_epub(epub_path, book)

        chapters = extract_chapters(epub_path)
        assert len(chapters) == 1
        ch = chapters[0]
        assert ch.title == "Chương 1: Hạ Chí đã tới (1)"
        assert "Hạ Chí đã tới" not in ch.html
        assert "Zalo" not in ch.html
        assert "Tháng tám" in ch.html


def test_fallback_title_when_toc_missing():
    with tempfile.TemporaryDirectory() as tmp:
        epub_path = str(Path(tmp) / "no_toc.epub")
        book = epub.EpubBook()
        book.set_identifier("no-toc")
        book.set_title("No TOC Book")
        book.set_language("en")

        c1 = epub.EpubHtml(title="c1", file_name="c1.xhtml", lang="en")
        c1.content = "<html><body><h1>Untitled Chapter</h1><p>Some text.</p></body></html>"
        book.add_item(c1)
        book.toc = ()
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["nav", c1]

        epub.write_epub(epub_path, book)

        chapters = extract_chapters(epub_path)
        assert len(chapters) == 1
        assert chapters[0].title == "Untitled Chapter"
