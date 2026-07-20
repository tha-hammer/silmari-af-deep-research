---
date: 2026-07-20T09:28:57-04:00
researcher: tha-hammer
git_commit: 7c2baa66b7343c8626e399c1aeb35f76a0c3be04
branch: feat/handoff-middleware
repository: silmari-af-deep-research
topic: "Search-provider availability and failure handling: where search failures are swallowed vs. propagated"
tags: [research, codebase, search, error-handling, fail-closed, providers, agentfield]
status: complete
last_updated: 2026-07-20
last_updated_by: tha-hammer
last_updated_note: "Added resolved design decisions from principal answers to the open questions"
---

# Research: Search-provider availability and failure handling in the deep-research pipeline

**Date**: 2026-07-20T09:28:57-04:00
**Researcher**: tha-hammer
**Git Commit**: 7c2baa66b7343c8626e399c1aeb35f76a0c3be04
**Branch**: feat/handoff-middleware
**Repository**: silmari-af-deep-research

## Research Question

Document how the codebase currently behaves when no search provider is available (or a
provider request fails), so the team can decide how to harden it toward a fail-closed error
that throws with the underlying API error or a descriptive error. This document maps the
current state only; it does not prescribe a change.

## Summary

The node exposes a unified web-search facade (`skills/search/`) whose provider classes and
top-level entry points **do raise** on failure (missing key → `ValueError`; no providers
configured → `RuntimeError`; HTTP/API error → `aiohttp.ClientResponseError` via
`response.raise_for_status()`). Those raises are **caught and converted to an empty list**
at exactly one active seam: `search_web_for_content` in `main.py:76-115`. Every failure
class — no providers, provider-not-available, and any HTTP/API error such as an out-of-balance
`402` — returns `[]` from that function (main.py:94-95, 110-112, 113-115).

An empty list then flows through the pipeline with **no code-level gate on source/evidence
count anywhere**: `execute_intelligence_stream_comprehensive` returns a `StreamOutput` with
empty `source_articles`/`article_evidence` (main.py:1493-1501); `prepare_research_package`
aggregates those empties and reports `total_sources: 0` / `final_quality_score: 0.0` in
metadata (main.py:1819-1836) without any `if`-branch on the count; `execute_deep_research`
passes the package straight into document generation (main.py:3133-3142); and
`generate_document_from_package_core` produces a `DocumentResponse` regardless (its two
"fallback" branches are both conditioned on non-empty facts, so they do **not** fire in the
fully-empty case — doc_generation_pipeline.py:833-851, 925-944). Continuation and quality
decisions are delegated entirely to LLM-schema calls (`ResearchQualityScore`, `LoopDecision`),
never to a Python threshold.

A second, independent search path exists: `utils.py:search_jina_ai` (its own Jina client,
utils.py:41-75) used by `reasoners/universal_reasoners.py:224,241`. It also swallows failures
to an empty `JinaSearchResponse` (utils.py:70-72 on non-200, utils.py:73-75 on any exception).
This path belongs to the `research_orchestrator.py` subsystem, which is **not** reached from the
UI entry point (`execute_deep_research`).

The AgentField framework already provides a fail-closed mechanism: a `@app.reasoner()` that
**raises** is reported to the control plane as `status="failed"` with the exception string as
the error message, then re-raised (decorators.py:459-496). A plain `return` — even a payload
that says failure — is always recorded `succeeded`; `agentfield/exceptions.py` documents this
and offers `ReasonerFailed(message, result=..., error_details=...)` for "the work ran but
failed" while preserving the structured result (exceptions.py:36-71). The repository's UI layer
already uses typed fail-closed exceptions extensively (`Denied`, `RepositoryUnavailable`,
`LaunchError`, `ControlPlaneUnreachable`, `BootstrapError`); the search/research pipeline does
not.

## Detailed Findings

### Search provider facade (`skills/search/`)

**Provider registry & availability detection.**
- `DEFAULT_PROVIDER_PRIORITY = ["jina", "tavily", "firecrawl", "serper"]` and
  `PROVIDER_CLASSES` map names to classes (registry.py:18-26).
- `SearchProvider.is_available()` returns `True` iff the API-key env var is a non-empty string —
  **key presence only, not health** (base.py:56-59). A configured-but-out-of-balance key
  reports available.
- `get_default_provider()` honors `SEARCH_PROVIDER` env: if set and its key is present it is
  returned unconditionally; the "try alternatives" fallthrough only runs when the preferred
  provider's key is **missing**, not when its request later fails (registry.py:70-94). With
  `SEARCH_PROVIDER=jina` and a present Jina key, Jina is always selected.
- `list_provider_status()` returns `{name: is_available}` for all four (registry.py:97-104).

**Where each layer raises.**
- Each provider `search()` raises `ValueError` when its key is missing and otherwise calls
  `response.raise_for_status()` (raises `aiohttp.ClientResponseError` on any non-2xx):
  jina.py:40-53, tavily.py:50-68, firecrawl.py:59-84, serper.py:61-78. None wrap the request
  in try/except.
- `search()` raises `RuntimeError("No search providers available...")` when
  `get_default_provider()` is `None`, else awaits `provider.search(query)` and lets the
  provider's exception propagate (`__init__.py:48-67`).
- `search_with_provider()` raises `ValueError` for unknown/unavailable provider
  (`__init__.py:70-89`).
- `parallel_search()` has its own graceful degradation: it gathers with
  `return_exceptions=True` and substitutes an empty `SearchResponse` per failed query
  (`__init__.py:118-134`). **This function is not on the active path** — `search_web_for_content`
  uses `search()`, not `parallel_search()`.

### The active swallow seam — `search_web_for_content` (main.py:76-115)

Sole active caller of the `skills.search` facade (`from skills.search import search,
list_provider_status`, main.py:85). Three failure paths, all returning `[]`:
- No providers available → early `return []` after a WARNING print (main.py:93-95).
- `except RuntimeError` (the facade's "no providers" raise) → `return []` (main.py:110-112).
- `except Exception` (any provider HTTP/API error, e.g. Jina `402`
  `InsufficientBalanceError`) → `return []` (main.py:113-115).

Only call site: `execute_intelligence_stream_comprehensive`, `search_tasks = [
search_web_for_content(query) for query in search_queries]` (main.py:1363). No other caller
exists in the repo.

### Downstream flow with an empty result (no gate)

- **`execute_intelligence_stream_comprehensive`** (main.py:1341-1501): flattened/deduped map
  is empty (main.py:1367-1371); the `source_articles` build loop never executes its body
  (main.py:1375-1387); evidence extraction over `[]` yields `[]` (main.py:1467-1476); returns
  `StreamOutput(source_articles=[], article_evidence=[], synthesized_intel={"evidence_count":
  0})` (main.py:1493-1501). `StreamOutput` (main.py:301-307) has no validators.
- **`prepare_research_package`** (main.py:1504-1836): per-iteration dedup extends
  `all_source_articles`/`all_article_evidence` with nothing (main.py:1636-1646); loop
  continuation is driven by `decide_iteration_continuation` (an LLM `LoopDecision` call,
  main.py:1180-1233) and `assess_research_completeness` (an LLM `ResearchQualityScore` call,
  main.py:989-1043) — the only Python branch on their output selects **note text** or `break`s
  on the LLM's boolean (main.py:1732-1746, 1770). Final metadata:
  `final_quality_score = quality_score.confidence_score if quality_score else 0.0`,
  `total_sources = len(all_source_articles)` — both emitted unconditionally, `0` is a legal
  value (main.py:1819-1836).
- **`execute_deep_research`** (main.py:3038-3159): no check between Phase 1 and Phase 2;
  `generate_document_from_package(package=research_response.research_package, ...)` is called
  unconditionally (main.py:3133-3142).
- **`generate_document_from_package` → `_core`** (main.py:2996-3030 →
  doc_generation_pipeline.py:718-1081): with empty `article_evidence`, `facts_to_assess` and
  `adjudicated_facts` are `[]`. The over-restrictive-adjudication fallback requires
  `facts_to_assess` truthy (doc_generation_pipeline.py:833-851) and the "ensure one section"
  fallback requires `adjudicated_facts` truthy (doc_generation_pipeline.py:925-944) — **neither
  fires** in the fully-empty case. Section planners still run on the LLM `research_digest`
  (`core_thesis`, `key_discoveries`, `<implications>`; doc_generation_pipeline.py:230-246,
  884-912), so sections can be planned with empty `evidence_to_use`; the section writer's
  citation binding comes only from `evidence_to_use` (doc_generation_pipeline.py:431-436,
  978-995). A `DocumentResponse` is returned regardless, with `sections` and `source_notes`
  possibly empty (doc_generation_pipeline.py:1072-1081).

This reconciles the observed run (`run_20260717_214240_s83r9rkx`, control-plane execution
`exec_20260717_214240_0q09m386`): `total_sources: 0` across 8 iterations, `final_quality_score:
0.1`, `source_notes: []` (0 citations), yet `sections: [23]` — planners drew on the LLM digest
while the fact pool was empty.

### Second, separate search path — `utils.py:search_jina_ai`

- `search_jina_ai` (utils.py:41-75) is a standalone Jina client that catches all failures:
  missing key → empty `JinaSearchResponse` + warning (utils.py:44-47); non-200 → empty +
  `logger.error` (utils.py:70-72); any exception → empty + `logger.error` (utils.py:73-75).
- Imported at `reasoners/universal_reasoners.py:30`; used inside `research_execution_reasoner`
  (universal_reasoners.py:224, 241). These reasoners are assembled by
  `create_universal_reasoners` and driven by `reasoners/research_orchestrator.py`
  (`dynamic_research_orchestrator` / `parallel_research_orchestrator`), which the UI entry point
  `execute_deep_research` does not call.

### Framework behavior for a raised reasoner exception (the fail-closed substrate)

- `@app.reasoner()` (agentfield/decorators.py) wraps the function; on the success path it
  `await func(...)`, reports completion, returns (decorators.py:459-479). On `except Exception
  as exc` it stamps duration, generates an error VC, calls `_send_workflow_error(..., str(exc),
  ...)`, and **re-raises** (decorators.py:480-496).
- `_send_workflow_error` posts `status="failed"` with the exception string as `error` to the
  control plane (decorators.py:657-696; `_compose_event_payload` sets `"status": status`,
  decorators.py:528-549).
- `agentfield/exceptions.py`: `ReasonerFailed(message, *, result=None, error_details=None)` is
  the documented way to report "work ran but failed" while preserving a structured `result`;
  its docstring states that a plain `return` (even `success: False`) is recorded `succeeded`
  because the handler only distinguishes "returned" from "raised" (exceptions.py:36-71). Also
  present: `ExecutionFailedError` (exceptions.py:17-33), `AgentFieldClientError`,
  `ValidationError`, timeout/cancel variants.

### Existing fail-closed / typed-error patterns in the repo (for reference)

- **Search providers**: required-env-var gate, identical in all four
  (jina.py:39-41, tavily.py:49-51, firecrawl.py:58-60, serper.py:60-62).
- **UI launch**: typed exceptions `LaunchError`, `ControlPlaneUnreachable`,
  `LaunchResponseInvalid` (ui/launch_adapter.py:31-39) raised on required-field/transport/shape
  failures (ui/launch_adapter.py:66-80) and mapped to HTTP status at the route boundary
  (ui/app.py:425-554: `Denied→403`, `RepositoryUnavailable→503`, `LaunchError→400`,
  `ControlPlaneUnreachable/LaunchResponseInvalid→502`, `Conflict→409`, `NotFound→404`).
- **Tenancy/identity**: fail-closed lookups `Denied` (ui/tenancy/context.py:66-77),
  `assert_run_access` (ui/workspace/research_run.py:206-210), resolve-before-write
  `BootstrapError` (ui/tenancy/identity.py:54-84).
- **Event contract**: status precondition raise (research_completed_event.py:94-100).
- **Dataclass invariants**: nine `raise ValueError` in `ResearchRunRef.__post_init__`
  (ui/workspace/research_run.py:111-131).
- **Doc-pipeline placeholder**: `ai_with_dynamic_params` raises `RuntimeError` if a real caller
  isn't injected (doc_generation_pipeline.py:23-30).

## Code References

- `skills/search/base.py:56-59` — `is_available()` = key presence only (not health).
- `skills/search/registry.py:70-94` — `get_default_provider()` + `SEARCH_PROVIDER` forcing.
- `skills/search/__init__.py:48-67` — `search()` raises `RuntimeError` if no provider, else
  propagates provider exception.
- `skills/search/{jina,tavily,firecrawl,serper}.py` — `ValueError` on missing key;
  `response.raise_for_status()` on HTTP error (jina.py:40-53, serper.py:61-78, tavily.py:50-68,
  firecrawl.py:59-84).
- `main.py:76-115` — `search_web_for_content`: **all failure paths return `[]`** (94-95,
  110-112, 113-115).
- `main.py:1363` — sole call site (`execute_intelligence_stream_comprehensive`).
- `main.py:1493-1501` — `StreamOutput` returned with empty lists.
- `main.py:1819-1836` — package metadata `total_sources`/`final_quality_score`, no gate.
- `main.py:3133-3142` — document generation called unconditionally.
- `doc_generation_pipeline.py:833-851, 925-944` — fallbacks conditioned on non-empty facts.
- `utils.py:41-75` — separate `search_jina_ai`, swallows to empty response.
- `.venv/.../agentfield/decorators.py:459-496` — raise → `status="failed"` + re-raise.
- `.venv/.../agentfield/exceptions.py:36-71` — `ReasonerFailed`; plain-return-is-succeeded note.

## Architecture Documentation

- **Two-layer search design**: a raising provider facade (`skills/search/`) beneath a
  swallowing helper (`main.py:search_web_for_content`). The raise/no-raise boundary is the
  helper, not the providers.
- **LLM-delegated control flow**: iteration continuation and quality assessment are LLM-schema
  outputs (`LoopDecision`, `ResearchQualityScore`), not Python thresholds; no numeric source/
  evidence count alters control flow.
- **Framework fail-closed idiom**: raising from a reasoner (or `ReasonerFailed`) yields a
  control-plane `status="failed"`; returning always yields `succeeded`. The UI layer already
  models fail-closed via typed exceptions mapped to HTTP codes; the pipeline layer does not.
- **Env config** (Railway `silmari-deep-research`): `SEARCH_PROVIDER=jina`, `JINA_API_KEY` and
  `SERPER_API_KEY` present; `DEFAULT_MODEL=openrouter/anthropic/claude-sonnet-4`.

## Workflow Closure Map

Behavior (as a promise): *a web-search failure surfaces on the run record as a failed
execution, instead of a succeeded, source-less document.* This is an error-propagation /
observability path, not a DB write→read workflow; the SOURCE is an HTTP boundary (the provider
response) and the OBSERVABLE is the control-plane execution record read back by the UI. The map
below documents the **current** chain, where the belt is open at the swallow seam.

Nodes (SOURCE → OBSERVABLE) and per-edge evidence:

| # | Node | Module | Label | Notes |
|---|------|--------|-------|-------|
| 0 | search provider response | `skills/search/__init__.py:48` (`search()`) | production-called | raises on missing-key/HTTP error; conceptual seedable source (HTTP, not DB) |
| 1 | `search_web_for_content` | `main.py:76` | production-called | **swallow seam**: raises→`[]` (main.py:110-115). Edge 0→1 drops the error. |
| 2 | `execute_intelligence_stream_comprehensive` | `main.py:1341` | production-called | empty search → `StreamOutput(source_articles=[])` (main.py:1493-1501) |
| 3 | `prepare_research_package` | `main.py:1504` | production-called | aggregates empties → `total_sources:0` (main.py:1819-1836); no gate |
| 4 | `execute_deep_research` | `main.py:3038` | production-called, entrypoint | UI-launched reasoner; calls doc-gen unconditionally (main.py:3133) |
| 5 | run execution record | `ui/server.py:251` (`result_for`) | production-called | observable: status + `DocumentResponse`; read via `cp_get("/executions/{eid}")` |

Edges (all synchronous):
- 0→1: provider exception / RuntimeError → **swallowed to `[]`** at main.py:110-115 (belt open here).
- 1→2: `[]` → empty `source_articles`/`article_evidence` (main.py:1367-1387).
- 2→3: empty `StreamOutput` aggregated with no count check (main.py:1636-1646).
- 3→4: package with `total_sources:0` passed through (main.py:3133-3142).
- 4→5: `DocumentResponse` recorded; reasoner returned (not raised) → framework records
  `status="succeeded"` (decorators.py:459-479).

Error behavior across the chain: no retries, no dead-letter, no raise; the only signal of a
zero-source run is the `total_sources`/`final_quality_score` metadata values, which are not
consulted by any conditional.

Tests exercising this exact edge: none found. `tests/` covers producer events, UI, and
migration; there are no tests over `skills/search/` or `search_web_for_content`.

`highest_new_connector`: `search_web_for_content` (main.py:76) — the topmost node a hardening
slice would change (the swallow seam). Downstream a gate node in `prepare_research_package` /
`execute_deep_research` would also change.

### ClosureMap (structured — derive() input)

```json
{
  "behavior": "A web-search failure surfaces on the run record as a failed execution instead of a succeeded, source-less document.",
  "git_commit": "7c2baa66b7343c8626e399c1aeb35f76a0c3be04",
  "repo": "/home/maceo/ntm_Dev/silmari-agentfield-system/silmari-af-deep-research",
  "nodes": [
    { "id": "provider_response", "module": "skills/search", "is_entrypoint": false, "adds_or_changes": false, "read_path": null, "seedable_store": "search_provider_response_http" },
    { "id": "search_web_for_content", "module": "main.py", "is_entrypoint": false, "adds_or_changes": true, "read_path": null, "seedable_store": null },
    { "id": "intelligence_stream", "module": "main.py", "is_entrypoint": false, "adds_or_changes": false, "read_path": null, "seedable_store": null },
    { "id": "prepare_research_package", "module": "main.py", "is_entrypoint": false, "adds_or_changes": true, "read_path": null, "seedable_store": null },
    { "id": "execute_deep_research", "module": "main.py", "is_entrypoint": true, "adds_or_changes": false, "read_path": null, "seedable_store": null },
    { "id": "run_execution_record", "module": "ui/server.py", "is_entrypoint": false, "adds_or_changes": false, "read_path": "result_for", "seedable_store": null }
  ],
  "edges": [
    { "is_async": false, "cross_boundary": true, "driver": null },
    { "is_async": false, "cross_boundary": false, "driver": null },
    { "is_async": false, "cross_boundary": false, "driver": null },
    { "is_async": false, "cross_boundary": false, "driver": null },
    { "is_async": false, "cross_boundary": true, "driver": null }
  ]
}
```

Notes on schema fidelity: `seedable_store` on the SOURCE node names an HTTP boundary
(`search_provider_response_http`), not a DB table — the closure test seeds it by substituting
the provider response (see staged adapter `/seed`). `read_path` on the OBSERVABLE node is
`result_for` (ui/server.py:251), the UI's production read of a run's status + document. Two
edges cross a registration boundary (provider→helper import; reasoner→control-plane report).

### Closure adapter (staged proposal — `2026-07-20-09-28-search-provider-failclosed.closure-adapter.py`)

Staged read-only as a sibling file next to this document (not wired into the repo). It maps the
oracle's 7 ops onto the resolved production symbols above; every production call is a
`TODO(promote)` naming the symbol + `file:line`. The map has ≥2 nodes (trigger
`execute_deep_research`/`execute_intelligence_stream_comprehensive` and observe `result_for`),
so an adapter is staged. `/seed` installs a fake provider response (an exception for the
red-at-seam case); there are no async edges, so `/drive` is a no-op.

## Historical Context (from thoughts/)

- `thoughts/searchable/shared/research/2026-07-11-09-02-deep-research-step-observability-gap.md`
  — prior research on step observability in the deep-research pipeline.
- `thoughts/searchable/shared/research/2026-07-12-11-46-deep-research-doc-pipeline-notes-trace-steps.md`
  — prior research on the doc-generation pipeline's notes/trace steps.
- `thoughts/searchable/shared/research/2026-07-13-09-21-research-to-reels-handoff-seams.md`
  — research→reels handoff seams (consumes `DocumentResponse`).

## Related Research

- Live diagnosis of `run_20260717_214240_s83r9rkx` (this session): Jina returned HTTP `402
  InsufficientBalanceError`; `SEARCH_PROVIDER=jina` prevented Serper fallback; the run produced
  a 23-section, 0-citation report with `total_sources:0`.

## Design Decisions (resolved 2026-07-20 by tha-hammer)

The open questions below were answered by the principal. These are target decisions for a
hardening slice; the sections above remain the current-state map.

1. **Fail closed at both seams.** Signal at `search_web_for_content` (stop returning `[]` for
   non-recoverable failures) **and** add a per-run zero-source gate before document generation.
   The two have different blast radii (per-query vs. per-run) and both are wanted.
2. **Classify errors: transient retry, non-recoverable fail hard.** Transient provider errors
   (timeouts, connection resets, HTTP 5xx, and rate-limit responses that carry a retry hint)
   retry with bounded backoff. Non-recoverable errors — misconfiguration (no providers /
   missing key), no credits (Jina `402 InsufficientBalanceError`), and other no-op/auth errors
   — do not retry; they fail hard (they are surfaced, not swallowed).
3. **Preserve the partial result.** Use `ReasonerFailed(message, result=...)`
   (agentfield/exceptions.py:36-71) so the run is recorded `status="failed"` while the partial
   payload (whatever entities/notes/streams were gathered) is retained on the execution record.
4. **Degrade to priority-ordered fallback on request failure.** When the forced/selected
   provider fails at request time (not just when its key is missing), fall through to the next
   available provider in `DEFAULT_PROVIDER_PRIORITY` (registry.py:18) before giving up. This
   changes `get_default_provider()`/the call path so a request-time failure — not only a
   missing key — triggers fallthrough (registry.py:70-94).
5. **Terminal failure floor: all iterations yield zero sources, with partial failures in the
   record.** Start with the run-level gate: after the iteration loop, if `total_sources == 0`
   (main.py:1819-1836), fail hard via `ReasonerFailed`, embedding the recorded per-provider /
   per-query failures in the result payload. Individual mid-run provider errors are recorded and
   drive retry/fallback (decisions 2 and 4) but do not necessarily abort the whole run
   mid-iteration.
6. **`utils.py:search_jina_ai` scope — UNRESOLVED (principal deferred).** Documented state: this
   path is on the `research_orchestrator.py` / `universal_reasoners.py` subsystem and is **not**
   reached from the UI entry point `execute_deep_research`, so it does not affect production
   runs today. It is a latent second swallow point (utils.py:70-75). Decision pending; see Open
   Question below.

### Reconciliation note (decisions 2 vs. 5)

Decisions 2 ("no credits / misconfig fail hard") and 5 ("fail when all iterations yield zero
sources") meet at one point and are reconciled as:

- **Zero search *capability*** — every provider is misconfigured / out of credits / auth-failed,
  so no provider can ever return results — is knowable early and fails hard **immediately** via
  `ReasonerFailed` (do not run 8 empty iterations against a dead provider set). This is the
  "misconfig / no credits fail hard" case from decision 2.
- **Capability exists but found nothing** — providers responded but returned zero usable
  sources across all iterations — fails hard at the **terminal** zero-source gate from decision
  5, carrying the recorded failures/empties in the result payload.

The single non-recoverable error that eliminates the *last* remaining provider is what flips a
run from "degrade and continue" (decision 4 fallback) to "fail hard now."

## Open Questions

- **Scope of `utils.py:search_jina_ai`** (decision 6 deferred): apply the same fail-closed
  treatment now, leave it untouched, or file a tracking issue to handle it if/when the
  `research_orchestrator.py` subsystem is revived? It is dead relative to the UI entry point
  today, so it does not block the production hardening slice.
