"""Prompt versioning registry (Phase LLM-5).

Immutable, versioned prompt templates with deterministic rendering,
pinning of agents/cohorts to exact versions, and a render audit trail.

Design points:
- Stored versions are immutable: a SQLAlchemy ``before_update`` guard
  rejects any change to ``body``/``version``/``template_id``.
- Rendering is plain ``{name}`` substitution with strict binding checks:
  missing OR extra variables raise :class:`UnknownPromptVariable`. No
  format specs, no attribute access, no recursion — same inputs plus the
  same version always produce byte-identical output.
- Pinning is one row per ``(target_kind, target_key)`` pointing at an
  exact version number; resolution precedence is agent pin first, then
  cohort pin (cohort key taken from ``Agent.config["cohort"]``).
- Every registry-mediated render writes a :class:`PromptRenderAudit`
  row (variables + sha256), giving the joinable audit trail of which
  prompt version produced which run without schema churn on
  ``agent_run`` / ``llm_usage``.

PARITY FREEZE (Wave 5): nothing here changes any live effective prompt.
The whole path is inert unless ``Config.PROMPT_VERSIONING_ENABLED``
is switched to ``true`` AFTER the AC-P3 measurement window closes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Mapping
from sqlalchemy import event

from deaddit import Config
from deaddit.extensions import db
from deaddit.models import (
    PromptPin,
    PromptRenderAudit,
    PromptTemplate,
    PromptTemplateVersion,
)

_TOKEN_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class WeightedCatalogItem:
    """One weighted, stable-id option in a visit profile catalog."""

    id: str
    text: str
    weight: float


@dataclass(frozen=True)
class BehaviorBlock:
    """An ordered behavior instruction block."""

    id: str
    text: str


@dataclass(frozen=True)
class VisitProfile:
    """Immutable, validated document used to prepare an agent visit."""

    schema_version: int
    system_template: str
    layouts: Mapping[str, str]
    behavior_blocks: tuple[BehaviorBlock, ...]
    intent_mix: Mapping[str, float]
    length_catalog: Mapping[str, tuple[WeightedCatalogItem, ...]]
    direction_catalog: Mapping[str, tuple[WeightedCatalogItem, ...]]
    sample_count: int
    # Resolver metadata; never serialized in the profile body.
    profile_version: int | None = None
    profile_ref: str | None = None

    @property
    def version(self) -> int | None:
        return self.profile_version

    @property
    def ref(self) -> str | None:
        return self.profile_ref


_PROFILE_TEMPLATE_NAME = "agent.visit_profile"
_PROFILE_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "system_template",
        "layouts",
        "behavior_blocks",
        "intent_mix",
        "length_catalog",
        "direction_catalog",
        "sample_count",
    }
)
_PROFILE_INTENTS = frozenset({"post", "image", "website"})
_PROFILE_CONTENT_KINDS = frozenset({"comment", "text_post", "media_post"})
_PROFILE_DIRECTION_KINDS = frozenset({"post", "comment"})
_PROFILE_VARIABLES = frozenset(
    {
        "persona",
        "persona_block",
        "autonomy_tier",
        "tier_line",
        "rules_block",
        "tools",
        "tools_line",
        "genuine",
        "genuine_line",
        "quality_rules",
        "profile_quality_rules",
        "capability_guidance",
        "memories",
        "memory_block",
        "subscriptions",
        "subscriptions_section",
        "community_hint",
        "intent",
        "content_kind",
        "length_target",
        "directions",
        "sample_count",
    }
)
_PROFILE_VARIABLE_RE = re.compile(r"\{([^{}]*)\}")


def _profile_error(message: str) -> PromptError:
    return PromptError(f"Invalid agent.visit_profile: {message}")


def _profile_string(value: object, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        adjective = "non-empty " if nonempty else ""
        raise _profile_error(f"{path} must be a {adjective}string")
    return value


def _validate_profile_variables(text: str, path: str) -> None:
    for match in _PROFILE_VARIABLE_RE.finditer(text):
        name = match.group(1)
        if not _TOKEN_RE.fullmatch(match.group(0)) or name not in _PROFILE_VARIABLES:
            raise _profile_error(
                f"{path} contains unknown or unsafe variable {match.group(0)!r}"
            )
    leftovers = _PROFILE_VARIABLE_RE.sub("", text)
    if "{" in leftovers or "}" in leftovers:
        raise _profile_error(f"{path} contains an unmatched brace")


def _catalog_items(
    value: object, path: str, *, direction: bool
) -> tuple[WeightedCatalogItem, ...]:
    if not isinstance(value, list):
        raise _profile_error(f"{path} must be an array")
    result: list[WeightedCatalogItem] = []
    ids: set[str] = set()
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict) or set(item) != {"id", "text", "weight"}:
            raise _profile_error(f"{item_path} must contain only id, text, and weight")
        item_id = _profile_string(item["id"], f"{item_path}.id")
        text = _profile_string(item["text"], f"{item_path}.text")
        _validate_profile_variables(text, f"{item_path}.text")
        weight = item["weight"]
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise _profile_error(f"{item_path}.weight must be numeric")
        if not math.isfinite(float(weight)) or float(weight) <= 0:
            raise _profile_error(f"{item_path}.weight must be finite and positive")
        if item_id in ids:
            raise _profile_error(f"duplicate stable id {item_id!r}")
        expected_prefix = f"{path.rsplit('.', 1)[-1]}."
        if not item_id.startswith(expected_prefix):
            raise _profile_error(f"{item_path}.id is incompatible with its content kind")
        ids.add(item_id)
        result.append(WeightedCatalogItem(item_id, text, float(weight)))
    return tuple(result)



def parse_visit_profile(body: str | dict) -> VisitProfile:
    """Parse and strictly validate a canonical ``agent.visit_profile`` body."""
    if isinstance(body, str):
        try:
            document = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise _profile_error("body is not valid JSON") from exc
    elif isinstance(body, dict):
        document = body
    else:
        raise _profile_error("body must be JSON text or an object")
    if not isinstance(document, dict) or set(document) != _PROFILE_TOP_LEVEL:
        raise _profile_error("unknown or missing top-level fields")
    if document["schema_version"] != 1:
        raise _profile_error("schema_version must be 1")
    system_template = _profile_string(
        document["system_template"], "system_template", nonempty=False
    )
    # ``system_template`` is opaque by design: migrations must retain legacy
    # prompt bytes even when they contain braces intended for another layer.
    raw_layouts = document["layouts"]
    if not isinstance(raw_layouts, dict) or set(raw_layouts) != {
        "system",
        "lurker",
        "browse",
        "post",
    }:
        raise _profile_error("layouts must contain exactly system, lurker, browse, and post")
    layouts = {}
    for name in ("system", "lurker", "browse", "post"):
        layouts[name] = _profile_string(raw_layouts[name], f"layouts.{name}")
        if name != "system":
            _validate_profile_variables(layouts[name], f"layouts.{name}")
    raw_blocks = document["behavior_blocks"]
    if not isinstance(raw_blocks, list):
        raise _profile_error("behavior_blocks must be an array")
    blocks: list[BehaviorBlock] = []
    block_ids: set[str] = set()
    for index, block in enumerate(raw_blocks):
        path = f"behavior_blocks[{index}]"
        if not isinstance(block, dict) or set(block) != {"id", "text"}:
            raise _profile_error(f"{path} must contain only id and text")
        block_id = _profile_string(block["id"], f"{path}.id")
        text = _profile_string(block["text"], f"{path}.text")
        _validate_profile_variables(text, f"{path}.text")
        if block_id in block_ids:
            raise _profile_error(f"duplicate stable id {block_id!r}")
        block_ids.add(block_id)
        blocks.append(BehaviorBlock(block_id, text))

    raw_mix = document["intent_mix"]
    if not isinstance(raw_mix, dict) or set(raw_mix) != _PROFILE_INTENTS:
        raise _profile_error("intent_mix must contain exactly post, image, and website")
    intent_mix: dict[str, float] = {}
    for name in sorted(_PROFILE_INTENTS):
        value = raw_mix[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _profile_error(f"intent_mix.{name} must be numeric")
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise _profile_error(f"intent_mix.{name} must be between 0 and 1")
        intent_mix[name] = value
    if intent_mix["image"] + intent_mix["website"] > 1.0 + 1e-9:
        raise _profile_error("image and website intent mix cannot exceed 1")

    raw_lengths = document["length_catalog"]
    if not isinstance(raw_lengths, dict) or set(raw_lengths) != _PROFILE_CONTENT_KINDS:
        raise _profile_error("length_catalog has incompatible content kinds")
    length_catalog = {
        kind: _catalog_items(raw_lengths[kind], f"length_catalog.{kind}", direction=False)
        for kind in sorted(_PROFILE_CONTENT_KINDS)
    }
    raw_directions = document["direction_catalog"]
    if not isinstance(raw_directions, dict) or set(raw_directions) != _PROFILE_DIRECTION_KINDS:
        raise _profile_error("direction_catalog has incompatible content kinds")
    direction_catalog = {
        kind: _catalog_items(raw_directions[kind], f"direction_catalog.{kind}", direction=True)
        for kind in sorted(_PROFILE_DIRECTION_KINDS)
    }
    stable_ids = {block.id for block in blocks}
    for catalog in (*length_catalog.values(), *direction_catalog.values()):
        for item in catalog:
            if item.id in stable_ids:
                raise _profile_error(f"duplicate stable id {item.id!r}")
    sample_count = document["sample_count"]
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or not 1 <= sample_count <= 20
    ):
        raise _profile_error("sample_count must be an integer between 1 and 20")
    for kind, catalog in direction_catalog.items():
        if len(catalog) < sample_count:
            raise _profile_error(
                f"direction_catalog.{kind} must contain at least sample_count entries"
            )
    return VisitProfile(
        schema_version=1,
        system_template=system_template,
        layouts=MappingProxyType(layouts),
        behavior_blocks=tuple(blocks),
        intent_mix=MappingProxyType(intent_mix),
        length_catalog=MappingProxyType(length_catalog),
        direction_catalog=MappingProxyType(direction_catalog),
        sample_count=sample_count,
    )


def _profile_document(profile: VisitProfile) -> dict:
    return {
        "schema_version": profile.schema_version,
        "system_template": profile.system_template,
        "layouts": dict(profile.layouts),
        "behavior_blocks": [
            {"id": block.id, "text": block.text} for block in profile.behavior_blocks
        ],
        "intent_mix": dict(profile.intent_mix),
        "length_catalog": {
            kind: [
                {"id": item.id, "text": item.text, "weight": item.weight}
                for item in items
            ]
            for kind, items in profile.length_catalog.items()
        },
        "direction_catalog": {
            kind: [
                {"id": item.id, "text": item.text, "weight": item.weight}
                for item in items
            ]
            for kind, items in profile.direction_catalog.items()
        },
        "sample_count": profile.sample_count,
    }


def serialize_visit_profile(profile: VisitProfile) -> str:
    """Serialize a validated profile into deterministic canonical JSON."""
    if not isinstance(profile, VisitProfile):
        raise TypeError("profile must be a VisitProfile")
    validated = parse_visit_profile(_profile_document(profile))
    return json.dumps(_profile_document(validated), sort_keys=True, separators=(",", ":"))


class PromptError(Exception):
    """Base class for prompt-registry errors."""


class UnknownPromptVariable(PromptError):
    """A render was attempted with missing or unexpected variables."""


class PromptVersionImmutable(PromptError):
    """An attempt was made to mutate a stored prompt template version."""


# --- rendering -----------------------------------------------------------


def render(body: str, variables: dict | None = None) -> str:
    """Render ``body`` with strict variable binding.

    Deterministic: identical ``(body, variables)`` always yield identical
    bytes. Missing and extra bindings are both errors.
    """
    variables = dict(variables or {})
    known = set(_TOKEN_RE.findall(body))
    missing = sorted(known - variables.keys())
    extra = sorted(variables.keys() - known)
    problems: list[str] = []
    if missing:
        problems.append(f"missing: {', '.join(repr(n) for n in missing)}")
    if extra:
        problems.append(f"unexpected: {', '.join(repr(n) for n in extra)}")
    if problems:
        raise UnknownPromptVariable("; ".join(problems))
    return _TOKEN_RE.sub(lambda m: str(variables[m.group(1)]), body)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_render(
    version_row: PromptTemplateVersion,
    rendered_text: str,
    subject_kind: str,
    subject_key: str | None,
    variables: dict | None = None,
) -> PromptRenderAudit:
    """Append one audit row proving which version produced which run."""
    row = PromptRenderAudit(
        template_id=version_row.template_id,
        template_version_id=version_row.id,
        subject_kind=subject_kind,
        subject_key=subject_key,
        rendered_sha256=_sha256(rendered_text),
        variables_json=json.dumps(variables or {}, sort_keys=True),
        created_at=datetime.utcnow(),
    )
    db.session.add(row)
    return row


# --- templates & versions ------------------------------------------------


def create_template(
    name: str,
    body: str,
    description: str | None = None,
    created_by: str | None = None,
) -> PromptTemplateVersion:
    """Create a template together with its immutable version 1."""
    if name == _PROFILE_TEMPLATE_NAME:
        body = serialize_visit_profile(parse_visit_profile(body))
    template = PromptTemplate(name=name, description=description)
    db.session.add(template)
    db.session.flush()  # assign template.id
    row = PromptTemplateVersion(
        template_id=template.id,
        version=1,
        body=body,
        created_by=created_by,
    )
    db.session.add(row)
    db.session.commit()
    return row
def create_version(
    name_or_template: str | PromptTemplate,
    body: str,
    created_by: str | None = None,
) -> PromptTemplateVersion:
    """Write version n+1 for the template; existing versions stay queryable."""
    template = (
        name_or_template
        if isinstance(name_or_template, PromptTemplate)
        else PromptTemplate.query.filter_by(name=name_or_template).first()
    )
    if template is None:
        raise PromptError(f"Unknown prompt template {name_or_template!r}")
    if template.name == _PROFILE_TEMPLATE_NAME:
        body = serialize_visit_profile(parse_visit_profile(body))
    last = (
        PromptTemplateVersion.query.filter_by(template_id=template.id)
        .order_by(PromptTemplateVersion.version.desc())
        .first()
    )
    row = PromptTemplateVersion(
        template_id=template.id,
        version=(last.version if last else 0) + 1,
        body=body,
        created_by=created_by,
    )
    db.session.add(row)
    db.session.commit()
    return row


def get_template(name: str) -> PromptTemplate | None:
    return PromptTemplate.query.filter_by(name=name).first()


def get_version(
    name_or_template: str | PromptTemplate, version: int | None = None
) -> PromptTemplateVersion | None:
    """Fetch one version; ``None`` selects the latest."""
    template = (
        name_or_template
        if isinstance(name_or_template, PromptTemplate)
        else get_template(name_or_template)
    )
    if template is None:
        return None
    query = PromptTemplateVersion.query.filter_by(template_id=template.id)
    if version is None:
        return query.order_by(PromptTemplateVersion.version.desc()).first()
    return query.filter_by(version=version).first()


# --- pins ----------------------------------------------------------------
def set_pin(
    target_kind: str,
    target_key: str,
    template_name: str,
    version_number: int,
) -> PromptPin:
    """Point one agent/cohort at an exact template version (upsert)."""
    template = get_template(template_name)
    if template is None:
        raise PromptError(f"Unknown prompt template {template_name!r}")
    if template_name == _PROFILE_TEMPLATE_NAME:
        version = get_version(template, version_number)
        if version is None:
            raise PromptError(
                f"Unknown version {version_number} of prompt template {template_name!r}"
            )
        # Validate before touching an existing pin, so failed writes cannot
        # accidentally replace a valid pin.
        parse_visit_profile(version.body)
    row = PromptPin.query.filter_by(
        target_kind=target_kind, target_key=target_key
    ).first()
    if row is None:
        row = PromptPin(target_kind=target_kind, target_key=target_key)
        db.session.add(row)
    row.template_id = template.id
    row.version_number = version_number
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return row


def clear_pin(target_kind: str, target_key: str) -> bool:
    row = PromptPin.query.filter_by(
        target_kind=target_kind, target_key=target_key
    ).first()
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def get_pin(target_kind: str, target_key: str) -> PromptPin | None:
    return PromptPin.query.filter_by(
        target_kind=target_kind, target_key=target_key
    ).first()


def _profile_pin_version(pin: PromptPin, source: str) -> PromptTemplateVersion:
    template = db.session.get(PromptTemplate, pin.template_id)
    if template is None or template.name != _PROFILE_TEMPLATE_NAME:
        raise PromptError(
            f"{source} pin must reference template {_PROFILE_TEMPLATE_NAME!r}"
        )
    version = PromptTemplateVersion.query.filter_by(
        template_id=pin.template_id, version=pin.version_number
    ).first()
    if version is None:
        raise PromptError(f"{source} pin references a missing profile version")
    parse_visit_profile(version.body)
    return version


def resolve_visit_profile(
    agent, source_default: VisitProfile | dict
) -> tuple[VisitProfile, PromptTemplateVersion | None, str]:
    """Resolve profile pins in agent, cohort, global, then source order."""
    candidates: list[tuple[str, PromptPin | None]] = [
        ("agent", get_pin("agent", str(agent.id))),
    ]
    cohort = (getattr(agent, "config", None) or {}).get("cohort")
    candidates.append(("cohort", get_pin("cohort", str(cohort)) if cohort else None))
    candidates.append(("global", get_pin("global", _PROFILE_TEMPLATE_NAME)))
    for source, pin in candidates:
        if pin is None:
            continue
        version = _profile_pin_version(pin, source)
        profile = parse_visit_profile(version.body)
        return (
            replace(
                profile,
                profile_version=version.version,
                profile_ref=f"{_PROFILE_TEMPLATE_NAME}:v{version.version}",
            ),
            version,
            source,
        )
    profile = (
        source_default
        if isinstance(source_default, VisitProfile)
        else parse_visit_profile(source_default)
    )
    return (
        replace(profile, profile_ref=profile.profile_ref or "source_default"),
        None,
        "default",
    )


def resolve_pin(target_kind: str, target_key: str) -> PromptPin | None:
    """Resolve the pin for a target: its own pin first, then its cohort's.

    Agent target keys are decimal agent ids. The cohort key is read from
    ``Agent.config["cohort"]`` when present; targets without a cohort fall
    back to their own pin only.
    """
    row = get_pin(target_kind, target_key)
    if row is not None:
        return row
    if target_kind == "agent":
        from deaddit.models import Agent

        try:
            agent = db.session.get(Agent, int(target_key))
        except (TypeError, ValueError):
            return None
        cohort = (agent.config or {}).get("cohort") if agent else None
        if cohort:
            return get_pin("cohort", str(cohort))
    return None


def render_pinned(
    target_kind: str,
    target_key: str,
    variables: dict | None = None,
    subject_key: str | None = None,
) -> tuple[str, PromptTemplateVersion] | None:
    """Render the pinned version for a target, auditing the render.

    Returns ``(rendered_text, version_row)`` or ``None`` when the target
    has no resolvable pin.
    """
    pin = resolve_pin(target_kind, target_key)
    if pin is None:
        return None
    version_row = PromptTemplateVersion.query.filter_by(
        template_id=pin.template_id, version=pin.version_number
    ).first()
    if version_row is None:
        raise PromptError(
            f"Pin for {target_kind}/{target_key} references missing "
            f"version {pin.version_number} of template {pin.template_id}"
        )
    text = render(version_row.body, variables)
    record_render(
        version_row,
        text,
        subject_kind=target_kind,
        subject_key=subject_key or target_key,
        variables=variables,
    )
    return text, version_row


# --- feature flag --------------------------------------------------------


def versioning_enabled() -> bool:
    """Whether versioned prompts drive live prompt assembly.

    PARITY FREEZE: default ``false``; flipping this AFTER the AC-P3 window
    is the documented one-command activation.
    """
    return Config.get("PROMPT_VERSIONING_ENABLED", "false") == "true"


# --- immutability guard --------------------------------------------------


@event.listens_for(PromptTemplateVersion, "before_update")
def _freeze_version(mapper, connection, target):  # noqa: ANN001
    """Reject any mutation of stored version content."""
    state = db.inspect(target)
    changed = [
        attr
        for attr in ("template_id", "version", "body")
        if state.attrs[attr].history.has_changes()
    ]
    if changed:
        raise PromptVersionImmutable(
            f"Prompt template versions are immutable "
            f"(attempted change to {', '.join(changed)}); "
            f"write a new version instead."
        )
