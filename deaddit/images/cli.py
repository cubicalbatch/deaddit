"""Click commands for manually verifying image-provider wiring.

Two commands live here, and they are not equivalent:

``check-connection`` runs an authenticated catalog search only. It never
generates an image, so it never costs money, and is safe to run as often as
needed while wiring up a provider's credential.

``smoke-fal`` requests exactly one real, billed image from fal.ai. It exists
so a developer can deliberately prove the full generation path works against
the live API at an explicit integration milestone - never automatically, and
never from a test. It refuses to run without --yes-i-know-this-costs-money.

Neither command is imported by, or runs under, pytest.
"""

from __future__ import annotations

import os

import click

from deaddit.images.client import generate
from deaddit.images.providers import register_default_adapters
from deaddit.images.types import Deadline, ImageProviderError
from deaddit.images.verification import test_connection
from deaddit.models import ImageProvider

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
    """Manual image-provider verification (never invoked by the test suite)."""


@images.command("check-connection")
@click.argument("provider_type", type=click.Choice(sorted(_DEFAULT_CREDENTIAL_ENV)))
@click.option(
    "--credential-env",
    default=None,
    help="Environment variable holding the API key. "
    "Defaults to FALAI_API_KEY/RUNWARE_API_KEY per provider type.",
)
@click.option("--query", default="", show_default=True, help="Catalog search text.")
def check_connection_cmd(
    provider_type: str, credential_env: str | None, query: str
) -> None:
    """Authenticated catalog search only - free, safe to run repeatedly.

    Confirms PROVIDER_TYPE's credential is set and accepted by resolving it
    from the environment and running one search_models() call. Never
    generates an image.
    """
    env_name = credential_env or _DEFAULT_CREDENTIAL_ENV[provider_type]
    if not os.environ.get(env_name):
        raise click.ClickException(
            f"{env_name} is not set in the environment. Export it "
            "(e.g. from .env) before running this command."
        )

    register_default_adapters()
    provider = ImageProvider(
        name=f"{provider_type}-check-connection",
        provider_type=provider_type,
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
    help="Environment variable holding the fal.ai API key.",
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
    if not os.environ.get(credential_env):
        raise click.ClickException(
            f"{credential_env} is not set in the environment. Export it "
            "(e.g. from .env) before running this command."
        )

    register_default_adapters()
    provider = ImageProvider(
        name="fal-smoke",
        provider_type=_SMOKE_PROVIDER_TYPE,
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


__all__ = ["images"]
