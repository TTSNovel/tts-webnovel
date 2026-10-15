import json

import pytest

import translation


class FakeGoogle:
    def __init__(self, revision="r1"):
        self.revision = revision
        self.calls = []

    def translate(self, texts, source, target, glossary):
        self.calls.append((list(texts), glossary))
        return [f"vi:{t}" for t in texts]

    def glossary_fn(self, book_id, source, target):
        return ("novel-book-119-en-vi", self.revision) if self.revision else (None, None)


def run(site, fake, texts, chapter=3):
    return translation.translate_segments(
        site, 119, chapter, texts, "en", "vi", translate_fn=fake.translate, glossary_fn=fake.glossary_fn
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


def test_glossary_failure_falls_back_to_plain_translation(tmp_path):
    class Flaky(FakeGoogle):
        def translate(self, texts, source, target, glossary):
            self.calls.append((list(texts), glossary))
            if glossary:
                raise translation.TranslationError("glossary not found")
            return [f"plain:{t}" for t in texts]

    fake = Flaky()
    out, _ = run(tmp_path, fake, ["Dao."])
    assert out == ["plain:Dao."]
    assert [g for _, g in fake.calls] == ["novel-book-119-en-vi", None]
    # Stored without a revision, so it's redone once the glossary is back.
    _, stats = run(tmp_path, FakeGoogle(), ["Dao."])
    assert stats["translated"] == 1


# ── Glossary ────────────────────────────────────────────────────────────────


def test_normalize_terms_trims_dedupes_and_assigns_ids():
    terms = translation.normalize_terms([
        {"source": "  Dao ", "target": "Đạo", "book_id": None},
        {"source": "class", "target": "lớp", "book_id": 119},
        {"source": "class", "target": "chức nghiệp", "book_id": 119},
        {"id": "keep", "source": "class", "target": "lớp học", "book_id": None},
    ])
    assert [(t["source"], t["target"], t["book_id"]) for t in terms] == [
        ("Dao", "Đạo", None), ("class", "chức nghiệp", 119), ("class", "lớp học", None)
    ]
    assert all(t["id"] for t in terms) and terms[2]["id"] == "keep"


def test_blank_target_keeps_the_term_as_is():
    terms = translation.normalize_terms([{"source": "class", "target": " "}, {"source": "Dao"}])
    assert [(t["source"], t["target"]) for t in terms] == [("class", "class"), ("Dao", "Dao")]


@pytest.mark.parametrize("bad", [
    "nope",
    [{"source": "", "target": "x"}],
    [{"source": "x", "target": "y", "book_id": "119"}],
    [{"source": "x" * 201, "target": "y"}],
])
def test_normalize_terms_rejects_invalid(bad):
    with pytest.raises(ValueError):
        translation.normalize_terms(bad)


def test_glossary_contents_merges_shared_into_book_glossaries():
    terms = translation.normalize_terms([
        {"source": "Dao", "target": "Đạo", "book_id": None},
        {"source": "class", "target": "lớp", "book_id": None},
        {"source": "class", "target": "chức nghiệp", "book_id": 119},
        {"source": "林动", "target": "Lâm Động", "book_id": 7},
    ])
    assert translation.glossary_contents(terms) == {
        "novel-global-en-vi": "Dao\tĐạo\nclass\tlớp\n",
        "novel-book-119-en-vi": "Dao\tĐạo\nclass\tchức nghiệp\n",
        "novel-book-7-zh-vi": "林动\tLâm Động\n",
    }


class FakeGlossaries:
    def __init__(self, existing=()):
        self.ids = set(existing)
        self.created, self.deleted = [], []

    def list_ids(self):
        return sorted(self.ids)

    def create(self, gid, text):
        self.created.append((gid, text))
        self.ids.add(gid)

    def delete(self, gid):
        self.deleted.append(gid)
        self.ids.discard(gid)


def test_save_glossary_publishes_only_what_changed(tmp_path):
    google = FakeGlossaries(existing=["novel-book-5-en-vi"])
    terms = [
        {"source": "Dao", "target": "Đạo", "book_id": None},
        {"source": "class", "target": "chức nghiệp", "book_id": 119},
    ]
    data, changes = translation.save_glossary(tmp_path, terms, google)
    assert changes == {"updated": ["novel-book-119-en-vi", "novel-global-en-vi"], "deleted": ["novel-book-5-en-vi"]}
    assert translation.load_glossary(tmp_path)["terms"] == data["terms"]

    # Same list again: nothing to publish.
    google.created.clear()
    _, changes = translation.save_glossary(tmp_path, data["terms"], google)
    assert changes == {"updated": [], "deleted": []} and google.created == []

    # A shared term changed: both the global and the book glossary include it.
    edited = [dict(t, target="Đại Đạo") if t["source"] == "Dao" else t for t in data["terms"]]
    _, changes = translation.save_glossary(tmp_path, edited, google)
    assert changes["updated"] == ["novel-book-119-en-vi", "novel-global-en-vi"]
    assert "novel-global-en-vi" in google.deleted


def test_save_glossary_keeps_terms_when_publish_fails(tmp_path):
    class Broken(FakeGlossaries):
        def create(self, gid, text):
            raise translation.TranslationError("boom")

    with pytest.raises(translation.TranslationError):
        translation.save_glossary(tmp_path, [{"source": "Dao", "target": "Đạo"}], Broken())
    stored = translation.load_glossary(tmp_path)
    assert [t["source"] for t in stored["terms"]] == ["Dao"] and stored["published"] == {}
