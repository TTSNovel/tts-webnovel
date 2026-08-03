"""Extract an .epub file into ordered, plain-HTML chapters.

Deliberately does NOT use epub.js-style iframe rendering — the output here
is meant to be inserted directly into a normal page DOM (or served as a
plain chapter page), so the existing extension's read-aloud logic (which
walks page text nodes) can read it exactly like a web novel page.
"""
from __future__ import annotations

from dataclasses import dataclass

from bs4 import BeautifulSoup
from ebooklib import epub, ITEM_DOCUMENT


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

        title = toc_titles.get(item.get_name()) or _fallback_title(html, len(chapters))
        chapters.append(Chapter(index=len(chapters), title=title, html=html, text=text))

    return chapters
