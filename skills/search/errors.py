"""Typed failures and retry classification for search providers."""

import asyncio
from enum import Enum
from typing import List, Sequence, Tuple

import aiohttp


class Transience(Enum):
    """Whether retrying a failed provider request may succeed."""

    TRANSIENT = "transient"
    NON_RECOVERABLE = "non_recoverable"


class SearchProvidersExhausted(RuntimeError):
    """Raised after every configured search provider has failed."""

    def __init__(self, provider_errors: Sequence[Tuple[str, str]]) -> None:
        self.provider_errors: List[Tuple[str, str]] = list(provider_errors)
        details = "; ".join(
            f"{provider}: {error}" for provider, error in self.provider_errors
        )
        super().__init__(f"Search providers exhausted: {details}")


_TRANSIENT_HTTP_STATUSES = frozenset({408, 429})


def classify_search_error(exc: BaseException) -> Transience:
    """Classify provider failures, defaulting unknown errors to fail-closed."""
    if isinstance(
        exc,
        (TimeoutError, asyncio.TimeoutError, aiohttp.ClientConnectionError),
    ):
        return Transience.TRANSIENT
    if isinstance(exc, aiohttp.ClientResponseError):
        if exc.status in _TRANSIENT_HTTP_STATUSES or 500 <= exc.status <= 599:
            return Transience.TRANSIENT
    return Transience.NON_RECOVERABLE
