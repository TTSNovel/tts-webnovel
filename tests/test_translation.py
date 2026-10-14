import json

import translation


class FakeGoogle:
    def __init__(self, revision="r1"):
        self.revision = revision
        self.calls = []

    def translate(self, texts, source, target, glossary):
        self.calls.append((list(texts), glossary))
        return [f"vi:{t}" for t in texts]

    def revision_fn(self, book_id, source, target):
        return self.revision


def run(site, fake, texts, chapter=3):
    return translation.translate_segments(
        site, 119, chapter, texts, "en", "vi", translate_fn=fake.translate, revision_fn=fake.revision_fn
    )


def test_translates_only_missing_segments_and_stores_them(tmp_path):
    fake = FakeGoogle()
    out, stats = run(tmp_path, fake, ["Hello.", "World."])
    assert out == ["vi:Hello.", "vi:World."]
    assert stats == {"cached": 0, "translated": 2, "glossary": "r1"}
    assert fake.calls == [(["Hello.", "World."], "novel-book-119-en-vi")]

    # A later request with a mix of old and new segments, in another order
    # and chunking, only pays for the new one.
    out, stats = run(tmp_path, fake, ["World.", "Again.", "Hello."])
    assert out == ["vi:World.", "vi:Again.", "vi:Hello."]
    assert stats["cached"] == 2 and stats["translated"] == 1
    assert fake.calls[-1][0] == ["Again."]

    stored = json.loads((tmp_path / "translations/119/en-vi/0003.json").read_text())
    assert len(stored["entries"]) == 3


def test_glossary_revision_change_retranslates(tmp_path):
    fake = FakeGoogle(revision="r1")
    run(tmp_path, fake, ["Dao."])
    fake.revision = "r2"
    _, stats = run(tmp_path, fake, ["Dao."])
    assert stats["translated"] == 1


def test_no_glossary_translates_without_one(tmp_path):
    fake = FakeGoogle(revision=None)
    _, stats = run(tmp_path, fake, ["Hi."])
    assert fake.calls == [(["Hi."], None)]
    assert stats["glossary"] is None


def test_blank_and_duplicate_segments(tmp_path):
    fake = FakeGoogle()
    out, stats = run(tmp_path, fake, ["", "Same.", "Same."])
    assert out == ["", "vi:Same.", "vi:Same."]
    assert fake.calls[0][0] == ["Same."]
    assert stats["translated"] == 1


def test_stray_spaces_inside_quotes_are_tidied(tmp_path):
    class Spacey(FakeGoogle):
        def translate(self, texts, source, target, glossary):
            return ["“ Thiên Đạo Vô Tình” [ Nút Nexus]", "“Menu, ” Zac nói.", "thiên về Nhanh Nhẹn . Rồi"]

    out, _ = run(tmp_path, Spacey(), ["a", "b", "c"])
    assert out == ["“Thiên Đạo Vô Tình” [Nút Nexus]", "“Menu,” Zac nói.", "thiên về Nhanh Nhẹn. Rồi"]


def test_list_and_delete(tmp_path):
    fake = FakeGoogle()
    run(tmp_path, fake, ["A."], chapter=1)
    run(tmp_path, fake, ["B.", "C."], chapter=2)
    listed = translation.list_stored(tmp_path, 119)
    assert [(c["chapter"], c["segments"]) for c in listed] == [(1, 1), (2, 2)]
    assert translation.delete_stored(tmp_path, 119, chapter=1) == 1
    assert [c["chapter"] for c in translation.list_stored(tmp_path, 119)] == [2]
    assert translation.delete_stored(tmp_path, 119) == 1
    assert translation.list_stored(tmp_path, 119) == []
