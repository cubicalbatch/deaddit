"""Provider verification hooks and the images CLI.

Every test here uses FakeImageAdapter registered through
deaddit.images.client.register_adapter()/reset_adapters() - no test contacts
fal.ai or Runware, and no test reads FALAI_API_KEY or RUNWARE_API_KEY. The
generate() smoke path (deaddit images smoke-fal) is exercised with
deaddit.images.cli.generate monkeypatched to a stub, so it never reaches a real
adapter, a real transport, or a real credential.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from deaddit import cli as cli_module
from deaddit.images import cli as images_cli
from deaddit.images.client import get_adapter, register_adapter, reset_adapters
from deaddit.images.providers import register_default_adapters
from deaddit.images.providers.fal import FalAdapter
from deaddit.images.providers.runware import RunwareAdapter
from deaddit.images.types import (
    Deadline,
    ImageAuthError,
    ImageCredentialError,
    ImageGenerationResult,
    ImageProviderDisabledError,
    MalformedImageResultError,
    ModelOption,
    ModelSearchResult,
)
from deaddit.images.verification import test_connection as check_connection
from deaddit.models import ImageProvider
from tests.fakes import FakeImageAdapter

# Invented credential names: no test here ever reads a real provider key.
_FAKE_CREDENTIAL_ENV = "TEST_IMAGE_PROVIDER_CREDENTIAL"
_CLI_CRED = "TEST_CLI_IMAGES_CREDENTIAL"


@pytest.fixture(autouse=True)
def _clean_adapter_registry():
    reset_adapters()
    yield
    reset_adapters()


def _provider(**overrides) -> ImageProvider:
    fields = {
        "name": "Example Provider",
        "provider_type": "fal",
        "credential_env": _FAKE_CREDENTIAL_ENV,
        "default_model": "example/model",
        "is_enabled": True,
    }
    fields.update(overrides)
    return ImageProvider(**fields)


def _search_result(count: int) -> ModelSearchResult:
    return ModelSearchResult(
        options=[
            ModelOption(model_id=f"m{i}", display_name=f"Model {i}")
            for i in range(count)
        ],
        next_cursor=None,
    )


def test_connection_check_is_search_only_and_never_raises(monkeypatch):
    monkeypatch.setenv(_FAKE_CREDENTIAL_ENV, "fake-value")

    fake = FakeImageAdapter()
    fake.enqueue_search(_search_result(2))
    register_adapter("fal", fake)

    result = check_connection(_provider())
    assert result.ok is True
    assert "2 models" in result.message
    assert result.sample_model_ids == ["m0", "m1"]
    assert fake.search_calls[0]["credential"] == "fake-value"
    assert fake.search_calls[0]["query"] == ""
    assert fake.search_calls[0]["cursor"] is None
    # A connection check must never spend money.
    assert fake.generate_calls == []

    # An empty catalog is still a working connection; samples are capped at 5.
    fake.enqueue_search(_search_result(0))
    empty = check_connection(_provider())
    assert empty.ok is True and "0 models" in empty.message
    assert empty.sample_model_ids == []
    fake.enqueue_search(_search_result(8))
    many = check_connection(_provider())
    assert "8 models" in many.message
    assert many.sample_model_ids == ["m0", "m1", "m2", "m3", "m4"]

    # Every failure mode is reported, not raised.
    disabled = check_connection(_provider(is_enabled=False))
    assert disabled.ok is False and "disabled" in disabled.message
    assert disabled.sample_model_ids == []

    unknown_type = check_connection(_provider(provider_type="unregistered-type"))
    assert unknown_type.ok is False and "unregistered-type" in unknown_type.message

    for error in (
        ImageAuthError("credential rejected"),
        MalformedImageResultError("bad payload"),
        ImageCredentialError("no credential"),
    ):
        fake.enqueue_error(error, method="search_models")
        translated = check_connection(_provider())
        assert translated.ok is False
        assert translated.message == str(error)

    monkeypatch.delenv(_FAKE_CREDENTIAL_ENV, raising=False)
    missing = check_connection(_provider())
    assert missing.ok is False and _FAKE_CREDENTIAL_ENV in missing.message

    # Registering the real adapters is pure bookkeeping - no call, no network.
    reset_adapters()
    register_default_adapters()
    assert isinstance(get_adapter("fal"), FalAdapter)
    assert isinstance(get_adapter("runware"), RunwareAdapter)


def test_images_cli_checks_connections_free_and_guards_the_paid_smoke_path(monkeypatch):
    monkeypatch.setattr(images_cli, "register_default_adapters", lambda: None)
    runner = CliRunner()

    def check(*args):
        return runner.invoke(
            cli_module.cli,
            ["images", "check-connection", "fal", "--credential-env", _CLI_CRED, *args],
        )

    monkeypatch.delenv(_CLI_CRED, raising=False)
    missing = check()
    assert missing.exit_code != 0
    assert _CLI_CRED in missing.output

    monkeypatch.setenv(_CLI_CRED, "fake-value-for-test")
    fake = FakeImageAdapter()
    fake.enqueue_search(
        ModelSearchResult(
            options=[ModelOption(model_id="fal-ai/flux/schnell", display_name="Flux")],
            next_cursor=None,
        )
    )
    register_adapter("fal", fake)
    ok = check()
    assert ok.exit_code == 0, ok.output
    assert "1 model" in ok.output and "fal-ai/flux/schnell" in ok.output

    fake.enqueue_error(ImageAuthError("bad key"), method="search_models")
    failed = check()
    assert failed.exit_code != 0 and "bad key" in failed.output

    # smoke-fal is the only path that could ever spend money. generate() is
    # stubbed throughout, and no real adapter is ever registered for it.
    calls: list[dict] = []

    def fake_generate(provider, model_id, prompt, deadline):
        calls.append(
            {
                "provider": provider,
                "model_id": model_id,
                "prompt": prompt,
                "deadline": deadline,
            }
        )
        return ImageGenerationResult(
            request_id="fake-req-1",
            image_url="https://example.invalid/fake.png",
            image_bytes=None,
            mime_type="image/png",
            width=512,
            height=512,
        )

    monkeypatch.setattr(images_cli, "generate", fake_generate)

    def smoke(*args):
        return runner.invoke(
            cli_module.cli,
            ["images", "smoke-fal", "--credential-env", _CLI_CRED, *args],
        )

    unconfirmed = smoke()
    assert unconfirmed.exit_code != 0
    assert "yes-i-know-this-costs-money" in unconfirmed.output
    assert calls == []

    monkeypatch.delenv(_CLI_CRED, raising=False)
    uncredentialed = smoke("--yes-i-know-this-costs-money")
    assert uncredentialed.exit_code != 0
    assert _CLI_CRED in uncredentialed.output
    assert calls == []

    monkeypatch.setenv(_CLI_CRED, "fake-value-for-test")
    confirmed = smoke("--yes-i-know-this-costs-money")
    assert confirmed.exit_code == 0, confirmed.output
    assert len(calls) == 1, "a confirmed smoke run generates exactly once"
    assert calls[0]["model_id"] == "fal-ai/flux-1/schnell"
    assert calls[0]["provider"].provider_type == "fal"
    assert calls[0]["provider"].credential_env == _CLI_CRED
    assert isinstance(calls[0]["deadline"], Deadline)
    assert "fake-req-1" in confirmed.output

    monkeypatch.setattr(
        images_cli,
        "generate",
        lambda *a, **k: (_ for _ in ()).throw(
            ImageProviderDisabledError("provider disabled")
        ),
    )
    errored = smoke("--yes-i-know-this-costs-money")
    assert errored.exit_code != 0 and "provider disabled" in errored.output
