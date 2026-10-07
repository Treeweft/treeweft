# Data Model: LLM Response Signals and Agent Repeat-Call Metric

Nothing is persisted in a database. The entities below are in-memory values, span attributes,
metric series, and fields in the benchmark's result files.

## Service

### ResponseSignals (new, `domain/llm_response.py`)

What one chat response reports about itself. Immutable. Built from the parsed response body.

| Field | Type | Source | Unset when |
|---|---|---|---|
| `served_model` | text or none | `model` | absent or empty |
| `input_tokens` | integer or none | `usage.prompt_tokens` | `usage` absent, or not an integer |
| `output_tokens` | integer or none | `usage.completion_tokens` | `usage` absent, or not an integer |
| `finish_reason` | text or none | `choices[0].finish_reason` | absent or null |
| `truncated` | boolean or none | `finish_reason`, compared case-insensitively, is `length` or `max_tokens` | `finish_reason` unset |
| `empty` | boolean or none | content absent, or empty after reasoning blocks are removed | no `choices` |

Rules:

- A missing value is `none`, never `0`, `""` or a copy of the request (FR-003).
- Building it never raises, whatever shape the response has (R7).
- It holds no prompt or response text (FR-005).

The module also holds the one shared definition of `strip_thinking` (removes `<think>` blocks;
treats a missing value as empty text). `domain/benchmark/agent_protocol.py` re-exports it
instead of keeping its own copy.

### ServedModelBaseline (new, `domain/llm_response.py`)

Remembers, per requested model, the first served model seen in this process.

| Operation | Behaviour |
|---|---|
| `observe(requested, served)` with `served` none | returns none; nothing remembered |
| first `observe` for a requested model | remembers `served`; returns `false` |
| later `observe`, same `served` | returns `false` |
| later `observe`, different `served` | returns `true`; the remembered value does not change |

State transition: `unset → set`, once. Never reset except by process restart (FR-006).

### LLM call record (existing `llm.chat` span, extended)

Existing attributes are unchanged. Added attributes are listed in
[contracts/telemetry.md](contracts/telemetry.md).

### Metric series (new, `infrastructure/metrics.py`)

Two counters, defined in [contracts/telemetry.md](contracts/telemetry.md).

## Benchmark harness

### ChatResult (new, `adapters/benchmark/agent_llm.py`)

What `chat_full()` returns.

| Field | Type | Notes |
|---|---|---|
| `content` | text | as returned by `chat()` today |
| `usage` | mapping or none | as today |
| `tool_calls` | list or none | as today |
| `finish_reason` | text or none | `finish_reason` for OpenAI-compatible responses, `stop_reason` for the native Anthropic path |

`chat()` returns `(content, usage, tool_calls)` from it, unchanged.

### ToolCallTally (new, `domain/benchmark/tool_call_stats.py`)

Accumulates the executed tool calls of one agent run.

| Member | Meaning |
|---|---|
| `record(tool, args)` | count one executed call |
| `counts_by_tool` | mapping of tool name to number of executed calls |
| `repeated` | calls whose tool and arguments equal an earlier call's; for each distinct call, occurrences beyond the first |
| `looped` | true when any distinct call occurred `LOOP_THRESHOLD` or more times |

`LOOP_THRESHOLD = 3`. Two calls are the same when the tool name matches and the arguments are
equal after serialising with sorted keys and compact separators.

Invariants:

- `sum(counts_by_tool.values())` equals the run's existing `tool_calls`.
- `repeated` equals `tool_calls` minus the number of distinct calls.
- `looped` implies `repeated >= 2`.

### AgentRunResult (existing, extended)

| New field | Type | Default |
|---|---|---|
| `tool_call_counts` | mapping tool → integer | empty |
| `repeated_tool_calls` | integer | 0 |
| `looped` | boolean | false |
| `truncated_responses` | integer | 0 |

`truncated_responses` counts agent responses in the run whose finish reason indicates the token
limit, including the forced final answer. A response with an unknown finish reason is not
counted.

### JudgeScore (existing, extended)

| New field | Type | Meaning |
|---|---|---|
| `truncated` | boolean or none | whether the verdict response was cut off; none when unknown, or when no call was made (missing gold answer, empty candidate, call failed) |

Scores and `parse_ok` are computed exactly as today.

### Per-query row, arm summary, run summary, rollup entry

Field-level definitions are in
[contracts/benchmark-results.md](contracts/benchmark-results.md).

## Relationships

```text
service:    response body ──> ResponseSignals ──┬─> span attributes
                                                 ├─> ServedModelBaseline ─> mismatch flag
                                                 └─> counters

harness:    ChatResult.finish_reason ─┐
            executed tool calls ──────┼─> AgentRunResult ─> per-query row ─> arm summary
            JudgeScore.truncated ─────┘                                   ─> run summary ─> rollup
```

The two halves share only the truncation rule in `domain/llm_response.py`.
