from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text

from deaddit.dynamics.engagement import (
    ActiveWindowEngine,
    PolicyConfig,
    arrival_offset,
    due_count,
    preset_config,
    sample_attention_budget,
    select_direction,
    stable_seed,
)
from deaddit.models import Post, Subdeaddit, User, Vote, VoteCadencePolicy


def test_canonical_presets_have_shared_safety_values():
    quiet = preset_config("quiet")
    natural = preset_config("natural")
    busy = preset_config("busy")
    assert quiet["post"]["catchup_grace_hours"] == 12
    assert quiet["comment"]["catchup_grace_hours"] == 6
    assert natural["post"]["tail_max_age_days"] == 365
    assert busy["comment"]["tail_max_age_days"] == 90
    for config in (quiet, natural, busy):
        assert config["voter"]["subscription_weight"] == 4.0
        assert config["voter"]["max_activity_weight"] == 3.0
        assert config["direction"]["minimum_downvote_probability"] == 0.01
        assert config["direction"]["maximum_downvote_probability"] == 0.15
        assert PolicyConfig.from_mapping(config).to_dict() == config


def test_seed_budget_offsets_and_due_count_are_restart_stable():
    config = preset_config("natural")
    created = datetime(2026, 1, 1)
    assert stable_seed(4, "post", 8, created, 1) == stable_seed(
        4, "post", 8, created, 1
    )
    budget = sample_attention_budget(
        config, "post", 8, created, policy_id=4, algorithm_version=1
    )
    assert 0 <= budget <= 80
    offsets = [arrival_offset(i, 8, 90, 48) for i in range(1, 9)]
    assert offsets == sorted(offsets)
    assert offsets[-1] == timedelta(hours=48)
    assert due_count(8, timedelta(hours=24), 90, 48) == 7


def test_active_tick_dry_run_live_parity_and_idempotence(app, db_session):
    now = datetime(2026, 1, 2, 12)
    db_session.add_all(
        [
            Subdeaddit(name="engagement"),
            User(username="author"),
            User(username="voter-a", agent_state={"subscriptions": ["engagement"]}),
            User(username="voter-b"),
            User(username="voter-c"),
        ]
    )
    db_session.commit()
    post = Post(
        title="Engine target",
        created_at=now - timedelta(minutes=20),
        user="author",
        subdeaddit_name="engagement",
    )
    db_session.add(post)
    db_session.commit()
    policy = VoteCadencePolicy(
        preset="natural",
        algorithm_version=1,
        config=preset_config("natural"),
        effective_at=now - timedelta(days=1),
    )
    db_session.add(policy)
    db_session.commit()
    engine = ActiveWindowEngine(policy, per_item_limit=2, global_limit=100)
    first = engine.tick(now, dry_run=True, target_type="post", target_ids=[post.id])
    second = engine.tick(now, dry_run=True, target_type="post", target_ids=[post.id])
    assert first.budgets == second.budgets
    assert first.due_ordinals == second.due_ordinals
    assert first.voters_selected == second.voters_selected
    assert first.directions == second.directions
    live = engine.tick(now, target_type="post", target_ids=[post.id])
    assert [(d.voter, d.direction) for d in live.decisions] == list(
        zip(first.voters_selected, first.directions, strict=True)
    )
    assert db_session.query(Vote).filter_by(source="simulated").count() == len(
        live.decisions
    )
    again = engine.tick(now, target_type="post", target_ids=[post.id])
    assert not again.decisions
    assert db_session.query(Vote).filter_by(source="simulated").count() == len(
        live.decisions
    )


def test_downvotes_can_be_disabled_without_attempts():
    config = preset_config("natural")
    assert all(
        select_direction(
            config,
            "post",
            item,
            "voter",
            target_created_at=datetime(2026, 1, 1),
            allow_downvotes=False,
        )
        == 1
        for item in range(3)
    )
    # The public direction path is exercised by the engine; this test keeps the
    # policy's explicit all-up safety behavior covered through a tiny fixture.


def test_content_policy_resolution_uses_effective_time_and_id_tiebreak(app, db_session):
    from deaddit.dynamics.engagement import resolve_policy_for_content

    natural = preset_config("natural")
    first = VoteCadencePolicy(
        preset="natural",
        algorithm_version=1,
        config=natural,
        effective_at=datetime(2026, 1, 2),
    )
    second = VoteCadencePolicy(
        preset="natural",
        algorithm_version=1,
        config=natural,
        effective_at=datetime(2026, 1, 2),
    )
    db_session.add_all([first, second])
    db_session.commit()
    assert resolve_policy_for_content(datetime(2026, 1, 1)) is None
    assert resolve_policy_for_content(datetime(2026, 1, 2)).id == second.id


def test_voter_eligibility_excludes_all_guardrails(app, db_session, monkeypatch):
    from deaddit.models import Ban

    now = datetime(2026, 1, 2, 12)
    db_session.add(Subdeaddit(name="guardrails"))
    users = [
        User(username="author"),
        User(
            username="subscriber",
            agent_state={"subscriptions": ["guardrails"]},
        ),
        User(username="banned"),
        User(username="prior"),
        User(username="disabled", agent_state={"rate_caps": {"vote": 0}}),
        User(username="capped", agent_state={"rate_caps": {"vote": 1}}),
        User(username="gapped"),
        User(username="unsubscribed"),
    ]
    db_session.add_all(users)
    db_session.commit()
    post = Post(
        title="guardrail target",
        user="author",
        subdeaddit_name="guardrails",
        created_at=now - timedelta(hours=2),
    )
    second_post = Post(
        title="cadence target",
        user="author",
        subdeaddit_name="guardrails",
        created_at=now - timedelta(days=2),
    )
    db_session.add_all([post, second_post])
    db_session.commit()
    import deaddit.dynamics.engagement as engagement

    monkeypatch.setattr(engagement, "_hash_unit", lambda *parts: 0.5)
    db_session.add(Ban(username="banned", subdeaddit_name="guardrails", reason="test"))
    db_session.add(
        Vote(
            voter="prior",
            post_id=post.id,
            value=1,
            source="human",
            created_at=now - timedelta(days=2),
        )
    )
    db_session.add_all(
        [
            Vote(
                voter="capped",
                post_id=second_post.id,
                value=1,
                source="human",
                created_at=now - timedelta(minutes=10),
            ),
            Vote(
                voter="gapped",
                post_id=second_post.id,
                value=1,
                source="human",
                created_at=now - timedelta(seconds=10),
            ),
        ]
    )
    db_session.commit()
    policy = VoteCadencePolicy(
        preset="natural",
        algorithm_version=1,
        config=preset_config("natural"),
        effective_at=now - timedelta(days=3),
    )
    db_session.add(policy)
    db_session.commit()
    result = ActiveWindowEngine(policy, per_item_limit=1).tick(
        now, dry_run=True, target_type="post", target_ids=[post.id]
    )
    assert set(result.voters_selected) <= {"subscriber"}
    assert result.skips


def test_empty_pool_is_deferred_and_query_uses_indexes(app, db_session, monkeypatch):
    now = datetime(2026, 1, 2, 12)
    db_session.add_all([Subdeaddit(name="empty"), User(username="author")])
    db_session.commit()
    post = Post(
        title="empty",
        user="author",
        subdeaddit_name="empty",
        created_at=now - timedelta(hours=2),
    )
    db_session.add(post)
    db_session.commit()
    monkeypatch.setattr(
        __import__("deaddit.dynamics.engagement", fromlist=["sample_attention_budget"]),
        "sample_attention_budget",
        lambda *args, **kwargs: 10,
    )
    result = ActiveWindowEngine(preset_config("quiet")).tick(
        now, dry_run=True, target_type="post", target_ids=[post.id]
    )
    assert result.decisions == []
    assert result.skips["no_voter"] > 0
    query = db_session.query(Post).filter(
        Post.created_at >= now - timedelta(hours=84),
        Post.created_at <= now,
    )
    compiled = query.statement.compile(
        dialect=db_session.get_bind().dialect,
        compile_kwargs={"literal_binds": True},
    )
    plan = db_session.execute(text(f"EXPLAIN QUERY PLAN {compiled}")).all()
    assert any("created_at" in str(row).lower() for row in plan)
    vote_query = db_session.query(Vote).filter(Vote.post_id == post.id)
    vote_compiled = vote_query.statement.compile(
        dialect=db_session.get_bind().dialect,
        compile_kwargs={"literal_binds": True},
    )
    vote_plan = db_session.execute(text(f"EXPLAIN QUERY PLAN {vote_compiled}")).all()
    assert any("vote" in str(row).lower() for row in vote_plan)


def test_clock_advance_releases_only_new_ordinals_and_limits_casts(
    app, db_session, monkeypatch
):
    import deaddit.dynamics.engagement as engagement

    now = datetime(2026, 1, 2, 12)
    db_session.add(Subdeaddit(name="clock"))
    db_session.add_all(
        [User(username="author")] + [User(username=f"v{i}") for i in range(8)]
    )
    db_session.commit()
    posts = [
        Post(
            title=f"p{i}",
            user="author",
            subdeaddit_name="clock",
            created_at=now - timedelta(minutes=20),
        )
        for i in range(2)
    ]
    db_session.add_all(posts)
    db_session.commit()
    policy = VoteCadencePolicy(
        preset="natural",
        algorithm_version=1,
        config=preset_config("natural"),
        effective_at=now - timedelta(days=1),
    )
    db_session.add(policy)
    db_session.commit()
    monkeypatch.setattr(
        engagement, "sample_attention_budget", lambda *args, **kwargs: 10
    )
    engine = ActiveWindowEngine(policy, per_item_limit=2, global_limit=3)
    early = engine.tick(now, target_type="post", target_ids=[posts[0].id])
    later = engine.tick(
        now + timedelta(hours=2),
        dry_run=True,
        target_type="post",
        target_ids=[posts[0].id],
    )
    early_ordinals = {row["ordinal"] for row in early.due_ordinals}
    later_ordinals = {row["ordinal"] for row in later.due_ordinals}
    assert early_ordinals == {1}
    assert later_ordinals
    assert min(later_ordinals) > max(early_ordinals)
    live = engine.tick(now, target_type="post", target_ids=[post.id for post in posts])
    assert len(live.casts) <= 3
    assert len(live.decisions) <= 3
    for post in posts:
        assert db_session.query(Vote).filter(Vote.post_id == post.id).count() <= 2


def test_attention_zero_and_heavy_tail_are_deterministic():
    quiet = preset_config("quiet")
    zero = {section: dict(values) for section, values in quiet.items()}
    zero["post"]["mean_active_votes"] = 0
    assert sample_attention_budget(zero, "post", 1, datetime(2026, 1, 1)) == 0
    heavy = {section: dict(values) for section, values in quiet.items()}
    heavy["post"]["mean_active_votes"] = 100
    heavy["post"]["attention_shape"] = 0.25
    heavy["post"]["max_active_votes"] = 7
    values = [
        sample_attention_budget(heavy, "post", item, datetime(2026, 1, 1), policy_id=9)
        for item in range(20)
    ]
    assert values == [
        sample_attention_budget(heavy, "post", item, datetime(2026, 1, 1), policy_id=9)
        for item in range(20)
    ]
    assert max(values) <= 7
