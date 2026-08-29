"""Forced post mix: RNG seed parity and categorical distribution tests.

Phase 4: the mix lives in the pinned immutable ``agent.visit_profile``
document, not in Config settings.
"""

from __future__ import annotations

import random
from unittest.mock import patch

from deaddit.agents.prompts import prepare_agent_visit
from deaddit.models import Agent, User

from tests.visit_profiles import pin_intent_mix


def _make_test_agent(db_session, username="alice", **kwargs):
    user = db_session.get(User, username)
    if not user:
        user = User(username=username)
        db_session.add(user)
        db_session.flush()

    config = kwargs.get(
        "config",
        {
            "image_posts": {
                "enabled": True,
                "policy": "optional",
                "provider_id": 1,
                "model": None,
            },
            "website_posts": {"enabled": True, "policy": "optional"},
        },
    )
    agent = Agent(
        user_username=username,
        autonomy_tier=kwargs.get("tier", "regular"),
        is_enabled=True,
        status="idle",
        config=config,
        state={},
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def _kickoff(db_session, agent, **kwargs):
    """Prepare one visit and return (kickoff text, resolved intent)."""
    visit = prepare_agent_visit(
        agent, db_session.get(User, agent.user_username), **kwargs
    )
    return visit.messages[1]["content"], visit.plan.intent


def test_seeded_parity_under_default_zero_forced_chances(seeded_db, db_session):
    """Under a 0.30/0/0 profile mix the RNG consumption must match the legacy
    single-roll behaviour exactly."""
    agent = _make_test_agent(db_session, "alice")
    pin_intent_mix(agent, post=0.30, image=0.0, website=0.0)

    for seed in range(50):
        random.seed(seed)
        prompt, intent = _kickoff(db_session, agent, unread=0)

        # Re-run the length-quantile and intent RNG consumption manually.
        random.seed(seed)
        _length_quantile = random.choices(range(100), k=1)[0]
        legacy_is_post = random.random() < 0.30

        if legacy_is_post:
            assert intent == "post"
            assert (
                "create_post" in prompt
                or "create_image_post" in prompt
                or "create_website" in prompt
            )
        else:
            assert intent == "browse"
            assert "browse" in prompt.lower()


def test_categorical_interval_boundaries(seeded_db, db_session):
    """Verify exact categorical interval mapping [0, image_share), [image_share, image_share + website_share), [sum, 1)."""
    agent = _make_test_agent(db_session, "alice")
    pin_intent_mix(agent, post=1.0, image=0.20, website=0.30)

    # Image slice: r < 0.20
    with patch(
        "random.random", side_effect=[0.5, 0.10]
    ):  # 1st roll post-chance (0.5 < 1.0), 2nd roll 0.10
        _, intent = _kickoff(db_session, agent, unread=0)
        assert intent == "image"

    # Image boundary exact
    with patch("random.random", side_effect=[0.5, 0.19999]):
        _, intent = _kickoff(db_session, agent, unread=0)
        assert intent == "image"

    # Website slice: 0.20 <= r < 0.50
    with patch("random.random", side_effect=[0.5, 0.20]):
        _, intent = _kickoff(db_session, agent, unread=0)
        assert intent == "website"

    with patch("random.random", side_effect=[0.5, 0.49999]):
        _, intent = _kickoff(db_session, agent, unread=0)
        assert intent == "website"

    # Post remainder slice: r >= 0.50
    with patch("random.random", side_effect=[0.5, 0.50]):
        _, intent = _kickoff(db_session, agent, unread=0)
        assert intent == "post"

    with patch("random.random", side_effect=[0.5, 0.99]):
        _, intent = _kickoff(db_session, agent, unread=0)
        assert intent == "post"


def test_ineligible_selected_slices_degrade_to_post_without_transfer(
    seeded_db, db_session
):
    """An agent without website capability that draws a 'website' slice degrades to 'post'

    and does NOT transfer its allocation to 'image'.
    """
    # Agent with images enabled, but websites disabled
    agent = _make_test_agent(
        db_session,
        "bob",
        config={
            "image_posts": {
                "enabled": True,
                "policy": "optional",
                "provider_id": 1,
                "model": None,
            },
            "website_posts": {"enabled": False, "policy": "optional"},
        },
    )
    pin_intent_mix(agent, post=1.0, image=0.30, website=0.40)

    # Draw website slice: r = 0.35 (in [0.30, 0.70)) -> should degrade to "post", not "image"
    with patch("random.random", side_effect=[0.5, 0.35]):
        _, intent = _kickoff(db_session, agent, unread=0)
        assert intent == "post"


def test_unread_and_lurker_gates_resolve_browse(seeded_db, db_session):
    """Lurkers and personas with unread replies resolve to 'browse' under automatic runs."""
    lurker = _make_test_agent(db_session, "lurky", tier="lurker")
    regular = _make_test_agent(db_session, "regular_alice", tier="regular")

    pin_intent_mix(lurker, post=1.0, image=1.0)
    pin_intent_mix(regular, post=1.0, image=1.0)

    # Lurker always browse
    _, lurk_intent = _kickoff(db_session, lurker, unread=0)
    assert lurk_intent == "browse"

    # Unread > 0 resolves to browse under automatic run
    _, unread_intent = _kickoff(db_session, regular, unread=3)
    assert unread_intent == "browse"


def test_explicit_special_intent_overrides_unread(seeded_db, db_session):
    """Explicit requested special intent overrides unread replies."""
    agent = _make_test_agent(db_session, "alice")
    prompt, intent = _kickoff(db_session, agent, unread=2, requested_intent="image")
    assert intent == "image"
    assert "create_image_post" in prompt
    assert "view_inbox" in prompt or "inbox" in prompt.lower()
