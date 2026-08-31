from hanviet import load_dict, to_hanviet

_TABLE = load_dict()


def test_to_hanviet_converts_han_characters():
    assert to_hanviet("第九章", _TABLE) == "đệ cửu chương"


def test_to_hanviet_preserves_non_han_runs():
    assert to_hanviet("Hello 世界 123", _TABLE) == "Hello thế giới 123"


def test_to_hanviet_keeps_unknown_characters_as_is():
    assert to_hanviet("𠀀", _TABLE) == "𠀀"
