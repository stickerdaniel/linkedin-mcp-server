"""
Retry utilities for handling transient failures.

Provides exponential backoff retry decorator for async functions.
"""

import asyncio
import logging
from functools import wraps
from typing import Any, Callable, Tuple, Type, TypeVar

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def retry_async(
    max_attempts: int = 3,
    backoff: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (PlaywrightTimeoutError,),
) -> Callable[[F], F]:
    """
    Decorator for retrying async functions with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts (default: 3)
        backoff: Backoff multiplier - wait time doubles each retry (default: 2.0)
        exceptions: Tuple of exception types to retry on

    Returns:
        Decorated function with retry logic

    Example:
        @retry_async(max_attempts=3, backoff=2.0)
        async def scrape_profile(url: str):
            ...
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None

            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        wait_time = backoff**attempt
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_attempts} failed: {e}. "
                            f"Retrying in {wait_time:.1f}s..."
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(
                            f"All {max_attempts} attempts failed for {getattr(func, '__name__', repr(func))}"
                        )

            if last_exception:
                raise last_exception
            raise RuntimeError("Unexpected state in retry logic")

        return wrapper  # type: ignore[return-value]

    return decorator
