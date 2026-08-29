"""Top-level deaddit command line interface."""

import json

import click

from deaddit import create_app
from deaddit.agents.cli import agent
from deaddit.dynamics import baseline, seeding
from deaddit.images.cli import images
from deaddit.websites.cli import websites


@click.group()
def cli() -> None:
    """Deaddit administration CLI."""


cli.add_command(agent)
cli.add_command(images)
cli.add_command(websites)


@click.group()
def dynamics() -> None:
    """Manage platform dynamics."""


cli.add_command(dynamics)


@dynamics.command("baseline-report")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Path to a SQLite DB COPY (read-only); defaults to instance/deaddit.db",
)
@click.option(
    "--as-of",
    default=None,
    help="UTC snapshot time (ISO 8601); defaults to the latest DB timestamp.",
)
@click.option(
    "--top-k",
    type=click.IntRange(min=0, max=1000),
    default=10,
    show_default=True,
    help="Number of active posts in the hot-feed replay listing.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Dump the stable report object as JSON.",
)
def baseline_report(
    db_path: str | None, as_of: str | None, top_k: int, as_json: bool
) -> None:
    """Report Phase 0 agent/vote baseline metrics without writing state."""
    try:
        conn = baseline.connect_ro(db_path)
        try:
            report = baseline.compute_report(conn, as_of=as_of, top_k=top_k)
        finally:
            conn.close()
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(report, indent=2, sort_keys=True))
    else:
        click.echo(baseline.render_text(report))


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
