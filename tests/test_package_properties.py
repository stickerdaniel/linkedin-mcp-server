"""Properties of setting parsers, redaction and wire codecs outside ``linkedin/``."""

from __future__ import annotations

import json
import os
import stat
import string
import tempfile
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path, PurePosixPath
from typing import Literal
from types import ModuleType
from unittest.mock import patch
from urllib.parse import quote

import pytest
from hypothesis import example, given, strategies as st
from pydantic import ValidationError

from linkedin_mcp_server import bootstrap, common_utils, login_app, login_relay
from linkedin_mcp_server.browser_downgrade import parse_build
from linkedin_mcp_server.config import loaders
from linkedin_mcp_server.config.schema import (
    AppConfig,
    BrowserConfig,
    ConfigurationError,
)
from linkedin_mcp_server.core import proxy_errors
from linkedin_mcp_server.core.destination import (
    describe_landing,
    is_linkedin_landing,
)
from linkedin_mcp_server.daemon import config as daemon_config
from linkedin_mcp_server.daemon import startup
from linkedin_mcp_server.daemon.descriptor import DaemonDescriptor
from linkedin_mcp_server.error_handler import value_free_summary
from linkedin_mcp_server.login_viewer import mount_records
from linkedin_mcp_server.storage_class import parse_mountinfo

CANARY = "s3cr3tZQX"
secrets = st.text(
    st.characters(categories=("Lu", "Ll", "Nd"), include_characters="@:/%#?!$ "),
    max_size=8,
).map(lambda tail: CANARY + tail)

SETTING_PARSERS: dict[str, Callable[[str], object]] = {
    "flag": loaders._flag,
    "log_level": loaders._log_level,
    "transport": loaders._transport,
    "trace_mode": loaders._trace_mode,
    "integer": loaders._integer,
    "positive": loaders._finite(positive=True),
    "non_negative": loaders._finite(positive=False),
    "viewport": loaders._viewport,
}
setting_text = st.text(max_size=20) | st.from_regex(
    r"[ -]?[0-9xX.eE+_infINFnaNA -]{0,12}", fullmatch=True
)


@pytest.mark.parametrize("name", SETTING_PARSERS)
@given(raw=setting_text)
@example(raw="")
@example(raw=" \n")
def test_a_setting_value_is_parsed_or_refused_as_a_setting_error(
    name: str, raw: str
) -> None:
    if not raw.strip():
        with pytest.raises(loaders.SettingError):
            SETTING_PARSERS[name](raw)
        return
    try:
        result = SETTING_PARSERS[name](raw)
    except loaders.SettingError:
        return

    assert result is not None


WELL_FORMED: dict[str, st.SearchStrategy[tuple[str, object]]] = {
    "flag": st.sampled_from(
        (("1", True), (" TRUE\n", True), ("on", True), ("0", False), ("No ", False))
    ),
    "log_level": st.sampled_from(((" debug", "DEBUG"), ("Warning", "WARNING"))),
    "transport": st.sampled_from(
        (("stdio", "stdio"), (" Streamable-HTTP ", "streamable-http"))
    ),
    "trace_mode": st.sampled_from(
        (("always", "always"), ("KEEP", "always"), ("off", "off"), ("1", "on_error"))
    ),
    "integer": st.integers(-(10**9), 10**9).map(lambda n: (f" {n}\n", n)),
    "positive": st.integers(1, 10**6).map(lambda n: (f"{n}.5", n + 0.5)),
    "non_negative": st.integers(0, 10**6).map(lambda n: (f"{n}", float(n))),
    "viewport": st.tuples(st.integers(1, 9999), st.integers(1, 9999)).map(
        lambda size: (f"{size[0]}X{size[1]}", size)
    ),
}


@pytest.mark.parametrize("name", SETTING_PARSERS)
@given(data=st.data())
def test_a_well_formed_setting_value_parses_to_what_it_says(
    name: str, data: st.DataObject
) -> None:
    raw, meaning = data.draw(WELL_FORMED[name])

    assert SETTING_PARSERS[name](raw) == meaning


BUNDLE_SETTINGS = (
    "CHROME_PATH",
    "PROXY_SERVER",
    "PROXY_USERNAME",
    "PROXY_PASSWORD",
    "PROXY_BYPASS",
)


def _placeholder(key: str) -> str:
    return "${user_config." + key.lower() + "}"


@given(
    key=st.sampled_from(BUNDLE_SETTINGS),
    other=st.sampled_from(BUNDLE_SETTINGS),
    value=st.text(
        st.characters(exclude_categories=("Cs",), exclude_characters="\0"),
        max_size=12,
    ),
    shape=st.sampled_from(("own", "other", "padded", "embedded", "plain")),
)
def test_only_its_own_unfilled_placeholder_reads_as_an_unset_bundle_setting(
    key: str, other: str, value: str, shape: str
) -> None:
    raw = {
        "own": _placeholder(key),
        "other": _placeholder(other),
        "padded": f" {_placeholder(key)}",
        "embedded": f"{value}{_placeholder(key)}x",
        "plain": value,
    }[shape]
    with patch.dict(os.environ, {key: raw}):
        read = loaders._env(key)

    assert read == (None if raw in ("", _placeholder(key)) else raw)


@st.composite
def proxy_servers(draw: st.DrawFn) -> str:
    scheme = draw(
        st.sampled_from(("", "http://", "https://", "socks5://", "ftp://", "HTTP://"))
    )
    user = draw(
        st.sampled_from(("", "user")) | secrets.map(lambda s: quote(s, safe=""))
    )
    password = draw(st.just("") | secrets | secrets.map(lambda s: quote(s, safe="")))
    userinfo = draw(
        st.sampled_from(("", f"{user}:{password}@", f"{user}%3A{password}%40"))
    )
    host = draw(
        st.sampled_from(
            ("proxy.example", "PROXY.Example", "127.0.0.1", "[::1]", "[", "")
        )
    )
    well_formed = draw(st.booleans())
    port = ":8080" if well_formed else draw(st.sampled_from((":0", ":99999", "", ":x")))
    tail = draw(st.sampled_from(("", "/") if well_formed else ("/p", "?q=1", "#f")))
    return f"{scheme}{userinfo}{host}{port}{tail}"


@given(
    server=proxy_servers(),
    username=st.sampled_from((None, "", "user")),
    password=st.sampled_from((None, "")) | secrets,
)
def test_proxy_validation_keeps_credentials_out_of_everything_it_shows(
    server: str, username: str | None, password: str | None
) -> None:
    config = BrowserConfig(
        proxy_server=server, proxy_username=username, proxy_password=password
    )
    try:
        config.validate()
    except ConfigurationError as refusal:
        assert CANARY not in str(refusal)
        return
    settled = (config.proxy_server, config.proxy_username, config.proxy_password)

    assert CANARY.lower() not in (config.proxy_server or "").lower()
    config.validate()
    assert (
        config.proxy_server,
        config.proxy_username,
        config.proxy_password,
    ) == settled


@given(
    scheme=st.sampled_from(("", "http://", "https://", "socks5://")),
    user=st.sampled_from(("user", "")),
    secret=secrets,
    host=st.sampled_from(("proxy.example", "127.0.0.1", "PROXY.Example")),
    port=st.integers(1, 65535),
)
def test_a_proxy_address_with_an_encoded_userinfo_separator_is_refused(
    scheme: str, user: str, secret: str, host: str, port: int
) -> None:
    userinfo = f"{user}%3A{quote(secret, safe='')}%40"
    config = BrowserConfig(proxy_server=f"{scheme}{userinfo}{host}:{port}")

    with pytest.raises(ConfigurationError) as refusal:
        config.validate()

    assert CANARY.lower() not in str(refusal.value).lower()


plain_secret = st.text(
    st.characters(exclude_categories=("Cs",), exclude_characters="*"),
    min_size=1,
    max_size=8,
)
plain_text = st.text(st.characters(exclude_characters="*"), max_size=12)


@given(
    password=plain_secret,
    username=plain_secret,
    before=plain_text,
    after=plain_text,
    encoded=st.booleans(),
    quoted=st.sampled_from(("password", "username")),
)
def test_redaction_removes_the_proxy_credentials_plain_and_escaped(
    password: str,
    username: str,
    before: str,
    after: str,
    encoded: bool,
    quoted: str,
) -> None:
    config = BrowserConfig(
        proxy_server="http://proxy.example:8080",
        proxy_username=username,
        proxy_password=password,
    )
    secret = password if quoted == "password" else username
    shown = quote(secret, safe="") if encoded else secret
    with patch.object(proxy_errors, "_browser_config", lambda: config):
        redacted = proxy_errors.redact_proxy_credentials(f"{before}{shown}{after}")

    assert secret not in redacted and shown not in redacted


@given(
    scheme=st.sampled_from(("http", "https")),
    host=st.sampled_from(
        (
            ("portal.example", "portal.example"),
            ("PORTAL.Example", "portal.example"),
            ("www.linkedin.com", "www.linkedin.com"),
            ("10.0.0.1", "10.0.0.1"),
            ("[::1]", "::1"),
            ("", None),
        )
    ),
    port=st.sampled_from(("", ":8443")),
    place=st.sampled_from(("user", "password", "path", "query", "fragment")),
)
def test_a_described_landing_names_the_origin_and_nothing_behind_it(
    scheme: str, host: tuple[str, str | None], port: str, place: str
) -> None:
    written, named = host
    userinfo = {"user": f"{CANARY}@", "password": f"user:{CANARY}@"}.get(place, "")
    path = f"/{CANARY}" if place == "path" else "/login"
    query = f"?token={CANARY}" if place == "query" else ""
    fragment = f"#{CANARY}" if place == "fragment" else ""

    described = describe_landing(
        f"{scheme}://{userinfo}{written}{port}{path}{query}{fragment}"
    )

    assert CANARY not in described
    if named is None:
        assert described == f"a {scheme}: document"
    else:
        spellings = {f"{scheme}://{named}{port}"}
        if ":" in named:
            spellings.add(f"{scheme}://[{named}]{port}")
        assert described in spellings


host_labels = st.text(
    string.ascii_lowercase + string.digits, min_size=1, max_size=15
) | st.sampled_from(("www", "de", "xn--bcher-kva", "xn--lnkedin-9za"))
other_domains = st.lists(host_labels, min_size=1, max_size=4).map(".".join)
LOOKALIKE_HOSTS = (
    "evil-linkedin.com",
    "linkedin.com.evil.io",
    "www.linkedin.com.",
    "WWW.LinkedIn.COM",
    "l\u0456nkedin.com",
    "l\u0131nkedin.com",
    "linkedin\u3002com",
    "xn--lnkedin-9za.com",
    "www.linkedin.com..",
)
landing_hosts = st.one_of(
    st.just("linkedin.com"),
    st.lists(host_labels, min_size=1, max_size=4).map(
        lambda labels: ".".join(labels) + ".linkedin.com"
    ),
    host_labels.map(lambda label: label + "-linkedin.com"),
    other_domains.map(lambda suffix: "linkedin.com." + suffix),
    other_domains,
    st.sampled_from(LOOKALIKE_HOSTS),
    st.sampled_from(string.ascii_lowercase + string.digits + "-").map(
        lambda separator: "www.linkedin" + separator + "com"
    ),
)


@given(host=landing_hosts, uppercase=st.booleans(), trailing_dot=st.booleans())
@example(host="l\u0131nkedin.com", uppercase=False, trailing_dot=False)
def test_a_landing_is_linkedins_only_at_a_label_boundary(
    host: str, uppercase: bool, trailing_dot: bool
) -> None:
    host = (host.upper() if uppercase else host) + ("." if trailing_dot else "")
    labels = host.lower().removesuffix(".").split(".")

    assert is_linkedin_landing(f"https://{host}/feed/") == (
        labels[-2:] == ["linkedin", "com"]
    )


@st.composite
def refused_view_arguments(draw: st.DrawFn) -> dict[str, object]:
    event = draw(
        st.sampled_from(
            (
                {"type": "type", "text": CANARY * 4000},
                {"type": "paste", "text": [CANARY]},
                {"type": "key", "key": CANARY * 3},
                {"type": "key", "key": "a", "modifiers": [CANARY]},
                {"type": "click", "x": CANARY, "y": 0.5},
                {"type": CANARY},
                CANARY,
            )
        )
    )
    arguments: dict[str, object] = {
        "attempt_id": "a" * 12,
        "grant": "g",
        "page_id": "p",
        "geometry": "10x10",
        "events": [event],
    }
    field = draw(
        st.sampled_from(("attempt_id", "grant", "page_id", "geometry", "events", None))
    )
    if field is not None:
        arguments[field] = draw(
            st.sampled_from((CANARY * 100, [CANARY], {"k": CANARY}, None, 1.5))
        )
    return arguments


@given(arguments=refused_view_arguments())
def test_a_refused_view_argument_is_reported_without_its_value(
    arguments: dict[str, object],
) -> None:
    with pytest.raises(ValidationError) as refusal:
        login_app._InputArgs.model_validate(arguments)

    assert CANARY not in value_free_summary(refusal.value)


version = st.lists(st.integers(0, 99999), min_size=3, max_size=5).map(tuple)
build_suffix = st.sampled_from(
    (
        "",
        " snap",
        " beta",
        " built on Debian 10.9, running on Debian 10.9",
        " built on Ubuntu 22.04.3 LTS",
        "\nwrapper 1.2.3",
    )
)


@given(
    product=st.sampled_from(("Chromium", "Google Chrome", "Google Chrome for Testing")),
    numbers=version,
    suffix=build_suffix,
)
def test_a_version_line_reads_back_as_its_product_and_leading_version(
    product: str, numbers: tuple[int, ...], suffix: str
) -> None:
    line = f"\n{product} {'.'.join(map(str, numbers))}{suffix}\n"

    assert parse_build(line) == (product, numbers)


json_leaves = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=8)
)
json_values = st.recursive(
    json_leaves,
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.dictionaries(st.text(max_size=5), inner, max_size=3)
    ),
    max_leaves=8,
)
json_objects = st.dictionaries(st.text(max_size=5), json_values, max_size=4)
CODECS: dict[str, tuple[ModuleType, type[Exception]]] = {
    "login_relay": (login_relay, login_relay.RelayProtocolError),
    "daemon_startup": (startup, startup.ProtocolError),
}
hostile_bodies = (
    st.binary(max_size=40)
    | st.text(
        st.sampled_from('{}[]":,0123456789.eE-+ \\ntrufalsN aIinity'), max_size=30
    ).map(str.encode)
    | st.integers(1, 6000).map(lambda depth: b"[" * depth)
    | st.integers(4000, 6000).map(lambda digits: b'{"a":' + b"9" * digits + b"}")
)


@pytest.mark.parametrize("name", CODECS)
@given(content=json_objects)
def test_a_frame_decodes_to_the_content_it_was_encoded_from(
    name: str, content: dict[str, object]
) -> None:
    codec, _ = CODECS[name]
    frame = codec.encode(content)

    assert int.from_bytes(frame[:4], "big") == len(frame) - 4
    assert codec.decode(frame[4:]) == content


@pytest.mark.parametrize("name", CODECS)
@given(body=hostile_bodies)
def test_a_frame_body_decodes_to_an_object_or_a_protocol_error(
    name: str, body: bytes
) -> None:
    codec, refusal = CODECS[name]
    try:
        content = codec.decode(body)
    except refusal:
        return

    assert isinstance(content, dict)


@given(
    is_error=st.booleans(),
    text=st.text(max_size=10),
    image=st.none() | st.text(max_size=10),
    meta=st.none() | json_objects,
)
def test_an_envelope_survives_the_relay_wire(
    is_error: bool, text: str, image: str | None, meta: dict[str, object] | None
) -> None:
    envelope = login_relay.Envelope(is_error, text, image, meta)
    frame = login_relay.encode(envelope.to_wire())

    assert login_relay.Envelope.from_wire(login_relay.decode(frame[4:])) == envelope


seconds = st.floats(0, 40, allow_nan=False)
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
ProxySettings = tuple[str | None, str | None, str | None, str | None]
blank = st.sampled_from((None, ""))
bypass = st.sampled_from((None, "", "a.example, B.example"))
proxy_address = st.sampled_from(("http://proxy.example:8080", "https://[::1]:1"))
proxy_settings: st.SearchStrategy[ProxySettings] = st.one_of(
    st.tuples(st.none(), blank, blank, blank),
    st.tuples(proxy_address, blank, st.none(), bypass),
    st.tuples(
        proxy_address, st.just("user"), st.sampled_from((None, "", "pw")), bypass
    ),
)
log_levels: st.SearchStrategy[LogLevel] = st.sampled_from(
    ("DEBUG", "INFO", "WARNING", "ERROR")
)


@given(
    headless=st.booleans(),
    slow_mo=st.integers(0, 5000),
    viewport=st.tuples(st.integers(1, 4000), st.integers(1, 4000)),
    timings=st.tuples(seconds, seconds, seconds, seconds, seconds),
    auto_import=st.sampled_from((None, True, False)),
    proxy=proxy_settings,
    tool_timeout=st.floats(1, 1000, allow_nan=False),
    log_level=log_levels,
)
def test_an_owner_receives_every_setting_its_frontend_resolved(
    headless: bool,
    slow_mo: int,
    viewport: tuple[int, int],
    timings: tuple[float, float, float, float, float],
    auto_import: bool | None,
    proxy: ProxySettings,
    tool_timeout: float,
    log_level: LogLevel,
) -> None:
    config = AppConfig()
    browser = config.browser
    browser.headless, browser.slow_mo = headless, slow_mo
    browser.viewport_width, browser.viewport_height = viewport
    (
        browser.login_timeout_seconds,
        browser.login_inline_wait_seconds,
        browser.browser_wait_seconds,
        browser.browser_min_hold_seconds,
        browser.browser_idle_timeout_seconds,
    ) = timings
    browser.auto_import_from_browser = auto_import
    (
        browser.proxy_server,
        browser.proxy_username,
        browser.proxy_password,
        browser.proxy_bypass,
    ) = proxy
    config.server.tool_timeout_seconds = tool_timeout
    config.server.log_level = log_level
    config.validate()

    received = daemon_config.decode(
        json.loads(json.dumps(daemon_config.encode(config)))
    )

    for field in fields(browser):
        assert getattr(received.browser, field.name) == getattr(browser, field.name)
    assert received.server.tool_timeout_seconds == config.server.tool_timeout_seconds
    assert received.server.log_level == log_level


digest = st.from_regex(r"[0-9a-f]{64}", fullmatch=True)
label = st.text(st.characters(exclude_categories=("Cs",)), max_size=10)


@given(
    descriptor=st.builds(
        DaemonDescriptor,
        instance_id=st.uuids(version=4).map(str),
        schema_version=st.integers(0, 9),
        protocol_version=st.integers(0, 9),
        package_version=label,
        runtime_id=label,
        profile_path=label,
        profile_identity=label,
        host=st.sampled_from(("127.0.0.1", "::1")),
        port=st.integers(1, 65535),
        path=st.from_regex(r"/[!-~]{0,10}", fullmatch=True),
        token_sha256=digest,
        config_fingerprint=digest,
        started_at=label,
        log_path=label,
        pid=st.integers(0, 2**31),
    )
)
def test_a_descriptor_reads_back_as_it_was_published(
    descriptor: DaemonDescriptor,
) -> None:
    assert DaemonDescriptor.from_mapping(json.loads(descriptor.to_json())) == descriptor


def _fed_in_pieces(raw: bytes, limit: int, cuts: list[int]) -> str:
    tail = bootstrap._RedactedTail(limit)
    for start, end in zip([0, *cuts], [*cuts, len(raw)]):
        tail.feed(raw[start:end])
    return tail.finish()


prefix = st.text(
    st.sampled_from("ab0_ ('\"=:\x1b[;m\n"), max_size=12
) | st.sampled_from(
    ("\x1b[0m", "\x1b[36;1m", "Downloading from ", "x" * 70 + " \x1b[0m")
)


@given(
    scheme=st.sampled_from(("http", "https", "HTTPS")),
    place=st.sampled_from(("path", "query", "user", "password")),
    before=prefix,
    after=st.sampled_from(("", " now", "'", "'. URL: x", "\n")),
    limit=st.integers(8, 80),
    data=st.data(),
)
def test_a_url_secret_never_survives_the_retained_tail(
    scheme: str, place: str, before: str, after: str, limit: int, data: st.DataObject
) -> None:
    userinfo = {"user": f"{CANARY}@", "password": f"user:{CANARY}@"}.get(place, "")
    path = f"/{CANARY}/x.zip" if place == "path" else "/x.zip"
    query = f"?token={CANARY}" if place == "query" else ""
    raw = f"{before}{scheme}://{userinfo}cdn.example{path}{query}{after}".encode()

    cuts = sorted(data.draw(st.lists(st.integers(0, len(raw)), max_size=4)))

    retained = _fed_in_pieces(raw, limit, cuts)

    assert CANARY not in retained
    assert len(retained) <= limit + len("***")


plain = st.lists(
    st.sampled_from(("a", "z", "0", "_", " ", "\n", ".", ":", "-", "é", "T", "ttps:")),
    max_size=120,
).map("".join)


words_holding_an_h = st.lists(
    st.sampled_from(("a", "z", "_", " ", "\n", ":", "ah", "oht", "ahttps:", "0H")),
    max_size=120,
).map("".join)


@given(text=plain, limit=st.integers(1, 40), data=st.data())
def test_output_without_an_h_or_a_slash_keeps_its_exact_tail(
    text: str, limit: int, data: st.DataObject
) -> None:
    raw = text.encode()
    cuts = sorted(data.draw(st.lists(st.integers(0, len(raw)), max_size=6)))

    assert _fed_in_pieces(raw, limit, cuts) == text[-limit:]


@given(text=words_holding_an_h, limit=st.integers(1, 40))
def test_one_read_of_words_that_hold_an_h_keeps_its_exact_tail(
    text: str, limit: int
) -> None:
    assert _fed_in_pieces(text.encode(), limit, []) == text[-limit:]


def _as_the_kernel_writes(mount_point: bytes) -> str:
    escaped = b"".join(
        b"\\%03o" % byte if byte in b" \t\n\\" else bytes([byte])
        for byte in mount_point
    )
    return os.fsdecode(escaped)


names = (
    st.text(
        st.sampled_from("ab-é \t\n\\\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"),
        min_size=1,
        max_size=6,
    ).map(str.encode)
    | st.binary(min_size=1, max_size=6)
).filter(
    lambda name: b"/" not in name and b"\0" not in name and name not in (b".", b"..")
)


mounts = st.tuples(
    st.lists(names, max_size=3).map(lambda parts: b"/" + b"/".join(parts)),
    st.sampled_from(("ext4", "nfs4", "tmpfs", "fuse.sshfs", "overlay")),
    st.lists(st.sampled_from(("shared:1", "master:2")), max_size=2),
)


Table = list[tuple[bytes, str, list[str]]]


def _mountinfo(table: Table) -> list[str]:
    return [
        f"36 35 98:0 / {_as_the_kernel_writes(point)} rw {' '.join([*tags, '-'])} "
        f"{fstype} /dev/sda1 rw\n"
        for point, fstype, tags in table
    ]


@given(table=st.lists(mounts, max_size=4))
def test_both_mountinfo_readers_return_every_mount_point_the_kernel_wrote(
    table: Table,
) -> None:
    text = "".join(_mountinfo(table))
    written = [
        (PurePosixPath(os.fsdecode(point)), fstype) for point, fstype, _ in table
    ]

    assert [(mount.mount_point, mount.fstype) for mount in parse_mountinfo(text)] == (
        written
    )
    assert [
        (PurePosixPath(record.point), record.filesystem)
        for record in mount_records(text)
    ] == written


@given(table=st.lists(mounts, max_size=4), data=st.data())
def test_a_record_without_a_separator_is_refused_by_one_reader_and_skipped_by_the_other(
    table: Table, data: st.DataObject
) -> None:
    lines = _mountinfo(table)
    lines.insert(data.draw(st.integers(0, len(lines))), "36 35 98:0 / /mnt/a\rb rw\n")
    text = "".join(lines)

    with pytest.raises(ValueError, match="does not parse"):
        parse_mountinfo(text)
    assert [PurePosixPath(record.point) for record in mount_records(text)] == [
        PurePosixPath(os.fsdecode(point)) for point, _, _ in table
    ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory modes")
@given(
    parts=st.lists(
        st.text(string.ascii_lowercase + string.digits, min_size=1, max_size=12),
        min_size=1,
        max_size=5,
    ),
    existing=st.integers(0, 5),
    existing_mode=st.sampled_from((0o755, 0o750, 0o700, 0o770)),
    umask=st.sampled_from((0o022, 0o077, 0o027)),
)
def test_secure_mkdir_makes_every_new_directory_private(
    parts: list[str], existing: int, existing_mode: int, umask: int
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        root.chmod(0o755)
        chain = [root.joinpath(*parts[: depth + 1]) for depth in range(len(parts))]
        for ancestor in chain[:existing]:
            ancestor.mkdir()
            ancestor.chmod(existing_mode)
        before = {
            path: stat.S_IMODE(path.stat().st_mode)
            for path in [root, *chain[:existing]]
        }
        previous = os.umask(umask)
        try:
            common_utils.secure_mkdir(chain[-1])
        finally:
            os.umask(previous)

        assert [stat.S_IMODE(path.stat().st_mode) for path in chain[existing:]] == [
            0o700
        ] * len(chain[existing:])
        assert {path: stat.S_IMODE(path.stat().st_mode) for path in before} == before
