"""Persistence-facing simulated-voting policy helpers.

The simulator itself is intentionally not part of this module.  This module
owns the value validation and the small atomic aggregate write used by a
worker, so policy/config semantics do not depend on either web or worker
process memory.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

from deaddit.dynamics.moderation import active_ban_for
from deaddit.extensions import db
from deaddit.models import (
    Comment,
    Post,
    Setting,
    User,
    Vote,
    VoteCadencePolicy,
    VoteSimulationHourly,
)

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
    def from_mapping(cls, config: Mapping[str, Any] | str) -> PolicyConfig:
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
    if isinstance(value, bool) or not isinstance(value, int | float):
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
    if (
        direction["minimum_downvote_probability"]
        > direction["maximum_downvote_probability"]
    ):
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
        raise ValueError(
            f"unsupported policy algorithm version {policy.algorithm_version}"
        )
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
        value = value.astimezone(UTC).replace(tzinfo=None)
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


# These are the only canonical preset definitions.  Consumers should use
# ``preset_config`` instead of copying values into API or worker code.
PRESET_CONFIGS: Mapping[str, Mapping[str, Mapping[str, Any]]] = MappingProxyType(
    {
        "quiet": {
            "post": {
                "mean_active_votes": 3,
                "attention_shape": 1.0,
                "half_life_minutes": 180,
                "active_window_hours": 72,
                "catchup_grace_hours": 12,
                "max_active_votes": 30,
                "tail_half_life_days": 21,
                "tail_max_age_days": 365,
                "tail_vote_probability_per_exposure": 0.005,
            },
            "comment": {
                "mean_active_votes": 1,
                "attention_shape": 1.0,
                "half_life_minutes": 120,
                "active_window_hours": 36,
                "catchup_grace_hours": 6,
                "max_active_votes": 10,
                "tail_half_life_days": 10,
                "tail_max_age_days": 90,
                "tail_vote_probability_per_exposure": 0.002,
            },
            "voter": {
                "default_hourly_cap": 8,
                "minimum_gap_seconds": 120,
                "subscription_weight": 4.0,
                "max_activity_weight": 3.0,
            },
            "direction": {
                "base_downvote_probability": 0.04,
                "minimum_downvote_probability": 0.01,
                "maximum_downvote_probability": 0.15,
            },
        },
        "natural": {
            "post": {
                "mean_active_votes": 8,
                "attention_shape": 1.0,
                "half_life_minutes": 90,
                "active_window_hours": 48,
                "catchup_grace_hours": 12,
                "max_active_votes": 80,
                "tail_half_life_days": 14,
                "tail_max_age_days": 365,
                "tail_vote_probability_per_exposure": 0.015,
            },
            "comment": {
                "mean_active_votes": 3,
                "attention_shape": 1.0,
                "half_life_minutes": 60,
                "active_window_hours": 24,
                "catchup_grace_hours": 6,
                "max_active_votes": 30,
                "tail_half_life_days": 7,
                "tail_max_age_days": 90,
                "tail_vote_probability_per_exposure": 0.005,
            },
            "voter": {
                "default_hourly_cap": 20,
                "minimum_gap_seconds": 45,
                "subscription_weight": 4.0,
                "max_activity_weight": 3.0,
            },
            "direction": {
                "base_downvote_probability": 0.05,
                "minimum_downvote_probability": 0.01,
                "maximum_downvote_probability": 0.15,
            },
        },
        "busy": {
            "post": {
                "mean_active_votes": 18,
                "attention_shape": 1.0,
                "half_life_minutes": 45,
                "active_window_hours": 36,
                "catchup_grace_hours": 12,
                "max_active_votes": 200,
                "tail_half_life_days": 10,
                "tail_max_age_days": 365,
                "tail_vote_probability_per_exposure": 0.03,
            },
            "comment": {
                "mean_active_votes": 7,
                "attention_shape": 1.0,
                "half_life_minutes": 30,
                "active_window_hours": 18,
                "catchup_grace_hours": 6,
                "max_active_votes": 80,
                "tail_half_life_days": 5,
                "tail_max_age_days": 90,
                "tail_vote_probability_per_exposure": 0.01,
            },
            "voter": {
                "default_hourly_cap": 40,
                "minimum_gap_seconds": 15,
                "subscription_weight": 4.0,
                "max_activity_weight": 3.0,
            },
            "direction": {
                "base_downvote_probability": 0.07,
                "minimum_downvote_probability": 0.01,
                "maximum_downvote_probability": 0.15,
            },
        },
    }
)


PRESETS = PRESET_CONFIGS
POLICY_PRESETS = PRESET_CONFIGS
# A descriptive spelling useful to callers that call these "presets".
CANONICAL_PRESETS = PRESET_CONFIGS


def preset_config(name: str) -> dict[str, dict[str, Any]]:
    """Return a detached, validated configuration for a canonical preset."""
    try:
        config = PRESET_CONFIGS[name.lower()]
    except (AttributeError, KeyError) as exc:
        raise ValueError(f"unknown policy preset: {name}") from exc
    return validate_policy(config)


def get_preset_config(name: str) -> dict[str, dict[str, Any]]:
    return preset_config(name)


class _StableStream:
    """Small project-owned deterministic byte stream.

    Keeping this separate from ``random.Random`` means Python implementation
    changes cannot silently alter simulator schedules.
    """

    def __init__(self, seed: int):
        self.seed = seed.to_bytes(32, "big", signed=False)
        self.counter = 0

    def unit(self) -> float:
        digest = hashlib.sha256(self.seed + self.counter.to_bytes(8, "big")).digest()
        self.counter += 1
        return (int.from_bytes(digest[:8], "big") + 0.5) / 2**64

    def normal(self) -> float:
        u1 = max(self.unit(), 1e-15)
        u2 = self.unit()
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def stable_seed(
    policy_id: int | str,
    target_type: str,
    target_id: int | str,
    target_created_at: datetime | str,
    algorithm_version: int = 1,
) -> int:
    """Build the restart-safe seed mandated by the cadence contract."""
    created = (
        _as_utc_naive(target_created_at).isoformat()
        if isinstance(target_created_at, datetime)
        else str(target_created_at)
    )
    material = "|".join(
        (
            str(policy_id),
            target_type,
            str(target_id),
            created,
            str(algorithm_version),
        )
    ).encode()
    return int.from_bytes(hashlib.sha256(material).digest(), "big")


build_stable_seed = stable_seed


def _gamma_sample(stream: _StableStream, shape: float) -> float:
    if shape < 1.0:
        return _gamma_sample(stream, shape + 1.0) * stream.unit() ** (1.0 / shape)
    d = shape - 1.0 / 3.0
    c = 1.0 / math.sqrt(9.0 * d)
    while True:
        x = stream.normal()
        v = (1.0 + c * x) ** 3
        if v > 0:
            u = stream.unit()
            if u < 1.0 - 0.0331 * x**4 or math.log(u) < (
                0.5 * x * x + d * (1.0 - v + math.log(v))
            ):
                return d * v


def _poisson_sample(stream: _StableStream, mean: float) -> int:
    if mean <= 0:
        return 0
    if mean < 80:
        threshold = math.exp(-mean)
        probability = 1.0
        count = 0
        while probability > threshold:
            count += 1
            probability *= stream.unit()
        return count - 1
    # The normal approximation is only a safety path for pathological custom
    # policies; ordinary presets remain exact via inversion above.
    return max(0, int(round(mean + math.sqrt(mean) * stream.normal())))


def sample_attention_budget(
    policy: VoteCadencePolicy | PolicyConfig | Mapping[str, Any],
    target_type: str,
    target_id: int | str,
    target_created_at: datetime,
    *,
    policy_id: int | str | None = None,
    algorithm_version: int = 1,
) -> int:
    """Sample one bounded negative-binomial active lifetime budget."""
    config = (
        load_policy_config(policy)
        if isinstance(policy, VoteCadencePolicy)
        else policy
        if isinstance(policy, PolicyConfig)
        else PolicyConfig.from_mapping(policy)
    )
    if target_type not in {"post", "comment"}:
        raise ValueError("target_type must be 'post' or 'comment'")
    section = config.post if target_type == "post" else config.comment
    mean = float(section["mean_active_votes"])
    maximum = int(section["max_active_votes"])
    if mean <= 0 or maximum <= 0:
        return 0
    shape = float(section["attention_shape"])
    seed = stable_seed(
        policy_id if policy_id is not None else 0,
        target_type,
        target_id,
        target_created_at,
        algorithm_version,
    )
    stream = _StableStream(seed)
    # Gamma-Poisson is the negative-binomial parameterization with mean and
    # shape, and naturally retains zero-vote outcomes.
    rate = _gamma_sample(stream, shape) * (mean / shape)
    return min(maximum, _poisson_sample(stream, rate))


sample_active_budget = sample_attention_budget
sample_lifetime_attention = sample_attention_budget


def arrival_offset(
    ordinal: int,
    budget: int,
    half_life_minutes: float,
    active_window_hours: float,
) -> timedelta:
    """Return the deterministic quantile offset for a one-based ordinal."""
    if ordinal < 1 or ordinal > budget:
        raise ValueError("ordinal must be between 1 and budget")
    if budget <= 0:
        raise ValueError("budget must be positive")
    half_life = float(half_life_minutes)
    window = float(active_window_hours) * 60.0
    denominator = 1.0 - 2.0 ** (-window / half_life)
    # ordinal / N is an inverse-CDF quantile.  Distinct ordinals therefore
    # cannot pile up on one polling boundary, while D(a)=floor(N*F(a)).
    quantile = ordinal / budget
    minutes = -half_life * math.log2(1.0 - quantile * denominator)
    return timedelta(minutes=minutes)


deterministic_arrival_offset = arrival_offset


def due_count(
    budget: int,
    age: timedelta | float,
    half_life_minutes: float,
    active_window_hours: float,
) -> int:
    """Calculate D(a)=floor(N*F(a)), including the post-window plateau."""
    if budget <= 0:
        return 0
    age_minutes = (
        age.total_seconds() / 60.0 if isinstance(age, timedelta) else float(age)
    )
    if age_minutes <= 0:
        return 0
    window = float(active_window_hours) * 60.0
    if age_minutes >= window:
        return int(budget)
    half_life = float(half_life_minutes)
    fraction = (1.0 - 2.0 ** (-age_minutes / half_life)) / (
        1.0 - 2.0 ** (-window / half_life)
    )
    return min(int(budget), max(0, math.floor(budget * fraction)))


calculate_due_count = due_count


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _policy_config(
    policy: VoteCadencePolicy | PolicyConfig | Mapping[str, Any],
) -> PolicyConfig:
    if isinstance(policy, VoteCadencePolicy):
        return load_policy_config(policy)
    if isinstance(policy, PolicyConfig):
        return policy
    return PolicyConfig.from_mapping(policy)


def _section_for(
    policy: VoteCadencePolicy | PolicyConfig | Mapping[str, Any],
    target_type: str,
) -> Mapping[str, Any]:
    config = _policy_config(policy)
    if target_type not in {"post", "comment"}:
        raise ValueError("target_type must be 'post' or 'comment'")
    return config.post if target_type == "post" else config.comment


def _hash_unit(*parts: object) -> float:
    material = "|".join(str(part) for part in parts).encode()
    digest = hashlib.sha256(material).digest()
    return (int.from_bytes(digest[:8], "big") + 0.5) / 2**64


def _allow_downvotes() -> bool:
    value = Setting.get_value("allow_downvotes", "true")
    return (value or "").strip().lower() in {"true", "1", "on", "yes"}


def _subscriptions(user: User) -> set[str]:
    state = user.agent_state or {}
    values = state.get("subscriptions") or []
    if isinstance(values, str):
        return {values}
    return {str(value) for value in values}


def _vote_cap(user: User, default: int | float) -> int:
    state = user.agent_state or {}
    caps = state.get("rate_caps") or {}
    override = caps.get("vote") if isinstance(caps, Mapping) else None
    value = default if override is None else override
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _target_subdeaddit(target: Post | Comment, target_type: str) -> str:
    return (
        target.subdeaddit_name if target_type == "post" else target.post.subdeaddit_name
    )


def select_direction(
    policy: VoteCadencePolicy | PolicyConfig | Mapping[str, Any],
    target_type: str,
    target_id: int | str,
    voter: str,
    *,
    policy_id: int | str | None = None,
    target_created_at: datetime | str = "",
    subscribed: bool = False,
    community_activity: float = 0.0,
    allow_downvotes: bool | None = None,
) -> int:
    """Select a stable direction without using score or current vote count."""
    if allow_downvotes is None:
        allow_downvotes = _allow_downvotes()
    if not allow_downvotes:
        return 1
    config = _policy_config(policy)
    direction = config.direction
    quality = _hash_unit(
        policy_id if policy_id is not None else 0,
        target_type,
        target_id,
        target_created_at,
        "quality",
    )
    controversy = _hash_unit(
        policy_id if policy_id is not None else 0,
        target_type,
        target_id,
        voter,
        "controversy",
    )
    # Keep each adjustment deliberately small and bounded.  Subscription and
    # prior activity are useful affinity signals, but neither can dominate the
    # policy's safe probability range.
    affinity = 0.02 if subscribed else -0.01
    activity = min(1.0, max(0.0, float(community_activity)))
    affinity += 0.02 * activity
    probability = float(direction["base_downvote_probability"])
    probability += (quality - 0.5) * 0.04
    probability += (controversy - 0.5) * 0.02
    probability += affinity - 0.01
    probability = min(
        float(direction["maximum_downvote_probability"]),
        max(float(direction["minimum_downvote_probability"]), probability),
    )
    unit = _hash_unit(
        policy_id if policy_id is not None else 0,
        target_type,
        target_id,
        voter,
        target_created_at,
        "direction",
    )
    return -1 if unit < probability else 1


choose_direction = select_direction


@dataclass(frozen=True)
class VoteDecision:
    target_type: str
    target_id: int
    ordinal: int
    budget: int
    voter: str
    direction: int
    offset: timedelta

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "ordinal": self.ordinal,
            "budget": self.budget,
            "voter": self.voter,
            "direction": self.direction,
            "offset_seconds": self.offset.total_seconds(),
        }


@dataclass
class TickResult:
    """Stable, inspectable output shared by dry-run and live ticks."""

    targets_examined: int = 0
    budgets: dict[tuple[str, int], int] = field(default_factory=dict)
    due_ordinals: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[VoteDecision] = field(default_factory=list)
    casts: list[dict[str, Any]] = field(default_factory=list)
    skips: dict[str, int] = field(default_factory=dict)

    @property
    def target_budgets(self) -> dict[tuple[str, int], int]:
        return self.budgets

    @property
    def selected_voters(self) -> list[str]:
        return self.voters_selected

    @property
    def skip_reasons(self) -> dict[str, int]:
        return self.skips

    @property
    def voters_selected(self) -> list[str]:
        return [decision.voter for decision in self.decisions]

    @property
    def directions(self) -> list[int]:
        return [decision.direction for decision in self.decisions]

    @property
    def cast_count(self) -> int:
        return len(self.casts)

    def skip(self, reason: str) -> None:
        self.skips[reason] = self.skips.get(reason, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        budgets = {
            f"{target}:{target_id}": value
            for (target, target_id), value in self.budgets.items()
        }
        return {
            "targets_examined": self.targets_examined,
            "budgets": budgets,
            "due_ordinals": list(self.due_ordinals),
            "voters_selected": list(self.voters_selected),
            "directions": list(self.directions),
            "decisions": [decision.to_dict() for decision in self.decisions],
            "casts": list(self.casts),
            "skips": dict(self.skips),
        }


def _candidate_horizon(
    policy: VoteCadencePolicy | PolicyConfig | Mapping[str, Any] | None,
    target_type: str | None,
    now: datetime,
) -> float:
    if policy is not None:
        types = (target_type,) if target_type else ("post", "comment")
        return max(
            float(_section_for(policy, kind)["active_window_hours"])
            + float(_section_for(policy, kind)["catchup_grace_hours"])
            for kind in types
        )
    rows = VoteCadencePolicy.query.filter(VoteCadencePolicy.effective_at <= now).all()
    horizons = []
    for row in rows:
        config = load_policy_config(row)
        for kind in (target_type,) if target_type else ("post", "comment"):
            section = _section_for(config, kind)
            horizons.append(
                float(section["active_window_hours"])
                + float(section["catchup_grace_hours"])
            )
    return max(horizons, default=0.0)


def _candidate_query(
    target_type: str,
    now: datetime,
    horizon_hours: float,
    target_ids: Sequence[int] | None,
) -> list[Post | Comment]:
    model = Post if target_type == "post" else Comment
    lower = now - timedelta(hours=horizon_hours)
    query = db.session.query(model).filter(
        model.created_at >= lower,
        model.created_at <= now,
    )
    if target_ids is not None:
        query = query.filter(model.id.in_(list(target_ids)))
    return query.order_by(model.created_at.asc(), model.id.asc()).all()


def _existing_votes(target_type: str, target_id: int) -> list[Vote]:
    field = Vote.post_id if target_type == "post" else Vote.comment_id
    return db.session.query(Vote).filter(field == target_id).all()


def _select_voter(
    target: Post | Comment,
    target_type: str,
    ordinal: int,
    policy: VoteCadencePolicy | PolicyConfig | Mapping[str, Any],
    now: datetime,
    existing_voters: set[str],
    selected: set[str],
    recent_counts: Mapping[str, int],
    hourly_counts: Mapping[str, int],
    latest_votes: Mapping[str, datetime],
) -> tuple[User | None, str]:
    config = _policy_config(policy)
    voter_config = config.voter
    gap = float(voter_config["minimum_gap_seconds"])
    users = db.session.query(User).order_by(User.username.asc()).all()
    pool: list[tuple[User, float]] = []
    reasons: list[str] = []
    sub_name = _target_subdeaddit(target, target_type)
    for user in users:
        if user.username == target.user:
            reasons.append("author")
            continue
        if user.username in existing_voters or user.username in selected:
            reasons.append("prior_voter")
            continue
        if active_ban_for(user.username, sub_name) is not None:
            reasons.append("banned")
            continue
        cap = _vote_cap(user, voter_config["default_hourly_cap"])
        if cap == 0 or hourly_counts.get(user.username, 0) >= cap:
            reasons.append("cap")
            continue
        latest = latest_votes.get(user.username)
        if latest is not None and latest >= now - timedelta(seconds=gap):
            reasons.append("min_gap")
            continue
        subscribed = sub_name in _subscriptions(user)
        activity = min(
            float(voter_config["max_activity_weight"]),
            float(recent_counts.get(user.username, 0)),
        )
        weight = (float(voter_config["subscription_weight"]) if subscribed else 1.0) + (
            activity
        )
        if weight <= 0:
            reasons.append("disabled")
            continue
        pool.append((user, weight))
    if not pool:
        if reasons and all(reason == "cap" for reason in reasons):
            return None, "cap"
        if reasons and all(reason == "min_gap" for reason in reasons):
            return None, "min_gap"
        return None, "no_voter"
    # Exponential-race keys implement weighted selection without mutable RNG
    # state; the hash key is stable across process restarts.
    best: tuple[float, User] | None = None
    for user, weight in pool:
        unit = _hash_unit(
            target_type,
            target.id,
            target.created_at,
            ordinal,
            user.username,
            "voter",
        )
        key = -math.log(max(unit, 1e-15)) / weight
        if (
            best is None
            or key < best[0]
            or (key == best[0] and user.username < best[1].username)
        ):
            best = (key, user)
    return best[1], "selected"


class ActiveWindowEngine:
    """Evaluate routine active-window proposals and optionally cast them."""

    def __init__(
        self,
        policy: VoteCadencePolicy | PolicyConfig | Mapping[str, Any] | None = None,
        *,
        per_item_limit: int = 2,
        global_limit: int = 100,
    ):
        if per_item_limit < 0 or global_limit < 0:
            raise ValueError("tick limits must not be negative")
        self.policy = policy
        self.per_item_limit = per_item_limit
        self.global_limit = global_limit

    def tick(
        self,
        now: datetime | None = None,
        *,
        dry_run: bool = False,
        target_type: str | None = None,
        target_ids: Sequence[int] | None = None,
        allow_downvotes: bool | None = None,
    ) -> TickResult:
        now = _as_utc_naive(now or datetime.utcnow())
        kinds = (target_type,) if target_type else ("post", "comment")
        result = TickResult()
        casts_used = 0
        recent_counts: dict[str, int] = {}
        recent_rows = (
            db.session.query(Vote.voter)
            .filter(Vote.created_at >= now - timedelta(days=7))
            .all()
        )
        for row in recent_rows:
            recent_counts[row[0]] = recent_counts.get(row[0], 0) + 1
        hourly_counts: dict[str, int] = {}
        latest_votes: dict[str, datetime] = {}
        hourly_rows = (
            db.session.query(Vote.voter, Vote.created_at)
            .filter(Vote.created_at >= now - timedelta(hours=1))
            .all()
        )
        for voter, created_at in hourly_rows:
            hourly_counts[voter] = hourly_counts.get(voter, 0) + 1
            if created_at is not None and (
                voter not in latest_votes or created_at > latest_votes[voter]
            ):
                latest_votes[voter] = created_at
        for kind in kinds:
            if kind not in {"post", "comment"}:
                raise ValueError("target_type must be 'post' or 'comment'")
            horizon = _candidate_horizon(self.policy, kind, now)
            ids = target_ids if target_type == kind else None
            targets = _candidate_query(kind, now, horizon, ids)
            for target in targets:
                policy = self.policy or resolve_policy_for_content(target.created_at)
                if policy is None:
                    continue
                section = _section_for(policy, kind)
                age = now - _as_utc_naive(target.created_at)
                active_hours = float(section["active_window_hours"])
                grace_hours = float(section["catchup_grace_hours"])
                if age.total_seconds() < 0 or age > timedelta(
                    hours=active_hours + grace_hours
                ):
                    continue
                result.targets_examined += 1
                policy_id = policy.id if isinstance(policy, VoteCadencePolicy) else 0
                algorithm_version = (
                    policy.algorithm_version
                    if isinstance(policy, VoteCadencePolicy)
                    else 1
                )
                budget = sample_attention_budget(
                    policy,
                    kind,
                    target.id,
                    target.created_at,
                    policy_id=policy_id,
                    algorithm_version=algorithm_version,
                )
                key = (kind, target.id)
                result.budgets[key] = budget
                existing = _existing_votes(kind, target.id)
                simulated_count = sum(vote.source == "simulated" for vote in existing)
                due = due_count(
                    budget,
                    age,
                    section["half_life_minutes"],
                    active_hours,
                )
                result.due_ordinals.extend(
                    {
                        "target_type": kind,
                        "target_id": target.id,
                        "ordinal": ordinal,
                    }
                    for ordinal in range(simulated_count + 1, due + 1)
                )
                selected: set[str] = set()
                for ordinal in range(simulated_count + 1, due + 1):
                    if casts_used >= self.global_limit:
                        result.skip("global_limit")
                        continue
                    if len(selected) >= self.per_item_limit:
                        result.skip("item_limit")
                        continue
                    voter, reason = _select_voter(
                        target,
                        kind,
                        ordinal,
                        policy,
                        now,
                        {vote.voter for vote in existing},
                        selected,
                        recent_counts,
                        hourly_counts,
                        latest_votes,
                    )
                    if voter is None:
                        result.skip(reason)
                        continue
                    selected.add(voter.username)
                    hourly_counts[voter.username] = (
                        hourly_counts.get(voter.username, 0) + 1
                    )
                    latest_votes[voter.username] = now
                    subscribed = _target_subdeaddit(target, kind) in _subscriptions(
                        voter
                    )
                    direction = select_direction(
                        policy,
                        kind,
                        target.id,
                        voter.username,
                        policy_id=policy_id,
                        target_created_at=target.created_at,
                        subscribed=subscribed,
                        community_activity=recent_counts.get(voter.username, 0),
                        allow_downvotes=allow_downvotes,
                    )
                    offset = arrival_offset(
                        ordinal,
                        budget,
                        section["half_life_minutes"],
                        active_hours,
                    )
                    decision = VoteDecision(
                        kind,
                        target.id,
                        ordinal,
                        budget,
                        voter.username,
                        direction,
                        offset,
                    )
                    result.decisions.append(decision)
                    if dry_run:
                        cast_result: dict[str, Any] = {
                            "status": "dry_run",
                            "target_type": kind,
                            "target_id": target.id,
                            "voter": voter.username,
                            "value": direction,
                        }
                    else:
                        from deaddit.dynamics.votes import cast_vote

                        cast_result = cast_vote(
                            voter.username,
                            kind,
                            target.id,
                            direction,
                            source="simulated",
                            allow_recast=False,
                        )
                    result.casts.append(cast_result)
                    casts_used += 1
        return result

    run = tick


def run_active_tick(
    policy: VoteCadencePolicy | PolicyConfig | Mapping[str, Any] | None = None,
    now: datetime | None = None,
    **kwargs: Any,
) -> TickResult:
    """Convenience entry point used by tests and the future worker."""
    if now is None:
        now = datetime.utcnow()
    return ActiveWindowEngine(
        policy,
        **{
            name: kwargs.pop(name)
            for name in ("per_item_limit", "global_limit")
            if name in kwargs
        },
    ).tick(now, **kwargs)


simulate_active_tick = run_active_tick
tick_active_window = run_active_tick
