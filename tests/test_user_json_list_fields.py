"""User ``interests`` / ``personality_traits``: admin validation and safe reads.

Both columns hold a JSON array of strings. The admin edit PUT must reject
anything else without touching the row, and readers (public ``/api/users``,
model getters) must treat a blank or malformed row as ``[]`` instead of 500.
"""

import json

import pytest

from deaddit.extensions import db
from deaddit.models import User
from deaddit.utils import safe_json_list

ORIGINAL_TRAITS = '["witty","blunt"]'
ORIGINAL_INTERESTS = '["testing"]'


@pytest.fixture()
def admin_client(client, admin_login):
    return admin_login(client)


@pytest.fixture()
def alice(app, seeded_db):
    user = db.session.get(User, "alice")
    user.personality_traits = ORIGINAL_TRAITS
    user.interests = ORIGINAL_INTERESTS
    db.session.commit()
    return user


def _snapshot(user):
    return {col.name: getattr(user, col.name) for col in User.__table__.columns}


# --- Admin PUT: valid input ---------------------------------------------------


def test_put_traits_as_list_stores_normalised_json(admin_client, alice):
    resp = admin_client.put(
        "/admin/api/users/alice",
        json={"personality_traits": [" witty ", "", "   ", "blunt"]},
    )

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert alice.personality_traits == json.dumps(["witty", "blunt"])
    assert alice.interests == ORIGINAL_INTERESTS


def test_put_list_fields_as_json_strings_store_normalised_json(admin_client, alice):
    resp = admin_client.put(
        "/admin/api/users/alice",
        json={
            "personality_traits": '[\n  " curious ",\n  "",\n  "dry"\n]',
            "interests": '["chess", "  birding  "]',
        },
    )

    assert resp.status_code == 200
    assert alice.personality_traits == json.dumps(["curious", "dry"])
    assert alice.interests == json.dumps(["chess", "birding"])


@pytest.mark.parametrize("blank", ["", "   \n\t", None])
def test_put_blank_list_fields_store_empty_array(admin_client, alice, blank):
    resp = admin_client.put(
        "/admin/api/users/alice",
        json={"personality_traits": blank, "interests": blank},
    )

    assert resp.status_code == 200
    assert alice.personality_traits == "[]"
    assert alice.interests == "[]"


def test_edit_modal_round_trip_preserves_list_fields(admin_client, alice):
    """Echoing back what GET returned (what the edit modal now does) keeps
    the traits, instead of the old behaviour of blanking them."""
    fetched = admin_client.get("/admin/api/users/alice").get_json()

    resp = admin_client.put(
        "/admin/api/users/alice",
        json={
            "bio": "edited bio",
            "interests": fetched["interests"],
            "personality_traits": fetched["personality_traits"],
        },
    )

    assert resp.status_code == 200
    assert alice.bio == "edited bio"
    assert json.loads(alice.personality_traits) == ["witty", "blunt"]
    assert json.loads(alice.interests) == ["testing"]


# --- Admin PUT: invalid input -------------------------------------------------


@pytest.mark.parametrize("field", ["personality_traits", "interests"])
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("witty, blunt", id="invalid-json"),
        pytest.param('["witty",', id="truncated-json"),
        pytest.param('{"a": 1}', id="json-object-string"),
        pytest.param('"hello"', id="json-scalar-string"),
        pytest.param("5", id="json-number-string"),
        pytest.param({"a": 1}, id="object"),
        pytest.param(5, id="number"),
        pytest.param(True, id="bool"),
        pytest.param("[1, 2]", id="json-array-of-numbers"),
        pytest.param('["ok", {"a": 1}]', id="json-array-with-object"),
        pytest.param(["ok", 3], id="list-with-number"),
        pytest.param(["ok", None], id="list-with-null"),
    ],
)
def test_put_invalid_list_field_is_rejected_and_changes_nothing(
    admin_client, alice, field, bad
):
    before = _snapshot(alice)

    resp = admin_client.put(
        "/admin/api/users/alice",
        json={
            "bio": "should not be written",
            "age": 99,
            "writing_style": "should not be written",
            "is_troll": True,
            field: bad,
        },
    )

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["success"] is False
    assert field in body["error"]
    # Nothing was assigned in-session (validation runs before mutation) ...
    assert _snapshot(alice) == before
    # ... and nothing reached the database.
    db.session.expire_all()
    assert _snapshot(db.session.get(User, "alice")) == before


def test_put_without_traits_key_leaves_traits_untouched(admin_client, alice):
    resp = admin_client.put("/admin/api/users/alice", json={"bio": "new bio"})

    assert resp.status_code == 200
    assert alice.bio == "new bio"
    # Byte-identical: absent keys are not re-normalised either.
    assert alice.personality_traits == ORIGINAL_TRAITS
    assert alice.interests == ORIGINAL_INTERESTS


# --- Public /api/users --------------------------------------------------------


def test_public_api_users_tolerates_blank_and_malformed_rows(client, db_session):
    db_session.add_all(
        [
            User(username="good", interests='["chess"]', personality_traits='["dry"]'),
            User(username="blank", interests="", personality_traits=""),
            User(username="broken", interests="chess, go", personality_traits="{oops"),
            User(username="wrongtype", interests='{"a": 1}', personality_traits='"x"'),
            User(username="nulls", interests=None, personality_traits=None),
        ]
    )
    db_session.commit()

    resp = client.get("/api/users")

    assert resp.status_code == 200
    users = {u["username"]: u for u in resp.get_json()["users"]}
    assert users["good"]["interests"] == ["chess"]
    assert users["good"]["personality_traits"] == ["dry"]
    for name in ("blank", "broken", "wrongtype", "nulls"):
        assert users[name]["interests"] == [], name
        assert users[name]["personality_traits"] == [], name


# --- Shared helper and model getters ------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('["a", "b"]', ["a", "b"]),
        ("[]", []),
        ('["a", 1]', ["a", 1]),
        (None, []),
        ("", []),
        ("not json", []),
        ("{oops", []),
        ('{"a": 1}', []),
        ('"hello"', []),
        ("5", []),
        (5, []),
    ],
)
def test_safe_json_list(raw, expected):
    assert safe_json_list(raw) == expected


def test_user_list_getters_are_tolerant():
    good = User(interests='["chess"]', personality_traits='["dry"]')
    assert good.get_interests() == ["chess"]
    assert good.get_personality_traits() == ["dry"]

    for raw in (None, "", "{oops", '{"a": 1}'):
        bad = User(interests=raw, personality_traits=raw)
        assert bad.get_interests() == [], raw
        assert bad.get_personality_traits() == [], raw
