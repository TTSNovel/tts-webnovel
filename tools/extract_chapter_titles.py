"""Recover real chapter titles for chapters that fell back to a bare
"Chương N" / "Chapter N" placeholder at import time (see epub_parser.py's
_fallback_title) — a lot of source epubs put the real title as running text
right at the start of the chapter body instead of in a <h1>/toc entry, so
it's still recoverable even though the importer missed it.

Patterns handled (each novel's scraped source formats this differently, see
tools/extract_chapter_titles.py's caller for the pilot-book writeup):
  - "<h1>Chương 5</h1><p>Chia tài sản</p><p>Chương 5: Chia tài sản</p>..."
    -> a clean standalone title paragraph, confirmed by a duplicate
       "Chương N: <title>" line right after it.
  - "<h1>Chương 606</h1><p>&lt;Chương 606: Tập 11 – ...&gt;</p>..."
    -> title-only line (no separate clean paragraph), sometimes wrapped in
       bracket punctuation ("<...>", "[...]") that must be stripped.
  - A leading <a> with a non-#-anchor href gets decomposed by
    _clean_html_and_text's cross-document-link stripping, and when that
    anchor wrapped only the first few letters of "Chương" (some scraped
    sources hyperlink mid-word for a footnote/anchor target), the result is
    a mangled prefix like "ng 40:" or "ơng 47:" — the text AFTER the
    chapter number is untouched, so the marker regex keys off the number,
    not the (possibly-mangled) word before it.
  - "<h1>Chương 57</h1><p>Hẹn gặp Từ Hàm Lan</p><p>Hẹn gặp Từ Hàm Lan</p>..."
    -> no number anywhere, just the same short line twice in a row right at
       the top (a different scrape-artifact than the "Chương N: ..." pair
       above, but the same underlying idea: the title got duplicated
       instead of structured). Caught as a fallback when no numbered
       marker matches — two identical short paragraphs among the first few,
       neither of which is itself just "Chương N".

A residual few chapters per book genuinely have no title anywhere in their
own text (verified by reading them, not just failing to match a pattern) —
those are left untouched rather than guessed at.

Usage: python3 tools/extract_chapter_titles.py <site_dir> <book_id> [--apply]
Without --apply, only prints the old -> new mapping (dry run).
"""
import html
import json
import re
import sys
from pathlib import Path

GENERIC_TITLE_RE = re.compile(r"^(Chương|Chapter)\s*(\d+)$", re.IGNORECASE)
H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL | re.IGNORECASE)
P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL | re.IGNORECASE)

_WRAP_PAIRS = {"<": ">", "[": "]", "【": "】", "《": "》", "「": "」"}


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _normalize(s: str) -> str:
    # Trailing punctuation is where a "duplicate" title line most often
    # differs from its twin (one copy ends with a period the other lacks,
    # see "Cùng Ngọc Thuần cắm trại." vs "...cắm trại") -- stripped so that
    # still counts as the same line rather than missing the match.
    return re.sub(r"[.!?…\"'”“]+$", "", re.sub(r"\s+", " ", s).strip().lower())


# A line starting with a quote/dash is dialogue, not a title -- scraped
# sources never open a chapter title with either.
_DIALOGUE_START_RE = re.compile(r'^[\-–—"\'“‘「]')


def _strip_wrap(s: str) -> str:
    while True:
        if len(s) >= 4 and s.startswith("**") and s.endswith("**"):
            s = s[2:-2].strip()
            continue
        if s and s[0] in _WRAP_PAIRS and s[-1:] == _WRAP_PAIRS[s[0]]:
            s = s[1:-1].strip()
            continue
        break
    return s


def _looks_truncated(title: str) -> bool:
    """Catches the same source-side corruption epub_parser.py's docstring
    calls out (a leading <a> wrapping only the first few letters of a title
    line, decomposed as a cross-document link, leaves the rest dangling —
    "Chương 40: ..." becomes "ng 40:", with literally nothing after the
    colon). A truncated-but-substantive fragment ("rinh nữ đồng ruộng 2",
    missing its leading "T") is still worth keeping as-is — a typo'd real
    title beats the generic "Chương N" placeholder — so this only screens
    out the *empty* case, not every truncation."""
    return not title or title.rstrip().endswith(":")


_ANY_MARKER_RE = re.compile(r"^\S{0,8}\s*\d+\s*[:.\-]\s*(.+)$")
_HAS_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_SENTENCE_END_RE = re.compile(r"^.{1,70}?[.!?…]", re.DOTALL)
_EXCERPT_MAX_LEN = 70


_LEADING_BRACKET_RE = re.compile(r"^[<\[【《「][^<>\[\]【】《》「」]{0,100}[>\]】》」]\s*")
_LEADING_PUNCT_RE = re.compile(r'^[\s.…\-–—"\'"“”‘’~*]+')


def _clean_excerpt_candidate(p: str) -> str:
    """A candidate paragraph can carry the same marker/bracket noise the
    other branches strip -- e.g. book 56's "< Chương 572: Tập 4- Con Đường
    Chưa Đi (1) > 「Chúng ta...", where the bracket only wraps the FIRST
    part of the line (not the whole paragraph, so _strip_wrap's whole-string
    check doesn't fire) and real prose follows it. Peeled off here instead
    of trusting the raw paragraph text directly."""
    p = _LEADING_BRACKET_RE.sub("", p.strip())
    p = _strip_wrap(p)
    m = _ANY_MARKER_RE.match(p)
    if m and m.group(1).strip():
        p = m.group(1).strip()
    return _LEADING_PUNCT_RE.sub("", p).strip()


def _shorten(p: str) -> str:
    m = _SENTENCE_END_RE.match(p)
    if m:
        return m.group(0).strip()
    if len(p) <= _EXCERPT_MAX_LEN:
        return p
    cut = p[:_EXCERPT_MAX_LEN].rsplit(" ", 1)[0].strip()
    return (cut or p[:_EXCERPT_MAX_LEN]) + "…"


def _excerpt_header(paragraphs: list[str]) -> str | None:
    """Absolute fallback once every pattern-based branch has failed: the
    chapter has no title anywhere, embedded or not, so there is nothing
    left to "extract" -- only something reasonable to show in its place.
    Once a book has no more truly-empty chapters (see process_book's
    caller), leaving the generic "Chương N" placeholder is no longer an
    acceptable end state, so the opening line itself becomes the header
    (trimmed to the first sentence, or word-boundary-truncated if that
    single sentence still runs long) rather than nothing at all. Doesn't
    consume the paragraph -- it stays in the body as real story content,
    this is just reusing its text for the header too."""
    candidates = [_clean_excerpt_candidate(p) for p in paragraphs]
    candidates = [p for p in candidates if p and _HAS_LETTER_RE.search(p) and not _looks_truncated(p)]
    if not candidates:
        return None
    # A line of dialogue ("anh dung co noi kieu day.") reads as a worse
    # header than plain narration -- prefer the first non-dialogue line
    # among the opening few, but don't insist on one if the chapter opens
    # in dialogue throughout (some do).
    # Preference order, not an exclusive shortlist -- a junk fragment
    # ("zm", a page-break artifact in book 21) can itself be non-dialogue
    # and so wrongly monopolize a plain non_dialogue-or-nothing shortlist,
    # starving out the real (dialogue-opening) content right behind it.
    # Trying every candidate, just non-dialogue-first, means a fragment
    # that fails the quality bar below still falls through to the rest.
    non_dialogue = [p for p in candidates[:5] if not _DIALOGUE_START_RE.match(p)]
    ordered = non_dialogue + [p for p in candidates if p not in non_dialogue]

    for p in ordered:
        excerpt = _shorten(p)
        # A page-break/OCR artifact can leave a lone word-fragment as its
        # own paragraph ("ịm", "al." -- seen in book 21, a mangled epub
        # conversion) which passes every check above but reads as
        # meaningless on its own; skip to the next candidate rather than
        # using it, same reasoning as the punctuation-only case below.
        if excerpt and _HAS_LETTER_RE.search(excerpt) and (len(excerpt) >= 8 or " " in excerpt):
            return excerpt
    # Nothing cleared that bar -- still return the best candidate available
    # rather than nothing at all (see this function's docstring on why
    # "Chương N" is no longer an acceptable final state).
    return ordered[0]


def _extract_from_paragraphs(
    paragraphs: list[str], n: int, allow_last_resort: bool = True
) -> tuple[str | None, int | None]:
    """Returns (new_title, number_of_leading_paragraphs_to_drop) or (None, None).

    allow_last_resort=False restricts this to the marker/duplicate branches
    only (see process_book: whether a book's source embeds titles as
    running text at all has to be established from ITS OWN confirmed
    matches first — the last-resort branch below has no such confirmation
    of its own, so on a book that turns out to have no such convention it
    just grabs arbitrary short prose ("tiền vừa rồi.", a mid-sentence
    fragment) or a stray sound-effect line ("~~\"") as if it were a title."""
    # Bracket-wrapped marker lines ("<Chương 606: ...>") need the wrap gone
    # before any of the checks below can see the number/colon inside it, so
    # this is done once up front rather than piecemeal per branch.
    paragraphs = [_strip_wrap(p) for p in paragraphs]

    marker_re = re.compile(r"^\S{0,8}\s*" + str(n) + r"\s*[:.\-]\s*(.+)$")
    for i, p in enumerate(paragraphs[:8]):
        m = marker_re.match(p)
        if not (m and m.group(1).strip()):
            continue
        marker_title = m.group(1).strip()
        # marker_title is everything AFTER the number+separator, which the
        # source-side truncation bug never reaches (it only ever eats
        # characters before the number) -- no _looks_truncated check needed
        # here, unlike the last-resort branch below.
        if len(marker_title) > 120:
            return None, None
        for j in range(i):
            if paragraphs[j].strip() and _normalize(paragraphs[j]) == _normalize(marker_title):
                return paragraphs[j].strip(), i + 1  # clean paragraph is the title; still drop both lines
        return marker_title, i + 1

    # Some sources embed a SECOND "Chương N: ..." line whose N is off by one
    # from the chapter's real position (a translator/source numbering slip,
    # not a data error on our end) -- still a genuine marker line, just not
    # keyed to the expected n, so match on ANY number rather than dropping
    # it to the duplicate/last-resort branches below (which would otherwise
    # keep the "Chương 556: " prefix baked into the title verbatim).
    for i, p in enumerate(paragraphs[:2]):
        m = _ANY_MARKER_RE.match(p)
        if m and m.group(1).strip() and len(m.group(1).strip()) <= 120:
            return m.group(1).strip(), i + 1

    # Fallback: no "Chương N: ..." marker anywhere, but the same short line
    # appears twice back-to-back right at the top -- some sources duplicate
    # the title paragraph itself instead of pairing it with a numbered
    # restatement (see "Chương 57" example in this file's docstring).
    # (Also no _looks_truncated check here -- two independent copies of the
    # same corrupted prefix normalizing equal would be a wild coincidence,
    # and rejecting on that basis would only throw away genuine matches
    # like "đánh mạt chược" that just happen to start lowercase in the
    # source's own styling.)
    for i in range(min(3, len(paragraphs) - 1)):
        a, b = paragraphs[i].strip(), paragraphs[i + 1].strip()
        if (
            a
            and b
            and _normalize(a) == _normalize(b)
            and len(a) <= 100
            and not GENERIC_TITLE_RE.match(a)
            and _HAS_LETTER_RE.search(a)
        ):
            return a, i + 2

    if not allow_last_resort:
        return None, None

    # Last resort: a short, non-dialogue first paragraph immediately
    # followed by a much longer one reads as "title, then the chapter's
    # actual prose" even with no number and no duplicate to confirm it
    # (e.g. "Dã chiến tiểu xử nữ" followed by a full narrative paragraph).
    # Only reached when process_book has already confirmed (via the
    # branches above, across the whole book) that this source really does
    # embed titles as running text -- see allow_last_resort's docstring.
    if len(paragraphs) >= 2:
        a, b = paragraphs[0].strip(), paragraphs[1].strip()
        if (
            a
            and len(a) <= 40
            and len(b) >= 60
            and not GENERIC_TITLE_RE.match(a)
            and not _DIALOGUE_START_RE.match(a)
            and not _looks_truncated(a)
            and _HAS_LETTER_RE.search(a)
            # A lone word-fragment ("zm", "al." -- a page-break/OCR
            # artifact, see book 21) clears every check above just as
            # easily as a real short title does; requiring some actual
            # heft (multiple words, or long enough to be one on its own)
            # is what tells the two apart.
            and (len(a) >= 8 or " " in a)
        ):
            return a, 1
    return None, None


def extract_title(
    fragment_html: str, n: int, allow_last_resort: bool = True, guarantee: bool = False
) -> tuple[str | None, str | None]:
    """Returns (new_title, new_fragment_html_with_marker_lines_dropped) or (None, None).

    guarantee=True adds _excerpt_header as a final, unconditional fallback
    -- unlike allow_last_resort (gated at the book level, see process_book),
    this applies per-chapter regardless: it isn't claiming the text IS a
    title, just that some header beats none."""
    body = H1_RE.sub("", fragment_html, count=1).strip()
    raw_paragraphs = P_RE.findall(body)
    paragraphs = [_strip_tags(p) for p in raw_paragraphs]

    new_title, drop_count = (
        _extract_from_paragraphs(paragraphs, n, allow_last_resort) if paragraphs else (None, None)
    )
    if new_title is None and guarantee:
        # Falls back to the whole body as one extra candidate, appended
        # after the real paragraphs (only reached if none of those had
        # anything usable) -- covers both "no <p> tags at all" (a plain-
        # text import with a single continuous blob, seen in book 109) and
        # "<p> tags present but empty/whitespace-only, with the actual
        # prose sitting unwrapped outside them" (also book 109 -- a couple
        # of "&nbsp;"-only <p> spacers with real text before/after them).
        # Either way there's no paragraph boundary to safely bound a
        # marker/duplicate match to, so only the excerpt fallback -- which
        # only reads the opening sentence and never touches the body -- is
        # attempted on it.
        excerpt = _excerpt_header(paragraphs + ([_strip_tags(body)] if body else []))
        if excerpt is not None:
            new_body = "\n".join(f"<p>{p}</p>" for p in raw_paragraphs) if raw_paragraphs else body
            return excerpt, new_body
    if new_title is None:
        return None, None
    # Drop every leading paragraph that was just restating the title (not
    # story content) -- mirrors epub_parser.py's _strip_leading_boilerplate_html,
    # applied post-hoc here.
    remaining = raw_paragraphs[drop_count:]
    new_body = "\n".join(f"<p>{p}</p>" for p in remaining)
    return new_title, new_body


# Below this many book-wide confirmed matches, there's no real evidence
# the source embeds titles as running text at all -- letting the
# last-resort branch guess anyway is how "tiền vừa rồi." (a mid-sentence
# fragment) and "~~\"" (a sound effect) got mistaken for chapter titles in
# a book that, on inspection, just doesn't have per-chapter titles in its
# text. Tuned to the smallest number that still clears random short-line
# coincidences across a whole book.
_LAST_RESORT_CONFIRMATION_THRESHOLD = 5


def process_book(site_dir: Path, book_id: int, apply: bool) -> None:
    book_dir = site_dir / "books" / str(book_id)
    titles_path = book_dir / "titles.json"
    index_path = book_dir / "index.html"
    data_dir = book_dir / "data"

    titles = json.loads(titles_path.read_text(encoding="utf-8"))
    index_html = index_path.read_text(encoding="utf-8")

    generic = []  # (idx, old_title, n, fragment)
    for idx, old_title in enumerate(titles):
        m = GENERIC_TITLE_RE.match(old_title.strip())
        if not m:
            continue
        n = int(m.group(2))
        fragment = (data_dir / f"{idx:04d}.html").read_text(encoding="utf-8")
        generic.append((idx, old_title, n, fragment))

    # Pass 1: confirmed branches only, book-wide -- establishes whether this
    # source embeds titles as running text at all before the last-resort
    # branch is allowed to guess on anything (see its threshold above).
    confirmed = {idx: extract_title(frag, n, allow_last_resort=False) for idx, _, n, frag in generic}
    allow_last_resort = sum(1 for t, _ in confirmed.values() if t) >= _LAST_RESORT_CONFIRMATION_THRESHOLD

    changes = []  # (chapter_index, old_title, new_title)
    for idx, old_title, n, fragment in generic:
        new_title, new_body = confirmed[idx]
        if new_title is None and allow_last_resort:
            new_title, new_body = extract_title(fragment, n)
        if new_title is None:
            # No pattern matched at all -- with empty chapters already
            # cleared out elsewhere, every remaining chapter has real
            # content, so an excerpt-of-the-opening-line header is used
            # rather than leaving the generic placeholder in place.
            new_title, new_body = extract_title(fragment, n, allow_last_resort=False, guarantee=True)
        if new_title is None:
            continue

        frag_path = data_dir / f"{idx:04d}.html"
        changes.append((idx, old_title, new_title))
        if not apply:
            continue

        frag_path.write_text(f"<h1>{html.escape(new_title)}</h1>\n{new_body}", encoding="utf-8")
        titles[idx] = new_title
        index_html = index_html.replace(
            f'<a href="chapters/{idx:04d}.html"><span class="num">{idx + 1}</span>'
            f'<span class="title">{html.escape(old_title)}</span></a>',
            f'<a href="chapters/{idx:04d}.html"><span class="num">{idx + 1}</span>'
            f'<span class="title">{html.escape(new_title)}</span></a>',
        )

    for idx, old_title, new_title in changes:
        print(f"  [{idx:04d}] {old_title!r} -> {new_title!r}")
    print(f"\n{'Applied' if apply else 'Would change'} {len(changes)} chapter title(s) in book {book_id}.")

    if apply and changes:
        titles_path.write_text(json.dumps(titles, ensure_ascii=False), encoding="utf-8")
        index_path.write_text(index_html, encoding="utf-8")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--apply"]
    if len(args) != 2:
        print(__doc__)
        sys.exit(1)
    process_book(Path(args[0]), int(args[1]), apply="--apply" in sys.argv)
