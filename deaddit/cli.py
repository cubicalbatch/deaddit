"""Top-level deaddit command line interface."""

import json
import shlex

import click

from deaddit import create_app
from deaddit.agents.cli import agent
from deaddit.dynamics import seeding
from deaddit.dynamics.seeding import _resolves_to_production, backfill_history


@click.group()
def cli() -> None:
    """Deaddit administration CLI."""


cli.add_command(agent)


@click.group()
def dynamics() -> None:
    """Manage platform dynamics."""


cli.add_command(dynamics)


@dynamics.command("backfill")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Compute the report without writing any rows.",
)
@click.option("--batch-size", type=int, default=500, show_default=True)
@click.option(
    "--i-know-this-is-prod",
    is_flag=True,
    default=False,
    help="Explicitly allow backfilling the production database (instance/deaddit.db).",
)
def backfill(dry_run: bool, batch_size: int, i_know_this_is_prod: bool) -> None:
    """Backfill synthetic vote history for legacy posts and comments."""
    app = create_app()
    with app.app_context():
        if not i_know_this_is_prod and _resolves_to_production(
            app.config.get("SQLALCHEMY_DATABASE_URI"), app.instance_path
        ):
            raise click.ClickException(
                "Refusing to backfill the production database (instance/deaddit.db). "
                "Pass --i-know-this-is-prod to force."
            )
        try:
            report = backfill_history(
                batch_size=batch_size,
                dry_run=dry_run,
                allow_production=i_know_this_is_prod,
            )
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(report, indent=2))


@dynamics.command("seed-history")
@click.option("--days", type=int, default=14, show_default=True)
@click.option("--seed", type=int, default=42, show_default=True)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Compute the projected report without writing any rows.",
)
@click.option(
    "--i-know-this-is-prod",
    is_flag=True,
    default=False,
    help="Explicitly allow seeding the production database (instance/deaddit.db).",
)
def seed_history_cmd(
    days: int,
    seed: int,
    dry_run: bool,
    i_know_this_is_prod: bool,
) -> None:
    """Deterministically seed a synthetic content history."""
    app = create_app()
    with app.app_context():
        if not i_know_this_is_prod and seeding._resolves_to_production(
            app.config.get("SQLALCHEMY_DATABASE_URI"), app.instance_path
        ):
            raise click.ClickException(
                "Refusing to seed the production database (instance/deaddit.db). "
                "Pass --i-know-this-is-prod to force."
            )
        try:
            report = seeding.seed_history(
                days=days,
                seed=seed,
                dry_run=dry_run,
                allow_production=i_know_this_is_prod,
            )
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(report, indent=2))


@cli.command("secrets-drain")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the export without deleting any rows.",
)
@click.option(
    "--i-know-this-is-prod",
    is_flag=True,
    default=False,
    help="Explicitly allow draining the production database (instance/deaddit.db).",
)
def secrets_drain_command(dry_run: bool, i_know_this_is_prod: bool) -> None:
    """Export legacy database-stored secrets as .env lines, then scrub them."""
    app = create_app()
    with app.app_context():
        if not i_know_this_is_prod and _resolves_to_production(
            app.config.get("SQLALCHEMY_DATABASE_URI"), app.instance_path
        ):
            raise click.ClickException(
                "Refusing to drain the production database (instance/deaddit.db). "
                "Pass --i-know-this-is-prod to force."
            )
        from deaddit.settings.drain import drain_secrets

        report = drain_secrets(dry_run=dry_run)
        env_lines = [
            f"{key}={shlex.quote(value)}"
            for key, value in sorted(report["found"].items())
        ]
        if env_lines:
            header = "# DRY RUN — rows left in place\n" if dry_run else ""
            click.echo(header + "\n".join(env_lines))
        # Summary counts only — the .env block above is the one place the
        # secret values appear.
        click.echo(
            json.dumps(
                {
                    "found": len(report["found"]),
                    "removed": len(report["removed"]),
                    "dry_run": report["dry_run"],
                }
            )
        )


if __name__ == "__main__":
    cli()
