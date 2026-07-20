import asyncio
from typing import Callable, Sequence

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from skills.search import SearchResponse, SearchResult, search, search_sync
from skills.search.errors import SearchProvidersExhausted


async def _noop(_: float) -> None:
    return None


class Returns:
    def __init__(self, urls: Sequence[str]) -> None:
        self.urls = urls
        self.calls = 0

    async def search(self, query: str) -> SearchResponse:
        self.calls += 1
        results = [
            SearchResult(
                title=f"Result {url}",
                url=url,
                content=f"Content for {url}",
                description=None,
                published_time=None,
            )
            for url in self.urls
        ]
        return SearchResponse(
            results=results,
            total_results=len(results),
            query_used=query,
            provider="fake",
        )


class AlwaysRaises:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def search(self, query: str) -> SearchResponse:
        self.calls += 1
        raise self.error


class TransientThenReturns(Returns):
    def __init__(self, failures: int, urls: Sequence[str]) -> None:
        super().__init__(urls)
        self.failures = failures

    async def search(self, query: str) -> SearchResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("temporary timeout")
        self.calls -= 1
        return await super().search(query)


def _http_error(status: int) -> aiohttp.ClientResponseError:
    url = URL("https://provider.example/search")
    headers = CIMultiDictProxy(CIMultiDict())
    request_info = aiohttp.RequestInfo(url, "GET", headers, url)
    return aiohttp.ClientResponseError(
        request_info, (), status=status, message=f"status {status}"
    )


def test_no_available_providers_raises_exhausted(no_providers: None) -> None:
    with pytest.raises(SearchProvidersExhausted) as error_info:
        asyncio.run(search("q", sleep=_noop))

    assert error_info.value.provider_errors == [
        ("<none>", "No search providers configured")
    ]


def test_falls_back_to_next_provider_on_non_recoverable(
    fake_providers: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = AlwaysRaises(_http_error(402))
    second = Returns(["r1"])
    fake_providers(a=first, b=second)

    response = asyncio.run(search("q", sleep=_noop))

    assert [result.url for result in response.results] == ["r1"]
    assert first.calls == 1
    assert second.calls == 1
    fallback_log = capsys.readouterr().out
    assert "a" in fallback_log
    assert "status 402" in fallback_log


def test_retries_transient_then_succeeds(
    fake_providers: Callable[..., None],
) -> None:
    provider = TransientThenReturns(failures=2, urls=["ok"])
    fake_providers(a=provider)
    delays = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    response = asyncio.run(search("q", sleep=record_sleep))

    assert provider.calls == 3
    assert delays == [1.0, 2.0]
    assert [result.url for result in response.results] == ["ok"]


def test_transient_exhaustion_falls_back(
    fake_providers: Callable[..., None],
) -> None:
    first = AlwaysRaises(TimeoutError("still unavailable"))
    second = Returns(["fallback"])
    fake_providers(a=first, b=second)

    response = asyncio.run(search("q", sleep=_noop, max_retries=2))

    assert first.calls == 3
    assert [result.url for result in response.results] == ["fallback"]


def test_all_non_recoverable_errors_raise_exhausted(
    fake_providers: Callable[..., None],
) -> None:
    fake_providers(
        a=AlwaysRaises(_http_error(402)),
        b=AlwaysRaises(_http_error(401)),
    )

    with pytest.raises(SearchProvidersExhausted) as error_info:
        asyncio.run(search("q", sleep=_noop))

    assert [name for name, _ in error_info.value.provider_errors] == ["a", "b"]
    assert "status 402" in error_info.value.provider_errors[0][1]
    assert "status 401" in error_info.value.provider_errors[1][1]
    assert "status 402" in str(error_info.value)
    assert "status 401" in str(error_info.value)


def test_search_provider_env_forces_selection(
    monkeypatch: pytest.MonkeyPatch,
    fake_providers: Callable[..., None],
) -> None:
    first = Returns(["from_a"])
    second = Returns(["from_b"])
    fake_providers(a=first, b=second)

    monkeypatch.setenv("SEARCH_PROVIDER", " B ")
    forced_response = asyncio.run(search("q", sleep=_noop))
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    priority_response = asyncio.run(search("q", sleep=_noop))

    assert [result.url for result in forced_response.results] == ["from_b"]
    assert [result.url for result in priority_response.results] == ["from_a"]


def test_unavailable_forced_provider_keeps_priority_order(
    monkeypatch: pytest.MonkeyPatch,
    fake_providers: Callable[..., None],
) -> None:
    fake_providers(a=Returns(["from_a"]), b=Returns(["from_b"]))
    monkeypatch.setenv("SEARCH_PROVIDER", "missing")

    response = asyncio.run(search("q", sleep=_noop))

    assert [result.url for result in response.results] == ["from_a"]


def test_search_sync_does_not_repeat_exhausted_search(
    fake_providers: Callable[..., None],
) -> None:
    provider = AlwaysRaises(_http_error(402))
    fake_providers(a=provider)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        with pytest.raises(SearchProvidersExhausted):
            search_sync("q")
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert provider.calls == 1


def test_negative_retry_bound_is_rejected(
    fake_providers: Callable[..., None],
) -> None:
    provider = Returns(["unused"])
    fake_providers(a=provider)

    with pytest.raises(ValueError, match="max_retries"):
        asyncio.run(search("q", sleep=_noop, max_retries=-1))

    assert provider.calls == 0
