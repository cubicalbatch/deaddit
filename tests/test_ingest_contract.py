"""HTTP contract tests for /api/ingest and /api/ingest/user (Phase A4, slice S2).

The views are thin wrappers over ``deaddit.services.content``; these tests pin
the legacy observable behavior that must survive the delegation: exact status
codes, exact error messages, processing order, all-or-nothing validation, and
the 201 success shapes.
"""

from __future__ import annotations

import pytest

from deaddit.models import Comment, Post, Subdeaddit, User

REQUIRED_USER_FIELDS = [
    "username",
    "age",
    "gender",
    "bio",
    "interests",
    "occupation",
    "education",
    "writing_style",
    "personality_traits",
]

VALID_POST = {
    "title": "T",
    "content": "C",
    "user": "alice",
    "subdeaddit": "testsub",
    "upvote_count": 1,
}


def user_payload(**overrides):
    payload = {
        "username": "carol",
        "age": 30,
        "gender": "Female",
        "bio": "carol curious",
        "interests": ["testing", "contracts"],
        "occupation": "tester",
        "education": "self-taught",
        "writing_style": "precise",
        "personality_traits": ["meticulous"],
        "model": "contract-model",
    }
    payload.update(overrides)
    return payload


def _baseline_counts(seeded_db):
    return (
        Post.query.count(),
        Comment.query.count(),
        Subdeaddit.query.count(),
        User.query.count(),
    )


# ---------------------------------------------------------------------------
# ingest: valid mixed payload


def test_ingest_mixed_payload_201_shape(client, seeded_db):
    long_content = "x" * 80
    resp = client.post(
        "/api/ingest",
        json={
            "subdeaddits": [
                {
                    "name": "testsub",
                    "description": "Updated description",
                    "post_types": ["text"],
                }
            ],
            "posts": [
                {
                    "title": "Contract Post",
                    "content": "Body of the contract post",
                    "user": "alice",
                    "subdeaddit": "testsub",
                    "upvote_count": 3,
                    "model": "contract-model",
                }
            ],
            "comments": [
                # Legacy order: posts are created before comments.
                {
                    "post_id": seeded_db["posts"][0].id,
                    "content": long_content,
                    "user": "bob",
                },
                {
                    "parent_id": seeded_db["comments"][0].id,
                    "post_id": seeded_db["posts"][0].id,
                    "content": "A reply comment",
                    "user": "alice",
                },
            ],
        },
    )

    assert resp.status_code == 201
    body = resp.get_json()

    assert set(body) == {"message", "added", "posts", "comments"}
    assert body["message"] == "Posts and comments created successfully"
    assert isinstance(body["added"], list)

    assert len(body["posts"]) == 1
    assert set(body["posts"][0]) == {"id", "title"}
    assert isinstance(body["posts"][0]["id"], int)
    assert body["posts"][0]["title"] == "Contract Post"
    persisted = Post.query.filter_by(title="Contract Post").one()
    assert body["posts"][0]["id"] == persisted.id

    assert len(body["comments"]) == 2
    for entry in body["comments"]:
        assert set(entry) == {"id", "content"}
        assert isinstance(entry["id"], int)
    assert body["comments"][0]["content"] == long_content[:50]
    assert Comment.query.get(body["comments"][0]["id"]).parent_id is None
    assert (
        Comment.query.get(body["comments"][1]["id"]).parent_id
        == seeded_db["comments"][0].id
    )

    assert body["added"] == [
        "Contract Post",
        long_content,
        "A reply comment",
        "Updated subdeaddit: testsub",
    ]
    assert Subdeaddit.query.get("testsub").description == "Updated description"


def test_ingest_created_subdeaddit_label(client, seeded_db):
    resp = client.post(
        "/api/ingest",
        json={"subdeaddits": [{"name": "brandnew", "description": "A fresh sub"}]},
    )
    assert resp.status_code == 201
    assert resp.get_json()["added"] == ["Created subdeaddit: brandnew"]
    assert Subdeaddit.query.get("brandnew") is not None


# ---------------------------------------------------------------------------
# ingest: legacy 400 cases — exact message AND nothing created


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        # posts: unknown user takes precedence over missing fields
        (
            {"posts": [dict(VALID_POST, user="ghost", title=None)]},
            "User 'ghost' does not exist",
        ),
        # posts: required-fields check (missing title)
        (
            {"posts": [dict(VALID_POST, title=None)]},
            "Invalid post data",
        ),
        # posts: falsy upvote_count also counts as missing (legacy `not all`)
        (
            {"posts": [dict(VALID_POST, upvote_count=0)]},
            "Invalid post data",
        ),
        # posts: unknown subdeaddit checked last
        (
            {"posts": [dict(VALID_POST, subdeaddit="nosuchsub")]},
            "Subdeaddit 'nosuchsub' does not exist",
        ),
        # comments: unknown user first
        (
            {"comments": [{"post_id": 1, "content": "hi", "user": "nobody"}]},
            "User 'nobody' does not exist",
        ),
        # comments: missing fields joined in legacy order post_id, content
        # (user existence is checked first, so use an existing user)
        (
            {"comments": [{"post_id": None, "content": "", "user": "alice"}]},
            "Comment missing required fields: post_id, content",
        ),
        # subdeaddits: missing fields
        (
            {"subdeaddits": [{"name": "", "description": ""}]},
            "Subdeaddit missing required fields: name, description",
        ),
    ],
)
def test_ingest_legacy_400_messages_and_no_partial_writes(
    client, seeded_db, payload, expected_error
):
    before = _baseline_counts(seeded_db)
    resp = client.post("/api/ingest", json=payload)

    assert resp.status_code == 400
    assert resp.get_json() == {"error": expected_error}
    assert _baseline_counts(seeded_db) == before


def test_ingest_all_or_nothing_across_items(client, seeded_db):
    """A valid first item must NOT survive an invalid second item."""
    before = _baseline_counts(seeded_db)
    resp = client.post(
        "/api/ingest",
        json={
            "posts": [
                VALID_POST,
                dict(VALID_POST, title="Second", subdeaddit="ghostsub"),
            ]
        },
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "Subdeaddit 'ghostsub' does not exist"}
    assert _baseline_counts(seeded_db) == before

@pytest.mark.parametrize("path", ["/api/ingest", "/api/ingest/user"])
@pytest.mark.parametrize(
    "kwargs",
    [{"json": {}}, {"data": b"null", "content_type": "application/json"}],
    ids=["empty-object", "null"],
)
def test_ingest_empty_body_400(client, seeded_db, path, kwargs):
    before = _baseline_counts(seeded_db)
    resp = client.post(path, **kwargs)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "No data provided"}
    assert _baseline_counts(seeded_db) == before


# ---------------------------------------------------------------------------
# ingest/user


def test_ingest_user_happy_path(client, seeded_db):
    resp = client.post("/api/ingest/user", json=user_payload())
    assert resp.status_code == 201
    body = resp.get_json()
    assert set(body) == {"message", "username"}
    assert body == {"message": "User created successfully", "username": "carol"}

    row = User.query.get("carol")
    assert row is not None
    assert row.gender == "Female"


@pytest.mark.parametrize("field", REQUIRED_USER_FIELDS)
def test_ingest_user_missing_required_field(client, seeded_db, field):
    payload = user_payload()
    del payload[field]
    resp = client.post("/api/ingest/user", json=payload)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": f"Missing required field: {field}"}
    assert User.query.get("carol") is None


def test_ingest_user_gender_coercion(client, seeded_db):
    resp = client.post(
        "/api/ingest/user", json=user_payload(username="dave", gender="male")
    )
    assert resp.status_code == 201
    assert User.query.get("dave").gender == "Male"

    resp = client.post(
        "/api/ingest/user", json=user_payload(username="eve", gender="Female")
    )
    assert resp.status_code == 201
    assert User.query.get("eve").gender == "Female"


# ---------------------------------------------------------------------------
# auth gate


@pytest.fixture()
def api_token(monkeypatch):
    from deaddit.config import Config

    monkeypatch.setattr(
        Config,
        "get",
        classmethod(
            lambda cls, key, default=None: "sekret" if key == "API_TOKEN" else default
        ),
    )
    return "sekret"


def test_ingest_requires_bearer_token_when_configured(client, api_token):
    no_header = client.post("/api/ingest", json={"posts": []})
    assert no_header.status_code == 401
    assert no_header.get_json() == {"error": "Unauthorized"}

    wrong = client.post(
        "/api/ingest",
        json={"posts": []},
        headers={"Authorization": "Bearer wrong"},
    )
    assert wrong.status_code == 401
    assert wrong.get_json() == {"error": "Unauthorized"}


def test_ingest_correct_token_passes_gate(client, api_token, seeded_db):
    ok = client.post(
        "/api/ingest/user",
        json=user_payload(),
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert ok.status_code == 201


# ---------------------------------------------------------------------------
# delegation: the view must call the service, not re-implement persistence


def test_view_delegates_to_create_post(client, seeded_db, monkeypatch):
    import deaddit.api as api_module
    from deaddit.services.content import create_post as real_create_post

    calls = []

    def spy(**kwargs):
        calls.append(kwargs)
        return real_create_post(**kwargs)

    monkeypatch.setattr(api_module, "create_post", spy)

    resp = client.post("/api/ingest", json={"posts": [VALID_POST]})
    assert resp.status_code == 201
    assert len(calls) == 1
    assert calls[0]["title"] == VALID_POST["title"]
    assert calls[0]["user"] == "alice"
