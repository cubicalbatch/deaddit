"""Click commands for reconciling generated-website files."""

from __future__ import annotations

from pathlib import Path

import click

from deaddit import create_app
from deaddit.dynamics import seeding
from deaddit.models import GeneratedWebsite
from deaddit.websites.storage import reconcile_websites, website_root


@click.group()
def websites() -> None:
    """Operator reconciliation for generated-website files."""


@websites.command("reconcile-websites")
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="Actually delete orphaned files. Without this, only reports.",
)
@click.option(
    "--i-know-this-is-prod",
    "confirmed_prod",
    is_flag=True,
    default=False,
    help="Required alongside --apply against the production database "
    "(instance/deaddit.db).",
)
@click.option(
    "--root",
    "root_override",
    type=click.Path(file_okay=False, path_type=str),
    default=None,
    help="Website root to reconcile instead of the configured root.",
)
def reconcile_websites_cmd(
    apply_changes: bool, confirmed_prod: bool, root_override: str | None
) -> None:
    """Report and, with --apply, delete orphaned generated-website files.

    Dry-run is the default and never changes the filesystem. ``--root`` exists
    because ``GENERATED_WEBSITES_ROOT`` has no environment override and
    defaults below ``instance/``; operators and tests can therefore reconcile
    a copied or alternate tree without touching the live instance directory.
    This deliberately differs from ``images reconcile-media``, which has no
    root override.
    """
    app = create_app()
    with app.app_context():
        root = Path(root_override) if root_override else website_root(app)
        rows = GeneratedWebsite.query.all()

        if (
            apply_changes
            and not confirmed_prod
            and seeding._resolves_to_production(
                app.config.get("SQLALCHEMY_DATABASE_URI"), app.instance_path
            )
        ):
            raise click.ClickException(
                "Refusing to delete websites against the production database "
                "(instance/deaddit.db). Pass --i-know-this-is-prod to force."
            )

        report = reconcile_websites(root, rows, apply=apply_changes)
        if apply_changes:
            click.echo(f"Removed {len(report.orphaned_files)} orphaned file(s).")
        else:
            click.echo(
                f"[dry-run] {len(report.orphaned_files)} orphaned file(s) would be removed:"
            )
        for path in report.orphaned_files:
            click.echo(f"  {path}")

        click.echo(
            f"{len(report.missing_rows)} generated_website row(s) reference missing files:"
        )
        for row in report.missing_rows:
            click.echo(f"  post_id={row['post_id']} storage_path={row['storage_path']}")

        click.echo(
            f"{len(report.mismatched_rows)} generated_website row(s) have hash/size mismatches:"
        )
        for row in report.mismatched_rows:
            click.echo(f"  post_id={row['post_id']} storage_path={row['storage_path']}")

        if not apply_changes:
            click.echo("Dry run only - no files were deleted. Pass --apply to delete.")


__all__ = ["websites"]
