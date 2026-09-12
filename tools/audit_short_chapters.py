"""Flag chapters that look like epub-splitting mistakes rather than real
chapter content, and write one Markdown review doc per novel.

Three shapes show up in practice (found by manual inspection before writing
this tool — see the three example books referenced in comments below):

  TOC_DUMP    — a table-of-contents/"Contents" page that got kept as its own
                spine item and survived _clean_html_and_text's link-stripping
                because its entries don't match _CHAPTER_MARKER_RE (no
                "Chương N"/"Chapter N" prefix), so the link text stayed
                behind as "chapter" body. e.g. "Defiance of the Fall": a
                chapter titled "Contents" whose entire body is "1.\n2.\n3...".

  DIVIDER     — some epubs (esp. "Defiance of the Fall") ship one physical
                XHTML file per chapter *plus* a tiny "part divider" file
                right before it whose body is just the chapter number
                ("1"). Both get pulled in as separate chapters. The divider
                carries zero content the next chapter doesn't already have.

  MISSING_IN_SOURCE — the chapter's rendered body is short, and checking the
                underlying epub spine file confirms the raw source is
                *also* that short (not an artifact of our own boilerplate
                stripping) — e.g. a scraped chapter whose entire raw file is
                "Team: X\nNguồn: Y" with the actual story text never
                scraped. Nothing to recover from this epub.

  SHORT_BUT_REAL — short, but reads like real (if terse) chapter content —
                left for a human to confirm, not auto-classified as junk.

This tool only reads epubs and writes review docs — it never touches the
deployed site or deletes anything.

Usage: python3 tools/audit_short_chapters.py <epub_dir> [-o out_dir]
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import epub_parser as ep  # noqa: E402
from ebooklib import epub, ITEM_DOCUMENT  # noqa: E402

# A candidate worth a human look: either very few lines, or a short overall
# body. Real short-but-legit chapters (a one-paragraph interlude) still
# clear this bar sometimes, hence the four-way classification below rather
# than a hard cut.
_MAX_CANDIDATE_LINES = 4
_MAX_CANDIDATE_CHARS = 400

_TOC_TITLE_RE = re.compile(r"mục\s*lục|table of contents|^contents$|danh\s*sách\s*chương|^index$", re.IGNORECASE)
# Front/back-matter pages: real content of the epub, but never story prose —
# safe to drop without losing any chapter, unlike MISSING_IN_SOURCE (which
# means a *story* chapter's text never made it into the epub).
_FRONT_MATTER_TITLE_RE = re.compile(
    r"^(also in series|about the author|acknowledg|dedication|copyright|"
    r"disclaimer|author'?s note|translator'?s note|lời tựa|lời cảm ơn|"
    r"lời giới thiệu của (?:tác giả|dịch giả))",
    re.IGNORECASE,
)
# A "list-like" line: a bare number/roman numeral optionally followed by a
# dot, or a short "N. Title" line — the shape of a TOC entry once its <a>
# link text survives (see TOC_DUMP above).
_LIST_LINE_RE = re.compile(r"^\s*\d{1,4}\.?\s*.{0,60}$")
_BARE_NUMBER_RE = re.compile(r"^\s*[\divxlcIVXLC]{1,6}\.?\s*$")

# epub_parser's own credit-line detector covers the common Vietnamese
# scanlation-credit phrasing (nhóm dịch, dịch giả, biên tập, sưu tầm, ...);
# extend it with a couple of English-source and generic "Team:"/"Source:"
# forms seen in the corpus that epub_parser doesn't need to know about
# (it's tuned for Vietnamese-source boilerplate, not every language).
_EXTRA_JUNK_HINT_RE = re.compile(
    r"team\s*[:：]|source\s*[:：]|translat|proofread|chương trước|chương sau",
    re.IGNORECASE,
)


def _looks_like_junk_only(text: str) -> bool:
    lines = _nonempty_lines(text)
    if not lines:
        return False
    return all(ep._JUNK_LINE_RE.search(ln) or _EXTRA_JUNK_HINT_RE.search(ln) for ln in lines)


@dataclass
class RawEvidence:
    source_kind: str  # "single_file" | "marker_split"
    spine_name: str = ""
    raw_len: int = 0
    raw_preview: str = ""


@dataclass
class Candidate:
    chapter: "ep.Chapter"
    line_count: int
    char_count: int
    prev_title: str | None
    next_title: str | None
    raw: RawEvidence
    verdict: str = ""
    reason: str = ""


def _nonempty_lines(text: str) -> list[str]:
    return [ln for ln in text.split("\n") if ln.strip()]


def _looks_like_toc_dump_body(text: str) -> bool:
    lines = _nonempty_lines(text)
    if not lines:
        return False
    list_like = sum(1 for ln in lines if _LIST_LINE_RE.match(ln))
    return list_like / len(lines) > 0.6 and len(lines) >= 3


def _looks_like_bare_number(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and bool(_BARE_NUMBER_RE.match(stripped)) and len(stripped) <= 6


def _title_overlaps(a: str, b: str) -> bool:
    """True if two chapter titles look like they name the same chapter
    (divider title "1. Roll for Survival" vs content title "Roll for
    Survival") — normalize both and check substring either direction."""
    norm_a = re.sub(r"^\s*\d+[\.\):]?\s*", "", a).strip().lower()
    norm_b = re.sub(r"^\s*\d+[\.\):]?\s*", "", b).strip().lower()
    if not norm_a or not norm_b:
        return False
    return norm_a == norm_b or norm_a in norm_b or norm_b in norm_a


def _extract_with_raw_evidence(epub_path: str) -> tuple[dict, list[ep.Chapter], dict[int, RawEvidence]]:
    """Re-implements _extract_chapters_from_book's traversal so each final
    Chapter can be paired with the raw (pre-cleaning) evidence for the
    physical epub source it came from — needed to tell "epub itself is this
    short" apart from "our own cleanup ate the content"."""
    book = epub.read_epub(epub_path)
    toc_titles = ep._toc_titles_by_href(book.toc)

    dc_title = book.get_metadata("DC", "title")
    dc_creator = book.get_metadata("DC", "creator")
    metadata = {
        "title": dc_title[0][0] if dc_title else None,
        "author": dc_creator[0][0] if dc_creator else None,
    }

    chapters: list[ep.Chapter] = []
    evidence: dict[int, RawEvidence] = {}

    for idref, linear in book.spine:
        if linear == "no":
            continue
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ITEM_DOCUMENT:
            continue
        if isinstance(item, epub.EpubNav):
            continue

        raw = item.get_content()
        html, text = ep._clean_html_and_text(raw)
        if not text:
            continue

        split = None if ep._CHAPTER_ALREADY_SPLIT_MARKER in raw else ep._split_by_chapter_markers(text)
        if split is not None:
            for sub_title, sub_text_raw in split:
                sub_text = ep._strip_leading_boilerplate_text(sub_text_raw, sub_title)
                idx = len(chapters)
                chapters.append(ep.Chapter(
                    index=idx, title=sub_title, html=ep._text_to_html(sub_text), text=sub_text,
                ))
                evidence[idx] = RawEvidence(
                    source_kind="marker_split",
                    spine_name=item.get_name(),
                    raw_len=len(sub_text_raw),
                    raw_preview=sub_text_raw[:300],
                )
            continue

        title = toc_titles.get(item.get_name()) or ep._fallback_title(html, len(chapters))
        if (
            len(chapters) == 0
            and ep._GENERIC_FALLBACK_TITLE_RE.match(title)
            and not ep._STARTS_WITH_MARKER_RE.match(text)
        ):
            title = "Giới thiệu"
        stripped_html = ep._strip_leading_boilerplate_html(html, title)
        from bs4 import BeautifulSoup
        stripped_text = BeautifulSoup(stripped_html, "lxml").get_text(separator="\n", strip=True)
        if not stripped_text or ep._normalize_for_compare(stripped_text) == ep._normalize_for_compare(title):
            continue
        idx = len(chapters)
        chapters.append(ep.Chapter(index=idx, title=title, html=stripped_html, text=stripped_text))
        evidence[idx] = RawEvidence(
            source_kind="single_file",
            spine_name=item.get_name(),
            # length of the CLEANED pre-boilerplate-stripping text, not the
            # raw XHTML byte count — the raw bytes include a near-constant
            # ~250-400 bytes of DOCTYPE/xmlns/head markup regardless of how
            # much actual content the file has, which would make every
            # short chapter look like it lost content to our own stripping.
            raw_len=len(text),
            raw_preview=text[:300],
        )

    return metadata, chapters, evidence


def _classify(cand: Candidate) -> None:
    text = cand.chapter.text
    title = cand.chapter.title

    if _TOC_TITLE_RE.search(title) or _looks_like_toc_dump_body(text):
        cand.verdict = "TOC_DUMP"
        cand.reason = "Title/body reads like a table-of-contents listing, not chapter prose."
        return

    if _FRONT_MATTER_TITLE_RE.search(title):
        cand.verdict = "FRONT_MATTER"
        cand.reason = "Title matches known front/back-matter boilerplate (ad page, copyright, dedication, ...) — not a story chapter, no content is actually missing."
        return

    if _looks_like_bare_number(text) and cand.next_title and _title_overlaps(title, cand.next_title):
        cand.verdict = "DIVIDER"
        cand.reason = (
            f"Body is just a bare number/marker, and the next chapter "
            f"({cand.next_title!r}) is clearly the same chapter's real content."
        )
        return
    if _looks_like_bare_number(text) and cand.prev_title and _title_overlaps(title, cand.prev_title):
        cand.verdict = "DIVIDER"
        cand.reason = (
            f"Body is just a bare number/marker, and the previous chapter "
            f"({cand.prev_title!r}) is clearly the same chapter's real content."
        )
        return

    raw = cand.raw
    raw_is_also_short = raw.raw_len > 0 and raw.raw_len < max(300, cand.char_count * 3)
    if raw_is_also_short:
        # line_count alone is not a safe "no real sentence here" signal
        # (see the comment in audit_book about single-blob chapters), so
        # this shortcut also requires the body to be tiny in absolute
        # terms — a real one-sentence chapter almost always runs longer
        # than this in practice.
        if _looks_like_junk_only(text) or (cand.line_count <= 2 and cand.char_count <= 80):
            cand.verdict = "MISSING_IN_SOURCE"
            cand.reason = (
                "Raw epub source file itself is this short "
                f"({raw.raw_len} bytes) — content was never in the epub, this "
                "isn't a splitting bug. Dropping leaves a numbering gap."
            )
        else:
            cand.verdict = "SHORT_BUT_REAL"
            cand.reason = "Short, but source confirms this is genuinely how long the chapter is — looks like real content."
        return

    cand.verdict = "NEEDS_PARSER_FIX"
    cand.reason = (
        f"Raw source ({raw.raw_len} chars) is much longer than the final chapter "
        f"text ({cand.char_count} chars) — our own boilerplate-stripping may be "
        "eating real content here, not an epub problem."
    )


def audit_book(epub_path: str) -> tuple[dict, list[Candidate]]:
    metadata, chapters, evidence = _extract_with_raw_evidence(epub_path)
    candidates: list[Candidate] = []
    for ch in chapters:
        lines = _nonempty_lines(ch.text)
        # char_count is the only reliable "is this short" signal on its
        # own — line count is NOT: some source epubs render an entire
        # 10,000-char chapter as one unbroken text blob with no <p>
        # breaks, which get_text() collapses to a single "line". Using
        # line count as an independent OR trigger here previously flagged
        # full real chapters as one-line stubs. Line count is still useful
        # *within* an already-short candidate (e.g. telling a bare-number
        # divider apart from a one-paragraph micro-chapter).
        is_short = len(ch.text) <= _MAX_CANDIDATE_CHARS
        # A TOC dump can run to 50+ short list lines (hundreds of chars
        # total) — well past the short-chapter threshold above — so it
        # needs its own unconditional check, not gated on being "short".
        is_toc_shaped = _TOC_TITLE_RE.search(ch.title) or _looks_like_toc_dump_body(ch.text)
        if not (is_short or is_toc_shaped):
            continue
        prev_title = chapters[ch.index - 1].title if ch.index > 0 else None
        next_title = chapters[ch.index + 1].title if ch.index + 1 < len(chapters) else None
        cand = Candidate(
            chapter=ch,
            line_count=len(lines),
            char_count=len(ch.text),
            prev_title=prev_title,
            next_title=next_title,
            raw=evidence.get(ch.index, RawEvidence(source_kind="unknown")),
        )
        _classify(cand)
        candidates.append(cand)
    return metadata, candidates


_VERDICT_ORDER = ["TOC_DUMP", "DIVIDER", "FRONT_MATTER", "MISSING_IN_SOURCE", "NEEDS_PARSER_FIX", "SHORT_BUT_REAL"]
_VERDICT_LABEL = {
    "TOC_DUMP": "Mục lục lẫn vào làm chapter — nên xoá",
    "DIVIDER": "Chapter chia (tách) sai, trùng nội dung chapter kế — nên xoá",
    "FRONT_MATTER": "Trang bìa/quảng cáo/bản quyền, không phải nội dung truyện — an toàn để xoá",
    "MISSING_IN_SOURCE": "Thiếu nội dung ngay trong epub gốc — cần quyết định (xoá sẽ hụt số chương)",
    "NEEDS_PARSER_FIX": "Nghi ngờ lỗi khi tách/parse — nên sửa parser, KHÔNG xoá vội",
    "SHORT_BUT_REAL": "Ngắn nhưng có vẻ là nội dung thật — nên giữ",
}


def render_report(book_name: str, metadata: dict, candidates: list[Candidate]) -> str:
    total = len(candidates)
    counts = {v: sum(1 for c in candidates if c.verdict == v) for v in _VERDICT_ORDER}
    lines = [
        f"# {metadata.get('title') or book_name}",
        "",
        f"- File nguồn: `{book_name}`",
        f"- Tác giả: {metadata.get('author') or '(không rõ)'}",
        f"- Số chapter bị gắn cờ: {total}",
        "",
        "| Nhóm | Số lượng | Ý nghĩa |",
        "|---|---|---|",
    ]
    for v in _VERDICT_ORDER:
        if counts[v]:
            lines.append(f"| {v} | {counts[v]} | {_VERDICT_LABEL[v]} |")
    lines.append("")
    lines.append("---")

    for v in _VERDICT_ORDER:
        group = [c for c in candidates if c.verdict == v]
        if not group:
            continue
        lines.append(f"\n## {v} — {_VERDICT_LABEL[v]} ({len(group)})\n")
        for c in group:
            ch = c.chapter
            lines.append(f"### [{ch.index}] {ch.title}")
            lines.append("")
            lines.append(f"- Dòng: {c.line_count} · Ký tự: {c.char_count}")
            lines.append(f"- Chapter trước: {c.prev_title!r}")
            lines.append(f"- Chapter sau: {c.next_title!r}")
            lines.append(
                f"- Nguồn epub: `{c.raw.spine_name}` "
                f"({'file riêng' if c.raw.source_kind == 'single_file' else 'tách từ marker trong 1 file lớn'}), "
                f"raw dài {c.raw.raw_len} ký tự/byte"
            )
            lines.append(f"- Lý do: {c.reason}")
            lines.append("- Nội dung hiện tại:")
            lines.append("```")
            lines.append(ch.text[:600])
            lines.append("```")
            if c.raw.raw_preview and c.raw.raw_preview.strip() != ch.text[:300].strip():
                lines.append("- Nội dung thô trong epub gốc (trước khi lọc boilerplate):")
                lines.append("```")
                lines.append(c.raw.raw_preview)
                lines.append("```")
            lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("epub_dir")
    parser.add_argument("-o", "--out-dir", default="output/chapter_audit")
    parser.add_argument("--min-chars-summary", type=int, default=0)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(glob.glob(os.path.join(args.epub_dir, "*.epub")))
    print(f"{len(files)} epub files found in {args.epub_dir}")

    summary_rows = []
    for f in files:
        book_name = os.path.basename(f)
        try:
            metadata, candidates = audit_book(f)
        except Exception as e:
            print(f"  ERROR {book_name}: {e}")
            summary_rows.append((book_name, "ERROR", str(e)))
            continue
        if not candidates:
            summary_rows.append((book_name, 0, ""))
            continue
        report = render_report(book_name, metadata, candidates)
        slug = re.sub(r"[^\w\s-]", "", (metadata.get("title") or book_name), flags=re.UNICODE).strip()
        slug = re.sub(r"[\s_-]+", "_", slug)[:80] or "book"
        out_path = out_dir / f"{slug}.md"
        out_path.write_text(report, encoding="utf-8")
        counts = {v: sum(1 for c in candidates if c.verdict == v) for v in _VERDICT_ORDER}
        summary_rows.append((book_name, len(candidates), counts))
        print(f"  [{len(candidates):4d} flagged] {book_name} -> {out_path}")

    summary_lines = ["# Tổng hợp audit chapter theo novel", ""]
    summary_lines.append("| Novel | Tổng cờ | " + " | ".join(_VERDICT_ORDER) + " |")
    summary_lines.append("|---|---|" + "---|" * len(_VERDICT_ORDER))
    for book_name, total, counts in summary_rows:
        if total in (0, "ERROR") or not isinstance(counts, dict):
            continue
        row = " | ".join(str(counts.get(v, 0)) for v in _VERDICT_ORDER)
        summary_lines.append(f"| {book_name} | {total} | {row} |")
    (out_dir / "_summary.md").write_text("\n".join(summary_lines), encoding="utf-8")
    print(f"\nWrote summary to {out_dir / '_summary.md'}")


if __name__ == "__main__":
    main()
