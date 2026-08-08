"""Extract an .epub file into ordered, plain-HTML chapters.

Deliberately does NOT use epub.js-style iframe rendering — the output here
is meant to be inserted directly into a normal page DOM (or served as a
plain chapter page), so the existing extension's read-aloud logic (which
walks page text nodes) can read it exactly like a web novel page.
"""
from __future__ import annotations

import html as html_module
import logging
import re
import shutil
import warnings
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
from ebooklib import epub, ITEM_DOCUMENT
from ebooklib.epub import EpubReader

# Real-world epubs (esp. scraped/converted ones) sometimes declare a manifest
# item whose file was never actually included in the zip — e.g. a leftover
# reference to an editor's note file. ebooklib eagerly reads every manifest
# item's content while loading (not just the ones we end up using), so one
# missing zip entry crashes the whole read_epub() call with a raw KeyError.
# Patch it to skip missing entries instead of dying on them.
_original_read_file = EpubReader.read_file


def _lenient_read_file(self, name):
    try:
        return _original_read_file(self, name)
    except KeyError:
        logging.warning("epub_parser: manifest references missing zip entry %r, skipping", name)
        return b""


EpubReader.read_file = _lenient_read_file


@dataclass
class Chapter:
    index: int
    title: str
    html: str
    text: str


def _toc_titles_by_href(toc) -> dict[str, str]:
    """Flatten book.toc (nested Link/Section/tuple tree) into {href_without_fragment: title}."""
    titles: dict[str, str] = {}

    def walk(node):
        if isinstance(node, epub.Link):
            href = node.href.split("#")[0]
            titles.setdefault(href, node.title)
        elif isinstance(node, (tuple, list)):
            for child in node:
                walk(child)
        elif isinstance(node, epub.Section):
            walk(node.href if hasattr(node, "href") else [])

    walk(toc)
    return titles


def _clean_html_and_text(raw_bytes: bytes) -> tuple[str, str]:
    soup = BeautifulSoup(raw_bytes, "lxml")
    body = soup.body or soup
    # Strip elements that carry no readable content.
    for tag in body.find_all(["script", "style"]):
        tag.decompose()
    for a in body.find_all("a"):
        href = a.get("href")
        if not href:
            # No href at all — not a functional link, just inline markup
            # noise (seen in raw unpacked mobi/prc: real chapter headings
            # come wrapped in a bare `<a><span>Chương N: ...</span></a>`
            # with no href). Leave its text alone; it's real content.
            continue
        if not href.startswith("#"):
            # Cross-document link (another chapter file, "prev/next
            # chapter" nav) — never real chapter prose, a novel doesn't
            # hyperlink its own sentences. Left in, this bleeds into the
            # extracted text and gets mistaken for a real chapter-marker
            # boundary by _split_by_chapter_markers.
            a.decompose()
        elif _CHAPTER_MARKER_RE.match(a.get_text()):
            # Single-mega-document sources (raw unpacked mobi/prc, no epub
            # multi-file split) put their table of contents in the SAME
            # document instead, linking to a same-page byte offset like
            # href="#filepos301977" — same TOC-pollution problem as above,
            # just a same-page href instead of cross-document.
            a.decompose()
    html = body.decode_contents().strip()
    text = body.get_text(separator="\n", strip=True)
    return html, text


def _fallback_title(soup_html: str, index: int) -> str:
    soup = BeautifulSoup(soup_html, "lxml")
    for tag_name in ("h1", "h2", "title"):
        tag = soup.find(tag_name)
        if tag and tag.get_text(strip=True):
            return tag.get_text(strip=True)
    return f"Chapter {index + 1}"


# Some real-world epubs (esp. .txt-converted web novels) dump the entire
# book into a single spine file with no per-chapter HTML structure at all —
# chapters are only marked by a "Chương N: ..." text line. Those titles also
# tend to appear twice in a row right at the chapter boundary (title
# rendered, then repeated immediately before the body) — a `min_gap` filters
# out that immediate duplicate so it isn't treated as its own empty chapter.
_CHAPTER_MARKER_RE = re.compile(
    r"^[ \t]*(?:Q\d+\s*-\s*)?((?:Chương|Chapter)\s*\d+[^\n]{0,80})", re.IGNORECASE | re.MULTILINE
)

# Tag embedded (as an HTML comment, so it never renders) in spine files we
# generate ourselves — see build_library.py's physical re-split tooling —
# to mark "this file is already exactly one chapter, do not re-scan it for
# markers". A raw byte substring check, not a parsed-DOM one: cheap, and
# run before _clean_html_and_text so it applies regardless of that file's
# actual markup.
_CHAPTER_ALREADY_SPLIT_MARKER = b"<!--chapter-already-split-->"


def _looks_like_toc_dump(text: str) -> bool:
    """Some sites periodically embed a "coming up next" mini table-of-
    contents mid-story — a run of "Chương N: ..." lines with no prose
    between them. Those lines are close enough together that min_gap below
    dedups all but the first into one bogus "chapter" whose body is really
    just that listing. The titles it lists always turn up properly
    elsewhere with real content, so dropping the dump loses nothing."""
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return False
    marker_lines = sum(1 for line in lines if _CHAPTER_MARKER_RE.match(line))
    return marker_lines >= 2 and marker_lines / len(lines) > 0.5


def _split_by_chapter_markers(text: str, min_gap: int = 200) -> list[tuple[str, str]] | None:
    matches = list(_CHAPTER_MARKER_RE.finditer(text))

    boundaries: list[tuple[int, str]] = []
    last_pos = -min_gap * 2
    for m in matches:
        if m.start() - last_pos < min_gap:
            continue  # immediate duplicate of the marker just kept
        boundaries.append((m.start(), m.group(1).strip()))
        last_pos = m.start()

    if len(boundaries) < 2:
        return None  # not enough real markers to justify splitting

    sub_chapters = []
    for i, (pos, title) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        body = text[pos:end].split("\n", 1)
        body_text = body[1].strip() if len(body) > 1 else ""
        if _looks_like_toc_dump(body_text):
            continue
        sub_chapters.append((title, body_text))
    return sub_chapters


def _text_to_html(text: str) -> str:
    paragraphs = [line.strip() for line in text.split("\n") if line.strip()]
    return "\n".join(f"<p>{html_module.escape(p)}</p>" for p in paragraphs)


# A chapter's own title/first line normally starts with this same marker
# ("Chương N", "Chapter N") — used to tell a genuine (if short) chapter
# apart from front matter (title/author/synopsis) that only got a generic
# fallback title because it has no h1/h2 and no toc entry of its own.
_STARTS_WITH_MARKER_RE = re.compile(r"^\s*(?:Chương|Chapter)\s*\d+", re.IGNORECASE)
_GENERIC_FALLBACK_TITLE_RE = re.compile(r"^Chapter \d+$")
_MARKER_NUM_RE = re.compile(r"^\s*(?:Chương|Chapter)\s*(\d+)", re.IGNORECASE)

# Scraped/typeset .txt-to-epub web novels routinely (a) repeat the chapter
# title as the first line of the body too — book_renderer.render_chapter_fragment
# already renders an <h1> for chapter.title, so left in place this shows the
# title twice — and (b) insert a typesetting-group credit/watermark line
# right after it on every single chapter (contact handle, "converted by",
# translator credit, source link). Both are boilerplate, not story content.
_JUNK_LINE_RE = re.compile(
    r"zalo|nhóm\s*d(?:ị|i)ch|d(?:ị|i)ch\s*gi(?:ả|a)|convert(?:ed)?\s*by|"
    r"beta\s*read|editor\s*[:：]|biên\s*t(?:ậ|a)p|"
    r"ng(?:ườ|uo)i\s*l(?:à|a)m\s*s(?:á|a)ch|nguồn\s*[:：]|sưu\s*t(?:ầ|a)m",
    re.IGNORECASE,
)


def _normalize_for_compare(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _is_duplicate_title_line(line: str, title: str) -> bool:
    line_norm = _normalize_for_compare(line)
    if line_norm == _normalize_for_compare(title):
        return True
    line_num = _MARKER_NUM_RE.match(line)
    title_num = _MARKER_NUM_RE.match(title)
    return bool(line_num and title_num and line_num.group(1) == title_num.group(1))


def _strip_leading_boilerplate_text(text: str, title: str) -> str:
    """Drop leading blank/duplicate-title/credit lines from a chapter body
    (see _JUNK_LINE_RE) so they don't get rendered on top of the <h1> that
    already carries the title, or shown at all in the case of ad lines."""
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or _is_duplicate_title_line(line, title) or _JUNK_LINE_RE.search(line):
            i += 1
            continue
        break
    return "\n".join(lines[i:]).strip()


def _strip_leading_boilerplate_html(chapter_html: str, title: str) -> str:
    """HTML-aware counterpart of _strip_leading_boilerplate_text — removes
    leading elements (heading or paragraph) whose text is a duplicate title
    or credit/ad line, walking element-by-element rather than line-by-line
    since real HTML chapters aren't plain text.

    Calibre-converted chapters routinely bundle the heading, credit line,
    and every paragraph of a chapter as *siblings inside one wrapper div*
    (sometimes several such wrappers deep). Deciding keep-or-drop on a
    wrapper's own get_text() would concatenate the whole chapter into one
    string — a single credit-line keyword anywhere in it then nukes the
    real content bundled alongside it. So a <div>/<section> whose text
    looks like boilerplate is only decomposed outright if it has no child
    elements of its own (i.e. it really is just one line); otherwise its
    children are scanned the same way, recursively, and the wrapper itself
    is only dropped if that recursion finds nothing but boilerplate too."""
    soup = BeautifulSoup(chapter_html, "lxml")
    body = soup.body or soup

    def strip_leading(container) -> bool:
        """Returns True once real content is found (caller stops there)."""
        for tag in list(container.find_all(recursive=False)):
            tag_text = tag.get_text(strip=True)
            if not tag_text:
                tag.decompose()
                continue
            if _is_duplicate_title_line(tag_text, title) or _JUNK_LINE_RE.search(tag_text):
                if tag.name in ("div", "section") and tag.find(recursive=False):
                    if strip_leading(tag):
                        return True
                tag.decompose()
                continue
            return True
        return False

    strip_leading(body)
    return body.decode_contents().strip()


def _extract_chapters_from_book(book: epub.EpubBook) -> list[Chapter]:
    toc_titles = _toc_titles_by_href(book.toc)

    chapters: list[Chapter] = []
    for idx, (idref, linear) in enumerate(book.spine):
        if linear == "no":
            continue
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ITEM_DOCUMENT:
            continue
        if isinstance(item, epub.EpubNav):
            continue  # auto-generated nav/toc document, not real chapter content

        raw = item.get_content()
        html, text = _clean_html_and_text(raw)
        if not text:
            continue  # skip blank spine entries (e.g. separator pages)

        # _CHAPTER_ALREADY_SPLIT_MARKER lets content we generate ourselves
        # (one physical file per chapter — see build_library.py's re-split
        # tooling) opt out of marker re-scanning below: re-running it on a
        # file that's already exactly one chapter risks an incidental
        # in-body "Chương N" mention (a flashback, a dialogue aside)
        # false-positively fracturing an already-correct chapter in two.
        # Real-world scraped epubs never contain this marker, so this can't
        # change behavior for any book we didn't generate ourselves.
        split = None if _CHAPTER_ALREADY_SPLIT_MARKER in raw else _split_by_chapter_markers(text)
        if split is not None:
            for sub_title, sub_text in split:
                sub_text = _strip_leading_boilerplate_text(sub_text, sub_title)
                chapters.append(Chapter(
                    index=len(chapters),
                    title=sub_title,
                    html=_text_to_html(sub_text),
                    text=sub_text,
                ))
            continue

        title = toc_titles.get(item.get_name()) or _fallback_title(html, len(chapters))
        # The book's title/author/synopsis page has no toc entry and no
        # heading of its own, so it lands here with a generic "Chapter N"
        # placeholder — relabel it instead of letting it masquerade as a
        # real chapter (and collide with the real "Chapter 1" right after).
        if (
            len(chapters) == 0
            and _GENERIC_FALLBACK_TITLE_RE.match(title)
            and not _STARTS_WITH_MARKER_RE.match(text)
        ):
            title = "Giới thiệu"
        html = _strip_leading_boilerplate_html(html, title)
        text = BeautifulSoup(html, "lxml").get_text(separator="\n", strip=True)
        if not text or _normalize_for_compare(text) == _normalize_for_compare(title):
            continue  # blank page in the source (e.g. a scan with no OCR'd body) — nothing to show
        chapters.append(Chapter(index=len(chapters), title=title, html=html, text=text))

    return chapters


def extract_chapters(epub_path: str) -> list[Chapter]:
    """Parse an epub file and return its chapters in spine (reading) order.

    Uses the spine (not just iterating all ITEM_DOCUMENT items) so ordering
    matches how a reader app would actually present the book, and skips
    non-linear items (e.g. footnote/cover pages some epubs mark that way).
    """
    book = epub.read_epub(epub_path)
    return _extract_chapters_from_book(book)


def extract_book(epub_path: str) -> tuple[dict, list[Chapter]]:
    """Like extract_chapters, but also returns {"title", "author"} from epub metadata."""
    book = epub.read_epub(epub_path)
    dc_title = book.get_metadata("DC", "title")
    dc_creator = book.get_metadata("DC", "creator")
    dc_description = book.get_metadata("DC", "description")
    description = dc_description[0][0] if dc_description else ""
    metadata = {
        "title": dc_title[0][0] if dc_title else None,
        "author": dc_creator[0][0] if dc_creator else None,
        "description": BeautifulSoup(description, "lxml").get_text(" ", strip=True) if description else "",
    }
    return metadata, _extract_chapters_from_book(book)


_COVER_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}


def extract_cover(epub_path: str) -> tuple[bytes, str] | None:
    """Return (image_bytes, file_extension) for the epub's embedded cover
    image, or None if it has no properly-flagged cover. Legacy .prc/.mobi
    inputs aren't supported (PalmDOC has no structured cover slot to read
    without a full repack) — callers should just skip the cover for those."""
    book = epub.read_epub(epub_path, options={"ignore_ncx": True})
    cover_item = next((i for i in book.get_items() if isinstance(i, epub.EpubCover)), None)
    if cover_item is None:
        return None
    ext = _COVER_EXTENSIONS.get(cover_item.media_type, "jpg")
    return cover_item.get_content(), ext


def extract_book_from_mobi(path: str) -> tuple[dict, list[Chapter]]:
    """Extract a legacy .prc/.mobi/.azw3 file's chapters.

    calibre's ebook-convert was pathologically slow on some of these (tens
    of minutes, never finished on a 4.6MB file) — traced to the source
    having ~44,000 fragmented <p> tags from an old Word-HTML export, which
    its chapter/structure-detection heuristics choke on. The `mobi` package
    just unpacks the raw PalmDOC/MOBI records with no structure analysis
    (1-3 seconds for the same file), so this reuses epub_parser's own
    (already-fast, already-tested) HTML-cleaning + marker-based chapter
    splitting on the result instead of going through calibre at all.
    """
    import mobi

    book_dir, extracted_path = mobi.extract(path)
    try:
        if extracted_path.endswith(".epub"):
            # AZW3/KF8 unpacks straight to a real epub — reuse the normal path.
            return extract_book(extracted_path)

        raw = Path(extracted_path).read_bytes()
        html, text = _clean_html_and_text(raw)
        title = Path(path).stem
        metadata = {"title": title, "author": None, "description": ""}

        chapters: list[Chapter] = []
        split = _split_by_chapter_markers(text)
        if split is not None:
            for sub_title, sub_text in split:
                sub_text = _strip_leading_boilerplate_text(sub_text, sub_title)
                chapters.append(Chapter(
                    index=len(chapters), title=sub_title, html=_text_to_html(sub_text), text=sub_text,
                ))
        elif text:
            html = _strip_leading_boilerplate_html(html, title)
            text = BeautifulSoup(html, "lxml").get_text(separator="\n", strip=True)
            chapters.append(Chapter(index=0, title=title, html=html, text=text))

        return metadata, chapters
    finally:
        shutil.rmtree(book_dir, ignore_errors=True)
