# Feature Specification: LLM Response Signals and Agent Repeat-Call Metric

**Feature Branch**: `004-llm-response-signals`

**Created**: 2026-10-06

**Status**: Draft

**Input**: User description: "Three additive observability improvements. (1) Record
standard generative-AI attributes on every LLM chat call — requested model, served model,
input and output tokens, finish reason — alongside the existing `treeweft.*` attributes.
(2) Use those to flag three conditions: the served model differs from the requested one;
the response was cut off at the token limit; the response was empty. (3) In the agentic
benchmark, report per arm how often the agent repeats the same tool call with the same
arguments, to put a number on compensatory fetching next to mean turns." — "one spec, start it".

## Background

Treeweft calls an LLM for chunk summaries and HyDE, and for query generation in the benchmark
tooling.
Each call is traced, but the trace records only the configured model name, the operation and
the token limit. What the endpoint actually returned about the call — which model served it,
how many tokens it consumed, why generation stopped — is discarded as soon as the text is
extracted.

Three recurring problems are invisible as a result:

- **Model drift.** A vendor or a local server can serve a different model from the one
  configured. The benchmark harness already records served models for its own runs; the
  service itself does not, so drift during indexing goes unseen.
- **Degenerate responses.** A summary cut off at the token limit, or an empty response, is
  indistinguishable in traces and metrics from a good one.
- **Token cost per operation.** There is no way to see, from the running service, how many
  tokens summaries cost compared with HyDE.

Separately, the agentic benchmark's governing metric is mean agent turns judged with tokens.
Every payload-shrinking idea so far lost because the agent re-queried to compensate. Mean turns
shows that this happened; nothing measures the re-querying itself.

This feature records what the endpoint reports, flags the three conditions, and adds a
repeat-call measure to the benchmark. It changes no prompt, no ranking, no payload and no
default behaviour.

## Clarifications

### Session 2026-10-06

- Q: When should a service LLM call be flagged as a model mismatch, given that some servers report the served model in a different form from the one requested? → A: First-seen baseline. The served name reported the first time each requested model is seen after startup becomes the baseline; a later call is flagged when its served name differs from that baseline.
- Q: Besides exact repeats of the same tool call, should the benchmark also report how many times each tool was called per query, per arm? → A: Yes. Record calls per tool for each query and arm, and report mean calls per tool per arm, alongside the exact-repeat measures. No near-duplicate detection.
- Q: When an agent's answer or a judge's verdict was cut off at the token limit, how should that query be treated in the benchmark results? → A: Count and mark. The run summary reports the counts, and each affected query's result row is marked per arm. Aggregates and significance tests are computed exactly as today; nothing is excluded.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An operator sees what each LLM call actually consumed and who served it (Priority: P1)

An operator opens the trace for an indexing job or a search and looks at an LLM call. The call
shows the model that was requested, the model the endpoint reports having served, the input and
output token counts, and the reason generation stopped. These use the industry-standard
generative-AI attribute names, so stock dashboards and trace tools that understand those names
work without custom mapping. Every attribute the call carried before is still there, unchanged.

**Why this priority**: The other two stories in the service depend on this data being captured,
and it is independently useful: it is the first time token cost per operation is visible from
the running service.

**Independent Test**: Run one chunk-summary call against an endpoint that reports usage. The
resulting trace shows requested model, served model, input tokens, output tokens and finish
reason under the standard names, and every previously recorded attribute is still present with
its previous name and value.

**Acceptance Scenarios**:

1. **Given** an endpoint that reports usage, served model and finish reason, **When** any LLM
   chat call completes, **Then** the call's trace record carries all five values under the
   standard generative-AI attribute names.
2. **Given** the existing trace dashboard, **When** the feature is deployed, **Then** every
   panel renders as before, because no existing attribute was renamed or removed.
3. **Given** an endpoint that omits usage or the served-model field, **When** a call completes,
   **Then** the missing values are left unset rather than recorded as zero or as the requested
   model, and the call succeeds exactly as it does today.
4. **Given** LLM calls across several operations, **When** an operator views service metrics,
   **Then** input and output token totals are available broken down by operation.

---

### User Story 2 - Model drift and degenerate responses are flagged, not silent (Priority: P2)

An operator wants to know when the service is quietly doing something other than intended.
When the endpoint starts serving a different model from the one it was serving, when a response is cut off
at the token limit, or when a response comes back empty, the call is marked with that
condition and the occurrence is counted per operation. The operator can find the affected
calls in traces and see the rate over time in metrics.

**Why this priority**: These are the "keeps working while quietly doing something else"
failures the project treats as defects. They build directly on Story 1's data.

**Independent Test**: Drive calls through a stub endpoint: two for the same requested model
whose reported served models differ, one that stops for length, one that returns no text. Each
call's trace carries the matching condition, and each condition's counter increases by one for
that operation. A further, ordinary call carries no condition and increments nothing.

**Acceptance Scenarios**:

1. **Given** a requested model whose first call after startup reported served model X, **When**
   a later call for the same requested model reports served model Y, **Then** that call is
   marked as a model mismatch and the mismatch count for that operation increases.
2. **Given** a server that reports the served model in a different form from the requested
   name (a file path, a dated identifier) and reports it consistently, **When** calls complete,
   **Then** none is marked as a mismatch.
3. **Given** a response whose finish reason indicates the token limit was reached, **When** the
   call completes, **Then** it is marked as truncated and counted for that operation.
4. **Given** a response with no usable text after reasoning content is removed, **When** the
   call completes, **Then** it is marked as empty and counted for that operation.
5. **Given** any of the three conditions, **When** the call completes, **Then** the result
   returned to the caller is the same as it is today; flagging changes only what is observed.

---

### User Story 3 - A benchmark reader sees how much each arm repeats itself (Priority: P3)

Someone reading an agentic benchmark result wants to know whether an arm's extra turns come
from the agent re-issuing the same tool call. For each query and arm the harness records how
many tool calls repeated an earlier call exactly, and whether the query hit a loop (the same
call made three or more times), and how many times each tool was called. The per-arm summary
and the comparison table report the share of queries that looped, the mean number of repeated
calls, and the mean calls per tool, next to mean turns. Exact repeats show an agent that is
stuck; calls per tool show an agent that searched again with different wording or read more
files to compensate. The harness
also reports how many agent and judge responses were cut off at the token limit, and marks the
affected queries, so a truncated answer or verdict is not mistaken for a real one and can be
found again. Marked queries still count in every aggregate exactly as they do today.

**Why this priority**: It refines an existing measurement rather than exposing something
currently invisible in production, and it touches only the benchmark harness.

**Independent Test**: Run the harness with a scripted agent that calls one tool three times
with identical arguments and another tool twice with different arguments. The query's result
row reports two repeated calls, a loop, and call counts of three and two for the two tools;
the per-arm summary reports the looped share and the means. Token totals, turn counts and judge scores are identical to a run of the same script
without the feature.

**Acceptance Scenarios**:

1. **Given** an agent run in which the same tool is called with the same arguments three
   times, **When** the query finishes, **Then** its result records two repeated calls and marks
   the query as looped.
2. **Given** two calls to the same tool whose arguments differ only in key order or
   whitespace, **When** they are compared, **Then** they count as the same call.
3. **Given** a completed cell, **When** the summary is written, **Then** each arm reports the
   share of queries that looped and the mean repeated calls per query, and the comparison
   table shows them beside mean turns.
4. **Given** either agent protocol (structured tool calls or the text protocol), **When** a
   run completes, **Then** the repeat measures are computed the same way.
5. **Given** a multi-repo rollup that includes cells produced before this feature, **When** the
   rollup is built, **Then** those cells show the repeat measures as not available rather than
   as zero.
6. **Given** an arm whose agent called a search tool four times with four different queries,
   **When** the query finishes, **Then** its result records four calls for that tool and zero
   repeated calls.
7. **Given** a cell whose arms expose different tools, **When** the comparison table is built,
   **Then** each arm's calls-per-tool figures list only the tools that arm has.
8. **Given** agent or judge responses that stopped at the token limit, **When** the summary is
   written, **Then** it reports the count of each.
9. **Given** a query where one arm's agent response was cut off at the token limit, **When**
   its result row is written, **Then** that arm's entry is marked as having a cut-off agent
   response and the other arms' entries are not.
10. **Given** a query whose judge verdict for one arm was cut off, **When** its result row is
    written, **Then** that arm's entry is marked as having a cut-off verdict.
11. **Given** a cell containing marked queries, **When** aggregates and significance tests are
    computed, **Then** they include the marked queries and equal the values the harness
    produces today for the same data.

---

### Edge Cases

- The endpoint reports the served model as an identifier in an unrelated format, such as a
  local file path. Because comparison is against the first-seen served name, not the requested
  name, a consistent report is never flagged.
- A floating alias is requested and the vendor reports the dated model behind it. The first
  dated identifier seen becomes the baseline; a later remap to a different dated identifier is
  flagged.
- The wrong model is being served from the very first call. This is not flagged, because it is
  the baseline. Both the requested and the served name are on every call's trace record
  (Story 1), so the operator can still see it.
- The service restarts. The baseline is forgotten and re-established from the first call
  after startup.
- The first response for a requested model omits the served name. No baseline is set; the
  first response that does report one sets it.
- A response is empty because the model produced only reasoning content that is stripped.
  This counts as empty.
- A call fails with an error or times out. No response conditions are recorded; the existing
  error recording is unchanged.
- An agent's tool call has arguments that cannot be parsed. It is never counted as a repeat
  of another call, and it is not counted in calls per tool, because no tool ran.
- The agent names a tool that does not exist. It is not counted in calls per tool.
- A response is both truncated and reported under a different model. Both conditions are
  recorded.
- Tracing is disabled or the tracing library is absent. Calls behave exactly as today, and
  counters still work.

## Requirements *(mandatory)*

### Functional Requirements

**Recording what the endpoint reports (service)**

- **FR-001**: Every LLM chat call made by the service MUST record the requested model, the
  served model as reported by the endpoint, the input token count, the output token count and
  the finish reason on its trace record, using the OpenTelemetry generative-AI semantic
  convention names.
- **FR-002**: All attributes recorded on LLM calls today MUST remain present with unchanged
  names and values.
- **FR-003**: A value the endpoint does not report MUST be left unset. It MUST NOT be recorded
  as zero, as an empty string, or copied from the request.
- **FR-004**: The service MUST expose input and output token totals as metrics, broken down by
  operation.
- **FR-005**: Recording MUST NOT capture prompt or response text.

**Flagging conditions (service)**

- **FR-006**: For each requested model, the service MUST remember the served model reported by
  the first response after startup that includes one. A later call for that requested model
  MUST be marked as a model mismatch when its served model differs from the remembered one.
  The remembered value MUST NOT change for the life of the process.
- **FR-007**: A call MUST be marked as truncated when its finish reason indicates the token
  limit was reached.
- **FR-008**: A call MUST be marked as empty when the response contains no usable text after
  reasoning content is removed.
- **FR-009**: Each condition MUST be counted in service metrics, broken down by operation.
- **FR-010**: Flagging MUST NOT change the value returned to the caller, retry behaviour,
  caching behaviour or any default.
- **FR-011**: A condition MUST NOT be recorded when the data needed to evaluate it is missing.

**Benchmark harness**

- **FR-012**: For each query and arm, the harness MUST record the number of repeated tool
  calls: calls whose tool and arguments equal those of an earlier call in the same run.
- **FR-013**: For each query and arm, the harness MUST record whether the run looped: the same
  tool and arguments were called three or more times.
- **FR-014**: Argument comparison MUST ignore key order and insignificant whitespace. Calls
  whose arguments differ in any other way are different calls; there is no similarity matching.
- **FR-015**: The per-arm summary and the comparison table MUST report the share of queries
  that looped and the mean repeated calls per query.
- **FR-021**: For each query and arm, the harness MUST record the number of executed calls to
  each tool.
- **FR-022**: The per-arm summary and the comparison table MUST report mean calls per query
  for each tool the arm exposes.
- **FR-016**: The loop threshold used MUST be recorded in the run summary.
- **FR-017**: The repeat and per-tool measures MUST be computed from the calls the run already made. The
  feature MUST NOT add LLM calls, tool calls, or tokens, and MUST NOT alter what the agent sees.
- **FR-018**: The repeat and per-tool measures MUST be recorded whether or not full transcripts are being
  saved.
- **FR-019**: For cells that predate the feature, a rollup MUST report the repeat and per-tool
  measures as not available, never as zero: the rollup table shows the repeat measures, and
  the rollup data carries both the repeat and the per-tool measures.
- **FR-020**: The run summary MUST report how many agent responses and how many judge
  responses stopped at the token limit.
- **FR-023**: Each query's result MUST mark, per arm, whether any agent response in that run
  stopped at the token limit and whether the judge's verdict did.
- **FR-024**: Marked queries MUST remain in every aggregate and significance test, computed as
  they are today.

### Key Entities

- **LLM call record**: one chat call made by the service. Gains requested model, served model,
  input tokens, output tokens, finish reason, and zero or more response conditions.
- **Response condition**: one of model mismatch, truncated, empty. Attached to an LLM call
  record and counted per operation. "Truncated" is the term used in field and attribute names;
  "cut off" is its plain-language name in this document and means the same thing: generation
  stopped because the token limit was reached.
- **Agent run result**: one query run by one arm. Gains repeated-call count, a looped flag,
  a count of calls per tool, and cut-off markers for the agent's responses and the verdict.
- **Arm summary**: the per-arm aggregate for a cell. Gains looped share, mean repeated calls,
  mean calls per tool, and truncated-response counts.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For an endpoint that reports usage, 100% of service LLM calls show requested
  model, served model, input tokens, output tokens and finish reason in their trace.
- **SC-002**: An operator can answer "how many tokens did chunk summaries consume in the last
  hour, compared with HyDE?" from service metrics alone.
- **SC-003**: Each of the three conditions, when induced in a test, is visible on the affected
  call and in the per-operation count, with no false flag on an ordinary call.
- **SC-004**: On a deployment whose endpoint keeps serving the same model, the model-mismatch
  rate is zero, whatever form the endpoint reports the served name in.
- **SC-005**: The existing trace dashboard shows the same panels with the same data before and
  after the change.
- **SC-006**: A benchmark cell run with and without the feature, on the same scripted agent,
  produces identical token totals, turn counts and judge scores.
- **SC-007**: A reader can state, for each arm of a cell, what share of queries looped and how
  many times per query each tool was called, without opening a transcript.
- **SC-008**: Indexing one small reference repository takes the same wall-clock time before and
  after the change, to within ±5%.
- **SC-009**: A reader can list which queries in a cell had a cut-off agent response or verdict,
  per arm, from the result rows alone.

## Assumptions

- **Detection only.** A truncated or empty response is flagged and counted, not retried,
  rejected or kept out of the summary cache. Changing what the service does with such a
  response could move search quality and would need a benchmark run first; it is left to a
  follow-up informed by the rates this feature reveals.
- **Chat calls only.** Embedding and reranker calls are out of scope. They have their own
  trace records and their own failure modes.
- **The benchmark harness keeps its own served-model record.** It already records served
  models per run; this feature does not replace that.
- **Mismatch means drift, not misconfiguration.** The rule detects the served model changing
  while the service runs. A wrong model present from startup is visible on the trace record
  but is not flagged; verifying the served model before a long run remains a separate,
  existing obligation.
- **Loop threshold is three identical calls within one query's run**, fixed for now and
  recorded so it can be changed later without ambiguity about old results.
- **Additive benchmark fields.** The repeat and per-tool measures add columns to results; they do not
  change how any existing number is computed, so existing results remain comparable.
- **Backfill is limited.** Past runs can gain repeat measures only where full transcripts were
  saved; other past runs stay "not available".
- **No new services.** Signals go to the tracing and metrics destinations the project already
  uses.
