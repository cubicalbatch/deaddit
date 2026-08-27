"""One-app-boot smoke: key pages and API endpoints respond with sane bodies."""

from __future__ import annotations


def test_front_page_empty_db(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_seeded_pages_and_api(app, client, seeded_db):
    assert client.get("/").status_code == 200

    resp = client.get("/d/testsub")
    assert resp.status_code == 200
    assert b"Hello World" in resp.data or b"Seeded Post" in resp.data

    resp = client.get("/user/alice")
    assert resp.status_code == 200
    assert b"alice" in resp.data

    resp = client.get("/api/posts")
    assert resp.status_code == 200
    body = resp.get_json()
    titles = {p["title"] for p in body["posts"]}
    assert "Seeded Post" in titles

    resp = client.get("/api/subdeaddits")
    assert resp.status_code == 200
    body = resp.get_json()
    names = {s["name"] for s in body["subdeaddits"]}
    assert {"testsub", "askdeaddit"} <= names

    resp = client.get("/live")
    assert resp.status_code == 200
    assert b"<h1>Live</h1>" in resp.data or b"Live" in resp.data
