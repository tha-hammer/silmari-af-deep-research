import asyncio
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

import pytest
from agentfield import ReasonerFailed  # type: ignore[import-untyped]

import main
from main import QueryClassification
from skills.search import SearchResponse


class RaisesProviderError:
    async def search(self, query: str) -> SearchResponse:
        raise ValueError("provider credentials rejected")


class AvailableButUnused:
    async def search(self, query: str) -> SearchResponse:
        raise AssertionError("provider should not be called directly")


def test_zero_capability_fails_before_iterating_or_classifying(
    no_providers: None,
    spy_iterations: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_classification(
        *args: Any, **kwargs: Any
    ) -> QueryClassification:
        raise AssertionError("classification should not run without search")

    classifier_name = "classify_query_adaptive"
    monkeypatch.setattr(main, classifier_name, unexpected_classification)

    with pytest.raises(ReasonerFailed) as error_info:
        research_kwargs = {"query": "q", "max_research_loops": 3}
        research = main.prepare_research_package(**research_kwargs)
        asyncio.run(research)

    assert spy_iterations.count == 0
    assert error_info.value.result["total_sources"] == 0
    assert error_info.value.result["provider_errors"] == [
        ("<none>", "No search providers configured")
    ]
    assert "search capability" in str(error_info.value).lower()


def test_mid_iteration_capability_failure_preserves_provider_errors(
    register_fake: Callable[[str, object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_fake("stub", RaisesProviderError())

    async def classify(*args: Any, **kwargs: Any) -> QueryClassification:
        return QueryClassification(
            query_type="Entity_Analysis",
            core_subject="subject",
            key_question="question",
        )

    async def streams(*args: Any, **kwargs: Any) -> List[Dict[str, object]]:
        return [
            {
                "stream_name": "direct",
                "search_queries": ["q"],
                "analysis_focus": "focus",
            }
        ]

    monkeypatch.setattr(main, "classify_query_adaptive", classify)
    monkeypatch.setattr(main, "generate_adaptive_search_streams", streams)

    with pytest.raises(ReasonerFailed) as error_info:
        asyncio.run(
            main.prepare_research_package(
                query="q",
                max_research_loops=1,
                num_parallel_streams=1,
            )
        )

    assert error_info.value.result["total_sources"] == 0
    assert error_info.value.result["provider_errors"]
    assert error_info.value.result["provider_errors"][0][0] == "stub"
    assert "provider credentials rejected" in str(error_info.value)


def test_later_stream_failure_counts_completed_stream_sources(
    register_fake: Callable[[str, object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_fake("stub", AvailableButUnused())

    async def classify(*args: Any, **kwargs: Any) -> QueryClassification:
        return QueryClassification(
            query_type="Entity_Analysis",
            core_subject="subject",
            key_question="question",
        )

    async def streams(*args: Any, **kwargs: Any) -> List[Dict[str, object]]:
        return [
            {
                "stream_name": "first",
                "search_queries": ["q1"],
                "analysis_focus": "first",
            },
            {
                "stream_name": "second",
                "search_queries": ["q2"],
                "analysis_focus": "second",
            },
        ]

    calls = 0

    async def execute_stream(*args: Any, **kwargs: Any) -> main.StreamOutput:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise main.SearchUnavailable(
                "late provider failure",
                provider_errors=[("stub", "late provider failure")],
            )
        article = main.Article(
            id=1,
            title="Partial source",
            url="https://partial.example/1",
            content="Partial evidence",
            content_hash=main.create_content_hash("Partial evidence"),
        )
        return main.StreamOutput(
            stream_type="first",
            synthesized_intel={},
            source_articles=[article],
            article_evidence=[],
        )

    monkeypatch.setattr(main, "classify_query_adaptive", classify)
    monkeypatch.setattr(main, "generate_adaptive_search_streams", streams)
    monkeypatch.setattr(
        main, "execute_intelligence_stream_comprehensive", execute_stream
    )

    with pytest.raises(ReasonerFailed) as error_info:
        asyncio.run(
            main.prepare_research_package(
                query="q",
                max_research_loops=1,
                num_parallel_streams=2,
            )
        )

    assert error_info.value.result["total_sources"] == 1
    assert error_info.value.result["provider_errors"] == [
        ("stub", "late provider failure")
    ]
