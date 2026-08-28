"""Tests for deaddit.websites.generator: the dedicated create_website
HTML-generation request, and validation of its response against the spec's
Generated HTML contract.

No real network: fake_llm (tests/fakes.py FakeProvider) stands in for the
LLM transport per tests/conftest.py's autouse _network_guard.
"""

from __future__ import annotations

import logging

import pytest

from deaddit.llm.errors import PermanentLLMError, TransientLLMError
from deaddit.models import LLMUsage
from deaddit.websites.generator import (
    WebsiteGenerationError,
    WebsiteGenerationInvalidHTMLError,
    WebsiteGenerationResult,
    WebsiteGenerationTruncatedError,
    generate_website_html,
)
from deaddit.websites.storage import WebsiteGenerationSettings

VALID_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aurora Map</title>
<style>body { font-family: sans-serif; }</style>
</head>
<body>
<h1>Aurora Map</h1>
<svg width="10" height="10"><circle cx="5" cy="5" r="4"></circle></svg>
<img src="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=" alt="a small icon">
<a href="#details">Jump to details</a>
<script>console.log("hi");</script>
</body>
</html>"""

API_URL = "http://caller-endpoint.test/v1"
API_KEY = "sk-super-secret-caller-key"
MODEL = "caller-model"
AGENT = "persona-42"


def _settings(**overrides) -> WebsiteGenerationSettings:
    base = {
        "max_output_tokens": 32768,
        "generation_timeout_seconds": 300.0,
        "max_html_bytes": 1_048_576,
    }
    base.update(overrides)
    return WebsiteGenerationSettings(**base)


def _generate(fake_llm, **overrides):
    kwargs = {
        "website_description": ("A cozy fictional aurora-watching community site."),
        "hostname_hint": "www.fake-observatory.com",
        "page_name_hint": "aurora-map",
        "api_url": API_URL,
        "api_key": API_KEY,
        "model": MODEL,
        "agent": AGENT,
        "settings": _settings(),
    }
    kwargs.update(overrides)
    return generate_website_html(**kwargs)


class TestRequestShape:
    def test_uses_exact_caller_endpoint_model_key(self, app, fake_llm):
        with app.app_context():
            fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
            _generate(fake_llm)

        recorded = fake_llm.requests[0]
        assert recorded["api_url"] == API_URL
        assert recorded["api_key"] == API_KEY
        assert recorded["payload"]["model"] == MODEL

    def test_carries_no_tools(self, app, fake_llm):
        with app.app_context():
            fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
            _generate(fake_llm)

        assert "tools" not in fake_llm.requests[0]["payload"]

    def test_requests_at_least_the_configured_floor_tokens(self, app, fake_llm):
        with app.app_context():
            fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
            _generate(fake_llm)

        assert fake_llm.requests[0]["payload"]["max_tokens"] >= 32768

    def test_action_and_agent_attribution_recorded_in_ledger(self, app, fake_llm):
        with app.app_context():
            fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
            _generate(fake_llm)

            row = LLMUsage.query.one()
            assert row.action == "create_website"
            assert row.agent == AGENT
            assert row.api_url == API_URL
            assert row.model == MODEL

    def test_read_timeout_bounded_by_smaller_of_website_timeout_and_deadline(
        self, app, fake_llm
    ):
        with app.app_context():
            fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
            _generate(
                fake_llm,
                settings=_settings(generation_timeout_seconds=300.0),
                run_deadline_remaining=45.0,
            )

        assert fake_llm.requests[0]["read_timeout"] == 45.0

    def test_read_timeout_falls_back_to_website_timeout_when_deadline_larger(
        self, app, fake_llm
    ):
        with app.app_context():
            fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
            _generate(
                fake_llm,
                settings=_settings(generation_timeout_seconds=120.0),
                run_deadline_remaining=9000.0,
            )

        assert fake_llm.requests[0]["read_timeout"] == 120.0

    def test_read_timeout_falls_back_to_website_timeout_when_no_deadline(
        self, app, fake_llm
    ):
        with app.app_context():
            fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
            _generate(
                fake_llm,
                settings=_settings(generation_timeout_seconds=77.0),
                run_deadline_remaining=None,
            )

        assert fake_llm.requests[0]["read_timeout"] == 77.0

    def test_no_run_time_remaining_fails_before_any_request(self, app, fake_llm):
        with app.app_context(), pytest.raises(WebsiteGenerationError):
            _generate(fake_llm, run_deadline_remaining=0.0)

        assert fake_llm.requests == []


class TestSuccessPath:
    def test_valid_response_returns_result_with_expected_fields(self, app, fake_llm):
        with app.app_context():
            fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
            result = _generate(fake_llm)

        assert isinstance(result, WebsiteGenerationResult)
        assert result.html == VALID_HTML
        assert result.finish_reason == "stop"
        assert result.api_url == API_URL
        assert result.model == MODEL
        assert result.request_id


class TestFailureLeavesNoPublishableResult:
    """Each of these must raise before a WebsiteGenerationResult exists -
    that is the structural guarantee that no file can be written on
    failure, since only a WebsiteGenerationResult can reach store_website().
    """

    def test_length_finish_reason_is_rejected(self, app, fake_llm):
        with app.app_context():
            fake_llm.enqueue_content("<!doctype html><html>", finish_reason="length")
            with pytest.raises(WebsiteGenerationTruncatedError):
                _generate(fake_llm)

    def test_length_finish_reason_rejected_even_with_well_formed_looking_html(
        self, app, fake_llm
    ):
        # A length stop must never be published even if the truncated text
        # happens to look complete - no partial publication, ever.
        with app.app_context():
            fake_llm.enqueue_content(VALID_HTML, finish_reason="length")
            with pytest.raises(WebsiteGenerationTruncatedError):
                _generate(fake_llm)

    def test_timeout_fails_cleanly(self, app, fake_llm):
        with app.app_context():
            fake_llm.enqueue_error(TransientLLMError("timed out"))
            with pytest.raises(WebsiteGenerationError):
                _generate(fake_llm)

    def test_provider_error_fails_cleanly(self, app, fake_llm):
        with app.app_context():
            fake_llm.enqueue_error(PermanentLLMError("HTTP 500"))
            with pytest.raises(WebsiteGenerationError):
                _generate(fake_llm)

    def test_empty_output_fails_cleanly(self, app, fake_llm):
        with app.app_context():
            fake_llm.enqueue_content("   ", finish_reason="stop")
            with pytest.raises(WebsiteGenerationInvalidHTMLError):
                _generate(fake_llm)

    def test_markdown_fenced_output_fails_cleanly(self, app, fake_llm):
        with app.app_context():
            fenced = f"```html\n{VALID_HTML}\n```"
            fake_llm.enqueue_content(fenced, finish_reason="stop")
            with pytest.raises(WebsiteGenerationInvalidHTMLError):
                _generate(fake_llm)

    def test_oversized_output_fails_cleanly(self, app, fake_llm):
        with app.app_context():
            padding = "<!-- " + ("x" * 200) + " -->\n"
            bloated = VALID_HTML.replace("<body>", "<body>\n" + padding * 20)
            fake_llm.enqueue_content(bloated, finish_reason="stop")
            with pytest.raises(WebsiteGenerationInvalidHTMLError):
                _generate(fake_llm, settings=_settings(max_html_bytes=200))

    def test_missing_doctype_fails_cleanly(self, app, fake_llm):
        with app.app_context():
            fake_llm.enqueue_content(
                "<html><body>no doctype</body></html>", finish_reason="stop"
            )
            with pytest.raises(WebsiteGenerationInvalidHTMLError):
                _generate(fake_llm)

    def test_missing_closing_html_tag_fails_cleanly(self, app, fake_llm):
        with app.app_context():
            fake_llm.enqueue_content(
                "<!doctype html><html><body>cut off here",
                finish_reason="stop",
            )
            with pytest.raises(WebsiteGenerationInvalidHTMLError):
                _generate(fake_llm)

    def test_control_bytes_fail_cleanly(self, app, fake_llm):
        with app.app_context():
            poisoned = VALID_HTML.replace("Aurora Map</h1>", "Aurora\x00Map</h1>")
            fake_llm.enqueue_content(poisoned, finish_reason="stop")
            with pytest.raises(WebsiteGenerationInvalidHTMLError):
                _generate(fake_llm)

    @pytest.mark.parametrize(
        "snippet",
        [
            '<script src="https://cdn.example.com/lib.js"></script>',
            '<link rel="stylesheet" href="https://cdn.example.com/site.css">',
            '<iframe src="https://example.com"></iframe>',
            "<form><input></form>",
            '<img src="https://example.com/pic.png" alt="">',
            '<video src="https://example.com/movie.mp4"></video>',
            '<a href="https://example.com">offsite</a>',
        ],
    )
    def test_active_or_external_elements_are_rejected(self, app, fake_llm, snippet):
        with app.app_context():
            html = VALID_HTML.replace(
                "<h1>Aurora Map</h1>", f"<h1>Aurora Map</h1>{snippet}"
            )
            fake_llm.enqueue_content(html, finish_reason="stop")
            with pytest.raises(WebsiteGenerationInvalidHTMLError):
                _generate(fake_llm)


class TestSecretHandling:
    def test_api_key_never_appears_in_result(self, app, fake_llm):
        with app.app_context():
            fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
            result = _generate(fake_llm)

        for value in vars(result).values():
            assert API_KEY not in str(value)

    def test_api_key_never_appears_in_failure_messages(self, app, fake_llm):
        with app.app_context():
            fake_llm.enqueue_error(PermanentLLMError(f"HTTP 401 key={API_KEY}"))
            with pytest.raises(WebsiteGenerationError) as excinfo:
                _generate(fake_llm)

        assert API_KEY not in str(excinfo.value)

    def test_api_key_never_appears_in_logging_output(self, app, fake_llm, caplog):
        with app.app_context(), caplog.at_level(logging.DEBUG):
            fake_llm.enqueue_content(VALID_HTML, finish_reason="stop")
            _generate(fake_llm)

        for record in caplog.records:
            assert API_KEY not in record.getMessage()
