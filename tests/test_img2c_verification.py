"""Deterministic tests for provider verification hooks (Phase 2C).

Every test here uses FakeImageAdapter registered through
deaddit.images.client.register_adapter()/reset_adapters() - no test
contacts fal.ai or Runware, and no test reads FALAI_API_KEY or
RUNWARE_API_KEY. The generate() smoke path (deaddit images smoke-fal) is
exercised with deaddit.images.cli.generate monkeypatched to a stub, so it
never reaches a real adapter, a real transport, or a real credential.
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
from deaddit.images.verification import ConnectionTestResult
from deaddit.images.verification import test_connection as check_connection
from deaddit.models import ImageProvider
from tests.fakes import FakeImageAdapter

_FAKE_CREDENTIAL_ENV = "TEST_IMAGE_PROVIDER_CREDENTIAL"


@pytest.fixture(autouse=True)
def _clean_adapter_registry():
    """Every test starts and ends with an empty adapter registry."""
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


# --- check_connection() -------------------------------------------------------


def test_connection_reports_success_with_sample_models(monkeypatch):
    monkeypatch.setenv(_FAKE_CREDENTIAL_ENV, "fake-value")
    fake = FakeImageAdapter()
    fake.enqueue_search(
        ModelSearchResult(
            options=[
                ModelOption(model_id="m1", display_name="Model One"),
                ModelOption(model_id="m2", display_name="Model Two"),
            ],
            next_cursor=None,
        )
    )
    register_adapter("fal", fake)

    result = check_connection(_provider())

    assert isinstance(result, ConnectionTestResult)
    assert result.ok is True
    assert "2 models" in result.message
    assert result.sample_model_ids == ["m1", "m2"]
    assert len(fake.search_calls) == 1
    assert fake.search_calls[0]["credential"] == "fake-value"
    assert fake.search_calls[0]["query"] == ""
    assert fake.search_calls[0]["cursor"] is None


def test_connection_reports_success_with_empty_catalog(monkeypatch):
    monkeypatch.setenv(_FAKE_CREDENTIAL_ENV, "fake-value")
    fake = FakeImageAdapter()
    fake.enqueue_search(ModelSearchResult(options=[], next_cursor=None))
    register_adapter("fal", fake)

    result = check_connection(_provider())

    assert result.ok is True
    assert "0 models" in result.message
    assert result.sample_model_ids == []


def test_connection_caps_sample_ids_at_five(monkeypatch):
    monkeypatch.setenv(_FAKE_CREDENTIAL_ENV, "fake-value")
    fake = FakeImageAdapter()
    options = [
        ModelOption(model_id=f"m{i}", display_name=f"Model {i}") for i in range(8)
    ]
    fake.enqueue_search(ModelSearchResult(options=options, next_cursor=None))
    register_adapter("fal", fake)

    result = check_connection(_provider())

    assert result.ok is True
    assert "8 models" in result.message
    assert result.sample_model_ids == ["m0", "m1", "m2", "m3", "m4"]


def test_connection_reports_disabled_provider_without_raising():
    result = check_connection(_provider(is_enabled=False))
    assert result.ok is False
    assert "disabled" in result.message
    assert result.sample_model_ids == []


def test_connection_reports_missing_credential_without_raising(monkeypatch):
    monkeypatch.delenv(_FAKE_CREDENTIAL_ENV, raising=False)
    register_adapter("fal", FakeImageAdapter())

    result = check_connection(_provider())

    assert result.ok is False
    assert _FAKE_CREDENTIAL_ENV in result.message


def test_connection_reports_unregistered_provider_type_without_raising(monkeypatch):
    monkeypatch.setenv(_FAKE_CREDENTIAL_ENV, "fake-value")
    result = check_connection(_provider(provider_type="unregistered-type"))
    assert result.ok is False
    assert "unregistered-type" in result.message


@pytest.mark.parametrize(
    "error",
    [
        ImageAuthError("credential rejected"),
        MalformedImageResultError("bad payload"),
        ImageCredentialError("no credential"),
    ],
)
def test_connection_translates_adapter_errors_without_raising(monkeypatch, error):
    monkeypatch.setenv(_FAKE_CREDENTIAL_ENV, "fake-value")
    fake = FakeImageAdapter()
    fake.enqueue_error(error, method="search_models")
    register_adapter("fal", fake)

    result = check_connection(_provider())

    assert result.ok is False
    assert result.message == str(error)


def test_connection_never_calls_generate(monkeypatch):
    """test_connection is a search-only hook; it must never touch generate()."""
    monkeypatch.setenv(_FAKE_CREDENTIAL_ENV, "fake-value")
    fake = FakeImageAdapter()
    fake.enqueue_search(ModelSearchResult(options=[], next_cursor=None))
    register_adapter("fal", fake)

    check_connection(_provider())

    assert fake.generate_calls == []


# --- register_default_adapters() ---------------------------------------------


def test_register_default_adapters_wires_fal_and_runware():
    register_default_adapters()
    assert isinstance(get_adapter("fal"), FalAdapter)
    assert isinstance(get_adapter("runware"), RunwareAdapter)


def test_register_default_adapters_does_not_touch_generate_or_network():
    """Registering must be pure bookkeeping: no adapter method is invoked."""
    register_default_adapters()
    fal_adapter = get_adapter("fal")
    runware_adapter = get_adapter("runware")
    # Constructing the adapters must not have made any request; there is no
    # transport call log to inspect because none was made - the assertion
    # here is simply that construction/registration completed without
    # requiring network access (the autouse network guard in conftest.py
    # would fail any test that actually reached a socket).
    assert isinstance(fal_adapter, FalAdapter)
    assert isinstance(runware_adapter, RunwareAdapter)


# --- CLI: check-connection (free) --------------------------------------------
#
# Every check-connection test below passes an explicit --credential-env
# naming a variable this test file invents (never FALAI_API_KEY or
# RUNWARE_API_KEY), so no test here ever reads either real credential name.

_CLI_CHECK_CRED = "TEST_CLI_CHECK_CONNECTION_CREDENTIAL"


def test_cli_check_connection_requires_credential_env(monkeypatch):
    monkeypatch.delenv(_CLI_CHECK_CRED, raising=False)
    result = CliRunner().invoke(
        cli_module.cli,
        ["images", "check-connection", "fal", "--credential-env", _CLI_CHECK_CRED],
    )
    assert result.exit_code != 0
    assert _CLI_CHECK_CRED in result.output


def test_cli_check_connection_reports_success(monkeypatch):
    monkeypatch.setenv(_CLI_CHECK_CRED, "fake-value-for-test")
    fake = FakeImageAdapter()
    fake.enqueue_search(
        ModelSearchResult(
            options=[ModelOption(model_id="fal-ai/flux/schnell", display_name="Flux")],
            next_cursor=None,
        )
    )
    monkeypatch.setattr(images_cli, "register_default_adapters", lambda: None)
    register_adapter("fal", fake)

    result = CliRunner().invoke(
        cli_module.cli,
        ["images", "check-connection", "fal", "--credential-env", _CLI_CHECK_CRED],
    )

    assert result.exit_code == 0, result.output
    assert "1 model" in result.output
    assert "fal-ai/flux/schnell" in result.output


def test_cli_check_connection_fails_on_adapter_error(monkeypatch):
    monkeypatch.setenv(_CLI_CHECK_CRED, "fake-value-for-test")
    fake = FakeImageAdapter()
    fake.enqueue_error(ImageAuthError("bad key"), method="search_models")
    monkeypatch.setattr(images_cli, "register_default_adapters", lambda: None)
    register_adapter("fal", fake)

    result = CliRunner().invoke(
        cli_module.cli,
        ["images", "check-connection", "fal", "--credential-env", _CLI_CHECK_CRED],
    )

    assert result.exit_code != 0
    assert "bad key" in result.output


# --- CLI: smoke-fal (paid; must never actually generate in tests) -----------
#
# Every smoke-fal test below passes an explicit --credential-env naming a
# variable this test file invents (never FALAI_API_KEY), monkeypatches
# images_cli.generate to a local stub, and never registers a real adapter -
# so no test here can reach fal.ai, Runware, or any real credential.

_CLI_SMOKE_CRED = "TEST_CLI_SMOKE_FAL_CREDENTIAL"


def test_cli_smoke_fal_refuses_without_confirmation(monkeypatch):
    monkeypatch.setenv(_CLI_SMOKE_CRED, "fake-value-for-test")
    called = []
    monkeypatch.setattr(images_cli, "generate", lambda *a, **k: called.append(1))

    result = CliRunner().invoke(
        cli_module.cli, ["images", "smoke-fal", "--credential-env", _CLI_SMOKE_CRED]
    )

    assert result.exit_code != 0
    assert "yes-i-know-this-costs-money" in result.output
    assert called == []


def test_cli_smoke_fal_refuses_without_credential(monkeypatch):
    monkeypatch.delenv(_CLI_SMOKE_CRED, raising=False)
    called = []
    monkeypatch.setattr(images_cli, "generate", lambda *a, **k: called.append(1))

    result = CliRunner().invoke(
        cli_module.cli,
        [
            "images",
            "smoke-fal",
            "--credential-env",
            _CLI_SMOKE_CRED,
            "--yes-i-know-this-costs-money",
        ],
    )

    assert result.exit_code != 0
    assert _CLI_SMOKE_CRED in result.output
    assert called == []


def test_cli_smoke_fal_confirmed_calls_generate_exactly_once(monkeypatch):
    """The only path that could ever spend money; generate() is stubbed.

    This proves the command wires model/prompt/deadline correctly without
    ever registering a real adapter or reaching a real transport.
    """
    monkeypatch.setenv(_CLI_SMOKE_CRED, "fake-value-for-test")
    monkeypatch.setattr(images_cli, "register_default_adapters", lambda: None)
    calls = []

    def _fake_generate(provider, model_id, prompt, deadline):
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

    monkeypatch.setattr(images_cli, "generate", _fake_generate)

    result = CliRunner().invoke(
        cli_module.cli,
        [
            "images",
            "smoke-fal",
            "--credential-env",
            _CLI_SMOKE_CRED,
            "--yes-i-know-this-costs-money",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["model_id"] == "fal-ai/flux-1/schnell"
    assert calls[0]["provider"].provider_type == "fal"
    assert calls[0]["provider"].credential_env == _CLI_SMOKE_CRED
    assert isinstance(calls[0]["deadline"], Deadline)
    assert "fake-req-1" in result.output


def test_cli_smoke_fal_reports_provider_error_without_raising_out(monkeypatch):
    monkeypatch.setenv(_CLI_SMOKE_CRED, "fake-value-for-test")
    monkeypatch.setattr(images_cli, "register_default_adapters", lambda: None)

    def _fake_generate(provider, model_id, prompt, deadline):
        raise ImageProviderDisabledError("provider disabled")

    monkeypatch.setattr(images_cli, "generate", _fake_generate)

    result = CliRunner().invoke(
        cli_module.cli,
        [
            "images",
            "smoke-fal",
            "--credential-env",
            _CLI_SMOKE_CRED,
            "--yes-i-know-this-costs-money",
        ],
    )

    assert result.exit_code != 0
    assert "provider disabled" in result.output
