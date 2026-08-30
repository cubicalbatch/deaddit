"""Forced post mix: effective_post_configs, tools_for/specs_for, and executor guardrail tests."""

from __future__ import annotations

from deaddit.agents.executor import execute
from deaddit.agents.registry import (
    ToolContext,
    effective_post_configs,
    offered_post_tool_names,
    specs_for,
    tools_for,
)
from deaddit.models import Agent, AgentRun, User


def _make_agent_row(db_session, username="alice", **kwargs):
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


def _make_context(db_session, agent, intent="browse"):
    run = AgentRun(
        agent_id=agent.id,
        persona_username=agent.user_username,
        trigger="manual",
        intent=intent,
        status="running",
    )
    db_session.add(run)
    db_session.commit()
    return ToolContext(
        agent=agent, run=run, user_username=agent.user_username, post_intent=intent
    )


def test_effective_post_configs_truth_table(db_session):
    """Test effective_post_configs for various intents and static configurations."""
    agent_both_opt = _make_agent_row(
        db_session,
        "agent_opt",
        config={
            "image_posts": {
                "enabled": True,
                "policy": "optional",
                "provider_id": 1,
                "model": None,
            },
            "website_posts": {"enabled": True, "policy": "optional"},
        },
    )

    # 1. Image intent applies image_only lock and disables website
    eff_img, eff_web = effective_post_configs(agent_both_opt, intent="image")
    assert eff_img["enabled"] is True
    assert eff_img["policy"] == "image_only"
    assert eff_web["enabled"] is False
    assert offered_post_tool_names(eff_img, eff_web) == frozenset({"create_image_post"})

    # 2. Website intent applies website_only lock and disables image
    eff_img, eff_web = effective_post_configs(agent_both_opt, intent="website")
    assert eff_img["enabled"] is False
    assert eff_web["enabled"] is True
    assert eff_web["policy"] == "website_only"
    assert offered_post_tool_names(eff_img, eff_web) == frozenset({"create_website"})

    # 3. Post intent leaves static configs untouched
    eff_img, eff_web = effective_post_configs(agent_both_opt, intent="post")
    assert eff_img["policy"] == "optional"
    assert eff_web["policy"] == "optional"
    assert offered_post_tool_names(eff_img, eff_web) == frozenset(
        {"create_post", "create_image_post", "create_website"}
    )

    # 4. Backstage intent reserves a plain-text post.
    eff_img, eff_web = effective_post_configs(agent_both_opt, intent="backstage")
    assert offered_post_tool_names(eff_img, eff_web) == frozenset({"create_post"})

    # 5. Inconsistent special intent fails closed to no post tools
    agent_no_img = _make_agent_row(
        db_session,
        "agent_no_img",
        config={
            "image_posts": {"enabled": False, "policy": "optional"},
            "website_posts": {"enabled": True, "policy": "optional"},
        },
    )
    eff_img, eff_web = effective_post_configs(agent_no_img, intent="image")
    assert offered_post_tool_names(eff_img, eff_web) == frozenset()


def test_tools_for_and_specs_for_intent_filtering(db_session):
    """tools_for and specs_for strip create_comment during special intent visits and offer only the reserved post tool."""
    agent = _make_agent_row(db_session, "alice")

    # Image intent: create_image_post offered, create_post/create_website/create_comment NOT offered
    tools_img = tools_for("regular", agent=agent, intent="image")
    tool_names_img = {t.name for t in tools_img}
    assert "create_image_post" in tool_names_img
    assert "create_post" not in tool_names_img
    assert "create_website" not in tool_names_img
    assert "create_comment" not in tool_names_img
    assert "browse_feed" in tool_names_img

    specs_img = specs_for("regular", agent=agent, intent="image")
    spec_names_img = {s.name for s in specs_img}
    assert spec_names_img == tool_names_img

    # Website intent: create_website offered, create_post/create_image_post/create_comment NOT offered
    tools_web = tools_for("regular", agent=agent, intent="website")
    tool_names_web = {t.name for t in tools_web}
    assert "create_website" in tool_names_web
    assert "create_post" not in tool_names_web
    assert "create_image_post" not in tool_names_web
    assert "create_comment" not in tool_names_web

    # Backstage intent: only the reserved text post is writable.
    tools_backstage = tools_for("regular", agent=agent, intent="backstage")
    tool_names_backstage = {tool.name for tool in tools_backstage}
    assert "create_post" in tool_names_backstage
    assert "create_image_post" not in tool_names_backstage
    assert "create_website" not in tool_names_backstage
    assert "create_comment" not in tool_names_backstage

    # Browse intent: create_comment is present, standard tools available
    tools_browse = tools_for("regular", agent=agent, intent="browse")
    tool_names_browse = {t.name for t in tools_browse}
    assert "create_comment" in tool_names_browse
    assert "create_post" in tool_names_browse


def test_executor_guardrails_reject_unauthorized_calls(seeded_db, db_session):
    """Direct or forged tool calls are authorized against effective_post_configs."""
    agent_alice = _make_agent_row(db_session, "alice")
    agent_bob = _make_agent_row(db_session, "bob")

    # 1. During image reserved run, attempting create_post is rejected
    ctx_img = _make_context(db_session, agent_alice, intent="image")
    res_post = execute(
        "create_post",
        {"subdeaddit": "testsub", "title": "Hi", "content": "Text"},
        ctx_img,
    )
    assert res_post["ok"] is False
    assert "image posts, not text posts" in res_post["error"]

    # 2. During image reserved run, attempting create_comment is rejected
    res_comment = execute(
        "create_comment", {"post_id": 1, "content": "A comment"}, ctx_img
    )
    assert res_comment["ok"] is False
    assert (
        "comments are not available during a reserved image post visit"
        in res_comment["error"]
    )

    # Mark alice run completed before bob run
    ctx_img.run.status = "completed"
    db_session.commit()

    # 3. During website reserved run, attempting create_image_post is rejected
    ctx_web = _make_context(db_session, agent_bob, intent="website")
    res_img = execute(
        "create_image_post",
        {"subdeaddit": "testsub", "title": "Pic", "prompt": "Dog"},
        ctx_web,
    )
    assert res_img["ok"] is False
    assert "image posts are not enabled" in res_img["error"]

    # 4. During website reserved run, attempting create_comment is rejected
    res_comment_web = execute(
        "create_comment", {"post_id": 1, "content": "A comment"}, ctx_web
    )
    assert res_comment_web["ok"] is False
    assert (
        "comments are not available during a reserved website post visit"
        in res_comment_web["error"]
    )
