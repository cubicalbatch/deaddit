"""Visitor voting: anonymous cookie identity, dedup, toggle-off, rate limit.

Covers the /api/vote endpoint end to end — lazy cookie issuance, cast_vote's
visitor path (upsert + clear semantics), one-vote-per-browser dedup, the
in-RAM per-IP rate limit, and the server-rendered voted state in templates.
"""

from __future__ import annotations

import pytest

from deaddit import api as api_module
from deaddit.extensions import db
from deaddit.models import User, Vote


def _vote(client, post_id, value):
    return client.post(
        "/api/vote",
        json={"target": "post", "id": post_id, "value": value},
    )


@pytest.fixture(autouse=True)
def _fresh_rate_window():
    """The rate limiter is module state keyed by IP; isolate every test."""
    api_module._vote_hits.clear()
    yield
    api_module._vote_hits.clear()


def test_first_vote_issues_cookie_and_writes_human_row(client, seeded_db):
    post = seeded_db["posts"][1]  # bob's post
    response = _vote(client, post.id, 1)

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    assert body["score"] == 1
    assert body["my_vote"] == 1

    assert "deaddit_voter=" in response.headers.get("Set-Cookie", "")

    vote = Vote.query.one()
    assert vote.voter is None
    assert vote.source == "human"
    assert vote.value == 1
    assert len(vote.visitor_hash) == 64
    db.session.refresh(post)
    assert (post.score, post.vote_count) == (1, 1)
    assert db.session.get(User, "bob").post_karma == 1


def test_same_cookie_one_vote_per_post(client, seeded_db):
    post = seeded_db["posts"][1]
    assert _vote(client, post.id, 1).get_json()["changed"] is True
    # Same-value re-vote: pure no-op, still one row.
    again = _vote(client, post.id, 1)
    assert again.get_json() == {
        "status": "ok",
        "score": 1,
        "changed": False,
        "change_kind": "same_value_noop",
        "my_vote": 1,
    }
    assert Vote.query.count() == 1


def test_switch_and_toggle_off_round_trip(client, seeded_db):
    post = seeded_db["posts"][1]
    _vote(client, post.id, 1)
    switched = _vote(client, post.id, -1)
    assert switched.get_json()["score"] == -1
    assert switched.get_json()["my_vote"] == -1
    assert Vote.query.one().value == -1

    cleared = _vote(client, post.id, 0)
    body = cleared.get_json()
    assert (body["score"], body["my_vote"], body["change_kind"]) == (0, 0, "remove")
    assert Vote.query.count() == 0
    db.session.refresh(post)
    assert (post.score, post.vote_count) == (0, 0)
    assert db.session.get(User, "bob").post_karma == 0


def test_second_browser_is_an_independent_voter(app, client, seeded_db):
    post = seeded_db["posts"][1]
    _vote(client, post.id, 1)

    other = app.test_client()  # fresh cookie jar = fresh anonymous identity
    assert _vote(other, post.id, -1).get_json()["score"] == 0

    values = {vote.visitor_hash: vote.value for vote in Vote.query.all()}
    assert sorted(values.values()) == [-1, 1]
    db.session.refresh(post)
    assert post.vote_count == 2


def test_visitor_votes_do_not_collide_with_user_votes(client, seeded_db):
    from deaddit.dynamics.votes import cast_vote

    post = seeded_db["posts"][1]
    cast_vote("alice", "post", post.id, 1)
    _vote(client, post.id, 1)
    assert Vote.query.count() == 2
    db.session.refresh(post)
    assert post.score == 2


def test_malformed_requests_are_rejected(client):
    assert client.post("/api/vote", json={}).status_code == 400
    assert (
        client.post(
            "/api/vote", json={"target": "user", "id": 1, "value": 1}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/vote", json={"target": "post", "id": 1, "value": 5}
        ).status_code
        == 400
    )
    # Cross-site form posts cannot send JSON: body is not JSON-parseable.
    assert (
        client.post(
            "/api/vote",
            data="target=post&id=1&value=1",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 400
    )


def test_removed_post_rejection_uses_frozen_reason(client, seeded_db):
    post = seeded_db["posts"][1]
    _vote(client, post.id, 1)
    post.removed = True
    db.session.commit()

    body = _vote(client, post.id, -1).get_json()
    assert body["status"] == "rejected"
    assert body["reason"] == f"post {post.id} was removed"


def test_rate_limit_blocks_bursts(client, seeded_db, monkeypatch):
    monkeypatch.setattr(api_module, "_VOTES_PER_MINUTE", 2)
    post = seeded_db["posts"][1]
    assert _vote(client, post.id, 1).status_code == 200
    assert _vote(client, post.id, 0).status_code == 200
    assert _vote(client, post.id, 1).status_code == 429


def test_rendered_feed_shows_voted_state(client, seeded_db):
    post = seeded_db["posts"][1]
    _vote(client, post.id, 1)

    index = client.get("/").get_data(as_text=True)
    assert f'data-target-id="{post.id}"' in index
    assert "vote-up is-upvoted" in index

    detail = client.get(f"/d/{post.subdeaddit_name}/{post.id}").get_data(as_text=True)
    assert "vote-up is-upvoted" in detail

    down = _vote(client, post.id, -1)
    assert down.get_json()["my_vote"] == -1
    detail = client.get(f"/d/{post.subdeaddit_name}/{post.id}").get_data(as_text=True)
    assert "vote-down is-downvoted" in detail
    assert "vote-up is-upvoted" not in detail


def test_page_views_never_receive_a_voter_cookie(client, seeded_db):
    response = client.get("/")
    assert "deaddit_voter" not in response.headers.get("Set-Cookie", "")


def test_karma_repair_keeps_visitor_votes(client, seeded_db):
    from deaddit.dynamics.karma import recompute_scores_and_karma

    post = seeded_db["posts"][1]
    _vote(client, post.id, 1)

    post.score = 99  # corrupt the aggregate away from Vote truth
    db.session.commit()

    recompute_scores_and_karma()
    db.session.refresh(post)
    assert post.score == 1
    assert db.session.get(User, "bob").post_karma == 1
