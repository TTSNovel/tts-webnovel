import io

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    for k, v in {"SITE_DIR": str(tmp_path), "WEB_AUTH_USER": "u", "WEB_AUTH_PASS": "p", "SESSION_SECRET": "s",
                 "TTS_API_URL": "x", "TTS_API_SECRET": "x", "GPU_TTS_API_URL": "x"}.items():
        monkeypatch.setenv(k, v)
    import server
    monkeypatch.setattr(server, "SITE_DIR", str(tmp_path))
    monkeypatch.setattr(server, "add_book_to_site", lambda site_dir, path, used_ids, category=None: {
        "id": 1, "title": "T", "author": "A", "category": category, "n": 3, "cover": None})
    c = server.app.test_client()
    c.post("/login", data={"username": "u", "password": "p"})
    return c


def test_successful_import_renders_the_form_again_with_a_success_message(client):
    r = client.post("/import", data={"ebook": (io.BytesIO(b"x"), "t.epub"), "category": "Romance"},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'class="msg ok"' in body
    assert 'name="category"' in body
