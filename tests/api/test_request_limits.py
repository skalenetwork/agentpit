"""Caps on how much one request may ask for. Each cap has an at-cap test so a
legitimate maximum-size request keeps working."""

from fastapi.testclient import TestClient

from agentpit.api.main import app


def test_books_accepts_500_ids():
    with TestClient(app) as client:
        r = client.post("/books", json=[{"token_id": str(i)} for i in range(500)])
    assert r.status_code == 200
    assert r.json() == []  # unknown ids are skipped


def test_books_rejects_501_ids():
    with TestClient(app) as client:
        r = client.post("/books", json=[{"token_id": str(i)} for i in range(501)])
    assert r.status_code == 422


def test_books_rejects_an_overlong_token_id():
    with TestClient(app) as client:
        r = client.post("/books", json=[{"token_id": "1" * 101}])
    assert r.status_code == 422
