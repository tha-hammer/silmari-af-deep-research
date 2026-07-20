# Plan Review: Search-Provider Fail-Closed Hardening

**Plan reviewed**: `thoughts/searchable/shared/plans/2026-07-20-09-46-tdd-search-provider-failclosed.md`
**Reviewer**: pre-implementation architectural review (`/review_plan`)
**Date**: 2026-07-20
**Bead**: `silmari-agentfield-system-b2y`

---

## Verdict

**Needs Minor Revision** — the fail-closed *design* is sound and every load-bearing seam
was verified against real code, but the plan's one **BLOCKING closure test is not runnable
as specified**: the trigger chain makes 20+ real `app.ai` LLM calls that the plan never
stubs, which contradicts its "pure in-process pytest / no external infra" execution
guarantee. Fix that (add an LLM/`app.ai` injection seam) plus the test-isolation gaps and
the plan is ready. No structural redesign is required.

### Summary table

| Category | Status | Issues |
|---|---|---|
| Contracts | ✅ | 0 |
| Interfaces | ⚠️ | 1 (SEARCH_PROVIDER forcing under-specified) |
| Promises | ✅ | 0 |
| Data Models | ⚠️ | 1 (`provider_errors` semantics inconsistent between B4/B5) |
| APIs | ✅ | 0 |
| Workflow Closure | ❌ | 2 (LLM stub seam missing; registry isolation) |

**Findings by severity: 1 blocker · 3 should-fix · 3 nice-to-have.**

### What the code verification confirmed (design is correct)

- **Drivability of the closure trigger is real.** The AgentField reasoner decorator runs the
  raw function *without tracking* when there is no agent instance
  (`.venv/.../agentfield/decorators.py:296-301`), and `_send_workflow_error` returns early when
  `handler is None` and swallows its own exceptions (decorators.py:657-666, 694-696). So a
  `ReasonerFailed` raised inside `execute_deep_research` propagates cleanly to an in-process
  pytest — the plan's OBSERVE-on-the-raise is valid.
- **Propagation path has no swallow.** `search_web_for_content` → `execute_intelligence_stream_comprehensive`
  uses a plain `asyncio.gather(*search_tasks)` (main.py:1364, **no** `return_exceptions=True`),
  and streams run in a sequential `await` loop (main.py:1615-1626) with **no** wrapping
  try/except. A raised `SearchUnavailable` therefore reaches `prepare_research_package`
  unswallowed — Behavior 4's "wrap the search-driving portion" is both necessary and
  sufficient.
- **`execute_deep_research` does not catch Phase-1 failures** (main.py:3119 is a bare `await`),
  so the terminal `ReasonerFailed` skips Phase-2 doc generation (main.py:3133) exactly as
  Behavior 5 claims.
- **`register_provider` does make a fake selectable** — it appends to `DEFAULT_PROVIDER_PRIORITY`
  (registry.py:117-119), which `get_available_providers()` iterates (registry.py:39-52).
- **`ReasonerFailed(message, *, result=None, error_details=None)`** signature matches the plan
  exactly (exceptions.py:62-71); a plain `return` always records succeeded (decorators.py:459-479).

---

## Findings (ordered)

### 1. [BLOCKER] Closure test cannot run offline — the trigger chain makes ~20 unstubbed `app.ai` LLM calls
**Location**: `## Testing Strategy`; `## Workflow Closure` → EXECUTION bullet (plan lines 115-119);
Behavior 5 TDD Cycle / Success Criteria (plan lines 597-640).

**Problem**: The closure TRIGGER is `execute_deep_research`, and the plan's execution guarantee
says "pure in-process pytest — installs fake providers, calls the real reasoner chain. No
external infra." That is not true. `main.py` contains 23 `app.ai`/`ai_with_dynamic_params`
calls, and the trigger chain traverses many of them *before and around* the search seam:
`prepare_research_package` calls `classify_query_adaptive` (LLM) at main.py:1526 before the
iteration loop; `execute_intelligence_stream_comprehensive` calls `ai_with_dynamic_params`
for evidence extraction/synthesis (~main.py:1456); and the **RED-AT-SEAM control test**
(`test_red_at_seam_control_returns_document`) requires the *entire* `generate_document_from_package`
LLM pipeline to run and return a real `DocumentResponse`. The plan injects only search fakes
and a no-op sleep — nothing for the LLM. As written the closure test needs live LLM
credentials (external infra, contradicting the guarantee), or it fails/needs skipping in CI.
Per `review_plan` §6, a BLOCKING closure test that can't execute fail-closed in the gating
environment is a critical failure.

**Suggested change**: Add an `app.ai` / `ai_with_dynamic_params` injection seam (e.g. a fake
`app` or a monkeypatched `ai_with_dynamic_params` returning canned classification/synthesis/
document payloads) to the Testing Strategy, and wire it into the Behavior 4/5 setup so the
reasoner chain runs deterministically offline. Explicitly state that the red-at-seam control
produces a `DocumentResponse` from stubbed inference, not real doc-gen. If an LLM seam is
judged out of scope, raise it as a BLOCKING seam-remediation (don't silently rely on live
LLM or a skip guard).

### 2. [SHOULD-FIX] Fake-provider fixtures are non-hermetic — mutable global registry + real-key `is_available()`
**Location**: `## Testing Strategy` (plan lines 80-82); Behavior 2/4/5 fixtures
(`fake_providers`, `register_fake`, `no_providers`, `spy_iterations`), plan lines 411-424,
547-552, 601-613.

**Problem**: `register_provider` mutates module-global `PROVIDER_CLASSES` **and** appends to
`DEFAULT_PROVIDER_PRIORITY` (registry.py:117-119) with **no removal API**, so a registered
`"stub"` leaks into every subsequent test in the process. Worse, `get_available_providers()`
filters by `is_available()` = *real env-key presence* (base.py:56-59): in any dev/CI env where
`JINA_API_KEY`/`TAVILY_API_KEY`/`FIRECRAWL_API_KEY`/`SERPER_API_KEY` are set, the real
providers stay "available" and get tried alongside (or before) the fake — so the tests would
hit the network and the assertions become nondeterministic. The plan names the fixtures but
never specifies their isolation semantics, and never says how a `FakeProvider` reports
`is_available()` (the abstract `name`/`api_key_env_var` properties must be implemented and
`is_available()` forced `True`, or `get_available_providers()` skips it entirely).

**Suggested change**: Specify in the Testing Strategy that the fixtures (a) snapshot and
restore `PROVIDER_CLASSES` and `DEFAULT_PROVIDER_PRIORITY` around each test, (b)
monkeypatch/unset all four provider API-key env vars so only the fake is available, and (c)
define `FakeProvider` with `name`/`api_key_env_var` properties and `is_available()` overridden
to `True`. The `no_providers` fixture should clear the registry (or set every provider
unavailable) rather than assume a clean env.

### 3. [SHOULD-FIX] `provider_errors` in the result payload is inconsistent between Behavior 4 and Behavior 5
**Location**: Overview (plan line 61: "fails via `ReasonerFailed` with the accumulated
per-provider failures in the result payload"); Behavior 5 assertion `ei.value.result["provider_errors"] is not None`
(plan line 607) and prose "the recorded per-provider/per-query failures" (plan line 583).

**Problem**: Behavior 4 makes a mid-iteration `SearchUnavailable` (providers non-recoverable)
convert to `ReasonerFailed` **immediately** (capability gone). That means the **terminal gate**
of Behavior 5 is only ever reached on the *genuine-empty* path (`EmptyResults()`, HTTP 200,
zero hits) — where `search_web_for_content` returns `[]` and **no provider error is ever
recorded**. So at the terminal gate `provider_errors` is always `[]`, contradicting the
repeated claim that the failed run carries "accumulated per-provider failures." The two
`ReasonerFailed.result` payloads describe different situations: real `provider_errors` belong
to the capability-gone path (B4), empty diagnostics to the terminal-gate path (B5). The
assertion `is not None` passes on `[]` but the prose is misleading and the data model is
under-specified.

**Suggested change**: Split the contract: (a) capability-gone `ReasonerFailed` (B4) carries
non-empty `provider_errors`; (b) terminal-gate `ReasonerFailed` (B5) carries
`provider_errors: []` (or per-query empty-result diagnostics, explicitly *not* errors), plus
`total_sources: 0`. Fix the Overview wording and change the B5 assertion from `is not None` to
an explicit shape (e.g. `result["total_sources"] == 0` and `"provider_errors" in result`).
State in the §Flagged/System-Map grammar which of the two paths populates `provider_errors`.

### 4. [SHOULD-FIX] Closure classification of the S6 seam (run record `status="failed"`) is not stated; no automated coverage
**Location**: `## Workflow Closure` OBSERVE + EXECUTION (plan lines 102-119); Seam inventory
row **S6** (plan line 246).

**Problem**: Because the in-process closure test runs the reasoner raw (no agent instance →
decorators.py:296-301), it **never exercises** the decorator's raise→`_send_workflow_error`→
`status="failed"` mapping (decorators.py:480-496) or the S6 run-record read
(`result_for`, ui/server.py:251). The plan is honest that this is "asserted separately … reads
agentfield/decorators.py behavior," but it never provides that separate assertion and leaves
S6 unclassified. Per `review_plan` §6, every workflow behavior must be explicitly `BLOCKING`
or `LEAF: <reason>`; S6 is currently implied-but-unlabeled.

**Suggested change**: Explicitly classify S5→S6 as `LEAF: framework-owned mapping (agentfield
decorator posts status="failed" on any reasoner raise, decorators.py:480-496) — verified by
reading, not re-implemented in this slice`, and note S6 is covered only by manual staging E2E
(already listed under Integration & E2E). No new automated test is required if it is labeled
LEAF; leaving it unlabeled defaults to BLOCKING.

### 5. [NICE-TO-HAVE] `SEARCH_PROVIDER` forcing contract for the new `search()` is under-specified
**Location**: Behavior 2 Green (`_ordered_with_forced_first`, plan line 437) and Refactor note
"honors `SEARCH_PROVIDER`" (plan line 453); System-Map EBNF S2 (plan lines 259-262).

**Problem**: The new `search()` selects from `get_available_providers()` and reorders via a
plan-invented `_ordered_with_forced_first(providers)`, but neither the EBNF nor the seam
grammar documents *how* forcing works — the EBNF only describes `is_available` priority order.
The Behavior 5 closure relies on `SEARCH_PROVIDER=stub` to select the fake; if
`_ordered_with_forced_first` does not actually read `SEARCH_PROVIDER`, forcing silently
no-ops and the test only passes because (after finding 2's isolation) the stub happens to be
the sole available provider. The old `get_default_provider()` honored `SEARCH_PROVIDER`
(registry.py:80-87) but the new `search()` does not call it.

**Suggested change**: Add the `_ordered_with_forced_first` contract to the S2 grammar: reads
`os.getenv("SEARCH_PROVIDER")`, and if that provider is present in the available list, moves it
to the head; otherwise returns priority order unchanged. Add a unit test asserting forcing
reorders selection.

### 6. [NICE-TO-HAVE] main.py `async def` line citations are systematically off by one; return type mislabeled
**Location**: Throughout (System Map nodes, Seam inventory, Behavior "Files touched").

**Problem**: Every cited `async def` in main.py points one line high:
`execute_intelligence_stream_comprehensive` is **1342** (plan says 1341),
`prepare_research_package` is **1505** (plan says 1504), `execute_deep_research` is **3039**
(plan says 3038). (`search_web_for_content` at 76, the 1363 call site, 101-109 dict mapping,
1819-1836 gate, 1834 `total_sources`, and 3133-3142 Phase 2 are all correct.) Separately,
`prepare_research_package` returns a **`ModeAwareResearchResponse` object** (main.py:1514), not
a metadata dict — the terminal gate must `raise ReasonerFailed` *instead of* building/returning
that response, which the plan's Green prose implies but does not state.

**Suggested change**: Decrement the three `def` line numbers by one across the plan, and note
in Behavior 5 Green that the gate raises before constructing the `ModeAwareResearchResponse`
(the `all_source_articles` variable it counts is real: initialized main.py:1528, extended
main.py:1645).

### 7. [NICE-TO-HAVE] Nested reasoners will each emit a `failed` event in production
**Location**: Behavior 4/5 Green; Promise analysis (implicit).

**Problem**: In production (agent instance present), `SearchUnavailable`/`ReasonerFailed`
re-raises through each decorated reasoner (`execute_intelligence_stream_comprehensive` →
`prepare_research_package` → `execute_deep_research`), and each decorator's except block calls
`_send_workflow_error` for its own execution id before re-raising (decorators.py:487-496). The
top-level run correctly ends `failed`, but there will be multiple nested `failed` sub-execution
records. This is correct behavior, not a bug, but the plan should acknowledge it so the noise
isn't mistaken for a regression during staging E2E.

**Suggested change**: Add one sentence to the Promise/observability notes that nested reasoner
executions each record `failed` on the exhaustion path; the observable run-level status is the
top-level `execute_deep_research` record.

---

## Approval status

- [ ] Ready for Implementation
- [x] **Needs Minor Revision** — resolve finding 1 (blocker) and findings 2–4 (should-fix)
  before implementation; findings 5–7 can be folded into the same revision or handled during
  Refactor.
