import pytest

from build_library import write_index
from classify import CATEGORIES, classify


def test_there_is_no_catch_all_category():
    assert "Other" not in CATEGORIES
    assert "Khác" not in CATEGORIES


def test_unmatched_title_is_left_unclassified_not_dumped_in_other():
    assert classify("Zzz Qqq", "") is None


def test_english_titles_match_english_keywords():
    assert classify("Supreme Magus") == "Cultivation - Fantasy"
    assert classify("My Vampire System (WN)") == "LitRPG - Progression Fantasy"


def test_write_index_refuses_a_book_outside_the_category_list(tmp_path):
    manifest = [{"id": 1, "title": "X", "author": "Y", "category": "Other", "n": 1, "cover": None}]
    with pytest.raises(ValueError, match="invalid category"):
        write_index(tmp_path, manifest)
