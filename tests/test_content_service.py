"""Unit tests for deaddit.services.content (Phase A4 slice S1)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from deaddit.models import Comment, Post, Subdeaddit, User
from deaddit.services import content as content_service
from deaddit.services.content import (
    ContentValidationError,
    create_comment,
    create_post,
    create_subdeaddit,
    create_user,
)


@pytest.fixture()
def cache_spy(monkeypatch):
    """Replace _clear_read_caches with a recorder; returns the call list."""
    calls = []
    monkeypatch.setattr(
        content_service, "_clear_read_caches", lambda: calls.append("clear")
    )
    return calls


# ---------------------------------------------------------------------------
# create_post


def test_create_post_persists(seeded_db, db_session, cache_spy):
    post = create_post(
        title="Brand New",
        content="Fresh content",
        user="alice",
        subdeaddit="testsub",
        upvote_count=5,
        model="gpt-x",
        post_type="text",
    )

    fetched = Post.query.filter_by(id=post.id).one()
    assert fetched.title == "Brand New"
    assert fetched.content == "Fresh content"
    assert fetched.user == "alice"
    assert fetched.subdeaddit_name == "testsub"
    assert fetched.upvote_count == 5
    assert fetched.model == "gpt-x"
    assert fetched.post_type == "text"
    assert cache_spy == ["clear"]


def test_create_post_created_at_override(seeded_db, db_session, cache_spy):
    stamp = datetime(2020, 1, 2, 3, 4, 5)
    post = create_post(
        title="Backdated",
        content="Old news",
        user="bob",
        subdeaddit="askdeaddit",
        created_at=stamp,
    )
    assert Post.query.get(post.id).created_at == stamp


def test_create_post_unknown_user_message(seeded_db, cache_spy):
    with pytest.raises(ContentValidationError) as exc:
        create_post(title="T", content="C", user="ghost", subdeaddit="testsub")
    assert str(exc.value) == "User 'ghost' does not exist"


def test_create_post_unknown_subdeaddit_message(seeded_db, cache_spy):
    with pytest.raises(ContentValidationError) as exc:
        create_post(title="T", content="C", user="alice", subdeaddit="nope")
    assert str(exc.value) == "Subdeaddit 'nope' does not exist"


@pytest.mark.parametrize("kwargs_title", [True, False])
def test_create_post_empty_fields_message(seeded_db, kwargs_title, cache_spy):
    with pytest.raises(ContentValidationError) as exc:
        create_post(
            title="T" if kwargs_title else "",
            content="" if kwargs_title else "C",
            user="alice",
            subdeaddit="testsub",
        )
    assert str(exc.value) == "Invalid post data"
    assert Post.query.count() == len(seeded_db["posts"])


# ---------------------------------------------------------------------------
# create_comment


def test_create_comment_persists_with_parent(seeded_db, db_session, cache_spy):
    parent = seeded_db["comments"][0]
    comment = create_comment(
        post_id=parent.post_id,
        content="A reply",
        user="alice",
        parent_id=parent.id,
        upvote_count=2,
        model="m1",
    )
    fetched = Comment.query.filter_by(id=comment.id).one()
    assert fetched.content == "A reply"
    assert fetched.parent_id == parent.id
    assert fetched.upvote_count == 2
    assert fetched.model == "m1"
    assert cache_spy == ["clear"]


def test_create_comment_created_at_override(seeded_db, db_session, cache_spy):
    stamp = datetime(2021, 6, 7, 8, 9, 10)
    post = seeded_db["posts"][0]
    comment = create_comment(
        post_id=post.id, content="Later reply", user="bob", created_at=stamp
    )
    assert Comment.query.get(comment.id).created_at == stamp


def test_create_comment_missing_content_message(seeded_db, cache_spy):
    with pytest.raises(ContentValidationError) as exc:
        create_comment(post_id=seeded_db["posts"][0].id, content="", user="alice")
    assert str(exc.value) == "Comment missing required fields: content"


def test_create_comment_unknown_user_message(seeded_db, cache_spy):
    with pytest.raises(ContentValidationError) as exc:
        create_comment(post_id=seeded_db["posts"][0].id, content="hi", user="nobody")
    assert str(exc.value) == "User 'nobody' does not exist"


def test_create_comment_unknown_post_message(seeded_db, cache_spy):
    with pytest.raises(ContentValidationError) as exc:
        create_comment(post_id=99999, content="hi", user="alice")
    assert str(exc.value) == "Post '99999' does not exist"


# ---------------------------------------------------------------------------
# create_user


def test_create_user_persists(seeded_db, db_session, cache_spy):
    user = create_user(
        username="carol",
        age=30,
        gender="Female",
        bio="bio text",
        interests=["hiking", "chess"],
        occupation="engineer",
        education="MSc",
        writing_style="terse",
        personality_traits=["curious"],
        model="llama-3",
    )
    fetched = User.query.filter_by(username=user.username).one()
    assert fetched.gender == "Female"
    assert fetched.interests == json.dumps(["hiking", "chess"])
    assert json.loads(fetched.personality_traits) == ["curious"]
    assert fetched.model == "llama-3"
    assert cache_spy == ["clear"]


def test_create_user_gender_coercion_and_preserialized_json(db_session, cache_spy):
    female = create_user(
        username="dana",
        age=40,
        gender="Female",
        bio="b",
        interests='["pre-serialized"]',
        occupation="o",
        education="e",
        writing_style="w",
        personality_traits=["t"],
    )
    male = create_user(
        username="ed",
        age=50,
        gender="Nonbinary",
        bio="b",
        interests=[],
        occupation="o",
        education="e",
        writing_style="w",
        personality_traits=[],
    )
    assert User.query.get(female.username).gender == "Female"
    assert User.query.get(male.username).gender == "Male"
    # Pre-serialized strings are stored verbatim, not double-encoded.
    assert User.query.get(female.username).interests == '["pre-serialized"]'


def test_create_user_created_at_kwarg_is_accepted_but_ignored(db_session, cache_spy):
    # User has no created_at column; passing the kwarg must neither crash nor persist.
    user = create_user(
        username="frank",
        age=20,
        gender="Male",
        bio="b",
        interests=[],
        occupation="o",
        education="e",
        writing_style="w",
        personality_traits=[],
        created_at=datetime(2019, 1, 1),
    )
    assert not hasattr(User, "created_at")
    assert User.query.get(user.username).username == "frank"


def test_duplicate_username_rolls_back_and_session_stays_usable(
    seeded_db, db_session, cache_spy
):
    before = User.query.count()
    with pytest.raises(IntegrityError):
        create_user(
            username="alice",
            age=33,
            gender="Female",
            bio="dup",
            interests=[],
            occupation="o",
            education="e",
            writing_style="w",
            personality_traits=[],
        )
    assert db_session.query(User).count() == before

    survivor = create_user(
        username="grace",
        age=29,
        gender="Female",
        bio="ok after rollback",
        interests=[],
        occupation="o",
        education="e",
        writing_style="w",
        personality_traits=[],
    )
    assert User.query.get(survivor.username) is survivor
    assert User.query.count() == before + 1


# ---------------------------------------------------------------------------
# create_subdeaddit


def test_create_subdeaddit_new(seeded_db, db_session, cache_spy):
    sub = create_subdeaddit(
        name="newsub", description="Fresh sub", post_types=["text", "link"]
    )
    fetched = Subdeaddit.query.get(sub.name)
    assert fetched.description == "Fresh sub"
    assert fetched.get_post_types() == ["text", "link"]
    assert cache_spy == ["clear"]


def test_create_subdeaddit_missing_fields_messages(cache_spy):
    with pytest.raises(ContentValidationError) as exc:
        create_subdeaddit(name="", description="")
    assert str(exc.value) == "Subdeaddit missing required fields: name, description"

    with pytest.raises(ContentValidationError) as exc:
        create_subdeaddit(name="onlyname", description="")
    assert str(exc.value) == "Subdeaddit missing required fields: description"


def test_create_subdeaddit_exists_without_update_raises(seeded_db, cache_spy):
    original = Subdeaddit.query.get("testsub")
    with pytest.raises(ContentValidationError) as exc:
        create_subdeaddit(name="testsub", description="Overwrite attempt")
    assert str(exc.value) == "Subdeaddit 'testsub' already exists"
    assert Subdeaddit.query.get("testsub") is original


def test_create_subdeaddit_upsert_updates_existing(seeded_db, db_session, cache_spy):
    updated = create_subdeaddit(
        name="askdeaddit",
        description="Updated description",
        post_types=["poll"],
        update_if_exists=True,
    )
    fetched = Subdeaddit.query.get("askdeaddit")
    assert fetched is updated
    assert fetched.description == "Updated description"
    assert fetched.get_post_types() == ["poll"]
    assert cache_spy == ["clear"]


# ---------------------------------------------------------------------------
# Cache invalidation hook


def test_clear_read_caches_resets_model_cache(app):
    from deaddit.api import get_available_models

    with app.app_context():
        models = get_available_models()
        assert isinstance(models, list)
        assert get_available_models.cache_info().currsize == 1

        content_service._clear_read_caches()
        assert get_available_models.cache_info().currsize == 0
