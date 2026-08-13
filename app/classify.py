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

# (category, [keywords]) — checked in order, first match wins.
_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Tu Tiên - Huyền Huyễn", [
        "tu tien", "tu luyen", "dao ton", "tong mon", "linh khi", "kim dan",
        "nguyen anh", "hoa than", "phi thang", "tien dao", "huyen huyen",
        "ma phap", "phap su", "yeu nghiet", "than co nhan", "chan nhan",
        "huyen ao", "vo hiep", "vo thuat", "giang ho", "kiem hiep", "vo dao",
    ]),
    ("Tận Thế - Sinh Tồn", [
        "tan the", "mat the", "sinh ton", "khung bo", "zombie", "di gioi", "phuc bam",
        "phu ban", "song lai", "dai tai", "diet vong", "hoang da", "du hoa",
    ]),
    ("Kinh Dị - Linh Dị", [
        "kinh di", "linh di", "ma quy", "hanh gia", "quy bi", "am hon",
        "chet", "ta ac",
    ]),
    ("Đô Thị - Dị Năng", [
        "do thi", "di nang", "sieu nang luc", "hien dai", "thanh pho",
    ]),
    ("Khoa Huyễn - Cơ Giáp", [
        "khoa huyen", "co giap", "vu tru", "robot", "tri tue nhan tao", "hanh tinh",
    ]),
    ("Ngôn Tình - Sủng", [
        "ngon tinh", "sung", "yeu duong", "chuong mon", "my nu", "phu quan",
    ]),
    ("Lịch Sử - Xuyên Không", [
        "xuyen khong", "lich su", "trieu dai", "hoang de", "vuong gia",
        "vuong tri", "phan phoi", "cung dau", "da su", "trung sinh", "quan truong",
    ]),
]

_DEFAULT_CATEGORY = "Khác"


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
