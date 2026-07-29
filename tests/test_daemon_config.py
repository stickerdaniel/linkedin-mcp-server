"""Handing an owner the configuration it has to agree on.

The owner is a separate process that opens a browser against a logged-in
session, and it cannot work out its own settings: half of them arrived on the
command line of the *frontend*. Get this wrong in the quiet direction and the
result is not a visible error but a client that elects an owner and then refuses
to use it, because the fingerprint it compares covers exactly these fields.
"""

from __future__ import annotations

import json

import pytest

from linkedin_mcp_server import daemon_config
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_descriptor import config_fingerprint


def _config(**browser: object) -> AppConfig:
    config = AppConfig()
    config.browser.user_data_dir = "~/somewhere/profile"
    for name, value in browser.items():
        setattr(config.browser, name, value)
    return config


class TestRoundTrip:
    def test_the_owner_agrees_with_the_frontend_that_started_it(self):
        # The property that matters, stated the way the daemon uses it: the
        # fingerprint is what a client compares before attaching, so an owner
        # rebuilt from this must produce the same one. Anything dropped in
        # transit shows up here as a client that will not talk to the owner it
        # just started.
        original = _config(
            headless=False,
            user_agent="Mozilla/5.0 (test)",
            proxy_server="http://proxy.example:8080",
            proxy_username="user",
            proxy_password="secret",
            viewport_width=1920,
            login_timeout_seconds=900.0,
        )

        restored = daemon_config.decode(daemon_config.encode(original))

        key = "a-token"
        assert config_fingerprint(restored, key=key) == config_fingerprint(
            original, key=key
        )

    def test_settings_outside_the_fingerprint_survive_too(self):
        # The fingerprint says which differences stop two clients sharing an
        # owner. It does not say which settings the owner needs to do its job:
        # the handoff and wait timings are not in it, and an owner left on
        # defaults would hand the browser around on a schedule the user did not
        # choose.
        # Values that survive validate() untouched, so this pins the transport
        # rather than the clamping. The wait has a 45 second ceiling and the
        # minimum hold has to leave room inside it, and both clamps are correct:
        # picking values they act on would make this a test of the validator.
        original = _config(
            browser_wait_seconds=30.0,
            browser_min_hold_seconds=5.0,
            browser_idle_timeout_seconds=120.0,
        )

        restored = daemon_config.decode(daemon_config.encode(original))

        assert restored.browser.browser_wait_seconds == 30.0
        assert restored.browser.browser_min_hold_seconds == 5.0
        assert restored.browser.browser_idle_timeout_seconds == 120.0

    def test_the_tool_timeout_crosses_because_the_owner_enforces_it(self):
        original = AppConfig()
        original.browser.user_data_dir = "~/p/profile"
        original.server.tool_timeout_seconds = 42.5

        restored = daemon_config.decode(daemon_config.encode(original))

        assert restored.server.tool_timeout_seconds == 42.5


class TestRefusing:
    def test_the_frontends_own_invocation_does_not_cross(self):
        # An owner that adopted the frontend's transport would try to serve
        # stdio, and one that adopted its one-shot flags would re-run the
        # client's --login. Those describe how *that* process was started.
        original = AppConfig()
        original.browser.user_data_dir = "~/p/profile"
        original.server.login = True
        original.server.transport = "streamable-http"
        original.server.port = 9999

        carried = json.loads(daemon_config.encode(original))

        assert "login" not in carried["server"]
        assert "transport" not in carried["server"]
        assert "port" not in carried["server"]

    def test_an_unknown_setting_is_refused_rather_than_skipped(self):
        # Both ends are one installation, so a name this does not recognise did
        # not come from encode(). Skipping it quietly would be an owner running
        # on settings nobody chose.
        with pytest.raises(ValueError, match="unknown"):
            daemon_config.decode(json.dumps({"browser": {"nonsense": 1}, "server": {}}))

    def test_a_frontend_setting_smuggled_into_the_server_section_is_refused(self):
        # The allowed list is enforced on the way in, not only on the way out.
        with pytest.raises(ValueError, match="unknown"):
            daemon_config.decode(json.dumps({"browser": {}, "server": {"login": True}}))

    def test_a_value_of_the_wrong_type_is_refused_before_it_reaches_a_browser(self):
        # JSON cannot tell seconds from a count. Left unchecked this surfaces
        # deep inside a browser launch, in a detached process, as an error
        # nobody can trace back to here.
        with pytest.raises(ValueError, match="wrong type"):
            daemon_config.decode(
                json.dumps({"browser": {"slow_mo": "quickly"}, "server": {}})
            )

    def test_a_boolean_is_not_accepted_where_a_number_belongs(self):
        # bool is a subclass of int, so an isinstance check alone accepts True
        # as a timeout. That is exactly the silent nonsense this refuses.
        with pytest.raises(ValueError, match="wrong type"):
            daemon_config.decode(
                json.dumps({"browser": {"slow_mo": True}, "server": {}})
            )

    def test_an_integer_is_accepted_where_a_float_belongs(self):
        # The other direction, and it has to work: JSON writes 0 for 0.0, so
        # refusing would make an ordinary configuration untransportable.
        restored = daemon_config.decode(
            json.dumps({"browser": {"login_timeout_seconds": 0}, "server": {}})
        )

        assert restored.browser.login_timeout_seconds == 0

    def test_an_optional_setting_may_be_unset(self):
        restored = daemon_config.decode(
            json.dumps({"browser": {"user_agent": None}, "server": {}})
        )

        assert restored.browser.user_agent is None

    def test_a_log_level_outside_the_supported_set_is_refused(self):
        with pytest.raises(ValueError, match="wrong type"):
            daemon_config.decode(
                json.dumps({"browser": {}, "server": {"log_level": "CHATTY"}})
            )

    def test_a_configuration_the_frontend_would_reject_is_rejected_here_too(self):
        # The owner is the process that opens the browser, so a value refused at
        # the frontend must not reach Chromium through this back door.
        with pytest.raises(Exception):
            daemon_config.decode(
                json.dumps({"browser": {}, "server": {"tool_timeout_seconds": -1.0}})
            )

    def test_something_that_is_not_a_configuration_is_refused(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            daemon_config.decode("not json at all")

        with pytest.raises(ValueError, match="not an object"):
            daemon_config.decode("[1, 2, 3]")

        with pytest.raises(ValueError, match="no browser section"):
            daemon_config.decode(json.dumps({"server": {}}))
