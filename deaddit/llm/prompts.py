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
import re
from datetime import datetime

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
