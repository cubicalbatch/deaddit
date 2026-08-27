"""Moderation service (Phase D4, plan §6).

Minimal-viable moderation per refactor/platform-dynamics.md §6: reports,
soft removal (plan §1 — rows are kept so karma math is never corrupted by a
delete), and scoped or site-wide bans.

Resolution 1: this module is the ONLY place outside
:mod:`deaddit.services.content` that writes Report / Ban / soft-removal
bookkeeping through ``db.session``. Content creation itself stays in the
content service.

Mod-action notifications are emitted strictly AFTER the moderation
transaction has committed. :func:`notifications.notify_mod_action` honors
the D3 isolation contract and never raises; the call sites here add a
second guard (:func:`_notify_mod_action_safely`) so that a broken or
mocked-to-raise emitter can never roll back an already-committed action.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import or_

from deaddit.dynamics import activity, notifications
from deaddit.dynamics.karma import _strip_karma_on_remove, recompute_scores_and_karma
from deaddit.extensions import db
from deaddit.models import Ban, Comment, Post, Report, User

logger = logging.getLogger(__name__)

_SNIPPET_LENGTH = 200


def _notify_mod_action_safely(**kwargs) -> None:
    """Post-commit emission guard (defense in depth).

    Roll back before logging: ORM access against a failed flush would raise
    PendingRollbackError.
    """
    try:
        notifications.notify_mod_action(**kwargs)
    except Exception:
        db.session.rollback()
        logger.warning(
            "mod_action notification failed after committed action",
            exc_info=True,
        )


def _load_report(report_id: int) -> Report:
    report = db.session.get(Report, report_id)
    if report is None:
        raise ValueError(f"report {report_id} does not exist")
    return report


def _report_target(report: Report) -> tuple[Post | Comment, str]:
    """Resolve a report's target item; the XOR is service-enforced."""
    if report.post_id is None and report.comment_id is None:
        raise ValueError(f"report {report.id} has no target")
    if report.post_id is not None:
        item = db.session.get(Post, report.post_id)
        kind = "post"
    else:
        item = db.session.get(Comment, report.comment_id)
        kind = "comment"
    if item is None:
        raise ValueError(f"{kind} for report {report.id} does not exist")
    return item, kind


def report_content(reporter: str, target: str, target_id: int, reason: str) -> Report:
    """File a complaint about a post or comment; returns the open Report."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("report reason must be non-empty")
    if target not in ("post", "comment"):
        raise ValueError(f"unknown report target '{target}'")
    if db.session.get(User, reporter) is None:
        raise ValueError(f"user '{reporter}' does not exist")
    model = Post if target == "post" else Comment
    item = db.session.get(model, target_id)
    if item is None:
        raise ValueError(f"{target} {target_id} does not exist")
    if getattr(item, "removed", False):
        raise ValueError(f"{target} {target_id} is already removed")
    report = Report(
        reporter=reporter,
        reason=reason[:500],
        **({f"{target}_id": target_id}),
    )
    db.session.add(report)
    db.session.commit()
    activity.record_event(
        event_type="report",
        username=reporter,
        post_id=report.post_id,
        comment_id=report.comment_id,
    )
    return report


def remove_report(report_id: int, moderator: str, removal_reason: str) -> Report:
    """Action a report by soft-removing its target item (plan §1).

    Sets the soft-removal bookkeeping on the Post/Comment, marks the report
    actioned, commits, THEN emits a mod_action notification to the item's
    author. Emission failure cannot undo the removal.
    """
    report = _load_report(report_id)
    if report.status != "open":
        raise ValueError(f"report {report_id} is not open")
    item, kind = _report_target(report)

    now = datetime.utcnow()
    author = item.user  # snapshot before commit expires attributes
    snippet = (item.content or "")[:_SNIPPET_LENGTH]
    item.removed = True
    item.removed_by = moderator
    item.removal_reason = removal_reason
    item.removed_at = now
    report.status = "actioned"
    report.resolved_by = moderator
    report.resolved_at = now
    report.resolution_note = removal_reason
    db.session.commit()

    if _strip_karma_on_remove():
        recompute_scores_and_karma()

    _notify_mod_action_safely(
        recipient=author,
        actor=moderator,
        post_id=report.post_id,
        comment_id=report.comment_id,
        snippet=snippet,
    )
    return report


def dismiss_report(report_id: int, moderator: str, note: str | None = None) -> Report:
    """Dismiss a report without touching its target item."""
    report = _load_report(report_id)
    if report.status != "open":
        raise ValueError(f"report {report_id} is not open")
    report.status = "dismissed"
    report.resolved_by = moderator
    report.resolved_at = datetime.utcnow()
    report.resolution_note = note
    db.session.commit()
    return report


def ban_user(
    username: str,
    reason: str,
    subdeaddit_name: str | None = None,
    expires_at: datetime | None = None,
    banned_by: str | None = None,
) -> Ban:
    """Ban a user site-wide (subdeaddit_name=None) or from one subdeaddit.

    The schema has no actor column, so the actor is kept as a boring
    ``banned by <mod>: <reason>`` prefix inside ``reason``.
    """
    if db.session.get(User, username) is None:
        raise ValueError(f"user '{username}' does not exist")
    stored_reason = f"banned by {banned_by}: {reason}" if banned_by else reason
    ban = Ban(
        username=username,
        subdeaddit_name=subdeaddit_name,
        reason=stored_reason[:500],
        expires_at=expires_at,
    )
    db.session.add(ban)
    db.session.commit()

    _notify_mod_action_safely(
        recipient=username,
        actor=banned_by,
        post_id=None,
        comment_id=None,
        snippet=stored_reason[:_SNIPPET_LENGTH],
    )
    return ban


def lift_ban(ban_id: int) -> Ban:
    """Lift a ban; lifting an already-lifted ban is an error."""
    ban = db.session.get(Ban, ban_id)
    if ban is None:
        raise ValueError(f"ban {ban_id} does not exist")
    if ban.lifted_at is not None:
        raise ValueError(f"ban {ban_id} is already lifted")
    ban.lifted_at = datetime.utcnow()
    db.session.commit()
    return ban


def active_ban_for(username: str, subdeaddit_name: str | None = None) -> Ban | None:
    """Return the active Ban governing ``username``, or None.

    A site-wide ban always wins. Otherwise only a scoped ban matching
    ``subdeaddit_name`` applies (passing None means site-wide-only lookup).
    """

    def _query(scoped: bool):
        q = db.session.query(Ban).filter(
            Ban.username == username,
            Ban.lifted_at.is_(None),
            or_(Ban.expires_at.is_(None), Ban.expires_at > datetime.utcnow()),
        )
        if scoped:
            q = q.filter(Ban.subdeaddit_name == subdeaddit_name)
        else:
            q = q.filter(Ban.subdeaddit_name.is_(None))
        return q.order_by(Ban.created_at.desc(), Ban.id.desc())

    site_wide = _query(scoped=False).first()
    if site_wide is not None:
        return site_wide
    if subdeaddit_name is None:
        return None
    return _query(scoped=True).first()


def expire_bans() -> int:
    """Auto-lift every active ban past its expiry; returns the count."""
    now = datetime.utcnow()
    expired = (
        db.session.query(Ban)
        .filter(
            Ban.lifted_at.is_(None),
            Ban.expires_at.isnot(None),
            Ban.expires_at <= now,
        )
        .all()
    )
    for ban in expired:
        ban.lifted_at = now
    db.session.commit()
    if expired:
        logger.info("auto-lifted %d expired bans", len(expired))
    return len(expired)


def list_reports(status: str | None = "open"):
    """Read-only admin queue query, newest first."""
    query = db.session.query(Report).order_by(
        Report.created_at.desc(), Report.id.desc()
    )
    if status is not None:
        query = query.filter(Report.status == status)
    return query
