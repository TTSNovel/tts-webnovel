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
from dataclasses import dataclass

from bs4 import BeautifulSoup
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
_CHAPTER_MARKER_RE = re.compile(r"^[ \t]*((?:Chương|Chapter)\s*\d+[^\n]{0,80})", re.IGNORECASE | re.MULTILINE)


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
        sub_chapters.append((title, body_text))
    return sub_chapters


def _text_to_html(text: str) -> str:
    paragraphs = [line.strip() for line in text.split("\n") if line.strip()]
    return "\n".join(f"<p>{html_module.escape(p)}</p>" for p in paragraphs)


def extract_chapters(epub_path: str) -> list[Chapter]:
    """Parse an epub file and return its chapters in spine (reading) order.

    Uses the spine (not just iterating all ITEM_DOCUMENT items) so ordering
    matches how a reader app would actually present the book, and skips
    non-linear items (e.g. footnote/cover pages some epubs mark that way).
    """
    book = epub.read_epub(epub_path)
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

        html, text = _clean_html_and_text(item.get_content())
        if not text:
            continue  # skip blank spine entries (e.g. separator pages)

        split = _split_by_chapter_markers(text)
        if split is not None:
            for sub_title, sub_text in split:
                chapters.append(Chapter(
                    index=len(chapters),
                    title=sub_title,
                    html=_text_to_html(sub_text),
                    text=sub_text,
                ))
            continue

        title = toc_titles.get(item.get_name()) or _fallback_title(html, len(chapters))
        chapters.append(Chapter(index=len(chapters), title=title, html=html, text=text))

    return chapters
