# Contract: Service Telemetry

What the indexer emits for each LLM chat call. Dashboards, alerts and trace queries may rely on
these names; changing one is a breaking change to this contract.

## Span `llm.chat` (tracer `treeweft.llm`)

The span name and tracer name are unchanged.

### Existing attributes (unchanged)

| Attribute | Type | Value |
|---|---|---|
| `treeweft.llm_model` | string | configured model |
| `treeweft.operation` | string | a value of the `Operation` enum, or `unknown` when the caller names none |
| `treeweft.max_tokens` | int | token limit sent |

### Added at span start (always present)

| Attribute | Type | Value |
|---|---|---|
| `gen_ai.operation.name` | string | `chat` |
| `gen_ai.request.model` | string | configured model |
| `gen_ai.request.max_tokens` | int | token limit sent |

### Added after a response is parsed (each present only when the endpoint reported it)

| Attribute | Type | Value |
|---|---|---|
| `gen_ai.response.model` | string | served model as reported |
| `gen_ai.usage.input_tokens` | int | prompt tokens |
| `gen_ai.usage.output_tokens` | int | completion tokens |
| `gen_ai.response.finish_reasons` | string[] | one element: the finish reason |

### Added conditions (each present only when it could be evaluated)

| Attribute | Type | `true` when | Absent when |
|---|---|---|---|
| `treeweft.llm.model_mismatch` | bool | served model differs from the first served model seen for this requested model since startup | the response reports no served model |
| `treeweft.llm.truncated` | bool | finish reason, compared case-insensitively, is `length`, `max_tokens` or `model_context_window_exceeded` | the response reports no finish reason |
| `treeweft.llm.empty` | bool | no message content, or none left after reasoning blocks are removed | the response has no choices |

### Never recorded

Prompt text, response text, API keys, request headers.

### Errors

A request that fails or times out sets the span status to error exactly as today and carries
only the span-start attributes. No response attributes and no conditions are set.

## Prometheus metrics (`GET /metrics`)

Existing metric names are unchanged.

### `treeweft_llm_tokens_total`

Counter. Tokens reported by the endpoint for service LLM calls.

| Label | Values |
|---|---|
| `operation` | every value of the `Operation` enum, plus `unknown` |
| `direction` | `input`, `output` |

Incremented by the reported count. Not incremented for a response that omits usage.

### `treeweft_llm_response_conditions_total`

Counter. Service LLM responses on which a condition was detected.

| Label | Values |
|---|---|
| `operation` | as above |
| `condition` | `model_mismatch`, `truncated`, `empty` |

Incremented by one for each condition that evaluated to true. A response with two conditions
increments two series.

### Series initialisation

Every label combination above is exported with value 0 from process start, so `rate()` and
`increase()` see the first increment. The operation values are taken from the `Operation` enum
in code, not from a hand-written list, so the two cannot drift.

At the time of writing the enum holds `hyde`, `chunk_summary`, `community_summary` and
`benchmark_query`. Only `hyde`, `chunk_summary` and `unknown` (benchmark query generation)
currently reach the service's chat function, so the other series stay at zero until something
uses them.

## Example queries

```promql
# Tokens per hour by operation
sum by (operation, direction) (increase(treeweft_llm_tokens_total[1h]))

# Any model drift in the last day
sum(increase(treeweft_llm_response_conditions_total{condition="model_mismatch"}[1d])) > 0

# Share of chunk summaries cut off, needs a request counter to be exact; as a rate:
rate(treeweft_llm_response_conditions_total{operation="chunk_summary",condition="truncated"}[15m])
```

```traceql
{ name = "llm.chat" && span.treeweft.llm.truncated = true }
{ name = "llm.chat" && span.treeweft.llm.model_mismatch = true }
{ name = "llm.chat" && span.gen_ai.usage.output_tokens > 500 }
```
