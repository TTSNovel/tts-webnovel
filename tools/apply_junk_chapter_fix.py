"""Stage the fixed chapter lists for books whose deployed copy actually
changes under the new epub_parser filter (DIVIDER / TOC_DUMP / FRONT_MATTER
/ MISSING_IN_SOURCE chapters dropped — see app/epub_parser.py's
"Whole-chapter junk detection" section).

This script only WRITES to a local staging directory and prints the exact
gsutil commands that would sync it to the live site — it never touches the
GCS bucket itself. Run the printed commands yourself (or re-invoke with
--apply) once you've reviewed the plan.

Safety: a book is only staged if the *current* local epub, run through the
OLD (unfixed) parser, reproduces the exact chapter count the live site
already reports for that book id. That's the only way to be sure the local
epub file actually is the one the live book was built from — several local
files in this corpus are stale/duplicate copies of a title that no longer
match what's deployed, and staging from the wrong file would silently
corrupt a live book. Any book that fails this check is skipped and listed
under "SKIPPED (unsafe)" rather than guessed at.

Usage:
    python3 tools/apply_junk_chapter_fix.py <epub_dir> <books_json_path> -o <staging_dir>
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from build_library import _looks_like_real_title, _strip_title_tags  # noqa: E402
from book_renderer import (  # noqa: E402
    render_book_meta,
    render_book_page,
    render_chapter_fragment,
    render_chapter_titles,
)
import epub_parser as fixed_parser  # noqa: E402


def _load_old_parser():
    """Load the pre-fix epub_parser from git HEAD in isolation, so this
    script can tell "the epub changed" apart from "the live book was built
    from a different file than the one sitting in epub_dir today"."""
    import subprocess
    repo_root = Path(__file__).resolve().parent.parent
    orig_src = subprocess.run(
        ["git", "show", "HEAD:app/epub_parser.py"], cwd=repo_root, capture_output=True, check=True, text=True,
    ).stdout
    tmp_path = Path("/tmp/_epub_parser_orig_for_fix_script.py")
    tmp_path.write_text(orig_src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("epub_parser_orig", tmp_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epub_parser_orig"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("epub_dir")
    parser.add_argument("books_json_path")
    parser.add_argument("-o", "--out-dir", default="output/site_update_staging")
    args = parser.parse_args()

    old_parser = _load_old_parser()
    books = json.loads(Path(args.books_json_path).read_text(encoding="utf-8"))
    by_title: dict[str, list[dict]] = {}
    for b in books:
        by_title.setdefault(b["title"].strip().lower(), []).append(b)

    out_dir = Path(args.out_dir)
    (out_dir / "books").mkdir(parents=True, exist_ok=True)

    staged = []    # dicts describing each changed, safe-to-apply book
    skipped = []   # (filename, reason)
    unchanged = 0

    files = sorted(glob.glob(os.path.join(args.epub_dir, "*.epub")))
    for f in files:
        base = os.path.basename(f)
        try:
            old_meta, old_chapters = old_parser.extract_book(f)
        except Exception as e:
            skipped.append((base, f"OLD extract error: {e}"))
            continue

        title = old_meta["title"] if _looks_like_real_title(old_meta["title"]) else os.path.splitext(base)[0]
        title = _strip_title_tags(title).strip()
        author = old_meta.get("author")
        cands = by_title.get(title.lower(), [])
        exact = [b for b in cands if (b.get("author") or "").strip().lower() == (author or "").strip().lower()]
        pick_list = exact if exact else cands
        if not pick_list:
            continue  # not on the deployed site at all — nothing to update
        if len(pick_list) != 1:
            skipped.append((base, f"{len(pick_list)} ambiguous books.json candidates for title={title!r}"))
            continue

        book = pick_list[0]
        old_n = len(old_chapters)
        if old_n != book["n"]:
            skipped.append((
                base,
                f"OLD parser gives {old_n} chapters but live book id={book['id']} has n={book['n']} "
                "— this local file doesn't match the live source, skipping",
            ))
            continue

        _, new_chapters = fixed_parser.extract_book(f)
        new_n = len(new_chapters)
        if new_n == old_n:
            unchanged += 1
            continue

        book_id = book["id"]
        book_dir = out_dir / "books" / str(book_id)
        data_dir = book_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        for ch in new_chapters:
            (data_dir / f"{ch.index:04d}.html").write_text(render_chapter_fragment(ch), encoding="utf-8")
        (book_dir / "index.html").write_text(
            render_book_page(
                book_id=book_id, book_title=book["title"], author=book.get("author"),
                category=book["category"], chapters=new_chapters, cover=book.get("cover"),
            ),
            encoding="utf-8",
        )
        (book_dir / "meta.json").write_text(
            render_book_meta(
                title=book["title"], author=book.get("author"), category=book["category"],
                n=new_n, cover=book.get("cover"),
            ),
            encoding="utf-8",
        )
        (book_dir / "titles.json").write_text(render_chapter_titles(new_chapters), encoding="utf-8")

        stale_files = [f"{i:04d}.html" for i in range(new_n, old_n)]
        (book_dir / "_stale_data_files_to_delete.json").write_text(
            json.dumps(stale_files, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        book["n"] = new_n
        staged.append({
            "id": book_id, "title": book["title"], "old_n": old_n, "new_n": new_n,
            "removed": old_n - new_n, "epub_file": base,
        })

    (out_dir / "books.json").write_text(json.dumps(books, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "_plan.json").write_text(json.dumps({"staged": staged, "skipped": skipped}, ensure_ascii=False, indent=2), encoding="utf-8")

    staged.sort(key=lambda r: -r["removed"])
    print(f"Staged {len(staged)} changed books, {unchanged} confirmed-safe books with no change, "
          f"{len(skipped)} skipped as unsafe.\n")
    print(f"{'id':>5}  {'removed':>7}  {'old_n':>6}  {'new_n':>6}  title")
    for r in staged:
        print(f"{r['id']:>5}  {r['removed']:>7}  {r['old_n']:>6}  {r['new_n']:>6}  {r['title']}")
    print(f"\ntotal chapters removed: {sum(r['removed'] for r in staged)}")

    if skipped:
        print("\nSKIPPED (unsafe — not staged, needs manual attention):")
        for base, reason in skipped:
            print(f"  {base}: {reason}")

    print(f"\nStaged files written to {out_dir}/ — nothing uploaded yet.")
    print("To apply, for each staged book id run:")
    print(f"  gsutil -m rsync -r {out_dir}/books/<id>/ gs://tts-pipeline-yl-novel-web-site/books/<id>/")
    print("  (then delete each id's files listed in _stale_data_files_to_delete.json from the bucket)")
    print(f"  gsutil cp {out_dir}/books.json gs://tts-pipeline-yl-novel-web-site/books.json")


if __name__ == "__main__":
    main()
