"""
Configuration schema definitions for LinkedIn MCP Server.

Defines the dataclass schemas that represent the application's configuration
structure with type-safe configuration objects and default values.
"""

import ipaddress
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT_SECONDS: float = 180.0
DEFAULT_LOGIN_TIMEOUT_SECONDS: float = 1800.0  # 30 min; 0 = no limit
DEFAULT_LOGIN_INLINE_WAIT_SECONDS: float = 25.0  # bounded inline wait
# Clamp ceiling: scrape time stacks on top of the inline wait inside one tool
# call and the smallest MCP client timeout is ~60s, so the wait alone must stay
# well under that floor.
MAX_LOGIN_INLINE_WAIT_SECONDS: float = 45.0

# How long a tool call waits for another process to hand over the browser. Same
# budget and ceiling as the login inline wait, for the same reason: the wait is
# spent inside one tool call, ahead of the scrape itself.
DEFAULT_BROWSER_WAIT_SECONDS: float = 25.0
MAX_BROWSER_WAIT_SECONDS: float = 45.0
# Shortest time an owner keeps the browser before honouring a handoff request.
# Every handoff costs a reopen, and a reopen re-validates /feed/, so handing over
# on literally every call would multiply LinkedIn requests. Matched to the wait
# budget: a longer window would push waiters past their own timeout.
DEFAULT_BROWSER_MIN_HOLD_SECONDS: float = 20.0
# Slack between the end of the hold window and the waiter's deadline: the owner
# notices on a one-second poll and then has to tear Chromium down (~0.7s
# measured). Without it a waiter gives up moments before the handover lands.
BROWSER_HANDOFF_MARGIN_SECONDS: float = 3.0
# How long a one-shot operation waits for another process to hand the profile
# over. A user-experience limit rather than a derived worst case, and the
# distinction is worth stating because the arithmetic invites the wrong one: an
# idle holder does release quickly, after browser_min_hold_seconds plus about a
# second of teardown, but a holder running a tool call refuses to hand over at
# all until that call ends (`drivers/browser.py`), and a call may take up to
# tool_timeout_seconds. So this does not bound every cooperative case. It bounds
# how long someone who just asked to sign in is left staring at nothing, and past
# it they are told the browser is busy, which they can act on. Waiting three
# minutes in silence is the worse answer.
#
# Deliberately not configurable. Nothing measured suggests a user would need to
# tune it, and an unused setting is a support burden.
PROFILE_HANDOVER_WAIT_SECONDS: float = 60.0

# What fraction of a tool call a frontend may spend waiting for the sign-in it
# started, before answering without it.
#
# A fraction rather than a number of seconds, because the bound that matters is
# the call this wait is spent inside, and ``tool_timeout_seconds`` is something
# the user sets. A fixed value was measured against ``TOOL_TIMEOUT=60``: the
# client's call was cut short while the middleware was still waiting, so instead
# of the readable "sign-in still in progress" the user got a bare timeout. The
# login survived it and the next call succeeded, so nothing was lost but the
# explanation -- which is the part a person acts on.
#
# Five sixths leaves the remaining sixth for the replayed call, which is the
# whole point of having waited. Somebody still typing when it runs out loses only
# this attempt: the login keeps running and the next call finds what it wrote.
AUTH_REPAIR_LOGIN_WAIT_FRACTION: float = 5 / 6

# Close an idle browser and release the profile after this long with no calls.
# A backstop only — the handoff signal does the real work — so it is deliberately
# long: a reopen costs one more LinkedIn request. 0 disables it.
DEFAULT_BROWSER_IDLE_TIMEOUT_SECONDS: float = 600.0

#: The one profile root this server may destroy without being told it owns it.
#: Spelled with ``~`` rather than resolved here so it follows the account that
#: runs the process: on the host that is the user's home, and in the published
#: image it is ``/home/pwuser``, which is what ``docker-compose.yml`` mounts.
#: ``profile_claim`` compares against it, so it has to be one string rather than
#: a literal repeated at both ends.
DEFAULT_USER_DATA_DIR: str = "~/.linkedin-mcp/profile"


# Proxy schemes Chromium understands on --proxy-server. The SOCKS ones are
# usable without credentials only: the browser cannot answer a SOCKS auth
# challenge, so Patchright rejects that combination outright.
SOCKS_PROXY_SCHEMES: frozenset[str] = frozenset({"socks4", "socks5"})
PROXY_SCHEMES: frozenset[str] = frozenset({"http", "https"}) | SOCKS_PROXY_SCHEMES


# Names that resolve only on this machine. Compared case-insensitively and
# without a trailing root dot, both of which are the same name to a resolver.
LOOPBACK_HOSTNAMES: frozenset[str] = frozenset({"localhost"})


def is_loopback_host(host: str) -> bool:
    """Whether *host* is reachable only from this machine.

    Fails closed: anything this cannot positively identify as loopback counts
    as reachable from elsewhere, whether that is a wildcard bind, a LAN
    address, or a name that needs DNS to answer. Erring that way costs a
    warning and a skipped cookie import; erring the other way would hand a
    logged-in session to the network.

    Address literals go through :mod:`ipaddress` rather than a spelling list,
    so the whole 127/8 range and IPv4-mapped forms are recognised for what they
    are instead of being judged on how they were typed.
    """
    name = host.strip().rstrip(".").lower()
    if not name:
        return False
    if name in LOOPBACK_HOSTNAMES:
        return True

    literal = name
    if literal.startswith("[") and literal.endswith("]"):
        literal = literal[1:-1]  # bracketed IPv6, as it appears in a URL
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        # A name only DNS can resolve, and DNS can point anywhere.
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or bool(mapped and mapped.is_loopback)


class ConfigurationError(Exception):
    """Raised when configuration validation fails."""


@dataclass
class BrowserConfig:
    """Configuration for browser settings."""

    headless: bool = True
    slow_mo: int = 0  # Milliseconds between browser actions (debugging)
    # Always None: the override was removed and both settings that used to fill
    # this are now refused at startup. The field stays because
    # ``SHARED_CONFIG_FIELDS`` hashes it into the daemon's configuration
    # fingerprint, and dropping a field there makes a running owner unreadable
    # to a client of the other version instead of merely mismatched. Remove it
    # once owner turnover survives a fingerprint change.
    user_agent: str | None = None
    viewport_width: int = 1280
    viewport_height: int = 720
    default_timeout: int = 5000  # Milliseconds for page operations
    chrome_path: str | None = None  # Path to Chrome/Chromium executable
    user_data_dir: str = DEFAULT_USER_DATA_DIR  # Persistent browser profile
    # Proxy for the browser's own traffic. The server's MCP transport is not
    # routed through it. ``proxy_server`` accepts scheme://host:port and, from
    # the environment only, a provider string carrying credentials; validate()
    # splits those out so the stored value never holds a password.
    proxy_server: str | None = None
    # Both credentials are kept out of the repr, because cli_main logs the whole
    # config at DEBUG level and users paste those logs into issue reports. The
    # username is not merely a name: residential providers encode the account,
    # zone and session in it. The server stays visible; it holds no secret once
    # validate() has split the credentials off, and it is the field you need to
    # diagnose a proxy problem.
    proxy_username: str | None = field(default=None, repr=False)
    proxy_password: str | None = field(default=None, repr=False)
    proxy_bypass: str | None = None  # Comma-separated hosts to bypass
    # Manual-login wait timeout in seconds; 0 = unlimited
    login_timeout_seconds: float = DEFAULT_LOGIN_TIMEOUT_SECONDS
    # Bounded inline wait before the pending signal; 0 = immediate return
    login_inline_wait_seconds: float = DEFAULT_LOGIN_INLINE_WAIT_SECONDS
    # Wait for another process to hand over the browser; 0 = report busy at once
    browser_wait_seconds: float = DEFAULT_BROWSER_WAIT_SECONDS
    # Shortest ownership before a handoff request is honoured; 0 = hand over
    # after every call
    browser_min_hold_seconds: float = DEFAULT_BROWSER_MIN_HOLD_SECONDS
    # Close an idle browser after this long; 0 = keep it until the process exits
    browser_idle_timeout_seconds: float = DEFAULT_BROWSER_IDLE_TIMEOUT_SECONDS
    # Auto-import a LinkedIn session from a locally logged-in browser on the
    # first no-session tool call, before falling back to manual login. On by
    # default: None ("auto") and True both enable it across interactive and
    # non-interactive desktop runs on every platform. False disables it. No
    # effect under Docker (no host browser/keychain) or on a non-loopback HTTP
    # bind (a network-exposed server must not read the host browser cookie).
    # Note the non-loopback gate covers network-exposed HTTP only, not
    # stdio-over-SSH: a non-console session simply fails to read the local
    # user's keychain and degrades to manual login, and no cookie crosses the
    # network.
    auto_import_from_browser: bool | None = None
    # Read by nothing, and accepted anyway. It used to choose between installing
    # the full browser up front or lazily on the first headed login; there is
    # only one browser now, so both answers describe the same install.
    #
    # It stays for the same reason ``user_agent`` above does: it is part of the
    # daemon's configuration fingerprint (``SHARED_CONFIG_FIELDS``), and
    # ``daemon.py`` rejects a fingerprint mismatch *before* it compares package
    # versions. Drop the field and a running owner of the other version stops
    # being readable, so it can never be asked to stand down. Both can go once
    # owner turnover survives a fingerprint change.
    eager_full_chromium: bool = False

    def validate(self) -> None:
        """Validate browser configuration values."""
        if self.slow_mo < 0:
            raise ConfigurationError(
                f"slow_mo must be non-negative, got {self.slow_mo}"
            )
        if self.default_timeout <= 0:
            raise ConfigurationError(
                f"default_timeout must be positive, got {self.default_timeout}"
            )
        if self.viewport_width <= 0 or self.viewport_height <= 0:
            raise ConfigurationError(
                f"viewport dimensions must be positive, got {self.viewport_width}x{self.viewport_height}"
            )
        # 0 is a valid sentinel for both (unlimited login wait / no inline wait),
        # so these use >= 0 rather than the > 0 check tool_timeout_seconds uses.
        if not (
            math.isfinite(self.login_timeout_seconds)
            and self.login_timeout_seconds >= 0
        ):
            raise ConfigurationError(
                "login_timeout_seconds must be a non-negative finite number, "
                f"got {self.login_timeout_seconds}"
            )
        if not (
            math.isfinite(self.login_inline_wait_seconds)
            and self.login_inline_wait_seconds >= 0
        ):
            raise ConfigurationError(
                "login_inline_wait_seconds must be a non-negative finite number, "
                f"got {self.login_inline_wait_seconds}"
            )
        # Clamp (do not reject) so a misconfigured large value can never alone
        # approach the client timeout floor once scrape time is added on top.
        if self.login_inline_wait_seconds > MAX_LOGIN_INLINE_WAIT_SECONDS:
            logger.warning(
                "login_inline_wait_seconds %.1f exceeds the %.1fs ceiling; "
                "clamping (scrape time stacks on top of the wait inside one "
                "tool call).",
                self.login_inline_wait_seconds,
                MAX_LOGIN_INLINE_WAIT_SECONDS,
            )
            self.login_inline_wait_seconds = MAX_LOGIN_INLINE_WAIT_SECONDS
        for name in (
            "browser_wait_seconds",
            "browser_min_hold_seconds",
            "browser_idle_timeout_seconds",
        ):
            value = getattr(self, name)
            if not (math.isfinite(value) and value >= 0):
                raise ConfigurationError(
                    f"{name} must be a non-negative finite number, got {value}"
                )
        # Clamped for the same reason as the login inline wait: the wait happens
        # inside a tool call, ahead of the scrape.
        if self.browser_wait_seconds > MAX_BROWSER_WAIT_SECONDS:
            logger.warning(
                "browser_wait_seconds %.1f exceeds the %.1fs ceiling; clamping.",
                self.browser_wait_seconds,
                MAX_BROWSER_WAIT_SECONDS,
            )
            self.browser_wait_seconds = MAX_BROWSER_WAIT_SECONDS
        # The hold window has to end far enough inside the wait budget that the
        # owner still notices and finishes closing before the waiter gives up.
        # Equal values are not enough: the owner polls on an interval and then
        # has to tear Chromium down, so it would answer just after the deadline.
        latest_useful_hold = max(
            0.0, self.browser_wait_seconds - BROWSER_HANDOFF_MARGIN_SECONDS
        )
        if self.browser_min_hold_seconds > latest_useful_hold:
            logger.warning(
                "browser_min_hold_seconds %.1f leaves no room inside the %.1fs "
                "wait budget for the owner to notice and close; clamping to "
                "%.1f.",
                self.browser_min_hold_seconds,
                self.browser_wait_seconds,
                latest_useful_hold,
            )
            self.browser_min_hold_seconds = latest_useful_hold
        if self.chrome_path:
            chrome_path = Path(self.chrome_path)
            if not chrome_path.exists():
                raise ConfigurationError(
                    f"chrome_path '{self.chrome_path}' does not exist"
                )
            if not chrome_path.is_file():
                raise ConfigurationError(
                    f"chrome_path '{self.chrome_path}' is not a file"
                )
        self._normalize_proxy()

    def _normalize_proxy(self) -> None:
        """Split, check and canonicalize the proxy settings.

        Credentials embedded in ``proxy_server`` are moved into the separate
        fields, because Patchright's own normalization drops the userinfo from
        the server URL and would authenticate with nothing. Runs from
        ``validate()`` so a combined provider URL never survives in the config
        object, and so the failures below surface at startup rather than as an
        opaque 407 or a driver exception on the first navigation.

        No message here may quote the raw value: it can carry a password and
        configuration errors are printed to the console.
        """
        if not self.proxy_server:
            for name in ("proxy_username", "proxy_password", "proxy_bypass"):
                if getattr(self, name):
                    raise ConfigurationError(
                        f"{name} is set without proxy_server. "
                        "Set the proxy server too, or unset it."
                    )
            return

        raw = self.proxy_server.strip()
        # A bare host:port is what most providers hand out. Patchright assumes
        # HTTP for it; assume it here too so validation sees the same URL.
        if "://" not in raw:
            raw = f"http://{raw}"
        try:
            parsed = urlsplit(raw)
            hostname, port = parsed.hostname, parsed.port
        except ValueError:
            raise ConfigurationError(
                "proxy_server is not a valid URL. "
                "Expected scheme://host:port, for example http://proxy.example:8080."
            )

        if parsed.scheme not in PROXY_SCHEMES:
            raise ConfigurationError(
                f"proxy_server scheme '{parsed.scheme}' is not supported. "
                f"Use one of: {', '.join(sorted(PROXY_SCHEMES))}."
            )
        if not hostname or port is None:
            raise ConfigurationError(
                "proxy_server needs a host and an explicit port, "
                "for example http://proxy.example:8080."
            )
        if "@" in unquote(hostname):
            # A percent-encoded userinfo separator, as in
            # "http://user%3Apass%40host:7000". urlsplit reads that as a plain
            # hostname, so the credentials would neither be split out nor hidden
            # from the logs, while still being trivially decodable. Patchright
            # cannot parse it either and falls back to a nonsense host, so the
            # browser would not even reach the intended proxy.
            raise ConfigurationError(
                "proxy_server contains a percent-encoded '@'. Pass the "
                "credentials as PROXY_USERNAME and PROXY_PASSWORD instead of "
                "encoding them into the address."
            )
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            # Chromium takes only scheme, host and port; anything else would be
            # dropped silently and the user would never learn it was ignored.
            raise ConfigurationError(
                "proxy_server must not carry a path, query or fragment. "
                "Use only scheme://host:port."
            )

        embedded_user = unquote(parsed.username) if parsed.username else None
        embedded_password = unquote(parsed.password) if parsed.password else None
        if embedded_user or embedded_password:
            if self.proxy_username or self.proxy_password:
                # Picking a winner here is how the wrong password ships and
                # nobody can tell why, so refuse instead.
                raise ConfigurationError(
                    "Proxy credentials are set both inside proxy_server and "
                    "separately. Use one or the other."
                )
            self.proxy_username = embedded_user
            self.proxy_password = embedded_password

        # Store the host:port form Chromium actually receives.
        host = f"[{hostname}]" if ":" in hostname else hostname
        self.proxy_server = f"{parsed.scheme}://{host}:{port}"

        # A username with an empty password is legitimate -- Playwright supports
        # it explicitly for key-style proxy accounts -- so only the reverse is
        # rejected: a password alone can never be sent.
        if self.proxy_password is not None and not self.proxy_username:
            raise ConfigurationError(
                "A proxy password is set without a username. "
                "Set the username too, or unset the password."
            )
        if self.proxy_username and parsed.scheme in SOCKS_PROXY_SCHEMES:
            # Chromium cannot answer a SOCKS auth challenge, and Patchright
            # raises an opaque error at launch. Catch it here instead.
            raise ConfigurationError(
                f"Chromium cannot authenticate to a {parsed.scheme} proxy. "
                "Use an http:// or https:// endpoint from your provider, or "
                "run a local relay that holds the credentials."
            )

    def proxy_settings(self) -> dict[str, Any] | None:
        """Build the Patchright ``proxy`` option, or None when unconfigured.

        Both browser launch paths call this so the login session and the
        scraping session leave from the same address. A session created on one
        IP and used from another is what trips LinkedIn's security checkpoint.
        """
        if not self.proxy_server:
            return None
        settings: dict[str, Any] = {"server": self.proxy_server}
        if self.proxy_username:
            settings["username"] = self.proxy_username
            # Compared against None, not truthiness: an empty password is a
            # valid credential and must still be sent.
            if self.proxy_password is not None:
                settings["password"] = self.proxy_password
        if self.proxy_bypass:
            settings["bypass"] = self.proxy_bypass
        return settings


@dataclass
class ServerConfig:
    """MCP server configuration."""

    transport: Literal["stdio", "streamable-http"] = "stdio"
    transport_explicitly_set: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "WARNING"
    login: bool = False
    # Expose the Docker login browser through a short-lived authenticated noVNC
    # endpoint. CLI-only: there is deliberately no environment switch that could
    # expose browser control on an ordinary container startup.
    login_viewer: bool = False
    status: bool = False  # Check session validity and exit
    logout: bool = False
    # Take over a USER_DATA_DIR that is neither the default nor already ours.
    # Not in SHARED_CONFIG_FIELDS and deliberately so: it changes nothing about
    # the browser a client would be served, only whether this process is allowed
    # to write an ownership marker once.
    claim_profile_root: bool = False
    # Browser key or "auto"; triggers import-from-browser-and-exit.
    import_from_browser: str | None = None
    # HTTP transport configuration
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    # Serve every stdio client from one browser-owning process instead of
    # giving each its own. Off while the supervision and liveness work is
    # unfinished: an owner that outlives its client must be provably unable to
    # keep scraping, and until then this stays something you opt into. Only
    # applies to stdio; an explicit HTTP bind is already a single server.
    daemon_enabled: bool = False

    def validate(self) -> None:
        """Validate server configuration values."""
        if not (
            math.isfinite(self.tool_timeout_seconds) and self.tool_timeout_seconds > 0
        ):
            raise ConfigurationError(
                f"tool_timeout_seconds must be a positive finite number, got {self.tool_timeout_seconds}"
            )
        if self.login_viewer:
            if not self.login:
                raise ConfigurationError("--login-viewer requires --login")
            incompatible = []
            if self.status:
                incompatible.append("--status")
            if self.logout:
                incompatible.append("--logout")
            if self.import_from_browser is not None:
                incompatible.append("--import-from-browser")
            if incompatible:
                raise ConfigurationError(
                    "--login-viewer cannot be combined with " + ", ".join(incompatible)
                )
        if self.import_from_browser is not None:
            # Import the submodule, NOT the package, to avoid a config ->
            # browser_import -> drivers.browser -> config import cycle.
            from linkedin_mcp_server.browser_import.discovery import SUPPORTED_BROWSERS

            allowed = set(SUPPORTED_BROWSERS) | {"auto"}
            if self.import_from_browser not in allowed:
                raise ConfigurationError(
                    "import_from_browser "
                    f"'{self.import_from_browser}' is not supported. "
                    f"Choose one of: {', '.join(sorted(allowed))}"
                )


@dataclass
class AppConfig:
    """Main application configuration."""

    browser: BrowserConfig = field(default_factory=BrowserConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    is_interactive: bool = field(default=False)

    def validate(self) -> None:
        """Validate all configuration values. Call after modifying config."""
        self.browser.validate()
        self.server.validate()
        if self.server.transport == "streamable-http":
            self._validate_transport_config()
            self._validate_path_format()
        self._validate_port_range()

    def _validate_transport_config(self) -> None:
        """Validate transport configuration is consistent."""
        if not self.server.host:
            raise ConfigurationError("HTTP transport requires a valid host")
        if not self.server.port:
            raise ConfigurationError("HTTP transport requires a valid port")
        if not is_loopback_host(self.server.host):
            # Warned about rather than refused, and the distinction is not
            # timidity. A container has to bind the wildcard: a process bound to
            # 127.0.0.1 inside one is unreachable through a published port at
            # all, so refusing this would break the documented Docker command
            # while protecting nobody. What actually decides exposure is the
            # publish address on the host, and this process cannot see it --
            # `-p 127.0.0.1:8080:8080` and `-p 8080:8080` look identical from in
            # here. Container detection is no way out either, since
            # LINKEDIN_MCP_CONTAINER is a full override and therefore not a
            # security boundary. So the honest thing is to say what it costs and
            # name both remedies.
            logger.warning(
                "HTTP transport is binding to %s, which is reachable from "
                "outside this machine. The MCP endpoint has no authentication, "
                "so anyone who can reach that address can use your LinkedIn "
                "session. Host and origin checking is not access control; it "
                "stops a website from pointing a name at this server, not "
                "someone who can reach the address. Outside Docker, use "
                "--host 127.0.0.1. In Docker, keep --host 0.0.0.0 and publish "
                "to loopback instead: -p 127.0.0.1:PORT:PORT. For remote "
                "access, forward the port over SSH rather than exposing it.",
                self.server.host,
            )

    def _validate_port_range(self) -> None:
        """Validate port is in valid range."""
        if not (1 <= self.server.port <= 65535):
            raise ConfigurationError(
                f"Port {self.server.port} is not in valid range (1-65535)"
            )

    def _validate_path_format(self) -> None:
        """Validate path format for HTTP transport."""
        if not self.server.path.startswith("/"):
            raise ConfigurationError(
                f"HTTP path '{self.server.path}' must start with '/'"
            )
        if len(self.server.path) < 2:
            raise ConfigurationError(
                f"HTTP path '{self.server.path}' must be at least 2 characters"
            )
