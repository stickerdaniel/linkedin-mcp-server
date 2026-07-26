"""Tests for recognizing and safely reporting proxy failures."""

import pytest

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.core.exceptions import NetworkError, ProxyConnectionError
from linkedin_mcp_server.core.proxy_errors import (
    as_proxy_error,
    goto_reporting_proxy_errors,
    is_proxy_error,
    proxy_hint,
    raise_if_proxy_error,
    redact_proxy_credentials,
)


@pytest.fixture
def proxy_config(monkeypatch):
    config = AppConfig()
    config.browser.proxy_server = "http://gate.example:7000"
    config.browser.proxy_username = "user"
    config.browser.proxy_password = "s3cr3t p@ss"
    monkeypatch.setattr("linkedin_mcp_server.config.get_config", lambda: config)
    return config


class TestClassification:
    @pytest.mark.parametrize(
        "message",
        [
            "net::ERR_PROXY_CONNECTION_FAILED at https://www.linkedin.com/feed/",
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            "Page.goto: net::ERR_PROXY_AUTH_REQUESTED",
            "net::ERR_SOCKS_CONNECTION_FAILED",
            "net::ERR_NO_SUPPORTED_PROXIES",
        ],
    )
    def test_proxy_failures_are_recognized(self, message):
        assert is_proxy_error(Exception(message)) is True

    @pytest.mark.parametrize(
        "message",
        [
            "net::ERR_TOO_MANY_REDIRECTS",
            "net::ERR_ABORTED",
            "Timeout 30000ms exceeded",
            "Executable doesn't exist at /path/to/chrome",
        ],
    )
    def test_unrelated_failures_are_not_claimed(self, message):
        assert is_proxy_error(Exception(message)) is False

    def test_an_existing_proxy_error_passes_through(self):
        original = ProxyConnectionError("already converted")
        assert is_proxy_error(original) is True
        assert as_proxy_error(original) is original


class TestConversion:
    def test_message_names_the_proxy(self, proxy_config):
        converted = as_proxy_error(Exception("net::ERR_PROXY_CONNECTION_FAILED"))
        assert "http://gate.example:7000" in str(converted)

    def test_it_stays_a_network_error(self):
        # Subclassing NetworkError keeps a proxy outage out of the auth paths
        # that would retire the stored profile.
        assert issubclass(ProxyConnectionError, NetworkError)

    def test_the_raw_cause_is_dropped(self, proxy_config):
        # The top-level handlers log the whole cause chain, which would put the
        # unredacted driver message back into the log.
        original = Exception("net::ERR_PROXY_CONNECTION_FAILED user:s3cr3t p@ss@gate")
        try:
            raise_if_proxy_error(original)
        except ProxyConnectionError as converted:
            assert converted.__cause__ is None
            assert "s3cr3t" not in str(converted)
        else:  # pragma: no cover - the call above must raise
            pytest.fail("expected a ProxyConnectionError")

    def test_non_proxy_errors_are_left_alone(self):
        raise_if_proxy_error(Exception("net::ERR_TOO_MANY_REDIRECTS"))  # no raise


class TestRedaction:
    def test_plain_password_is_masked(self, proxy_config):
        assert "s3cr3t" not in redact_proxy_credentials("saw s3cr3t p@ss in the URL")

    def test_percent_encoded_password_is_masked(self, proxy_config):
        # A password inside a URL appears percent-encoded.
        redacted = redact_proxy_credentials("http://user:s3cr3t%20p%40ss@gate:7000")
        assert "s3cr3t" not in redacted and "%40ss" not in redacted

    def test_without_a_configured_password_nothing_changes(self, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.config.get_config", lambda: AppConfig()
        )
        assert redact_proxy_credentials("untouched") == "untouched"

    def test_reporting_survives_an_unreadable_config(self, monkeypatch):
        # Loading the config parses argv, and argparse exits the process on bad
        # arguments. Reporting a proxy failure must not be able to kill the run.
        def explode():
            raise SystemExit(2)

        monkeypatch.setattr("linkedin_mcp_server.config.get_config", explode)
        converted = as_proxy_error(Exception("net::ERR_PROXY_CONNECTION_FAILED"))
        assert "the configured proxy" in str(converted)


class TestProxyHint:
    """Auth failures name the proxy, because a wrong password looks like one.

    Verified against a local authenticating relay: bad credentials produce no
    proxy error code at all. Chromium keeps retrying the 407 challenge until
    the navigation times out, which is indistinguishable from a slow page, so
    the failure lands in the auth path however well the classifier works.
    """

    def test_hint_names_the_proxy_when_configured(self, proxy_config):
        assert "http://gate.example:7000" in proxy_hint()

    def test_hint_never_carries_the_password(self, proxy_config):
        assert "s3cr3t" not in proxy_hint()

    def test_no_hint_without_a_proxy(self, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.config.get_config", lambda: AppConfig()
        )
        assert proxy_hint() == ""


class TestAmbiguousAuthMarker:
    """Rejected proxy credentials, which Chromium reports without naming a proxy."""

    def test_counted_when_a_proxy_is_configured(self, proxy_config):
        assert is_proxy_error(Exception("net::ERR_INVALID_AUTH_CREDENTIALS")) is True

    def test_ignored_without_a_proxy(self, monkeypatch):
        # The same code covers a site's own HTTP auth, so claiming it with no
        # proxy configured would misreport an ordinary failure.
        monkeypatch.setattr(
            "linkedin_mcp_server.config.get_config", lambda: AppConfig()
        )
        assert is_proxy_error(Exception("net::ERR_INVALID_AUTH_CREDENTIALS")) is False


class TestGotoWrapper:
    """The navigations that run before any auth check are covered too."""

    async def test_proxy_failure_is_converted(self, proxy_config):
        page = _FakePage(Exception("net::ERR_PROXY_CONNECTION_FAILED"))
        with pytest.raises(ProxyConnectionError):
            await goto_reporting_proxy_errors(page, "https://www.linkedin.com/login")

    async def test_other_failures_propagate_unchanged(self, proxy_config):
        page = _FakePage(RuntimeError("net::ERR_ABORTED"))
        with pytest.raises(RuntimeError):
            await goto_reporting_proxy_errors(page, "https://www.linkedin.com/login")

    async def test_success_returns_the_response(self, proxy_config):
        page = _FakePage(None)
        assert await goto_reporting_proxy_errors(page, "https://example.com") == "ok"


class _FakePage:
    def __init__(self, error):
        self._error = error

    async def goto(self, url, **kwargs):
        if self._error:
            raise self._error
        return "ok"


class TestUsernameIsTreatedAsSecret:
    """Provider usernames carry account, zone and session identity."""

    USERNAME = "brd-customer-acct1-zone-resi"

    def _config(self, monkeypatch):
        config = AppConfig()
        config.browser.proxy_server = "http://gate.example:7000"
        config.browser.proxy_username = self.USERNAME
        monkeypatch.setattr("linkedin_mcp_server.config.get_config", lambda: config)
        return config

    def test_username_is_masked(self, monkeypatch):
        self._config(monkeypatch)
        redacted = redact_proxy_credentials(f"http://{self.USERNAME}:pw@gate:7000")
        assert self.USERNAME not in redacted

    def test_username_absent_from_the_config_repr(self, monkeypatch):
        # cli_main logs the whole config at DEBUG level.
        config = self._config(monkeypatch)
        assert self.USERNAME not in repr(config)

    def test_hint_still_names_the_server(self, proxy_config):
        # The server holds no secret and is what you need to diagnose.
        assert "gate.example" in proxy_hint()
