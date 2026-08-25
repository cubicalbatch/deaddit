"""Top-level deaddit command line interface."""

import json

import click

from deaddit import create_app
from deaddit.agents.cli import agent
from deaddit.dynamics.seeding import (
    _resolves_to_production,
    backfill_history,
)


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


if __name__ == "__main__":
    cli()
