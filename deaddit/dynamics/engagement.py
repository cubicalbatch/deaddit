"""Persistence-facing simulated-voting policy helpers.

The simulator itself is intentionally not part of this module.  This module
owns the value validation and the small atomic aggregate write used by a
worker, so policy/config semantics do not depend on either web or worker
process memory.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping

from deaddit.extensions import db
from deaddit.models import VoteCadencePolicy, VoteSimulationHourly

SUPPORTED_ALGORITHM_VERSIONS = frozenset({1})
MAX_CATCHUP_GRACE_HOURS = 168

_POLICY_FIELDS = {
    "post": {
        "mean_active_votes",
        "attention_shape",
        "half_life_minutes",
        "active_window_hours",
        "catchup_grace_hours",
        "max_active_votes",
        "tail_half_life_days",
        "tail_max_age_days",
        "tail_vote_probability_per_exposure",
    },
    "comment": {
        "mean_active_votes",
        "attention_shape",
        "half_life_minutes",
        "active_window_hours",
        "catchup_grace_hours",
        "max_active_votes",
        "tail_half_life_days",
        "tail_max_age_days",
        "tail_vote_probability_per_exposure",
    },
    "voter": {
        "default_hourly_cap",
        "minimum_gap_seconds",
        "subscription_weight",
        "max_activity_weight",
    },
    "direction": {
        "base_downvote_probability",
        "minimum_downvote_probability",
        "maximum_downvote_probability",
    },
}

_COUNTER_FIELDS = (
    "ticks",
    "errors",
    "active_proposals",
    "archive_proposals",
    "revival_proposals",
    "inserted_votes",
    "switched_votes",
    "upvotes",
    "downvotes",
    "cap_skips",
    "min_gap_skips",
    "no_voter_skips",
    "guardrail_skips",
)
_COUNTER_ALIASES = {
    "active": "active_proposals",
    "archive": "archive_proposals",
    "revival": "revival_proposals",
    "min_gap": "min_gap_skips",
    "no_voter": "no_voter_skips",
    "guardrail": "guardrail_skips",
}
 
 
@dataclass(frozen=True)
class PolicyConfig:
    """Validated immutable value object for one cadence policy."""

    post: Mapping[str, Any]
    comment: Mapping[str, Any]
    voter: Mapping[str, Any]
    direction: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any] | str) -> "PolicyConfig":
        values = validate_policy(config)
        return cls(
            post=MappingProxyType(values["post"]),
            comment=MappingProxyType(values["comment"]),
            voter=MappingProxyType(values["voter"]),
            direction=MappingProxyType(values["direction"]),
        )

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {
            "post": dict(self.post),
            "comment": dict(self.comment),
            "voter": dict(self.voter),
            "direction": dict(self.direction),
        }


CadencePolicyConfig = PolicyConfig


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _nonnegative(value: Any, name: str) -> None:
    if _number(value, name) < 0:
        raise ValueError(f"{name} must not be negative")


def _positive(value: Any, name: str) -> None:
    if _number(value, name) <= 0:
        raise ValueError(f"{name} must be positive")


def _probability(value: Any, name: str) -> None:
    number = _number(value, name)
    if not 0 <= number <= 1:
        raise ValueError(f"{name} must be between 0 and 1")


def validate_policy(config: Mapping[str, Any] | str) -> dict[str, dict[str, Any]]:
    """Validate and JSON-round-trip a cadence configuration.

    Validation is deliberately strict: a malformed row must fail closed at
    load time rather than silently selecting simulator defaults.  The returned
    mapping is detached from the caller and contains only JSON values.
    """
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError as exc:
            raise ValueError("policy config must be valid JSON") from exc
    if not isinstance(config, Mapping):
        raise ValueError("policy config must be an object")
    if set(config) != set(_POLICY_FIELDS):
        raise ValueError("policy config has an invalid schema")

    normalized: dict[str, dict[str, Any]] = {}
    for section, fields in _POLICY_FIELDS.items():
        value = config[section]
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError(f"policy config section '{section}' has an invalid schema")
        normalized[section] = dict(value)

    for section in ("post", "comment"):
        values = normalized[section]
        _nonnegative(values["mean_active_votes"], f"{section}.mean_active_votes")
        _positive(values["attention_shape"], f"{section}.attention_shape")
        _positive(values["half_life_minutes"], f"{section}.half_life_minutes")
        _positive(values["active_window_hours"], f"{section}.active_window_hours")
        _nonnegative(values["catchup_grace_hours"], f"{section}.catchup_grace_hours")
        if values["catchup_grace_hours"] > MAX_CATCHUP_GRACE_HOURS:
            raise ValueError(
                f"{section}.catchup_grace_hours exceeds {MAX_CATCHUP_GRACE_HOURS} hours"
            )
        _nonnegative(values["max_active_votes"], f"{section}.max_active_votes")
        _positive(values["tail_half_life_days"], f"{section}.tail_half_life_days")
        _positive(values["tail_max_age_days"], f"{section}.tail_max_age_days")
        _probability(
            values["tail_vote_probability_per_exposure"],
            f"{section}.tail_vote_probability_per_exposure",
        )

    voter = normalized["voter"]
    _nonnegative(voter["default_hourly_cap"], "voter.default_hourly_cap")
    _nonnegative(voter["minimum_gap_seconds"], "voter.minimum_gap_seconds")
    _nonnegative(voter["subscription_weight"], "voter.subscription_weight")
    _nonnegative(voter["max_activity_weight"], "voter.max_activity_weight")

    direction = normalized["direction"]
    for key, value in direction.items():
        _probability(value, f"direction.{key}")
    if direction["minimum_downvote_probability"] > direction[
        "maximum_downvote_probability"
    ]:
        raise ValueError("minimum downvote probability exceeds maximum")

    # JSON serialization catches non-JSON scalar values and returns a fresh
    # structure, avoiding accidental in-memory mutation of a policy row.
    try:
        return json.loads(json.dumps(deepcopy(normalized), allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("policy config must contain JSON values") from exc


def serialize_policy_config(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate and serialize a policy config for a JSON model column."""
    return validate_policy(config)


def load_policy_config(policy: VoteCadencePolicy) -> PolicyConfig:
    """Validate a database policy row, including its algorithm version."""
    if policy.algorithm_version not in SUPPORTED_ALGORITHM_VERSIONS:
        raise ValueError(f"unsupported policy algorithm version {policy.algorithm_version}")
    return PolicyConfig.from_mapping(policy.config)


def resolve_policy_for_content(created_at: datetime) -> VoteCadencePolicy | None:
    """Resolve the newest policy effective at content creation time."""
    return VoteCadencePolicy.resolve_for_content(created_at)


def resolve_policy_for_exposure(exposed_at: datetime) -> VoteCadencePolicy | None:
    """Resolve the newest policy effective at a tail exposure time."""
    return VoteCadencePolicy.resolve_for_exposure(exposed_at)
 
 
def resolve_policy_for_tail_exposure(exposed_at: datetime) -> VoteCadencePolicy | None:
    """Alias emphasizing that exposure resolution applies to tail work."""
    return resolve_policy_for_exposure(exposed_at)


def _utc_hour(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(minute=0, second=0, microsecond=0)


def upsert_hourly_summary(
    hour: datetime,
    mode: str,
    counters: Mapping[str, int] | None = None,
    **deltas: int,
) -> VoteSimulationHourly:
    """Atomically add counter deltas to one ``(UTC hour, mode)`` row.

    The helper commits its short transaction and returns the refreshed row.
    Separate mode values intentionally use separate composite-key rows.
    """
    if not isinstance(mode, str) or not mode:
        raise ValueError("hourly summary mode is required")
    values: dict[str, int] = dict(counters or {})
    values.update(deltas)
    normalized: dict[str, int] = {}
    for name, value in values.items():
        name = _COUNTER_ALIASES.get(name, name)
        if name not in _COUNTER_FIELDS:
            raise ValueError(f"unknown hourly summary counter: {name}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"hourly summary counter '{name}' must be an integer")
        normalized[name] = value

    bucket = _utc_hour(hour)
    now = datetime.utcnow()
    initial = {name: normalized.get(name, 0) for name in _COUNTER_FIELDS}
    initial.update(hour=bucket, mode=mode, updated_at=now)
    dialect = db.session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        insert = None

    if insert is not None:
        statement = insert(VoteSimulationHourly).values(**initial)
        statement = statement.on_conflict_do_update(
            index_elements=["hour", "mode"],
            set_={
                name: getattr(VoteSimulationHourly, name) + statement.excluded[name]
                for name in _COUNTER_FIELDS
            }
            | {"updated_at": now},
        )
        db.session.execute(statement)
    else:
        row = db.session.get(VoteSimulationHourly, (bucket, mode))
        if row is None:
            db.session.add(VoteSimulationHourly(**initial))
        else:
            for name, value in normalized.items():
                setattr(row, name, getattr(row, name) + value)
            row.updated_at = now
    db.session.commit()
    return db.session.get(VoteSimulationHourly, (bucket, mode))


# Worker-facing spelling retained as a descriptive alias.
upsert_vote_simulation_hourly = upsert_hourly_summary
