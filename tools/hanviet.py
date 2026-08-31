"""Convert Chinese text to Sino-Vietnamese (Han Viet) reading, character by
character — e.g. 第九章 -> "đệ cửu chương". This is a literal 1-1 phonetic
transliteration, not a phrase-level translation: each Han character is
looked up independently in tools/data/phienam.txt (community-maintained
table used by the VietPhrase/QuickTranslator fan-translation tools).
Non-Han characters (Latin text, punctuation, whitespace) pass through
unchanged.

Usage: python3 tools/hanviet.py "第九章 以战养"
       python3 tools/hanviet.py path/to/chapter.txt
       echo "第九章" | python3 tools/hanviet.py
"""
import re
import sys
from pathlib import Path

_DICT_PATH = Path(__file__).resolve().parent / "data" / "phienam.txt"


def load_dict(path: Path = _DICT_PATH) -> dict[str, str]:
    table: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        char, _, reading = line.partition("=")
        if char and reading:
            table[char] = reading
    return table


def to_hanviet(text: str, table: dict[str, str]) -> str:
    # Pad each looked-up reading with spaces so it separates from its
    # neighbors, while runs of untranslated characters (Latin words,
    # digits, punctuation) stay glued together exactly as written.
    parts = [f" {table[ch]} " if ch in table else ch for ch in text]
    return re.sub(r" {2,}", " ", "".join(parts)).strip()


def main() -> None:
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        path = Path(arg)
        text = path.read_text(encoding="utf-8") if path.is_file() else arg
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        print(__doc__)
        sys.exit(1)

    table = load_dict()
    for line in text.splitlines():
        print(to_hanviet(line, table))


if __name__ == "__main__":
    main()
