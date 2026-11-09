# Implementation Plan: LLM Response Signals and Agent Repeat-Call Metric

**Branch**: `004-llm-response-signals` | **Date**: 2026-10-06 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/004-llm-response-signals/spec.md`.

## Summary

Two independent pieces of work share one small pure module.

**Service.** `_chat` in `adapters/llm_api/llm_adapter.py` is the single place the service calls
an LLM. Today it reads only the message text from the response. It will also read the served
model, token usage and finish reason, and:

- set them on the existing `llm.chat` span under the OpenTelemetry generative-AI attribute
  names, next to the unchanged `treeweft.*` attributes;
- evaluate three conditions (model mismatch against a first-seen baseline, truncated, empty)
  and set each as a `treeweft.llm.*` span attribute;
- increment two new Prometheus counters: tokens by operation and direction, and conditions by
  operation and condition.

`_chat` keeps returning `str | None` with the same value in every case. No caller changes.

**Benchmark harness.** `AgentLLM` gains `chat_full()`, which returns the finish reason along
with what `chat()` returns today; `chat()` delegates to it and keeps its three-value result.
Both agent loops tally executed tool calls into a small pure counter and report, per query and
arm, calls per tool, exact repeats, a looped flag, and whether any agent response was cut off.
The judge reports whether its verdict was cut off. Aggregation, the comparison table and the
multi-repo rollup report the new figures and show "n/a" for results that predate them.

Nothing here changes a prompt, a ranking, a payload, a retry or a cache decision. Research and
decisions are R1–R12 in [research.md](research.md).

## Technical Context

**Language/Version**: Python ≥ 3.11.

**Primary Dependencies**: none added. Uses `opentelemetry-sdk` (≥1.27) and `prometheus_client`
(≥0.20), both already direct dependencies, and `httpx`.

**Storage**: none. The served-model baseline lives in process memory. Benchmark results gain
fields in the existing per-query rows file and `_summary.json`.

**Testing**: pytest unit tests only, no services. New pure-logic tests for the shared module
and the tool-call tally; adapter tests for `_chat` using the existing mocked-client fixture and
an in-memory span exporter; extensions to the existing agent-loop, runner, aggregation, rollup
and judge tests.

**Target Platform**: the indexer service (host process or container) and the benchmark CLI.

**Project Type**: web service plus CLI harness, single Python package.

**Performance Goals**: no measurable change to indexing throughput (SC-008). The added work per
LLM call is a handful of dictionary reads, at most six span attributes and at most five counter
increments; no I/O, no awaits, no locks.

**Constraints**:
- `_chat`'s return value, retry behaviour and caching are unchanged in every case (FR-010).
- No existing span attribute or metric is renamed or removed (FR-002); the Grafana dashboard
  in `assets/grafana/` depends on those names.
- No prompt or response text is recorded (FR-005).
- The benchmark feature adds no LLM calls, tool calls or tokens (FR-017).
- Provider-neutral: nothing assumes a particular LLM vendor (constitution IV).

**Scale/Scope**: 2 source files changed and 1 added in the service path; 9 changed and 1 added
in the harness; 6 test files added and 4 extended; 3 docs and the changelog updated.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate | Status |
|---|---|---|
| I. Verification First | Each story has a named check in [quickstart.md](quickstart.md): unit tests for all three, plus a live `/metrics` and trace check for the service. The live check needs the homelab stack; if it is down, that is reported, not skipped silently. | Pass |
| II. Test-First, strict taxonomy | All new tests are unit tests with mocked HTTP and an in-memory span exporter; none touches a real model server, database or tracing backend. Each behaviour gets a test written first and shown failing. No integration test is needed: nothing new talks to a real service. | Pass |
| III. Evidence-Gated Retrieval Changes | Nothing that can move search quality or agent cost changes: no prompt, ranking, payload, retry or cache change (FR-010, FR-017). The benchmark gains reporting only; existing numbers are computed identically (FR-024, SC-006), so no new harness era is created. No benchmark run is required. | Pass |
| IV. Layered Architecture | The shared logic is pure and lives in `domain/`; it imports nothing from `adapters/`. Adapters and the application layer call into it. The MCP server is untouched. No startup-path import changes. No provider is assumed: the service reads standard OpenAI-compatible response fields, and the harness maps the one native non-OpenAI response shape it already supports. | Pass |
| V. Fail Loud | This feature is an application of the principle: three silent conditions become counted and visible. A value the endpoint does not report is left unset, never defaulted (FR-003, FR-011), so absence of data cannot read as "no problem". | Pass |
| VI. Secure by Default | No new endpoint. No prompt or response text is recorded. The served-model name and token counts are not secrets. `/metrics` is an existing endpoint with its existing exposure. | Pass |
| VII. Versioned Data and Releases | No change to the indexer HTTP API, the MCP tool surface or the index schema, so no contract snapshot changes and no `INDEX_SCHEMA_VERSION` bump. Additive observability is a SemVer MINOR at the next release. | Pass |
| Workflow | Work lands by pull request from a feature branch. `docs/engineering-notes.md`, `docs/observability-runbook.md`, `docs/benchmark-eval.md` and `CHANGELOG.md` are updated in the same pull request. No ADR: this is not an architectural decision. | Pass |

**Post-design re-check (after Phase 1)**: unchanged, all pass. The design added no dependency,
no service, no new layer and no exception to record.

## Project Structure

### Documentation (this feature)

```text
specs/004-llm-response-signals/
├── plan.md              # This file
├── research.md          # Phase 0: decisions R1–R12
├── data-model.md        # Phase 1: records and fields
├── quickstart.md        # Phase 1: how to verify each story
├── contracts/
│   ├── telemetry.md         # span attributes and metric names the service emits
│   └── benchmark-results.md # fields added to rows, summary, tables, rollup
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 output (/speckit-tasks), not created here
```

### Source Code (repository root)

```text
src/treeweft/
├── domain/
│   ├── llm_response.py              # NEW  pure: read response fields, truncation/empty
│   │                                #      rules, first-seen served-model baseline, the
│   │                                #      shared strip_thinking
│   └── benchmark/
│       ├── tool_call_stats.py       # NEW  pure: per-tool counts, exact repeats, looped
│       ├── agent_protocol.py        # strip_thinking re-exported from domain/llm_response.py
│       ├── judge_schema.py          # JudgeScore gains `truncated`
│       ├── agent_metrics.py         # per-arm means for the new fields; loop threshold
│       └── rollup.py                # comparison table rows; rollup fields and columns
├── adapters/
│   ├── llm_api/llm_adapter.py       # _chat: record span attributes, conditions, counters
│   └── benchmark/
│       ├── agent_llm.py             # chat_full() returning finish reason; chat() delegates
│       └── opik_tracing.py          # mirror the new per-query figures as feedback scores
├── application/benchmark/
│   ├── agent_loop.py                # tally tool calls and cut-off responses in both loops
│   ├── judge.py                     # carry the verdict's cut-off marker
│   └── agentic_runner.py            # write the new row fields and summary totals
└── infrastructure/metrics.py        # two new counters

tests/unit/
├── test_llm_response_signals.py     # NEW  domain/llm_response.py
├── test_llm_adapter_signals.py      # NEW  _chat span attributes, conditions, counters
├── test_tool_call_stats.py          # NEW  domain/benchmark/tool_call_stats.py
├── test_agent_llm_chat_full.py      # NEW  AgentLLM.chat_full
├── test_agent_loop_tool_stats.py    # NEW  both agent loops, one script per protocol
├── test_judge_truncation.py         # NEW  the verdict's cut-off marker
├── test_opik_tracing.py             # extended
├── test_agentic_runner.py           # extended
├── test_agent_metrics.py            # extended
└── test_rollup.py                   # extended

docs/
├── engineering-notes.md             # tracing section: new attributes and counters
├── observability-runbook.md         # what to alert on
└── benchmark-eval.md                # reading the new table rows
CHANGELOG.md
```

**Structure Decision**: single package, existing DDD layout. The two new modules are pure
domain code so that the service adapter and the benchmark adapter can share the truncation
rule without either importing the other. Everything else is an edit to an existing file.

## Complexity Tracking

No constitution violations. Nothing to justify.
