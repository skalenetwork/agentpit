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


def test_422_does_not_echo_the_request_input():
    big = "x" * 50_000
    with TestClient(app) as client:
        r = client.post("/books", json=[{"token_id": [big]}])
    assert r.status_code == 422
    assert big not in r.text
    detail = r.json()["detail"]
    assert detail and all("input" not in e and "url" not in e for e in detail)
    assert {"type", "loc", "msg"} <= set(detail[0])


def test_422_lists_at_most_20_errors():
    with TestClient(app) as client:
        r = client.post("/books", json=[{} for _ in range(100)])
    assert r.status_code == 422
    assert len(r.json()["detail"]) == 20
