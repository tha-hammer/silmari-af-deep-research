import asyncio
from typing import Callable, List

import pytest
from agentfield import ReasonerFailed  # type: ignore[import-untyped]

import main
from skills.search import SearchResponse, SearchResult


class EmptyResults:
    async def search(self, query: str) -> SearchResponse:
        return SearchResponse(
            results=[],
            total_results=0,
            query_used=query,
            provider="stub",
        )


class ReturnsSource:
    async def search(self, query: str) -> SearchResponse:
        result = SearchResult(
            title="Real source",
            url="https://real.example/1",
            content="Grounded source content",
            description="Evidence",
            published_time=None,
        )
        return SearchResponse(
            results=[result],
            total_results=1,
            query_used=query,
            provider="stub",
        )


def test_zero_sources_fails_closed(
    register_fake: Callable[[str, object], None],
    fake_ai: Callable[[], List[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_calls = fake_ai()
    register_fake("stub", EmptyResults())
    monkeypatch.setenv("SEARCH_PROVIDER", "stub")

    with pytest.raises(ReasonerFailed) as error_info:
        asyncio.run(
            main.execute_deep_research(
                query="q",
                max_research_loops=2,
                num_parallel_streams=1,
            )
        )

    assert error_info.value.result["total_sources"] == 0
    assert error_info.value.result["provider_errors"] == []
    assert "provider_errors" in error_info.value.result
    assert "zero sources" in str(error_info.value).lower()
    assert "ThematicBlueprint" not in ai_calls


def test_red_at_seam_control_returns_cited_document(
    register_fake: Callable[[str, object], None],
    fake_ai: Callable[[], List[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ai()
    register_fake("stub", ReturnsSource())
    monkeypatch.setenv("SEARCH_PROVIDER", "stub")

    response = asyncio.run(
        main.execute_deep_research(
            query="q",
            max_research_loops=1,
            num_parallel_streams=1,
        )
    )

    assert isinstance(response, main.DocumentResponse)
    assert response.research_package["source_notes"] == [
        {
            "citation_id": 1,
            "title": "Real source",
            "domain": "real.example",
            "url": "https://real.example/1",
        }
    ]
