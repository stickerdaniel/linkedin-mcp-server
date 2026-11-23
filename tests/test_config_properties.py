"""Properties of the whole configuration load, from environment and command line."""

from __future__ import annotations

import contextlib
import io
import os
from collections.abc import Callable, Sequence
from unittest.mock import patch

import pytest
from hypothesis import example, given, strategies as st

from linkedin_mcp_server import debug_trace, update_check
from linkedin_mcp_server.config import loaders
from linkedin_mcp_server.config.schema import AppConfig, ConfigurationError

Refused = None


def _spellings(words: Sequence[str]) -> st.SearchStrategy[str]:
    return st.builds(
        "{}{}{}".format,
        st.sampled_from(("", " ")),
        st.sampled_from(words).flatmap(
            lambda word: st.sampled_from((word, word.upper(), word.capitalize()))
        ),
        st.sampled_from(("", " ")),
    )


_NUMBERS = st.one_of(
    st.integers(-3, 10**6).map(str),
    st.floats(-3, 10**4, allow_nan=False).map(str),
    st.sampled_from(("0", "1", "05", " 7 ", "1e3", "nan", "inf", "-0")),
)
_TEXT = st.text(
    st.characters(codec="utf-8", exclude_categories=("Cs",), exclude_characters="\x00"),
    max_size=12,
)


def _well_formed(parse: Callable[[str], object]) -> st.SearchStrategy[str]:
    if parse is loaders._flag:
        return _spellings(loaders.TRUTHY_VALUES + loaders.FALSY_VALUES)
    if parse is loaders._log_level:
        return _spellings(loaders.LOG_LEVELS)
    if parse is loaders._transport:
        return _spellings(loaders.TRANSPORTS)
    if parse is loaders._trace_mode:
        return _spellings(tuple(loaders.TRACE_MODES))
    if parse is loaders._viewport:
        return st.builds("{}x{}".format, st.integers(-1, 4000), st.integers(-1, 4000))
    if parse is loaders._integer or getattr(parse, "__qualname__", "").startswith(
        "_finite"
    ):
        return _NUMBERS
    return st.sampled_from(("chrome", "localhost", "/mcp", "http://proxy:8080"))


def _values(parse: Callable[[str], object]) -> st.SearchStrategy[str]:
    return st.one_of(_well_formed(parse), _TEXT).filter(str.strip)


WITH_ENV: dict[str, loaders.Env] = {
    setting.env.key: setting.env for setting in loaders.SETTINGS if setting.env
}
# The variable holds the token itself and the option names a file holding it.
BOTH: dict[str, tuple[loaders.Env, loaders.Option]] = {
    setting.env.key: (setting.env, setting.cli)
    for setting in loaders.SETTINGS
    if setting.env
    and isinstance(setting.cli, loaders.Option)
    and setting.env.key != loaders.EnvironmentKeys.LINKEDIN_MCP_HTTP_TOKEN
}
SWITCHED: dict[str, tuple[str, ...]] = {
    setting.env.key: (setting.cli.on, setting.cli.off)
    if isinstance(setting.cli, loaders.SwitchPair)
    else (setting.cli.flag,)
    for setting in loaders.SETTINGS
    if setting.env and isinstance(setting.cli, loaders.Switch | loaders.SwitchPair)
}


def _load(env: dict[str, str], argv: Sequence[str] = ()) -> AppConfig | None:
    with (
        patch.dict(os.environ, env),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        try:
            return loaders.load_config(list(argv))
        except (ConfigurationError, SystemExit):
            return Refused


def _env_layer(base: AppConfig, env: dict[str, str]) -> AppConfig | None:
    with patch.dict(os.environ, env):
        try:
            return loaders.load_from_env(base)
        except ConfigurationError:
            return Refused


@pytest.mark.parametrize("key", WITH_ENV)
@given(data=st.data())
@example(data=None)
def test_an_accepted_environment_value_decides_its_setting_whatever_came_before(
    key: str, data: st.DataObject | None
) -> None:
    env = WITH_ENV[key]
    if data is None:
        if env.parse is not loaders._flag:
            return
        value, earlier = "flase", "off"
    else:
        value = data.draw(_values(env.parse), label="value")
        earlier = data.draw(_values(env.parse), label="earlier")
    if env.blank == "unset" and not value.strip():
        return

    from_defaults = _env_layer(AppConfig(), {key: value})
    before = _env_layer(AppConfig(), {key: earlier})
    if from_defaults is Refused or before is Refused:
        return

    assert _env_layer(before, {key: value}) == from_defaults


@pytest.mark.parametrize("key", BOTH)
@given(data=st.data())
@example(data=None)
def test_the_option_accepts_no_value_its_variable_refuses_and_applies_it_alike(
    key: str, data: st.DataObject | None
) -> None:
    env, option = BOTH[key]
    value = "0" if data is None else data.draw(_values(env.parse))

    through_env = _load({key: value})
    through_option = _load({}, [f"{option.flag}={value}"])

    if through_env is Refused:
        assert through_option is Refused
    elif through_option is not Refused:
        assert through_option == through_env


@pytest.mark.parametrize("key", BOTH)
@given(data=st.data())
@example(data=None)
def test_a_given_option_beats_its_variable_even_as_zero(
    key: str, data: st.DataObject | None
) -> None:
    env, option = BOTH[key]
    if data is None:
        from_env, given = "500", "0"
    else:
        from_env = data.draw(_values(env.parse), label="environment")
        given = data.draw(_values(env.parse), label="option")
    argv = [f"{option.flag}={given}"]

    alone = _load({}, argv)
    if alone is Refused or _load({key: from_env}) is Refused:
        return

    assert _load({key: from_env}, argv) == alone


@pytest.mark.parametrize("key", SWITCHED)
@given(environment=_spellings(loaders.TRUTHY_VALUES + loaders.FALSY_VALUES))
def test_a_given_switch_beats_its_variable(key: str, environment: str) -> None:
    for flag in SWITCHED[key]:
        assert _load({key: environment}, [flag]) == _load({}, [flag])


@given(value=st.one_of(_spellings(tuple(loaders.TRACE_MODES)), _TEXT))
def test_the_trace_mode_in_use_is_the_one_startup_accepted(value: str) -> None:
    key = loaders.EnvironmentKeys.LINKEDIN_TRACE_MODE
    if _load({key: value}) is Refused:
        return

    with patch.dict(os.environ, {key: value}):
        in_use = debug_trace._trace_mode()

    assert in_use == (loaders._trace_mode(value) if value.strip() else "on_error")


@given(value=st.one_of(_spellings(loaders.TRUTHY_VALUES + loaders.FALSY_VALUES), _TEXT))
def test_the_update_check_is_off_exactly_when_startup_read_it_as_off(
    value: str,
) -> None:
    key = loaders.EnvironmentKeys.LINKEDIN_MCP_CHECK_FOR_UPDATES
    if _load({key: value}) is Refused:
        return
    on = loaders._flag(value) if value.strip() else True

    with (
        patch.dict(os.environ, {key: value}),
        patch.object(update_check, "_is_source_install", return_value=False),
    ):
        os.environ.pop("CI", None)
        skipped = update_check._check_disabled()

    assert skipped is not on
