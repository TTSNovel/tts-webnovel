"""Approximate genre classification for Vietnamese web novels.

Keyword match over title + synopsis (epub DC description, when present).
Ordered most-specific-first so a book matching multiple keyword sets lands
in its most distinctive category rather than a generic catch-all. This is
intentionally rough (confirmed acceptable by the user) — meant to give a
reasonable starting grouping that can be hand-corrected later, not a
precise classifier.
"""
from __future__ import annotations

import re
import unicodedata

# (category, [keywords]) — checked in order, first match wins. Category
# labels are English (see build_library.py's _CATEGORY_RENAME_MAP for the
# migration that moved existing books off the old Vietnamese labels) even
# though the keywords themselves stay Vietnamese-Latin (title/description
# text this matches against is Vietnamese with diacritics stripped) —
# except the LitRPG bucket, whose source material is English to begin with.
_CATEGORIES: list[tuple[str, list[str]]] = [
    # Checked before Cultivation - Fantasy: English progression-fantasy terms
    # (this library's source text for these is English, e.g. "Defiance of
    # the Fall: A LitRPG Adventure") would never hit any of the
    # diacritic-stripped Vietnamese keywords below on their own.
    ("LitRPG - Progression Fantasy", [
        "litrpg", "system interface", "skill tree", "dungeon core", "class evolution",
    ]),
    ("Cultivation - Fantasy", [
        "tu tien", "tu luyen", "dao ton", "tong mon", "linh khi", "kim dan",
        "nguyen anh", "hoa than", "phi thang", "tien dao", "huyen huyen",
        "ma phap", "phap su", "yeu nghiet", "than co nhan", "chan nhan",
        "huyen ao", "vo hiep", "vo thuat", "giang ho", "kiem hiep", "vo dao",
    ]),
    ("Apocalypse - Survival", [
        "tan the", "mat the", "sinh ton", "khung bo", "zombie", "di gioi", "phuc bam",
        "phu ban", "song lai", "dai tai", "diet vong", "hoang da", "du hoa",
    ]),
    ("Horror - Supernatural", [
        "kinh di", "linh di", "ma quy", "hanh gia", "quy bi", "am hon",
        "chet", "ta ac",
    ]),
    ("Urban - Superpowers", [
        "do thi", "di nang", "sieu nang luc", "hien dai", "thanh pho",
    ]),
    ("Sci-Fi - Mecha", [
        "khoa huyen", "co giap", "vu tru", "robot", "tri tue nhan tao", "hanh tinh",
    ]),
    ("Romance", [
        "ngon tinh", "sung", "yeu duong", "chuong mon", "my nu", "phu quan",
    ]),
    ("Historical - Time Travel", [
        "xuyen khong", "lich su", "trieu dai", "hoang de", "vuong gia",
        "vuong tri", "phan phoi", "cung dau", "da su", "trung sinh", "quan truong",
    ]),
]

_DEFAULT_CATEGORY = "Other"


def _strip_diacritics(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return stripped.replace("đ", "d").replace("Đ", "D")


def classify(title: str, description: str = "") -> str:
    haystack = _strip_diacritics(f"{title} {description}".lower())
    haystack = re.sub(r"[^a-z0-9\s]", " ", haystack)

    for category, keywords in _CATEGORIES:
        for kw in keywords:
            if kw in haystack:
                return category
    return _DEFAULT_CATEGORY
