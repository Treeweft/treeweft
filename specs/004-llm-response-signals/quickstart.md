# Quickstart: Verifying LLM Response Signals and the Repeat-Call Metric

How to prove each story works. Field and attribute definitions are in
[contracts/telemetry.md](contracts/telemetry.md) and
[contracts/benchmark-results.md](contracts/benchmark-results.md).

## Prerequisites

- The repository checked out on the feature branch, dependencies installed.
- No services are needed for the unit checks.
- The live checks need a running indexer with an LLM endpoint configured, and a trace backend
  receiving its spans. If the homelab stack is down, report the live checks as not run.

All commands run from the repository root. `env -u PYTHONPATH` is required on this machine.

## Unit checks (all stories)

```bash
# New tests
env -u PYTHONPATH python -m pytest \
  tests/unit/test_llm_response_signals.py \
  tests/unit/test_llm_adapter_signals.py \
  tests/unit/test_tool_call_stats.py -q

# Extended tests
env -u PYTHONPATH python -m pytest \
  tests/unit/test_native_tools_loop.py \
  tests/unit/test_agentic_runner.py \
  tests/unit/test_agent_metrics.py \
  tests/unit/test_rollup.py \
  tests/unit/test_benchmark.py -q

# Whole unit suite: nothing else regressed
env -u PYTHONPATH python -m pytest tests/unit -q
```

Expected: all pass. Each new behaviour's test was first run and seen to fail before the
implementation existed.

## Story 1: what each call consumed and who served it

**Unit** (`test_llm_adapter_signals.py`), against a mocked endpoint and an in-memory span
exporter:

- A response with `model`, `usage` and `finish_reason` produces a span carrying all the
  attributes in the telemetry contract, and the three existing `treeweft.*` attributes with
  their previous values.
- A response with no `usage` and no `model` produces a span with none of the response
  attributes, and `_chat` returns the same text as before.
- `treeweft_llm_tokens_total` rises by the reported input and output counts for the call's
  operation, with tracing on and with tracing off.

**Live**:

```bash
# Trigger at least one LLM call (a search with HyDE, or index a small repo), then:
curl -s http://localhost:8001/metrics | grep '^treeweft_llm_tokens_total'
```

Expected: series for each operation and direction, with non-zero values for the operations
exercised. In the trace backend, open an `llm.chat` span and confirm `gen_ai.request.model`,
`gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` and
`gen_ai.response.finish_reasons` are present alongside `treeweft.operation`.

**Dashboard** (SC-005): open the existing indexer traces dashboard and confirm every panel
still renders data.

## Story 2: drift and degenerate responses are flagged

**Unit** (`test_llm_response_signals.py`, `test_llm_adapter_signals.py`):

| Stubbed responses | Expected |
|---|---|
| two calls, same requested model, served `m-1` then `m-2` | second span has `treeweft.llm.model_mismatch = true`; counter `condition="model_mismatch"` +1 |
| served model reported as a file path, consistently | never flagged |
| `finish_reason: "length"` | `treeweft.llm.truncated = true`; counter +1 |
| content empty after a `<think>` block | `treeweft.llm.empty = true`; counter +1 |
| ordinary response | all three attributes `false`; no counter change |
| response with no `finish_reason` | `treeweft.llm.truncated` absent |
| any of the above | `_chat` returns exactly what it returned before the change |

**Live**:

```bash
curl -s http://localhost:8001/metrics | grep '^treeweft_llm_response_conditions_total'
```

Expected: one series per operation and condition (twenty with today's operations and four
conditions), all present from startup. On a
stable deployment the `model_mismatch` series stay at 0 (SC-004). To see truncation for real,
set `LLM_SUMMARY_MAX_TOKENS` to a very small value in a throwaway environment, index one file,
and confirm the `chunk_summary` / `truncated` series rises. Restore the setting afterwards.

## Story 3: how much each arm repeats itself

**Unit** (`test_tool_call_stats.py`, `test_native_tools_loop.py`, `test_agentic_runner.py`),
with a scripted fake LLM:

- An agent that calls tool A three times with identical arguments and tool B twice with
  different arguments yields `repeated_tool_calls = 2`, `looped = true`,
  `tool_call_counts = {A: 3, B: 2}`.
- Arguments differing only in key order or whitespace count as the same call.
- A call with unparseable arguments, or to an unknown tool, is not counted.
- The same script gives the same figures under the native and the text protocol.
- A fake that reports a length finish reason on one agent response marks that arm's row
  `agent_truncated = true`; a judge fake doing the same sets `judge.truncated = true`.
- Token totals, turns and judge scores for the existing scripted tests are unchanged (SC-006):
  those tests pass without edits to their expected values.

**Aggregation and tables** (`test_agent_metrics.py`, `test_rollup.py`):

- Rows carrying the new fields produce `looped_share`, `mean_repeated_tool_calls` and
  `mean_tool_calls_by_tool` per arm, and the comparison table shows the new rows.
- Rows from before the feature produce null for those fields and `n/a` in the tables, and every
  existing figure is identical to today's.

**Live** (optional, costs tokens; pass `scripts/agentic_smoke.sh` first as usual):

```bash
python -m treeweft.benchmark agentic \
  --queries benchmarks/queries/myrepo-clean.jsonl \
  --repo /path/to/your/repo \
  --search-url http://localhost:8001 \
  --arms grep,treeweft \
  --limit 3
```

Expected: the printed comparison table includes mean turns, looped queries, mean repeated
calls and the calls-per-tool lines; `_summary.json` carries `loop_threshold: 3` and the
cut-off totals; each row's `tool_call_counts` sums to its `tool_calls`. The command is the one in
`docs/benchmark-eval.md`, step 3.

## Throughput (SC-008)

By construction the change adds no I/O. If the stack is up, index one small reference
repository before and after and compare wall-clock time; within ±5% counts as no change. If the
stack is down, report this step as not run.

## Final checks before opening the pull request

```bash
env -u PYTHONPATH python -m pytest tests/unit -q
git diff --stat main -- contracts/     # expect no changes: no API, MCP or schema change
```

Docs updated in the same pull request: `docs/engineering-notes.md`,
`docs/observability-runbook.md`, `docs/benchmark-eval.md`, `CHANGELOG.md`.
