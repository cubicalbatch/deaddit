"""Shared Phase 4 test helper: pin visit profiles with a chosen intent mix.

The legacy ``AGENT_*`` Config settings no longer exist; tests that need a
specific automatic-intent distribution pin an immutable
``agent.visit_profile`` version on the agent under test.
"""

from __future__ import annotations

import json

from deaddit.agents.prompts import DEFAULT_VISIT_PROFILE
from deaddit.llm.prompts import (
    create_template,
    create_version,
    get_template,
    serialize_visit_profile,
    set_pin,
)

PROFILE_TEMPLATE = "agent.visit_profile"


def _document(post: float, image: float, website: float, backstage: float = 0.0) -> str:
    document = json.loads(serialize_visit_profile(DEFAULT_VISIT_PROFILE))
    document["intent_mix"] = {
        "post": post,
        "image": image,
        "website": website,
        "backstage": backstage,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _write_version(body: str) -> int:
    if get_template(PROFILE_TEMPLATE) is None:
        return create_template(PROFILE_TEMPLATE, body).version
    return create_version(PROFILE_TEMPLATE, body).version


def pin_intent_mix(
    agent,
    *,
    post: float,
    image: float = 0.0,
    website: float = 0.0,
    backstage: float = 0.0,
):
    """Pin one immutable profile version with the given mix onto ``agent``."""
    version = _write_version(_document(post, image, website, backstage))
    return set_pin("agent", str(agent.id), PROFILE_TEMPLATE, version)


def pin_cohort_intent_mix(cohort: str, *, post: float, image: float, website: float):
    """Pin one immutable profile version with the given mix onto a cohort."""
    version = _write_version(_document(post, image, website))
    return set_pin("cohort", str(cohort), PROFILE_TEMPLATE, version)


def pin_global_intent_mix(*, post: float, image: float, website: float):
    """Pin one immutable profile version with the given mix globally."""
    version = _write_version(_document(post, image, website))
    return set_pin("global", PROFILE_TEMPLATE, PROFILE_TEMPLATE, version)


def pin_profile(agent, *, system_layout: str | None = None):
    """Pin one immutable profile version with an optional system-layout override."""
    document = json.loads(serialize_visit_profile(DEFAULT_VISIT_PROFILE))
    if system_layout is not None:
        document["system_template"] = system_layout
        document["layouts"]["system"] = system_layout
    body = json.dumps(document, sort_keys=True, separators=(",", ":"))
    version = _write_version(body)
    return set_pin("agent", str(agent.id), PROFILE_TEMPLATE, version)
