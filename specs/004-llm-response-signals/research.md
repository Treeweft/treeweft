# Research: LLM Response Signals and Agent Repeat-Call Metric

Decisions made while planning. Each was checked against the code as it stands on `main` at
2026-10-06. There were no `NEEDS CLARIFICATION` items left in the Technical Context.

## R1. Where the service change goes

**Decision**: one function, `_chat` in `adapters/llm_api/llm_adapter.py`.

**Rationale**: it is the only place the service posts to `/chat/completions`. `llm_caller`
(HyDE and chunk summaries, through the control layer) and
`application/benchmark/query_gen.py` both go through it. It already opens the `llm.chat` span
and already has the parsed response in hand.

**Alternatives considered**: recording in `llm_caller.call_with_control_layer`. Rejected: it
only sees the returned text, and `query_gen` bypasses it.

## R2. Attribute names

**Decision**: add these to the `llm.chat` span, following the OpenTelemetry generative-AI
semantic conventions:

| Attribute | Value |
|---|---|
| `gen_ai.operation.name` | `"chat"` |
| `gen_ai.request.model` | the configured model |
| `gen_ai.request.max_tokens` | the token limit sent |
| `gen_ai.response.model` | the `model` field of the response |
| `gen_ai.usage.input_tokens` | `usage.prompt_tokens` |
| `gen_ai.usage.output_tokens` | `usage.completion_tokens` |
| `gen_ai.response.finish_reasons` | one-element list holding `choices[0].finish_reason` |

The existing `treeweft.llm_model`, `treeweft.operation` and `treeweft.max_tokens` stay.

**Rationale**: these names are a published, vendor-neutral standard, so any trace tool or
dashboard that understands them works unchanged. The span name `llm.chat` is kept because the
shipped Grafana dashboard queries it.

**Not set**: `gen_ai.provider.name`. The conventions ask for it, but the service talks to "any
OpenAI-compatible endpoint" and does not know which provider is behind the URL. Guessing would
break the provider-neutral rule. It is omitted rather than set to a placeholder.

**Alternatives considered**: renaming the span to the conventions' `chat {model}` form.
Rejected: it would break the existing dashboard (FR-002).

## R3. Condition attributes

**Decision**: three boolean span attributes under the project's own prefix, because the
conventions define none for these:

- `treeweft.llm.model_mismatch`
- `treeweft.llm.truncated`
- `treeweft.llm.empty`

Each is set to `true` or `false` when it could be evaluated and left unset when the data needed
was missing (FR-011).

**Rationale**: setting `false` explicitly lets a trace query distinguish "checked, fine" from
"could not check", which matters for the fail-loud principle.

## R4. Metrics

**Decision**: two Prometheus counters on the service's existing registry.

- `treeweft_llm_tokens_total{operation, direction}` with `direction` in `input`, `output`.
- `treeweft_llm_response_conditions_total{operation, condition}` with `condition` in
  `model_mismatch`, `truncated`, `empty`.

Every series is created at zero at import for each value of the `Operation` enum plus
`unknown`, following the existing `hyde_fallbacks` pattern, so `rate()` sees the first
increment. The list is read from the enum, not written out by hand. Today only `hyde`,
`chunk_summary` and `unknown` reach `_chat`; `community_summary` and `benchmark_query` are
defined in the enum but have no caller, so their series stay at zero.

**Rationale**: the service exposes metrics through `prometheus_client`; it has no OpenTelemetry
meter provider. Operation is the only breakdown needed: a process has exactly one configured
model, so a model label would always have one value.

**Consequence to be honest about**: the standard attribute names apply to traces. Stock
dashboards built on the conventions' *metric* names (for example a token-usage histogram
emitted through an OpenTelemetry meter) will not light up from these counters. Trace-based
views will. Adding an OpenTelemetry meter pipeline is a larger change and is out of scope.

**Alternatives considered**: adding a `model` label (always one value per process, rejected);
emitting the conventions' metrics through a new OpenTelemetry meter provider (new pipeline and
collector configuration, rejected for this feature).

## R5. Model-mismatch rule

**Decision**: first-seen baseline, as settled in clarification. A small pure class keeps, per
requested model, the first served model name reported after startup. A later call whose served
name differs is a mismatch. The baseline never changes during the process's life. One instance
lives at module level in the adapter.

**Rationale**: a local server may report a file path and a vendor may report a dated identifier
behind an alias; comparing against the requested name would flag every call. Comparing against
what the endpoint itself said first gives zero false flags on a stable deployment (SC-004).

**Known limits, stated in the spec**: a wrong model present from the first call is not flagged;
a restart forgets the baseline; with several worker processes each keeps its own.

**Concurrency**: the service is single-threaded asyncio and the check contains no `await`, so a
plain dictionary is safe without a lock.

## R6. Truncated and empty

**Decision**:

- *Truncated*: the finish reason, compared case-insensitively, is `length` or `max_tokens`.
  Unset when the response has no finish reason.
- *Empty*: the message content is absent, or is empty after `<think>` blocks are removed.
  Unset when the response has no `choices`.

Signals are read from the parsed response **before** the existing two lines that extract and
strip the content, and those two lines are left exactly as they are.

**As built**: the body is parsed once into a local and both the recording and the extraction
read from it, so the extraction line reads `data[...]` where it read `resp.json()[...]`. The
indexing expression and the stripping call are otherwise unchanged.

**Rationale**: leaving the extraction lines untouched is the simplest proof that the return
value does not change. In particular a null `content` still raises inside the existing `try`
and still returns `None` with the span marked as an error, as today; the span now additionally
carries `treeweft.llm.empty = true`.

**Alternatives considered**: handling null content explicitly and returning `""`. Rejected: it
changes what callers receive (FR-010).

**One definition of reasoning-block stripping**: the rule already exists twice, as
`strip_thinking` in `domain/benchmark/agent_protocol.py` (treats a missing value as empty) and
as `_strip_thinking` in the adapter (raises on a missing value, which is what makes null
content return `None` today). The new module holds the shared, missing-value-tolerant
definition and `agent_protocol` re-exports it, so the count of copies does not grow. The
adapter's `_strip_thinking` is deliberately left alone: pointing it at the tolerant version
would turn a null-content response from `None` into `""`.

## R7. Failure isolation

**Decision**: reading signals and recording them is wrapped so that a malformed response field
(for example `usage` being a string) can never raise out of `_chat`. On such a failure the
affected values are simply left unset.

**Rationale**: observability must not be able to break an LLM call that would otherwise have
succeeded. The wrapper is around the recording only, not around the existing request and
extraction code.

## R8. Getting the finish reason out of the benchmark client

**Decision**: add `AgentLLM.chat_full()` returning a small result object (content, usage, tool
calls, finish reason). `chat()` calls it and returns the same three values as today. The agent
loops and the judge call a helper that uses `chat_full` when the client has it and otherwise
falls back to `chat` with an unknown finish reason.

**Rationale**:

- `chat()` has eight call sites and two test fakes that unpack three values; changing its
  shape would touch all of them for no benefit.
- An attribute such as `last_finish_reason` on the client would be wrong: the runner executes
  arms and judges concurrently with `asyncio.gather` on one shared client, so "last" is racy.
- The fallback keeps every existing fake and any duck-typed client working unchanged.

The native Anthropic path reports `stop_reason` and uses `max_tokens` where OpenAI-compatible
endpoints use `length`; R6's rule already accepts both.

## R9. Counting tool calls

**Decision**: a pure tally in `domain/benchmark/tool_call_stats.py`. Both loops call
`record(tool, args)` at the point where they already increment `tool_calls`, so only executed
calls are counted. Calls with unparseable arguments or an unknown tool name are never recorded.

- *Call identity*: the tool name plus the arguments serialised as JSON with sorted keys and
  compact separators. This ignores key order and whitespace and nothing else (FR-014).
- *Repeated calls*: for each identity, occurrences beyond the first, summed.
- *Looped*: any identity occurred `LOOP_THRESHOLD` (3) or more times.
- *Calls per tool*: executed calls grouped by tool name.

**Rationale**: the tally is fed from data the loop already has, so it cannot change a token or
a turn (FR-017). It is computed during the run because transcripts are only saved with
`--debug-transcripts` (FR-018).

**Alternatives considered**: deriving the figures afterwards from the transcript. Rejected:
unavailable for normal runs.

## R10. Old results in aggregates and tables

**Decision**: new per-arm means use a mean that returns `None` when no row has the field,
rather than the existing `_mean`, which returns `0.0`. Tables print `n/a` for `None`.

**Rationale**: `0.0` would claim "no loops" for results that never measured loops (FR-019).
The existing fields keep the existing `_mean`, so no current number changes (FR-024).

**Per-tool means**: within an arm, a row that has the field but did not call a given tool
contributes zero for that tool; a row without the field is skipped.

## R11. Where the new figures are shown

**Decision**:

- *Per-query row*: all new fields (see [contracts/benchmark-results.md](contracts/benchmark-results.md)).
- *Summary*: per-arm means and counts, plus the loop threshold and run-wide cut-off totals.
- *Comparison table* (grep vs treeweft): new rows for mean turns, looped queries, mean
  repeated calls, and a calls-per-tool line for each arm. The table currently has no mean-turns
  row; one is added so the new figures sit beside it as the spec asks.
- *Rollup*: per-repo looped share and mean repeated calls for both arms in the rollup data and
  as table columns; per-tool means in the rollup data only, because a per-tool column set
  would differ per arm and make the table unreadable.
- *Experiment tracking*: the per-query looped flag and repeated-call count are mirrored as
  feedback scores, post-hoc, like the existing scores.

**Harness era**: these are added fields. No existing figure changes, so this does not start a
new baseline epoch and does not affect `scripts/agentic_smoke_check.py`.

## R12. Verifying "no throughput change" (SC-008)

**Decision**: the criterion is met by construction and checked two ways.

1. A unit test asserts `_chat` makes exactly one HTTP request per call and returns the same
   value with and without tracing enabled.
2. Optionally, on a live stack, index one small reference repository before and after and
   compare wall-clock time; a difference within ±5% counts as noise.

**Rationale**: the added work is in-memory and constant per call, while each call costs a
network round trip to a model. A full before/after benchmark would cost far more than the risk
warrants, and it needs the homelab stack, which is not always up. Step 2 is reported as skipped
when the stack is unavailable.

## Out of scope, noted for follow-up

- Acting on a truncated or empty response (retry, reject, keep out of the summary cache). This
  could move search quality and needs a benchmark run first.
- Embedding and reranker calls.
- Gold-answer generation and query generation in the harness: a cut-off gold answer would also
  matter, but the spec covers agent and judge responses only.
- A dashboard panel for the new counters. The existing dashboard is left untouched.
