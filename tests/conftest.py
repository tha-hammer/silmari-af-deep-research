from typing import Any, Callable, Iterator, Type, cast

import pytest

from skills.search.base import SearchProvider, SearchResponse
from skills.search.registry import (
    DEFAULT_PROVIDER_PRIORITY,
    PROVIDER_CLASSES,
    register_provider,
)

# The upstream scaffold tests/test_agent.py imports a non-existent `agent`
# package (leftover from the af-deep-research template) and errors at
# collection. It is unrelated to the ui/ persistence work; ignore it so
# tests/ui/ collects cleanly.
collect_ignore = ["test_agent.py"]


_REAL_PROVIDER_KEYS = (
    "JINA_API_KEY",
    "TAVILY_API_KEY",
    "FIRECRAWL_API_KEY",
    "SERPER_API_KEY",
)


def _fake_provider_class(name: str, behavior: Any) -> Type[SearchProvider]:
    class FakeProvider(SearchProvider):
        @property
        def name(self) -> str:
            return name

        @property
        def api_key_env_var(self) -> str:
            return f"FAKE_{name.upper()}_API_KEY"

        def is_available(self) -> bool:
            return True

        async def search(self, query: str) -> SearchResponse:
            return cast(SearchResponse, await behavior.search(query))

    FakeProvider.__name__ = f"{name.title()}FakeProvider"
    return FakeProvider


@pytest.fixture
def isolated_search_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    provider_classes = dict(PROVIDER_CLASSES)
    provider_priority = list(DEFAULT_PROVIDER_PRIORITY)

    PROVIDER_CLASSES.clear()
    DEFAULT_PROVIDER_PRIORITY.clear()
    for env_var in (*_REAL_PROVIDER_KEYS, "SEARCH_PROVIDER"):
        monkeypatch.delenv(env_var, raising=False)

    try:
        yield
    finally:
        PROVIDER_CLASSES.clear()
        PROVIDER_CLASSES.update(provider_classes)
        DEFAULT_PROVIDER_PRIORITY[:] = provider_priority


@pytest.fixture
def register_fake(
    isolated_search_registry: None,
) -> Callable[[str, Any], None]:
    def register(name: str, behavior: Any) -> None:
        register_provider(name, _fake_provider_class(name, behavior))

    return register


@pytest.fixture
def fake_providers(
    register_fake: Callable[[str, Any], None],
) -> Callable[..., None]:
    def register_all(**providers: Any) -> None:
        for name, behavior in providers.items():
            register_fake(name, behavior)

    return register_all


@pytest.fixture
def no_providers(isolated_search_registry: None) -> None:
    return None
