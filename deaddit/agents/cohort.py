"""Wave-5 parity cohort spec: loading, validation, and summary helpers.

A cohort spec is a JSON document describing the full AC-P3 agent cohort:
one shared LLM endpoint plus 8-15 persona entries with per-agent cadence
bounds and an optional daily request ceiling. Guardrail caps
(``max_actions_per_run`` / ``max_run_seconds``) may be omitted (creation
injects ``loop.DEFAULT_CONFIG`` values); when present they must match the
loop defaults exactly so a spec can never widen them.
"""

from __future__ import annotations

import json
from pathlib import Path

from deaddit.agents.loop import DEFAULT_CONFIG
from deaddit.agents.registry import AutonomyTier

COHORT_SPEC_VERSION = 1

# Plan Phase 3 sizing: big enough for meaningful parity, small enough to stay
# inside endpoint rate limits.
MIN_AGENTS = 8
MAX_AGENTS = 15

_ALLOWED_AGENT_KEYS = frozenset(
    {
        "username",
        "tier",
        "min_delay",
        "max_delay",
        "daily_request_ceiling",
        "max_actions_per_run",
        "max_run_seconds",
    }
)
_GUARDRAIL_KEYS = ("max_actions_per_run", "max_run_seconds")


class CohortSpecError(ValueError):
    """Raised by load_spec; message lists every validation problem found."""


def validate_spec(spec: dict) -> list[str]:
    """Return one error string per violated rule (empty list = valid)."""
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["spec: must be a JSON object"]

    version = spec.get("version")
    if version != COHORT_SPEC_VERSION:
        errors.append(f"version: must be {COHORT_SPEC_VERSION}, got {version!r}")

    endpoint = spec.get("endpoint")
    if not isinstance(endpoint, dict):
        errors.append("endpoint: must be an object with api_url and model")
    else:
        for key in ("api_url", "model"):
            value = endpoint.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"endpoint.{key}: required non-empty string")

    agents = spec.get("agents")
    if not isinstance(agents, list):
        errors.append("agents: must be a list")
        agents = []
    if not MIN_AGENTS <= len(agents) <= MAX_AGENTS:
        errors.append(
            f"agents: expected between {MIN_AGENTS} and {MAX_AGENTS} entries, "
            f"got {len(agents)}"
        )

    seen_usernames: set[str] = set()
    valid_tiers = {t.value for t in AutonomyTier}
    for index, entry in enumerate(agents):
        prefix = f"agents[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: must be an object")
            continue

        unknown = sorted(set(entry) - _ALLOWED_AGENT_KEYS)
        if unknown:
            errors.append(f"{prefix}: unknown key(s): {', '.join(unknown)}")

        username = entry.get("username")
        if not isinstance(username, str) or not username.strip():
            errors.append(f"{prefix}.username: required non-empty string")
        elif username in seen_usernames:
            errors.append(f"{prefix}.username: duplicate username '{username}'")
        else:
            seen_usernames.add(username)

        tier = entry.get("tier")
        if tier not in valid_tiers:
            errors.append(
                f"{prefix}.tier: must be one of {sorted(valid_tiers)}, got {tier!r}"
            )

        min_delay = _as_positive_int(entry, "min_delay", prefix, errors)
        max_delay = _as_positive_int(entry, "max_delay", prefix, errors)
        if min_delay is not None and max_delay is not None and min_delay > max_delay:
            errors.append(
                f"{prefix}: min_delay ({min_delay}) must be <= max_delay ({max_delay})"
            )

        if "daily_request_ceiling" in entry:
            _as_positive_int(entry, "daily_request_ceiling", prefix, errors)

        # Guardrail caps are hard-coded here on purpose: they cite
        # loop.DEFAULT_CONFIG as the single source of truth.
        for key in _GUARDRAIL_KEYS:
            if key in entry and entry[key] != DEFAULT_CONFIG[key]:
                errors.append(
                    f"{prefix}.{key}: guardrail cap must be "
                    f"{DEFAULT_CONFIG[key]} (loop.DEFAULT_CONFIG); "
                    "omit the key to use the default"
                )

    return errors


def _as_positive_int(
    entry: dict, key: str, prefix: str, errors: list[str]
) -> int | None:
    value = entry.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        errors.append(f"{prefix}.{key}: must be a positive integer")
        return None
    return value


def load_spec(path: str | Path) -> dict:
    """Load and validate a cohort spec JSON file.

    Raises :class:`CohortSpecError` listing ALL problems at once.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CohortSpecError(f"Cohort spec not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CohortSpecError(f"Cohort spec unreadable/invalid JSON: {exc}") from exc

    problems = validate_spec(raw)
    if problems:
        details = "\n".join(f"  - {problem}" for problem in problems)
        raise CohortSpecError(
            f"Invalid cohort spec ({len(problems)} problem"
            f"{'s' if len(problems) != 1 else ''}):\n{details}"
        )
    return raw


def spec_summary(spec: dict) -> dict:
    """Small digest for CLI echo: agent count and tier histogram."""
    tiers: dict[str, int] = {}
    for entry in spec.get("agents") or []:
        tier = entry.get("tier") if isinstance(entry, dict) else None
        tiers[tier] = tiers.get(tier, 0) + 1
    return {"count": len(spec.get("agents") or []), "tiers": tiers}
