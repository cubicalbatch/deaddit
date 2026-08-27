"""Top-level deaddit command line interface."""

import json

import click

from deaddit import create_app
from deaddit.agents.cli import agent
from deaddit.dynamics import seeding


@click.group()
def cli() -> None:
    """Deaddit administration CLI."""


cli.add_command(agent)


@click.group()
def dynamics() -> None:
    """Manage platform dynamics."""


cli.add_command(dynamics)


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


if __name__ == "__main__":
    cli()
