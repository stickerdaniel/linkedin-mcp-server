"""Named stealth-posture presets for browser launch/navigation tuning.

Ported from cloudconsultants/linkedin-mcp-server's ``StealthProfile``
concept, scoped down to fields our codebase can actually consume today:

- ``delays``: consumed by ``core/humanize.py``'s bezier mouse path and
  keystroke timing, and by the interaction-simulation tiers.
- ``rate_limit_per_minute``: consumed by ``core/rate_limit.py``'s
  ``ActionRateLimiter``, applied to the read path (``get_person_profile``/
  ``search_people``) in addition to its existing write-path use.
- ``navigation``: consumed by the ``SEARCH_FIRST`` navigation-mode branch
  in ``scraping/extractor.py``.
- ``simulation``/``lazy_loading``/``telemetry``: consumed by the
  page-interaction-simulation, lazy-load-detector, and telemetry work.
- ``enable_fingerprint_masking``: NOT new code -- both Patchright and
  Camoufox already provide this natively (see ``core/engines.py``). The
  field exists so a profile's intent is self-documenting; toggling it
  doesn't add/remove masking logic, it's descriptive of what the chosen
  engine already does.
- ``session_warming``: maps onto the *existing* ``core/opsec.py``
  ``WARMUP_SCHEDULE``/``VIEW_PROFILE_WARMUP_SCHEDULE`` -- not new code
  either. ``False`` is intended to bypass that gate for ``NO_STEALTH``
  (matching the "skip everything" semantics every other NO_STEALTH field
  already has), not to add a second warmup mechanism.
- ``max_concurrent_profiles`` / ``session_rotation_threshold``: carried
  for config-shape parity with the fork, but have NO behavior wired --
  the fork itself has no session-pooling/rotation implementation behind
  either field. Revisit only if a real multi-session subsystem is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class NavigationMode(str, Enum):
    DIRECT = "direct"
    SEARCH_FIRST = "search_first"


class SimulationLevel(str, Enum):
    NONE = "none"
    BASIC = "basic"
    MODERATE = "moderate"
    COMPREHENSIVE = "comprehensive"


@dataclass
class DelayConfig:
    """Randomized delay ranges, in seconds, by interaction category."""

    base: tuple[float, float] = (1.5, 4.0)
    reading: tuple[float, float] = (2.0, 6.0)
    navigation: tuple[float, float] = (1.0, 3.0)
    typing: tuple[float, float] = (0.05, 0.15)
    scroll: tuple[float, float] = (0.5, 1.5)


@dataclass
class StealthProfile:
    name: str
    navigation: NavigationMode
    delays: DelayConfig
    simulation: SimulationLevel
    lazy_loading: bool = True
    telemetry: bool = True
    enable_fingerprint_masking: bool = True
    session_warming: bool = True
    rate_limit_per_minute: int = 1
    max_concurrent_profiles: int = 3
    session_rotation_threshold: int = 5

    @classmethod
    def no_stealth(cls) -> StealthProfile:
        """Fastest, no humanization -- CI/debugging, not for real accounts."""
        return cls(
            name="NO_STEALTH",
            navigation=NavigationMode.DIRECT,
            delays=DelayConfig(
                base=(0.1, 0.3),
                reading=(0.2, 0.5),
                navigation=(0.1, 0.3),
                typing=(0.01, 0.03),
                scroll=(0.1, 0.3),
            ),
            simulation=SimulationLevel.NONE,
            lazy_loading=False,
            telemetry=True,
            enable_fingerprint_masking=False,
            session_warming=False,
            rate_limit_per_minute=10,
            max_concurrent_profiles=5,
            session_rotation_threshold=20,
        )

    @classmethod
    def minimal_stealth(cls) -> StealthProfile:
        """Default posture: light humanization, direct navigation."""
        return cls(
            name="MINIMAL_STEALTH",
            navigation=NavigationMode.DIRECT,
            delays=DelayConfig(
                base=(0.5, 1.0),
                reading=(0.5, 1.5),
                navigation=(0.3, 0.8),
                typing=(0.03, 0.08),
                scroll=(0.3, 0.6),
            ),
            simulation=SimulationLevel.BASIC,
            lazy_loading=True,
            telemetry=True,
            enable_fingerprint_masking=True,
            session_warming=False,
            rate_limit_per_minute=3,
            max_concurrent_profiles=3,
            session_rotation_threshold=10,
        )

    @classmethod
    def moderate_stealth(cls) -> StealthProfile:
        """Heavier humanization and pacing; still direct navigation."""
        return cls(
            name="MODERATE_STEALTH",
            navigation=NavigationMode.DIRECT,
            delays=DelayConfig(
                base=(1.0, 2.5),
                reading=(1.5, 3.0),
                navigation=(0.8, 2.0),
                typing=(0.05, 0.12),
                scroll=(0.5, 1.0),
            ),
            simulation=SimulationLevel.MODERATE,
            lazy_loading=True,
            telemetry=True,
            enable_fingerprint_masking=True,
            session_warming=True,
            rate_limit_per_minute=2,
            max_concurrent_profiles=3,
            session_rotation_threshold=7,
        )

    @classmethod
    def maximum_stealth(cls) -> StealthProfile:
        """Slowest, most cautious posture -- routes through SEARCH_FIRST
        navigation instead of a direct profile URL hit."""
        return cls(
            name="MAXIMUM_STEALTH",
            navigation=NavigationMode.SEARCH_FIRST,
            delays=DelayConfig(
                base=(1.5, 4.0),
                reading=(2.0, 6.0),
                navigation=(1.0, 3.0),
                typing=(0.05, 0.15),
                scroll=(0.5, 1.5),
            ),
            simulation=SimulationLevel.COMPREHENSIVE,
            lazy_loading=True,
            telemetry=True,
            enable_fingerprint_masking=True,
            session_warming=True,
            rate_limit_per_minute=1,
            max_concurrent_profiles=3,
            session_rotation_threshold=5,
        )


_PRESET_FACTORIES = {
    "NO_STEALTH": StealthProfile.no_stealth,
    "MINIMAL_STEALTH": StealthProfile.minimal_stealth,
    "MODERATE_STEALTH": StealthProfile.moderate_stealth,
    "MAXIMUM_STEALTH": StealthProfile.maximum_stealth,
}

DEFAULT_STEALTH_PROFILE_NAME = "MINIMAL_STEALTH"
STEALTH_PROFILE_NAMES = tuple(_PRESET_FACTORIES)


def get_stealth_profile(name: str | None = None) -> StealthProfile:
    """Resolve a stealth-profile name (case-insensitive) to a fresh
    ``StealthProfile`` instance.

    Raises ``ValueError`` for an unknown name -- fails loud rather than
    silently falling back to a default that could mask a config typo.
    ``BrowserConfig.validate()`` is the layer that turns this into a
    user-facing ``ConfigurationError``.
    """
    resolved_name = (name or DEFAULT_STEALTH_PROFILE_NAME).strip().upper()
    factory = _PRESET_FACTORIES.get(resolved_name)
    if factory is None:
        raise ValueError(
            f"Unknown stealth profile '{name}'. "
            f"Valid: {', '.join(STEALTH_PROFILE_NAMES)}"
        )
    return factory()
