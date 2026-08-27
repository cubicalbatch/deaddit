"""Phase UX-6: public live-activity ticker, socket pump, and admin keyset API.

Deterministic only: in-memory sqlite, no network, no live endpoints. Socket
coverage uses the socketio test client + synchronous tick() driving; admin
coverage copies the admin_client session fixture from
tests/test_acp2_admin_api.py.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from deaddit import db as _db
from deaddit import live as live_mod
from deaddit.models import (
    Agent,
    AgentRun,
    AgentTurn,
    Comment,
    Post,
    Subdeaddit,
    ToolCall,
    User,
    Vote,
)
from deaddit.runtime.live_pump import get_live_pump, reset_live_pump

_BASE = datetime(2026, 1, 1, 12, 0, 0)

# (start index, group size) of identical-timestamp tie groups, positioned in
# the seeded newest-first event order. Groups sit strictly inside a
# PAGE_SIZE=30 window (page boundaries fall after items 30 and 60) so paging
# never splits one: the per-source keyset predicate resolves cross-kind ties
# Python-side within a page, which is the documented contract.
_TIE_GROUPS = ((8, 3), (38, 3), (66, 3))

_N_EVENTS = 76


@pytest.fixture(autouse=True)
def _fresh_live_pump():
    """No pump thread leaks between tests."""
    reset_live_pump()
    yield
    reset_live_pump()


@pytest.fixture()
def admin_client(client):
    """Client that passes the admin_required gate (ACP2 convention)."""
    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True
    return client


def _register_live_handlers():
    """Bind websocket.py handlers onto the CURRENT SocketIO server instance.

    flask_socketio mints a fresh bare Server per create_app(); the import-time
    decorators only bound to the first one (same convention as
    test_ux5_joblog._register_ux5_handlers).
    """
    from deaddit import websocket as ws_mod
    from deaddit.extensions import socketio

    socketio.on("join_activity", namespace="/live")(ws_mod.join_activity)
    socketio.on("leave_activity", namespace="/live")(ws_mod.leave_activity)
    socketio.on("activity_loaded", namespace="/live")(ws_mod.activity_loaded)


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _event_kinds() -> list[str]:
    """Newest-first kind sequence with cross-kind tie groups baked in."""
    base = ["post", "comment", "vote"]
    kinds = [base[i % 3] for i in range(_N_EVENTS)]
    rotations = iter(([0, 1, 2], [1, 2, 0], [2, 0, 1]))
    for start, size in _TIE_GROUPS:
        rotation = next(rotations)
        for offset, kind_idx in enumerate(rotation[:size]):
            kinds[start + offset] = base[kind_idx]
    return kinds


def _event_timestamps(kinds: list[str]) -> list[datetime]:
    """One fresh older ts per item; tie-group members share their leader's."""
    member_of = {}
    for start, size in _TIE_GROUPS:
        for j in range(start, start + size):
            member_of[j] = start
    times: dict[int, datetime] = {}
    step = 0
    for i in range(len(kinds)):
        leader = member_of.get(i)
        if leader is not None:
            if leader not in times:
                step += 1
                times[leader] = _BASE - timedelta(minutes=step)
            times[i] = times[leader]
        else:
            step += 1
            times[i] = _BASE - timedelta(minutes=step)
    return [times[i] for i in range(len(kinds))]


def _seed_activity_events(db_session):
    """Insert >=70 events across all three kinds with deterministic keys.

    Returns (posts, comments, expected_keys) where expected_keys is the exact
    set of (created_at, kind, id) tuples the ticker must render exactly once.
    """
    kinds = _event_kinds()
    times = _event_timestamps(kinds)
    users = [User(username=n) for n in ("alice", "bob", "carol")]
    sub = Subdeaddit(name="testsub", description="live ticker seed")
    db_session.add_all([*users, sub])

    # Posts first so comments/votes always have targets.
    posts = []
    for n, (_kind, ts) in enumerate(
        [(k, t) for k, t in zip(kinds, times, strict=True) if k == "post"], start=1
    ):
        posts.append(
            Post(
                title=f"Post number {n}",
                content=f"body {n}",
                user=("alice", "bob", "carol")[n % 3],
                subdeaddit_name="testsub",
                model="test-model",
                created_at=ts,
            )
        )
    db_session.add_all(posts)
    db_session.flush()
    anchor = posts[0]
    comments = []
    n_comment = 0
    for kind, ts in zip(kinds, times, strict=True):
        if kind == "comment":
            n_comment += 1
            comments.append(
                Comment(
                    post_id=anchor.id,
                    content=f"Plain comment body {n_comment}",
                    user=("alice", "bob", "carol")[n_comment % 3],
                    model="test-model",
                    created_at=ts,
                )
            )
    db_session.add_all(comments)
    db_session.flush()  # comments need ids before votes reference them

    votes = []
    n_vote = 0
    for kind, ts in zip(kinds, times, strict=True):
        if kind != "vote":
            continue
        n_vote += 1
        on_post = n_vote % 2 == 1
        target = (
            posts[n_vote % len(posts)].id
            if on_post
            else comments[n_vote % len(comments)].id
        )
        votes.append(
            Vote(
                voter=("alice", "bob", "carol")[n_vote % 3],
                post_id=target if on_post else None,
                comment_id=None if on_post else target,
                value=1 if n_vote % 3 else -1,
                source="human",
                created_at=ts,
            )
        )
    db_session.add_all(votes)
    db_session.flush()

    expected: set[tuple[datetime, str, int]] = set()
    counters = {"post": 0, "comment": 0, "vote": 0}
    for kind, ts in zip(kinds, times, strict=True):
        if kind == "post":
            expected.add((ts, "post", posts[counters["post"]].id))
            counters["post"] += 1
        elif kind == "comment":
            expected.add((ts, "comment", comments[counters["comment"]].id))
            counters["comment"] += 1
        elif kind == "vote":
            expected.add((ts, "vote", votes[counters["vote"]].id))
            counters["vote"] += 1
    db_session.commit()
    return posts, comments, expected


# ---------------------------------------------------------------------------
# Fragment parsing / page walking
# ---------------------------------------------------------------------------

_CURSOR_RE = re.compile(r'data-cursor="([^"]+)"')


def _fragment_keys(html: str) -> list[tuple[datetime, str, int]]:
    """Decoded (ts, kind, id) of every rendered <li>, in rendered order."""
    out = []
    for raw in _CURSOR_RE.findall(html):
        decoded = live_mod.decode_cursor(raw)
        assert decoded is not None, f"unrenderable cursor emitted: {raw!r}"
        out.append(decoded)
    return out


def _walk_before_pages(client):
    """Follow ?before= cursors to exhaustion; returns all decoded keys."""
    seen: list[tuple[datetime, str, int]] = []
    url = "/live?fragment=1"
    for _ in range(10):
        resp = client.get(url)
        assert resp.status_code == 200
        keys = _fragment_keys(resp.get_data(as_text=True))
        if not keys:
            return seen
        seen.extend(keys)
        last_ts, last_kind, last_id = seen[-1]
        url = "/live?fragment=1&before=" + live_mod.encode_cursor(
            last_ts, last_kind, last_id
        )
    raise AssertionError("?before= paging did not terminate within 10 pages")


# ---------------------------------------------------------------------------
# 1. Route & fragment modes
# ---------------------------------------------------------------------------


def test_live_full_page_renders_with_nav_on_empty_db(app, client):
    resp = client.get("/live")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "<h1>Live</h1>" in html
    assert 'href="/live"' in html  # base.html nav link survives
    assert "No recent activity to show." in html  # empty state


def test_live_fragment_param_returns_partial_without_html(app, client):
    resp = client.get("/live?fragment=1")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "<html" not in html
    assert "<!DOCTYPE" not in html
    assert "No recent activity to show." in html


def test_live_hx_request_header_is_fragment_equivalent(app, client):
    resp = client.get("/live", headers={"HX-Request": "true"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "<html" not in html


# ---------------------------------------------------------------------------
# 2. Keyset determinism across ?before= / ?since=
# ---------------------------------------------------------------------------


def test_before_walk_yields_every_event_once_in_strict_order(app, client):
    with app.app_context():
        _posts, _comments, expected = _seed_activity_events(_db.session)

        walked = _walk_before_pages(client)

        # Strictly descending (created_at DESC, kind DESC, id DESC).
        assert all(a > b for a, b in zip(walked, walked[1:], strict=False))
        # Every seeded event exactly once; terminal page renders nothing.
        assert len(walked) == len(expected)
        assert set(walked) == expected


def test_since_returns_only_strictly_newer_items(app, client):
    with app.app_context():
        _posts, _comments, _expected = _seed_activity_events(_db.session)

        walked = _walk_before_pages(client)

        # Pivot on a UNIQUE timestamp: ?since= must return exactly the
        # seeded events strictly newer than the cursor, newest-first.
        pivot = walked[5]
        assert sum(1 for k in walked if k[0] == pivot[0]) == 1
        resp = client.get(f"/live?fragment=1&since={live_mod.encode_cursor(*pivot)}")
        assert resp.status_code == 200
        keys = _fragment_keys(resp.get_data(as_text=True))
        assert keys == walked[:5]
        assert all(key > pivot for key in keys)
        assert len(keys) <= live_mod.NEWER_LIMIT

        # Pivot INSIDE a cross-kind tie group: the per-source keyset
        # predicate (contract: "mirror predicate strictly-greater") compares
        # ids across different tables, so same-ts neighbours of other kinds
        # are not guaranteed to come back -- but nothing older than the
        # cursor may ever leak in.
        tie_pivot = walked[10]
        resp = client.get(
            f"/live?fragment=1&since={live_mod.encode_cursor(*tie_pivot)}"
        )
        keys = _fragment_keys(resp.get_data(as_text=True))
        assert all(key > tie_pivot for key in keys)
        assert len(keys) <= live_mod.NEWER_LIMIT


def test_older_fragment_is_bare_items_with_oob_control(app, client):
    """htmx append paging: an older-page fragment is bare <li> nodes (no
    <ol> wrapper) plus an out-of-band #live-older replacement while more
    pages exist, and an oob delete of the control at end of history."""
    with app.app_context():
        _posts, _comments, expected = _seed_activity_events(_db.session)

    first = client.get("/live?fragment=1").get_data(as_text=True)
    keys = _fragment_keys(first)
    assert "<ol" in first  # cursor-less fragment keeps the wrapped layout
    resp = client.get("/live?fragment=1&before=" + live_mod.encode_cursor(*keys[-1]))
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "<ol" not in html and "</ol" not in html
    page_keys = _fragment_keys(html)
    assert all(k < keys[-1] for k in page_keys)
    if len(page_keys) == live_mod.PAGE_SIZE:
        assert 'hx-swap-oob="outerHTML"' in html
    else:
        assert 'hx-swap-oob="delete"' in html


def test_both_cursors_rejected_with_400(app, client):
    cursor = live_mod.encode_cursor(_BASE, "post", 1)

    plain = client.get(f"/live?before={cursor}&since={cursor}")
    assert plain.status_code == 400
    assert "error" in plain.get_json()

    fragment = client.get(f"/live?fragment=1&before={cursor}&since={cursor}")
    assert fragment.status_code == 400
    assert "not both" in fragment.get_data(as_text=True)


def test_garbage_cursor_falls_back_to_first_page(app, client):
    with app.app_context():
        _posts, _comments, _expected = _seed_activity_events(_db.session)
        first_page = _fragment_keys(
            client.get("/live?fragment=1").get_data(as_text=True)
        )

        bads = ("%21%21%21", "not-a-cursor", live_mod.encode_cursor(_BASE, "zzz", 1))
        for bad in bads:
            resp = client.get(f"/live?fragment=1&before={bad}")
            assert resp.status_code == 200
            assert _fragment_keys(resp.get_data(as_text=True)) == first_page
        assert len(first_page) == live_mod.PAGE_SIZE


# ---------------------------------------------------------------------------
# 3. Removed filtering
# ---------------------------------------------------------------------------


def test_removed_posts_and_comments_never_appear(app, client):
    with app.app_context():
        _users = [User(username=n) for n in ("alice", "bob", "carol")]
        sub = Subdeaddit(name="testsub", description="removal seed")
        _db.session.add_all([*_users, sub])
        visible = Post(
            title="Visible post",
            content="b",
            user="alice",
            subdeaddit_name="testsub",
            model="m",
            created_at=_BASE - timedelta(minutes=1),
        )
        removed_post = Post(
            title="Removed post unique-zz",
            content="b",
            user="bob",
            subdeaddit_name="testsub",
            model="m",
            created_at=_BASE - timedelta(minutes=2),
            removed=True,
        )
        _db.session.add_all([visible, removed_post])
        _db.session.flush()
        _db.session.add_all(
            [
                Comment(
                    post_id=visible.id,
                    content="kept comment qq",
                    user="carol",
                    model="m",
                    created_at=_BASE - timedelta(minutes=3),
                ),
                Comment(
                    post_id=visible.id,
                    content="removed comment zz",
                    user="carol",
                    model="m",
                    created_at=_BASE - timedelta(minutes=4),
                    removed=True,
                ),
                Comment(  # fine itself, but its parent post is removed
                    post_id=removed_post.id,
                    content="orphan under removed post yy",
                    user="alice",
                    model="m",
                    created_at=_BASE - timedelta(minutes=5),
                ),
            ]
        )
        _db.session.commit()

        html = client.get("/live?fragment=1").get_data(as_text=True)

    assert "Visible post" in html
    assert "kept comment qq" in html
    assert "Removed post unique-zz" not in html
    assert "removed comment zz" not in html
    assert "orphan under removed post yy" not in html


# ---------------------------------------------------------------------------
# 4. Socket pump (namespace /live, room activity)
# ---------------------------------------------------------------------------


def _join(client):
    client.emit("join_activity", {}, namespace="/live")
    received = client.get_received(namespace="/live")
    joined = [m for m in received if m["name"] == "joined"]
    assert joined and joined[0]["args"][0]["room"] == "activity"


def _names(client):
    return [m["name"] for m in client.get_received(namespace="/live")]


def _counts(client):
    return [
        m["args"][0]["count"]
        for m in client.get_received(namespace="/live")
        if m["name"] == "live_count"
    ]


def _seed_post(db_session, title, minutes, user="alice"):
    post = Post(
        title=title,
        content="b",
        user=user,
        subdeaddit_name="testsub",
        model="m",
        created_at=_BASE + timedelta(minutes=minutes),
    )
    db_session.add(post)
    return post


def test_join_emits_joined_and_starts_pump_thread(app):
    from deaddit.extensions import socketio

    _register_live_handlers()
    with app.app_context():
        _users = [User(username=n) for n in ("alice",)]
        _db.session.add_all(
            [*_users, Subdeaddit(name="testsub", description="pump seed")]
        )
        _seed_post(_db.session, "seed", minutes=0)
        _db.session.commit()

        client = socketio.test_client(app, namespace="/live")
        try:
            _join(client)
            assert get_live_pump().running is True
        finally:
            client.disconnect(namespace="/live")


def test_join_without_payload_is_accepted(app):
    """Real browsers emit join_activity/leave_activity with NO payload; the
    handlers must default the argument instead of raising TypeError (UX-6
    fix-loop: a payload-less join killed every live badge)."""
    from deaddit.extensions import socketio

    _register_live_handlers()
    with app.app_context():
        client = socketio.test_client(app, namespace="/live")
        try:
            client.emit("join_activity", namespace="/live")
            joined = [
                m
                for m in client.get_received(namespace="/live")
                if m["name"] == "joined"
            ]
            assert joined and joined[0]["args"][0]["room"] == "activity"
            assert get_live_pump().running is True
            client.emit("leave_activity", namespace="/live")
            left = [
                m for m in client.get_received(namespace="/live") if m["name"] == "left"
            ]
            assert left
        finally:
            client.disconnect(namespace="/live")


def test_tick_emits_live_count_then_ack_resets_watermark(app):
    from deaddit.extensions import socketio

    _register_live_handlers()
    with app.app_context():
        _users = [User(username=n) for n in ("alice", "bob", "carol")]
        _db.session.add_all(
            [*_users, Subdeaddit(name="testsub", description="pump seed")]
        )
        _seed_post(_db.session, "seed", minutes=0)
        _db.session.commit()

        client = socketio.test_client(app, namespace="/live")
        try:
            _join(client)  # watermark initialised to the seed's ts

            _seed_post(_db.session, "fresh", minutes=1, user="bob")
            _db.session.commit()

            assert get_live_pump().tick() is True
            counts = _counts(client)
            assert counts and counts[-1] >= 1

            # Client ack resets pending AND advances the watermark.
            client.emit(
                "activity_loaded",
                {"ts": (_BASE + timedelta(minutes=1)).isoformat()},
                namespace="/live",
            )
            assert get_live_pump().tick() is True
            assert "live_count" not in _names(client)

            # Only rows past the advanced watermark tick the counter now.
            _seed_post(_db.session, "fresher", minutes=2, user="carol")
            _db.session.commit()
            assert get_live_pump().tick() is True
            assert _counts(client) == [1]

            # Item content never travels over the socket: names only.
            assert set(_names(client)) <= {"live_count"}
        finally:
            client.disconnect(namespace="/live")


def test_leave_stops_emissions_and_idle_thread_exits(app, monkeypatch):
    from deaddit.runtime import live_pump

    monkeypatch.setattr(live_pump, "TICK_INTERVAL_SECONDS", 0.01)
    from deaddit.extensions import socketio

    _register_live_handlers()
    with app.app_context():
        _users = [User(username=n) for n in ("alice", "bob")]
        _db.session.add_all(
            [*_users, Subdeaddit(name="testsub", description="pump seed")]
        )
        _seed_post(_db.session, "seed", minutes=0)
        _db.session.commit()

        client = socketio.test_client(app, namespace="/live")
        try:
            _join(client)
            client.emit("leave_activity", {}, namespace="/live")
            left = [
                m for m in client.get_received(namespace="/live") if m["name"] == "left"
            ]
            assert left and left[0]["args"][0]["room"] == "activity"

            # Rows inserted after leaving must not reach the room...
            _seed_post(_db.session, "post-leave", minutes=1, user="bob")
            _db.session.commit()
            assert get_live_pump().tick() is False  # no participants -> no emit
            assert "live_count" not in _names(client)

            # ...and the daemon thread idles out once the room stays empty.
            deadline = datetime.now() + timedelta(seconds=5)
            while get_live_pump().running and datetime.now() < deadline:
                pass
            assert get_live_pump().running is False
        finally:
            client.disconnect(namespace="/live")


# ---------------------------------------------------------------------------
# 5. Admin runs keyset + tool-call content cards
# ---------------------------------------------------------------------------


def _make_agent(db_session, username="alice"):
    agent = Agent(
        user_username=username,
        autonomy_tier="regular",
        is_enabled=False,
        status="idle",
        config={},
        state={},
        consecutive_failures=0,
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def test_admin_runs_keyset_walk_exhausts_without_duplicates(
    seeded_db, admin_client, db_session
):
    agent = _make_agent(db_session)
    # Inserted oldest-first so autoincrement ids correlate with recency the
    # way production rows do (the before_id cursor assumes that shape).
    shared_slots = {15, 30, 45}
    runs = []
    for i in range(60):
        started = (
            runs[-1].started_at
            if i in shared_slots
            else _BASE - timedelta(minutes=60 - i)
        )
        runs.append(
            AgentRun(
                agent_id=agent.id,
                persona_username=agent.user_username,
                trigger="schedule" if i % 2 else "manual",
                status="completed" if i % 5 else "failed",
                started_at=started,
                # token_usage/finished_at left NULL: must serialize, not raise.
            )
        )
    db_session.add_all(runs)
    db_session.commit()

    expected_ids = [
        r.id
        for r in AgentRun.query.order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
    ]

    seen: list[int] = []
    url = f"/admin/api/agents/{agent.id}/runs"
    pages = 0
    while url:
        resp = admin_client.get(url)
        assert resp.status_code == 200
        body = resp.get_json()
        ids = [r["id"] for r in body["runs"]]
        assert len(ids) <= 25
        # Page ordering agrees with the global (started_at DESC, id DESC).
        positions = [expected_ids.index(i) for i in ids]
        assert positions == sorted(positions)
        seen.extend(ids)
        next_before_id = body["next_before_id"]
        assert next_before_id == (ids[-1] if len(ids) == 25 else None)
        url = (
            f"/admin/api/agents/{agent.id}/runs?before_id={next_before_id}"
            if next_before_id is not None
            else None
        )
        pages += 1

    assert pages == 3  # 25 + 25 + 10 (short terminating page)
    assert len(seen) == 60 and len(set(seen)) == 60
    assert seen == expected_ids

    sample = admin_client.get(f"/admin/api/agents/{agent.id}/runs?limit=1").get_json()
    row = sample["runs"][0]
    assert row["token_usage"] == {}  # null token_usage serializes without raise
    assert row["finished_at"] is None and row["error_message"] is None
    assert sample["next_before_id"] == row["id"]


def test_tool_call_content_cards_resolve_per_contract(
    seeded_db, admin_client, db_session
):
    agent = _make_agent(db_session, username="alice")
    run = AgentRun(
        agent_id=agent.id,
        persona_username=agent.user_username,
        trigger="manual",
        status="completed",
    )
    db_session.add(run)
    db_session.flush()
    turn = AgentTurn(
        run_id=run.id, seq=0, request_messages=[], response_message={}, model="llama3"
    )
    db_session.add(turn)
    db_session.flush()

    post = Post(
        title="Card target post",
        content="b",
        user="alice",
        subdeaddit_name="testsub",
        model="m",
        created_at=_BASE,
    )
    removed_post = Post(
        title="Moderated card post",
        content="b",
        user="bob",
        subdeaddit_name="testsub",
        model="m",
        created_at=_BASE,
        removed=True,
    )
    db_session.add_all([post, removed_post])
    db_session.flush()
    comment = Comment(
        post_id=post.id,
        content="card comment",
        user="bob",
        model="m",
        created_at=_BASE,
    )
    db_session.add(comment)
    db_session.flush()

    def _call(result):
        return ToolCall(
            run_id=run.id,
            turn_id=turn.id,
            name="create_post",
            arguments={},
            result=result,
            ok=True,
            duration_ms=1,
        )

    db_session.add_all(
        [
            _call({"post_id": post.id}),
            _call({"comment_id": comment.id}),
            _call({"post_id": removed_post.id}),
            _call({"truncated": True, "preview": "too big to keep"}),
            _call({"post_id": 999999}),  # hard-deleted row
            _call("not-a-dict"),
        ]
    )
    db_session.commit()

    body = admin_client.get(f"/admin/api/turns/{turn.id}/tool_calls").get_json()
    cards = [entry["content"] for entry in body["tool_calls"]]

    assert cards[0] == {
        "kind": "post",
        "href": f"/d/testsub/{post.id}",
        "label": "Card target post",
        "removed": False,
    }
    assert cards[1]["kind"] == "comment"
    assert cards[1]["href"].endswith(f"#comment-{comment.id}")
    assert cards[1]["removed"] is False
    assert cards[2]["removed"] is True
    assert cards[2]["href"] is None
    assert cards[2]["label"].endswith("(removed)")
    assert cards[3] is None  # preview wrapper -> null
    assert cards[4] is None  # missing row -> null
    assert cards[5] is None  # non-dict result -> null
