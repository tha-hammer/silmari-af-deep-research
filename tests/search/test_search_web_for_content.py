import asyncio
import importlib
from typing import List

import pytest

from main import SearchUnavailable, search_web_for_content
from skills.search import SearchResponse, SearchResult
from skills.search.errors import SearchProvidersExhausted

search_module = importlib.import_module("skills.search")


def _show_stub_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_module,
        "list_provider_status",
        lambda: {"stub": True},
    )


def test_raises_on_providers_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exhausted = SearchProvidersExhausted([("jina", "402 Payment Required")])

    async def raise_exhausted(query: str) -> SearchResponse:
        raise exhausted

    _show_stub_provider(monkeypatch)
    monkeypatch.setattr(search_module, "search", raise_exhausted)

    with pytest.raises(SearchUnavailable) as error_info:
        asyncio.run(search_web_for_content("q"))

    assert error_info.value.provider_errors == exhausted.provider_errors
    assert error_info.value.__cause__ is exhausted
    assert "402 Payment Required" in str(error_info.value)


def test_returns_empty_on_genuine_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def return_empty(query: str) -> SearchResponse:
        return SearchResponse(
            results=[],
            total_results=0,
            query_used=query,
            provider="stub",
        )

    _show_stub_provider(monkeypatch)
    monkeypatch.setattr(search_module, "search", return_empty)

    assert asyncio.run(search_web_for_content("q")) == []


def test_maps_successful_results_to_content_dicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def return_results(query: str) -> SearchResponse:
        return SearchResponse(
            results=[
                SearchResult(
                    title="Result",
                    url="https://example.test/result",
                    content="Evidence",
                    description="Description",
                    published_time=None,
                )
            ],
            total_results=1,
            query_used=query,
            provider="stub",
        )

    _show_stub_provider(monkeypatch)
    monkeypatch.setattr(search_module, "search", return_results)

    results: List[dict] = asyncio.run(search_web_for_content("q"))

    assert results == [
        {
            "url": "https://example.test/result",
            "title": "Result",
            "content": "Evidence",
            "description": "Description",
        }
    ]
