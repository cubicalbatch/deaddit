"""One-shot export and scrub of legacy database-stored secrets."""

from __future__ import annotations


def drain_secrets(dry_run: bool = False) -> dict:
    """Export legacy secret Setting rows and delete them from the database.

    Must be called inside an application context. Returns
    ``{"found": {key: value}, "removed": [keys], "dry_run": bool}``;
    idempotent by construction — once drained, later calls find nothing.
    """
    # Imported lazily: deaddit.config imports this package, so a top-level
    # import would be circular.
    from deaddit.config import is_secret_key
    from deaddit.extensions import db
    from deaddit.models import Setting

    found = {
        row.key: row.value if row.value is not None else ""
        for row in db.session.query(Setting).all()
        if is_secret_key(row.key)
    }

    removed: list[str] = []
    if not dry_run and found:
        for key in found:
            row = db.session.get(Setting, key)
            if row is not None:
                db.session.delete(row)
                removed.append(key)
        db.session.commit()

    return {"found": found, "removed": removed, "dry_run": dry_run}
