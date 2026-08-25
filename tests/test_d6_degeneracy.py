"""Phase D6: anti-degeneracy detectors, demotion, rate limits (plan §7)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from deaddit.dynamics import degeneracy
from deaddit.dynamics.degeneracy import (
    REPETITION_THRESHOLD,
    detect_repetition_for_comment,
    trigram_jaccard,
    with_repetition_demotion,
)
from deaddit.dynamics.ranking import post_order_by
from deaddit.models import (
    ActivityEvent,
    DegeneracyFlag,
    Post,
    Setting,
    Subdeaddit,
    User,
    Vote,
)
from deaddit.services.content import ContentValidationError, create_comment, create_post


def _ensure_user(db_session, name: str) -> None:
    if db_session.get(User, name) is None:
        db_session.add(User(username=name))


def _ensure_sub(db_session, name: str) -> None:
    if db_session.get(Subdeaddit, name) is None:
        db_session.add(Subdeaddit(name=name, description=f"{name} fixture"))


@pytest.fixture()
def author_and_post(app, db_session, seeded_db):
    """A dedicated author plus one open post to comment on."""
    _ensure_user(db_session, "spammy")
    _ensure_sub(db_session, "d6test")
    db_session.commit()
    return create_post(
        title="D6 target thread",
        content="A perfectly ordinary opening post about topic t.",
        user="spammy",
        subdeaddit="d6test",
    )


class TestTrigramJaccard:
    def test_identical_text_is_full_overlap(self):
        assert trigram_jaccard("hello world", "hello   WORLD") == 1.0

    def test_disjoint_text_is_zero(self):
        assert trigram_jaccard("alpha beta", "gamma delta") == 0.0

    def test_empty_side_is_zero(self):
        assert trigram_jaccard("", "hello world") == 0.0
        assert trigram_jaccard(None, None) == 0.0

    def test_partial_overlap_hand_computed(self):
        # "abcdef" -> {abc,bcd,cde,def}; "abcxyz" -> {abc,bcx,cxy,xyz}
        expected = 1 / 7  # |{abc}| / |7-item union|
        assert trigram_jaccard("abcdef", "abcxyz") == pytest.approx(expected)


class TestRepetitionDetector:
    def test_duplicate_comment_flags_and_threshold_holds(
        self, app, db_session, seeded_db, author_and_post
    ):
        original = (
            "I really think this exact sentence is the best take available today."
        )
        create_comment(post_id=author_and_post.id, content=original, user="alice")
        duplicate = (
            "I REALLY think this exact sentence is the best take available TODAY!"
        )
        comment = create_comment(post_id=author_and_post.id, content=duplicate, user="bob")

        flags = DegeneracyFlag.query.filter_by(kind="repetition").all()
        assert len(flags) == 1
        flag = flags[0]
        assert flag.username == "bob"
        assert flag.comment_id == comment.id
        assert flag.metric > REPETITION_THRESHOLD
        # The detector threshold is plan-fixed at >0.6; metric equals overlap.
        assert trigram_jaccard(original, duplicate) == pytest.approx(flag.metric)

    def test_distinct_comment_does_not_flag(
        self, app, db_session, seeded_db, author_and_post
    ):
        create_comment(
            post_id=author_and_post.id, content="First distinct view here.", user="alice"
        )
        create_comment(
            post_id=author_and_post.id,
            content="Completely different musings about gardening tools.",
            user="alice",
        )
        assert DegeneracyFlag.query.filter_by(kind="repetition").count() == 0

    def test_detection_failure_cannot_fail_creation(
        self, app, db_session, seeded_db, author_and_post, monkeypatch
    ):
        def boom(*args, **kwargs):
            raise RuntimeError("detector exploded")

        monkeypatch.setattr(degeneracy, "trigram_jaccard", boom)
        comment = create_comment(
            post_id=author_and_post.id, content="Some fresh text.", user="alice"
        )
        assert comment.id is not None
        assert DegeneracyFlag.query.count() == 0

    def test_flag_idempotent_per_comment(
        self, app, db_session, seeded_db, author_and_post
    ):
        dup = "Again I really think this exact sentence is the best take around."
        create_comment(
            post_id=author_and_post.id, content=dup + " v1 marker here.", user="alice"
        )
        comment = create_comment(post_id=author_and_post.id, content=dup, user="bob")
        first_count = DegeneracyFlag.query.filter_by(kind="repetition").count()
        assert first_count >= 1
        # Re-running detection on the same committed comment must not double-flag.
        detect_repetition_for_comment(comment)
        assert DegeneracyFlag.query.filter_by(kind="repetition").count() == first_count


class TestHotDemotion:
    def test_no_flags_returns_clauses_unchanged(self, app, db_session):
        clauses = post_order_by("hot")
        assert with_repetition_demotion(clauses) is clauses

    def test_flagged_author_demoted_half_hot_weight(self, app, db_session, seeded_db):
        for name in ("echo_a", "clean_b"):
            _ensure_user(db_session, name)
        _ensure_sub(db_session, "demo")
        db_session.commit()

        now = datetime.utcnow()
        # echo_a's higher score leads hot outright before demotion.
        loud = Post(title="loud", content="x", score=100, user="echo_a", subdeaddit_name="demo")
        quiet = Post(title="quiet", content="y", score=10, user="clean_b", subdeaddit_name="demo")
        db_session.add_all([loud, quiet])
        db_session.commit()

        feed_query = Post.query.filter(Post.removed.is_(False))
        unflagged_order = [*post_order_by("hot")]
        assert feed_query.order_by(*unflagged_order).first().user == "echo_a"

        db_session.add(
            DegeneracyFlag(
                kind="repetition",
                username="echo_a",
                metric=0.9,
                created_at=datetime.utcnow(),
            )
        )
        db_session.commit()

        demoted_order = with_repetition_demotion(post_order_by("hot"))
        assert demoted_order is not unflagged_order
        assert feed_query.order_by(*demoted_order).first().user == "clean_b"

        # Mirror check: demotion halves the frozen hot key.
        base = degeneracy.hot_rank_key_demoted(
            score=100, created_at=now, now=now, demoted=False
        )
        key = degeneracy.hot_rank_key_demoted(
            score=100, created_at=now, now=now, demoted=True
        )
        assert key == pytest.approx(base * 0.5)

    def test_expired_window_stops_demotion(self, app, db_session, seeded_db):
        _ensure_sub(db_session, "demo")
        db_session.commit()
        db_session.add(
            DegeneracyFlag(
                kind="repetition",
                username="ancient",
                metric=0.9,
                created_at=datetime.utcnow() - timedelta(days=30),
            )
        )
        db_session.commit()
        assert "ancient" not in degeneracy.flagged_hot_authors()


class TestRateLimits:
    def test_sixth_post_within_hour_rejected_rate_limited(
        self, app, db_session, seeded_db
    ):
        _ensure_user(db_session, "capper")
        _ensure_sub(db_session, "rl")
        db_session.commit()
        for i in range(5):
            create_post(
                title=f"rate limit probe {i}",
                content=f"body {i} with unique suffix {i}-xyz.",
                user="capper",
                subdeaddit="rl",
            )
        with pytest.raises(ContentValidationError) as err:
            create_post(
                title="one too many",
                content="overflow body unique-overflow-42.",
                user="capper",
                subdeaddit="rl",
            )
        assert str(err.value) == "rate_limited"

    def test_setting_tunable_cap_and_disable(self, app, db_session, seeded_db):
        _ensure_user(db_session, "capper")
        _ensure_sub(db_session, "rl2")
        db_session.commit()
        Setting.set_value("rate_limit_comments_per_hour", "2")
        pid = create_post(
            title="comment cap host",
            content="host body unique-host-77.",
            user="capper",
            subdeaddit="rl2",
        ).id
        create_comment(post_id=pid, content="one unique alpha.", user="capper")
        create_comment(post_id=pid, content="two unique bravo.", user="capper")
        with pytest.raises(ContentValidationError) as err:
            create_comment(post_id=pid, content="three unique charlie.", user="capper")
        assert str(err.value) == "rate_limited"

        Setting.set_value("rate_limit_comments_per_hour", "-1")
        create_comment(post_id=pid, content="four unique delta allowed.", user="capper")

    def test_non_hot_sorts_pass_through_unchanged(self, app, db_session):
        """Demotion wraps ONLY the hot fragment; new/top/rising untouched."""
        db_session.add(
            DegeneracyFlag(
                kind="repetition",
                username="x",
                metric=0.9,
                created_at=datetime.utcnow(),
            )
        )
        db_session.commit()
        for sort in ("new", "top", "rising"):
            clauses = post_order_by(sort)
            assert with_repetition_demotion(clauses) is clauses
        hot = post_order_by("hot")
        assert with_repetition_demotion(hot) is not hot

    def test_backdated_rows_do_not_trip_limit(self, app, db_session, seeded_db):
        """D5 seeder safety: backfilled created_at rows never hit the cap."""
        _ensure_user(db_session, "oldposter")
        _ensure_sub(db_session, "rl3")
        db_session.commit()
        old = datetime.utcnow() - timedelta(hours=3)
        for i in range(8):  # default cap is 5/hour
            create_post(
                title=f"backdated {i}",
                content=f"old body {i} backfill-marker-{i}.",
                user="oldposter",
                subdeaddit="rl3",
                created_at=old,
            )


class TestActivityEventsAndNightly:
    def test_events_emitted_for_actions(self, app, db_session, seeded_db):
        from deaddit.dynamics.moderation import report_content
        from deaddit.dynamics.votes import cast_vote

        _ensure_sub(db_session, "ev")
        db_session.commit()
        post = create_post(
            title="event source",
            content="eventful body q-99.",
            user="alice",
            subdeaddit="ev",
        )
        create_comment(post_id=post.id, content="reply text e-1.", user="bob")
        result = cast_vote("bob", "post", post.id, 1)
        assert result["status"] == "ok"
        report_content("bob", "post", post.id, "spam reason")

        kinds = {(row.event_type, row.username) for row in ActivityEvent.query.all()}
        assert ("post", "alice") in kinds
        assert ("comment", "bob") in kinds
        assert ("vote", "bob") in kinds
        assert ("report", "bob") in kinds

    def test_event_emission_failure_isolated(self, app, db_session):
        """record_event swallows its own internal failures (bad meta JSON)."""
        from deaddit.dynamics.activity import record_event

        class Unserializable:
            pass

        record_event(event_type="post", username="x", meta={"k": Unserializable()})
        assert ActivityEvent.query.count() == 0

    def test_nightly_registration_includes_d6_jobs(self, app, db_session):
        from deaddit.runtime.nightly import NIGHTLY_JOBS, register_nightly_jobs

        ids = {job.id for job in NIGHTLY_JOBS}
        assert {"dynamics-platform-rollup", "dynamics-degeneracy-scan"} <= ids

        class FakeScheduler:
            def __init__(self):
                self.added = []

            def add_job(self, func, trigger, **kwargs):
                self.added.append(kwargs.get("id"))

        sched = FakeScheduler()
        registered = register_nightly_jobs(sched)
        assert set(registered) == ids
        assert set(sched.added) == ids


class TestCommunityScans:
    def test_echo_chamber_scan_fires_and_is_idempotent_daily(
        self, app, db_session, seeded_db
    ):
        for name in ("power", "u1", "u2", "u3"):
            _ensure_user(db_session, name)
        _ensure_sub(db_session, "echo")
        db_session.commit()
        now = datetime.utcnow()
        # One extreme participant vs three single-action users: gini([1,1,1,100])
        # = 2(6+400)/(4·103) − 5/4 ≈ 0.721 > 0.7.
        db_session.add_all(
            [
                Post(
                    title=f"dom {i}",
                    content="c",
                    score=1,
                    user="power",
                    subdeaddit_name="echo",
                )
                for i in range(100)
            ]
        )
        for i, u in enumerate(("u1", "u2", "u3")):
            db_session.add(
                Post(title=f"once {i}", content="c", score=0, user=u, subdeaddit_name="echo")
            )
        db_session.commit()

        assert degeneracy.scan_echo_chambers(now=now) == 1
        rows = DegeneracyFlag.query.filter_by(
            kind="echo_chamber", subdeaddit_name="echo"
        ).all()
        assert len(rows) == 1
        assert rows[0].metric >= 0.7
        # Re-run within the dedupe day: no duplicate.
        assert degeneracy.scan_echo_chambers(now=now) == 0
        assert (
            DegeneracyFlag.query.filter_by(
                kind="echo_chamber", subdeaddit_name="echo"
            ).count()
            == 1
        )

    def test_brigading_scan_detects_overlapping_pairs(
        self, app, db_session, seeded_db
    ):
        for name in ("victim", "ring_a", "ring_b", "lone"):
            _ensure_user(db_session, name)
        _ensure_sub(db_session, "brig")
        db_session.commit()
        targets = []
        for i in range(12):
            p = Post(
                title=f"t{i}", content="c", score=2, user="victim", subdeaddit_name="brig"
            )
            db_session.add(p)
            targets.append(p)
        db_session.commit()
        now = datetime.utcnow()
        for p in targets:
            db_session.add(Vote(voter="ring_a", value=1, post_id=p.id))
            db_session.add(Vote(voter="ring_b", value=1, post_id=p.id))
        db_session.add(Vote(voter="lone", value=1, post_id=targets[0].id))
        db_session.commit()

        flagged = degeneracy.scan_brigading(now=now)
        pair_flags = [
            f
            for f in DegeneracyFlag.query.filter_by(kind="brigading")
            if f.username == "ring_a|ring_b"
        ]
        assert flagged >= 1
        assert len(pair_flags) == 1
        # Idempotent within the dedupe day.
        assert degeneracy.scan_brigading(now=now) == 0
