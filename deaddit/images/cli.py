"""Click commands for manually verifying image-provider wiring and cleanup.

Three commands live here, and they are not equivalent:

``check-connection`` runs an authenticated catalog search only. It never
generates an image, so it never costs money, and is safe to run as often as
needed while wiring up a provider's credential.

``smoke-fal`` requests exactly one real, billed image from fal.ai. It exists
so a developer can deliberately prove the full generation path works against
the live API at an explicit integration milestone - never automatically, and
never from a test. It refuses to run without --yes-i-know-this-costs-money.

``reconcile-media`` is the operator-safe cleanup command (plan 7A): it
reports generated-image files with no owning ``post_image`` row, and
database rows whose files have gone missing. It is dry-run by default and
only ever deletes files when passed --apply, at which point it applies the
same production-database guard as ``deaddit dynamics seed-history``.

``check-connection`` and ``smoke-fal`` are never imported by, or run under,
pytest; ``reconcile-media`` is safe to and is covered by the test suite.
"""

from __future__ import annotations

import os

import click

from deaddit import create_app
from deaddit.dynamics import seeding
from deaddit.images.client import generate
from deaddit.images.providers import register_default_adapters
from deaddit.images.service import preview_orphaned_media
from deaddit.images.storage import media_root, reconcile_media
from deaddit.images.types import Deadline, ImageProviderError
from deaddit.images.verification import test_connection
from deaddit.models import ImageProvider, PostImage

_DEFAULT_CREDENTIAL_ENV = {
    "fal": "FALAI_API_KEY",
    "runware": "RUNWARE_API_KEY",
}
_SMOKE_PROVIDER_TYPE = "fal"
_SMOKE_MODEL = "fal-ai/flux-1/schnell"
_SMOKE_CREDENTIAL_ENV = "FALAI_API_KEY"
_SMOKE_PROMPT = "a red bicycle leaning against a brick wall, photograph"
_SMOKE_DEADLINE_SECONDS = 120.0


@click.group()
def images() -> None:
    """Manual image-provider verification plus operator media cleanup."""


@images.command("check-connection")
@click.argument("provider_type", type=click.Choice(sorted(_DEFAULT_CREDENTIAL_ENV)))
@click.option(
    "--credential-env",
    default=None,
    help="Environment variable holding the API key (fallback when --api-key "
    "is not given). Defaults to FALAI_API_KEY/RUNWARE_API_KEY per provider type.",
)
@click.option(
    "--api-key",
    default=None,
    help="API key to use directly (equivalent to a key saved in the admin UI).",
)
@click.option("--query", default="", show_default=True, help="Catalog search text.")
def check_connection_cmd(
    provider_type: str, credential_env: str | None, api_key: str | None, query: str
) -> None:
    """Authenticated catalog search only - free, safe to run repeatedly.

    Confirms PROVIDER_TYPE's credential is set and accepted by resolving it
    from --api-key or the environment and running one search_models() call.
    Never generates an image.
    """
    env_name = credential_env or _DEFAULT_CREDENTIAL_ENV[provider_type]
    if not api_key and not os.environ.get(env_name):
        raise click.ClickException(
            f"Neither --api-key nor the {env_name} environment variable is "
            "set. Provide one before running this command."
        )

    register_default_adapters()
    provider = ImageProvider(
        name=f"{provider_type}-check-connection",
        provider_type=provider_type,
        api_key=api_key,
        credential_env=env_name,
        is_enabled=True,
    )

    result = test_connection(provider, query=query)
    click.echo(result.message)
    if result.sample_model_ids:
        click.echo("Sample model IDs:")
        for model_id in result.sample_model_ids:
            click.echo(f"  {model_id}")
    if not result.ok:
        raise click.ClickException(result.message)


@images.command("smoke-fal")
@click.option("--model", default=_SMOKE_MODEL, show_default=True)
@click.option("--prompt", default=_SMOKE_PROMPT, show_default=True)
@click.option(
    "--credential-env",
    default=_SMOKE_CREDENTIAL_ENV,
    show_default=True,
    help="Environment variable holding the fal.ai API key (fallback when "
    "--api-key is not given).",
)
@click.option(
    "--api-key",
    default=None,
    help="API key to use directly (equivalent to a key saved in the admin UI).",
)
@click.option(
    "--deadline-seconds",
    type=float,
    default=_SMOKE_DEADLINE_SECONDS,
    show_default=True,
)
@click.option(
    "--yes-i-know-this-costs-money",
    "confirmed",
    is_flag=True,
    default=False,
    help="Required. This command requests one real, billed fal.ai image.",
)
def smoke_fal_cmd(
    model: str,
    prompt: str,
    credential_env: str,
    api_key: str | None,
    deadline_seconds: float,
    confirmed: bool,
) -> None:
    """Request exactly one real fal.ai image to prove live wiring end to end.

    This is not a test: nothing in this codebase invokes it automatically.
    It bills a real fal.ai account for one image and counts against the
    project's fixed paid-generation budget, so it requires
    --yes-i-know-this-costs-money and must be run by hand, deliberately, at
    an explicit integration milestone.
    """
    if not confirmed:
        raise click.ClickException(
            "Refusing to spend money without --yes-i-know-this-costs-money. "
            f"This requests exactly one real, billed image from fal.ai "
            f"model {model!r}."
        )
    if not api_key and not os.environ.get(credential_env):
        raise click.ClickException(
            f"Neither --api-key nor the {credential_env} environment variable "
            "is set. Provide one before running this command."
        )

    register_default_adapters()
    provider = ImageProvider(
        name="fal-smoke",
        provider_type=_SMOKE_PROVIDER_TYPE,
        api_key=api_key,
        credential_env=credential_env,
        default_model=model,
        is_enabled=True,
    )

    click.echo(f"Requesting exactly one real image from fal.ai model {model!r}...")
    try:
        result = generate(provider, model, prompt, Deadline.after(deadline_seconds))
    except ImageProviderError as exc:
        raise click.ClickException(f"fal.ai generation failed: {exc}") from exc

    click.echo(f"request_id={result.request_id}")
    click.echo(f"image_url={result.image_url}")
    click.echo(f"safety_verdict={result.safety_verdict}")


@images.command("reconcile-media")
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
def reconcile_media_cmd(apply_changes: bool, confirmed_prod: bool) -> None:
    """Report - and, with --apply, delete - orphaned generated-image files.

    Dry-run by default: with no flags, this only reports files under the
    generated-image root that no ``post_image`` row references, plus rows
    whose files have gone missing from disk. It never touches the
    filesystem in this mode, so it is always safe to run, including
    against production.

    Pass --apply to actually delete the orphaned files it finds. Applying
    against the production database (instance/deaddit.db) additionally
    requires --i-know-this-is-prod, the same guard
    ``deaddit dynamics seed-history`` uses for its own production check.
    """
    app = create_app()
    with app.app_context():
        root = media_root(app)
        rows = PostImage.query.all()

        if not apply_changes:
            report = preview_orphaned_media(root, rows)
            click.echo(
                f"[dry-run] {len(report.removed_files)} orphaned file(s) would "
                "be removed:"
            )
            for path in report.removed_files:
                click.echo(f"  {path}")
            if report.incomplete_rows:
                click.echo(
                    f"{len(report.incomplete_rows)} post_image row(s) reference "
                    "missing files:"
                )
                for entry in report.incomplete_rows:
                    click.echo(
                        f"  post_id={entry['post_id']} missing={entry['missing']}"
                    )
            click.echo("Dry run only - no files were deleted. Pass --apply to delete.")
            return

        if not confirmed_prod and seeding._resolves_to_production(
            app.config.get("SQLALCHEMY_DATABASE_URI"), app.instance_path
        ):
            raise click.ClickException(
                "Refusing to delete media against the production database "
                "(instance/deaddit.db). Pass --i-know-this-is-prod to force."
            )

        report = reconcile_media(root, rows)
        click.echo(f"Removed {len(report.removed_files)} orphaned file(s).")
        for path in report.removed_files:
            click.echo(f"  {path}")
        if report.incomplete_rows:
            click.echo(
                f"{len(report.incomplete_rows)} post_image row(s) reference "
                "missing files:"
            )
            for entry in report.incomplete_rows:
                click.echo(f"  post_id={entry['post_id']} missing={entry['missing']}")


__all__ = ["images"]
