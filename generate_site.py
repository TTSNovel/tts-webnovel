"""Render an epub's chapters as a small static site for testing in a browser.

Wraps each chapter in <article> and links chapters with rel="next" — the
exact selectors kokoro-tts-addon/content.js already auto-detects for content
extraction and auto-advance, so this doubles as a manual end-to-end test rig
for the extension without needing a real web novel site.

Usage: python3 generate_site.py path/to/book.epub [output_dir]
"""
import re
import sys
from pathlib import Path

from epub_parser import extract_chapters


def _slugify(title: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug[:50] or "chapter"


_PAGE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ max-width: 700px; margin: 2rem auto; padding: 0 1rem; font-family: system-ui, sans-serif; line-height: 1.7; }}
  article p {{ margin: 1em 0; }}
  nav {{ display: flex; justify-content: space-between; margin: 2rem 0; }}
</style>
</head>
<body>
<article>
<h1>{title}</h1>
{content}
</article>
<nav>
  <a href="../index.html">Mục lục</a>
  {prev_link}
  {next_link}
</nav>
</body>
</html>
"""


def generate_site(epub_path: str, output_dir: str) -> int:
    chapters = extract_chapters(epub_path)
    out = Path(output_dir)
    chapters_dir = out / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)

    filenames = [f"{ch.index:04d}_{_slugify(ch.title)}.html" for ch in chapters]

    for i, ch in enumerate(chapters):
        prev_link = f'<a href="{filenames[i - 1]}">← Chương trước</a>' if i > 0 else "<span></span>"
        next_link = (
            f'<a rel="next" href="{filenames[i + 1]}">Chương tiếp theo →</a>'
            if i + 1 < len(chapters)
            else "<span></span>"
        )
        page = _PAGE.format(title=ch.title, content=ch.html, prev_link=prev_link, next_link=next_link)
        (chapters_dir / filenames[i]).write_text(page, encoding="utf-8")

    index_items = "\n".join(
        f'<li><a href="chapters/{fname}">{ch.title}</a></li>' for ch, fname in zip(chapters, filenames)
    )
    index_html = f"""<!doctype html>
<html lang="vi">
<head><meta charset="utf-8"><title>{Path(epub_path).stem}</title>
<style>body {{ max-width: 700px; margin: 2rem auto; padding: 0 1rem; font-family: system-ui, sans-serif; }}</style>
</head>
<body>
<h1>{Path(epub_path).stem}</h1>
<ol>
{index_items}
</ol>
</body>
</html>
"""
    (out / "index.html").write_text(index_html, encoding="utf-8")
    return len(chapters)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    epub_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "site_output"
    n = generate_site(epub_path, output_dir)
    print(f"Generated {n} chapter pages in {output_dir}/ (open {output_dir}/index.html)")
