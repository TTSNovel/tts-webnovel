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


def _write_book(path: str, spine_items: list, toc: tuple = ()) -> None:
    book = epub.EpubBook()
    book.set_identifier("junk-chapter-id")
    book.set_title("Junk Chapter Novel")
    book.set_language("vi")
    for item in spine_items:
        book.add_item(item)
    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *spine_items]
    epub.write_epub(path, book)


def test_drops_toc_dump_page():
    """A "Contents" page kept as its own spine item: its link text
    survives _clean_html_and_text (no "Chương N" prefix to match), so
    without this filter it would look like its own short chapter."""
    with tempfile.TemporaryDirectory() as tmp:
        epub_path = str(Path(tmp) / "toc_dump.epub")
        toc_page = epub.EpubHtml(title="Contents", file_name="toc.xhtml", lang="vi")
        toc_page.content = "<html><body><h1>Contents</h1><p>1.</p><p>2.</p><p>3.</p><p>4.</p></body></html>"
        c1 = epub.EpubHtml(title="Chương 1", file_name="chap_01.xhtml", lang="vi")
        c1.content = "<html><body><h1>Chương 1</h1><p>Nội dung chương một thật dài và đầy đủ.</p></body></html>"
        _write_book(epub_path, [toc_page, c1])

        chapters = extract_chapters(epub_path)
        assert [c.title for c in chapters] == ["Chương 1"]


def test_drops_divider_chapter_with_duplicate_content():
    """Some epubs ship a tiny "part divider" file (body is just the
    chapter number) immediately before the real content file for the same
    chapter — the divider carries no content the next chapter lacks."""
    with tempfile.TemporaryDirectory() as tmp:
        epub_path = str(Path(tmp) / "divider.epub")
        divider = epub.EpubHtml(title="1. Roll for Survival", file_name="divider_01.xhtml", lang="en")
        divider.content = "<html><body><p>1</p></body></html>"
        content = epub.EpubHtml(title="Roll for Survival", file_name="chap_01.xhtml", lang="en")
        content.content = "<html><body><h1>Roll for Survival</h1><p>A wall of real chapter text goes here.</p></body></html>"
        toc = (
            epub.Link("divider_01.xhtml", "1. Roll for Survival", "d1"),
            epub.Link("chap_01.xhtml", "Roll for Survival", "c1"),
        )
        _write_book(epub_path, [divider, content], toc=toc)

        chapters = extract_chapters(epub_path)
        assert [c.title for c in chapters] == ["Roll for Survival"]


def test_drops_front_matter_page():
    """An ad/copyright/dedication page is real epub content but never
    story prose — dropped outright rather than shown as a "chapter"."""
    with tempfile.TemporaryDirectory() as tmp:
        epub_path = str(Path(tmp) / "front_matter.epub")
        ad_page = epub.EpubHtml(title="Also in series", file_name="ad.xhtml", lang="en")
        ad_page.content = "<html><body><h1>Also in Series</h1><p>Defiance of the Fall</p></body></html>"
        c1 = epub.EpubHtml(title="Chapter 1", file_name="chap_01.xhtml", lang="en")
        c1.content = "<html><body><h1>Chapter 1</h1><p>A wall of real chapter text goes here.</p></body></html>"
        _write_book(epub_path, [ad_page, c1])

        chapters = extract_chapters(epub_path)
        assert [c.title for c in chapters] == ["Chapter 1"]


def test_drops_chapter_whose_raw_source_is_credit_line_only():
    """The raw epub source file for this chapter is nothing but a
    translator-credit line — the story text was never in the epub, not
    something our own boilerplate stripping ate."""
    with tempfile.TemporaryDirectory() as tmp:
        epub_path = str(Path(tmp) / "missing_content.epub")
        c1 = epub.EpubHtml(title="Chương 1", file_name="chap_01.xhtml", lang="vi")
        c1.content = "<html><body><h1>Chương 1</h1><p>Team: Vạn Yên Chi Sào</p><p>Nguồn: Truyenyy.com</p></body></html>"
        c2 = epub.EpubHtml(title="Chương 2", file_name="chap_02.xhtml", lang="vi")
        c2.content = "<html><body><h1>Chương 2</h1><p>Nội dung chương hai thật dài và đầy đủ.</p></body></html>"
        _write_book(epub_path, [c1, c2])

        chapters = extract_chapters(epub_path)
        assert [c.title for c in chapters] == ["Chương 2"]


def test_keeps_short_chapter_that_reads_like_real_content():
    """A short chapter with no credit/nav boilerplate and no bare-number
    body should survive — being short alone is not grounds to drop it."""
    with tempfile.TemporaryDirectory() as tmp:
        epub_path = str(Path(tmp) / "short_real.epub")
        c1 = epub.EpubHtml(title="Chương 1", file_name="chap_01.xhtml", lang="vi")
        c1.content = "<html><body><h1>Chương 1</h1><p>Nội dung chương một.</p></body></html>"
        _write_book(epub_path, [c1])

        chapters = extract_chapters(epub_path)
        assert [c.title for c in chapters] == ["Chương 1"]
        assert "Nội dung chương một." in chapters[0].text
