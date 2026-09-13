"""Operator-tunable limits, read from the environment at call time.

Every pacing number the server used to hard-code is a default here, and each
one has an environment variable that replaces it per process, which is per
profile since a process owns one ``USER_DATA_DIR``. Reading at call time
rather than import keeps tests and the daemon free of import-order surprises.

An unusable value falls back to the default with a warning rather than to
zero: a typo must not be the way pacing is turned off.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Defined once here (rather than in config/schema.py or config/loaders.py) so
# both can reference the same string without importing each other: schema.py
# reads the env var directly, loaders.py exposes it on EnvironmentKeys, and a
# schema.py -> loaders.py import would cycle since loaders.py imports schema.py.
LOGIN_INLINE_WAIT_MAX_KEY = "LOGIN_INLINE_WAIT_MAX"
BROWSER_WAIT_MAX_KEY = "BROWSER_WAIT_MAX"


def env_float(key: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r; using %s", key, raw, default)
        return default
    if value < minimum:
        logger.warning("Ignoring %s=%r below %s; using %s", key, raw, minimum, default)
        return default
    return value


def env_int(key: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r; using %s", key, raw, default)
        return default
    if value < minimum:
        logger.warning("Ignoring %s=%r below %s; using %s", key, raw, minimum, default)
        return default
    return value


def env_int_list(key: str, default: tuple[int, ...]) -> tuple[int, ...]:
    """Comma-separated non-negative integers, e.g. ``WARMUP_CAPS=10,20,50``."""
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        values = tuple(int(part) for part in raw.split(","))
    except ValueError:
        logger.warning("Ignoring malformed %s=%r; using %s", key, raw, default)
        return default
    if not values or any(v < 0 for v in values):
        logger.warning("Ignoring %s=%r; using %s", key, raw, default)
        return default
    return values
