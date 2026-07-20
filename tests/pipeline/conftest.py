from types import SimpleNamespace
from typing import Any, Callable, Iterator, List

import pytest
from agentfield import agent_registry  # type: ignore[import-untyped]

import main


@pytest.fixture(autouse=True)
def detached_agentfield_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Drive reasoners in-process without control-plane side effects."""
    previous_agent = agent_registry.get_current_agent_instance()
    agent_registry.clear_current_agent()
    for name, candidate in vars(main).copy().items():
        original = getattr(candidate, "_original_func", None)
        if original is not None:
            monkeypatch.setattr(main, name, original)
    monkeypatch.setattr(main.app, "note", lambda message: None)
    try:
        yield
    finally:
        if previous_agent is not None:
            agent_registry.set_current_agent(previous_agent)
        else:
            agent_registry.clear_current_agent()


@pytest.fixture
def spy_iterations(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    spy = SimpleNamespace(count=0)

    async def count_iteration(*args: Any, **kwargs: Any) -> None:
        spy.count += 1
        raise AssertionError("research iteration should not run")

    monkeypatch.setattr(
        main,
        "execute_intelligence_stream_comprehensive",
        count_iteration,
    )
    return spy


@pytest.fixture
def fake_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], List[str]]:
    executive_summary = "A grounded executive summary."
    payloads = {
        "QueryClassification": {
            "query_type": "Entity_Analysis",
            "core_subject": "subject",
            "key_question": "question",
        },
        "AdaptiveSearchStreams": {
            "streams": [
                {
                    "stream_name": "direct",
                    "search_queries": ["q1"],
                    "analysis_focus": "primary evidence",
                }
            ]
        },
        "ComprehensiveEvidence": {
            "relevance_summary": "Relevant evidence",
            "facts": ["A grounded fact"],
            "quotes": [],
        },
        "EntityBatchResult": {"entities": []},
        "IterativeRelationshipResult": {
            "relationships": [],
            "has_more_relationships": False,
            "confidence_in_completion": 1.0,
            "iteration_notes": "complete",
        },
        "MetaDiscoveries": {"key_discoveries": ["A discovery"]},
        "ResearchHypothesis": {
            "core_thesis": "A grounded thesis",
            "supporting_strengths": ["Evidence supports the thesis"],
            "counter_risks": [],
            "key_unknowns": [],
        },
        "ResearchQualityScore": {
            "confidence_score": 0.8,
            "evidence_adequacy": "sufficient",
            "critical_gaps_present": False,
        },
        "GapBatch": {"gaps": []},
        "LoopDecision": {
            "should_continue": False,
            "termination_reason": "sufficient evidence",
            "focus_areas": "",
        },
        "AdaptiveInquiryProbes": {"probes": []},
        "AIAssessmentList": {"assessments": []},
        "ThematicBlueprint": {
            "document_title": "Grounded Report",
            "themes": [
                {
                    "theme_title": "Evidence",
                    "planning_directive": "Present the grounded evidence",
                }
            ],
        },
        "PlanOnlySimple": {
            "plan": [
                {
                    "section_title": "Findings",
                    "writing_instructions": "Summarize the evidence",
                    "evidence_to_use": [1],
                }
            ]
        },
        "AIWriterOutput": {
            "markdown_content": "The grounded finding is supported [1]."
        },
        "FinalAssemblyInput": {"executive_summary": executive_summary},
        "DisclaimerList": {"disclaimers": []},
    }

    def install() -> List[str]:
        calls: List[str] = []

        async def call(*args: Any, **kwargs: Any) -> Any:
            schema = kwargs["schema"]
            schema_name = schema.__name__
            calls.append(schema_name)
            if schema_name not in payloads:
                message = f"No fake AI payload for schema {schema_name}"
                raise AssertionError(message)
            return schema.model_validate(payloads[schema_name])

        monkeypatch.setattr(main, "ai_with_dynamic_params", call)
        return calls

    return install
