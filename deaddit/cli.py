"""Top-level deaddit command line interface."""

import click

from deaddit.agents.cli import agent


@click.group()
def cli() -> None:
    """Deaddit administration CLI."""


cli.add_command(agent)


if __name__ == "__main__":
    cli()
