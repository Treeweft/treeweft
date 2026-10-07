# Tasks: LLM Response Signals and Agent Repeat-Call Metric

**Input**: Design documents from `specs/004-llm-response-signals/`: [plan.md](plan.md),
[spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md),
[contracts/telemetry.md](contracts/telemetry.md),
[contracts/benchmark-results.md](contracts/benchmark-results.md), [quickstart.md](quickstart.md).

**Tests**: REQUIRED (constitution II). In each phase the test tasks come first and MUST be run
and seen to fail before the implementation task that makes them pass. All tests are unit tests:
no real model server, database or tracing backend.

**Commands** (from the repository root; `env -u PYTHONPATH` is required on this machine):

```bash
env -u PYTHONPATH python -m pytest tests/unit/<file> -q
env -u PYTHONPATH python -m pytest tests/unit -q
```

**Binding constraints for every task**:

- `_chat` returns the same value as today in every case; no retry, cache or default changes
  (FR-010).
- No existing span attribute, metric name or result field is renamed, removed or recomputed
  (FR-002, FR-024).
- No prompt or response text is recorded (FR-005).
- The harness adds no LLM calls, tool calls or tokens (FR-017).
- Write original code against the published OpenTelemetry attribute names; do not copy code
  from any third-party instrumentation library.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US3 from spec.md

---

## Phase 1: Setup

- [X] T001 Create the feature branch `004-llm-response-signals` from an up-to-date `main` and
  carry the untracked `specs/004-llm-response-signals/` directory onto it. Do not commit to
  `main`. Confirm `env -u PYTHONPATH python -m pytest tests/unit -q` is green before any change
  and record the pass count as the baseline.

---

## Phase 2: Foundational (blocking prerequisites)

**Purpose**: the pure response-reading module that all three stories use.

**⚠️ CRITICAL**: no story work starts until this phase is complete.

- [X] T002 Write failing tests in `tests/unit/test_llm_response_signals.py` for
  `treeweft.domain.llm_response` covering `ResponseSignals` and `is_truncation`:
  - a full OpenAI-compatible body yields `served_model`, `input_tokens`
    (from `usage.prompt_tokens`), `output_tokens` (from `usage.completion_tokens`) and
    `finish_reason` (from `choices[0].finish_reason`);
  - "A missing value is `none`, never `0`, `""` or a copy of the request": body without
    `usage`, without `model`, with `model: ""`, with non-integer token values, with
    `finish_reason: null`;
  - `truncated` is true for finish reason `length` and for `max_tokens`, compared
    case-insensitively (`LENGTH` and `Max_Tokens` are true too), false for `stop` and
    `tool_calls`, and none when `finish_reason` is unset;
  - `strip_thinking` removes `<think>...</think>` blocks, is case-insensitive on the tag, and
    returns `""` for `None`; `treeweft.domain.benchmark.agent_protocol.strip_thinking` is the
    same object;
  - `empty` is true when content is absent, null, `""`, whitespace, or only a
    `<think>...</think>` block; false for ordinary text; none when there are no `choices`;
  - "Building it never raises, whatever shape the response has": `{}`, `None`, a list,
    `usage` as a string, `choices` as an empty list, `choices[0]` as a string;
  - the object exposes no prompt or response text.
- [X] T003 Implement `src/treeweft/domain/llm_response.py`: a frozen `ResponseSignals` dataclass
  with exactly the fields in data-model.md, a `from_response(body)` constructor that never
  raises, `TRUNCATION_REASONS = frozenset({"length", "max_tokens"})` and
  `is_truncation(finish_reason) -> bool | None`, which lower-cases the finish reason before
  comparing. Pure: import nothing from `adapters/`, `application/`, `infrastructure/` or the
  `domain.benchmark` package. Do not add another copy of the reasoning-block stripping rule:
  move `strip_thinking` and its regex from `src/treeweft/domain/benchmark/agent_protocol.py`
  into this module unchanged (it treats `None` as `""`), and have `agent_protocol.py` import
  and re-export it so every existing import keeps working. Leave `_strip_thinking` in
  `src/treeweft/adapters/llm_api/llm_adapter.py` exactly as it is: it raises on `None`, which
  is what makes a null-content response return `None` today (research R6). Make T002 pass and
  confirm the existing tests that use `agent_protocol` pass unmodified.

**Checkpoint**: `tests/unit/test_llm_response_signals.py` is green; the full unit suite still
matches the T001 baseline plus the new tests.

---

## Phase 3: User Story 1 — An operator sees what each LLM call consumed and who served it (Priority: P1) 🎯 MVP

**Goal**: every service LLM chat call records requested model, served model, input and output
tokens and finish reason on its span under the standard names, and token totals per operation
are available as metrics.

**Independent Test**: one mocked chunk-summary call against an endpoint that reports usage
produces a span with all the attributes in contracts/telemetry.md and the three unchanged
`treeweft.*` attributes, and `treeweft_llm_tokens_total` rises by the reported counts.

### Tests for User Story 1 (write first)

- [X] T004 [US1] Write failing tests in `tests/unit/test_llm_adapter_signals.py` for `_chat` in
  `treeweft.adapters.llm_api.llm_adapter`. Mock the HTTP client the way the `mock_llm_httpx`
  fixture in `tests/conftest.py` does (patch `_get_client`), and capture spans by patching
  `treeweft.infrastructure.tracing.get_tracer` to return a tracer from a local `TracerProvider`
  with an in-memory span exporter. Assert:
  - a response with `model`, `usage` and `finish_reason` yields an `llm.chat` span carrying
    `gen_ai.operation.name = "chat"`, `gen_ai.request.model`, `gen_ai.request.max_tokens`,
    `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` and
    `gen_ai.response.finish_reasons` as a one-element list (FR-001);
  - `treeweft.llm_model`, `treeweft.operation` and `treeweft.max_tokens` are still present with
    the same values as before (FR-002);
  - a response with no `usage` and no `model` yields a span with none of the four response
    attributes, and `_chat` still returns the text (FR-003);
  - no span attribute value contains the prompt or the response text (FR-005);
  - `treeweft_llm_tokens_total{operation, direction="input"|"output"}` rises by exactly the
    reported counts and does not move for a response without usage (FR-004);
  - a response whose `usage` is a string does not raise and returns the text (research R7);
  - a request that raises still returns `None`, marks the span as an error, and sets no
    response attributes;
  - exactly one HTTP request is made per call, and the return value is identical when
    `get_tracer` returns the no-op tracer (FR-010, SC-008);
  - with the no-op tracer the token counter still rises by the reported counts (edge case:
    tracing disabled).

### Implementation for User Story 1

- [X] T005 [US1] Add counter `treeweft_llm_tokens_total` with labels `operation` and `direction`
  to `src/treeweft/infrastructure/metrics.py` on the existing `registry`. Create every series
  at zero at import for every value of the `Operation` enum in `src/treeweft/domain/audit.py`
  plus `unknown`, and directions `input`, `output`, following the existing `hyde_fallbacks`
  pattern. Read the operation values from the enum; do not write the list out by hand. Do not
  rename or reorder existing metrics.
- [X] T006 [US1] Extend `_chat` in `src/treeweft/adapters/llm_api/llm_adapter.py`:
  - add `gen_ai.operation.name`, `gen_ai.request.model` and `gen_ai.request.max_tokens` to the
    attributes passed when the span is opened, alongside the three existing ones;
  - after `resp.raise_for_status()`, parse the body once, build
    `ResponseSignals.from_response(...)`, and set `gen_ai.response.model`,
    `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` and
    `gen_ai.response.finish_reasons` on the span, each only when its value is not none;
  - increment `metrics.llm_tokens_total` for the call's operation by the reported input and
    output counts;
  - wrap the reading and recording so it can never raise (research R7);
  - leave the two existing lines that extract and strip the content exactly as they are, after
    the recording, so the return value cannot change (research R6);
  - do not set `gen_ai.provider.name` (research R2).
  Make T004 pass. Then run `tests/unit/test_llm_adapter.py`,
  `tests/unit/test_llm_timeout_queue_wait.py` and `tests/unit/test_llm_search_reserved_slots.py`
  and confirm they pass unmodified.
- [X] T007 [P] [US1] Update the OpenTelemetry tracing bullet in `docs/engineering-notes.md` to
  list the new `llm.chat` attributes and the `treeweft_llm_tokens_total` counter, state that the
  existing `treeweft.*` attributes are unchanged, and link to
  `specs/004-llm-response-signals/contracts/telemetry.md`.

**Checkpoint**: US1's acceptance scenarios 1–4 are verified by unit tests. The live `/metrics`
and trace check in quickstart.md is run in Phase 6.

---

## Phase 4: User Story 2 — Model drift and degenerate responses are flagged (Priority: P2)

**Goal**: a served-model change, a truncated response and an empty response are each marked on
the call and counted per operation, without changing what the caller receives.

**Independent Test**: stubbed calls reproduce each condition; each affected span carries the
matching `treeweft.llm.*` attribute as true and its counter rises by one; an ordinary call
carries all three as false and moves no counter.

**Depends on**: Phase 3 (T006 edits the same function and provides the parsed signals).

### Tests for User Story 2 (write first)

- [X] T008 [P] [US2] Add failing tests to `tests/unit/test_llm_response_signals.py` for
  `ServedModelBaseline` with exactly this behaviour from data-model.md:
  - "`observe(requested, served)` with `served` none: returns none; nothing remembered";
  - "first `observe` for a requested model: remembers `served`; returns `false`";
  - "later `observe`, same `served`: returns `false`";
  - "later `observe`, different `served`: returns `true`; the remembered value does not
    change" — a third call with the original name is false again, and a third call with the
    new name is still true;
  - baselines for two different requested models are independent;
  - a served name in an unrelated form (a file path, a dated identifier) reported consistently
    is never a mismatch;
  - after a none `served`, the next non-none `served` sets the baseline.
- [X] T009 [US2] Add failing tests to `tests/unit/test_llm_adapter_signals.py` for conditions on
  `_chat`. Give each test a fresh baseline by patching the adapter's module-level
  `ServedModelBaseline` instance with a new one; do not add a reset method to the class:
  - two calls for the same requested model reporting served `m-1` then `m-2`: the second span
    has `treeweft.llm.model_mismatch = true` and
    `treeweft_llm_response_conditions_total{condition="model_mismatch"}` rises by one for that
    operation (FR-006, FR-009);
  - `finish_reason: "length"`: `treeweft.llm.truncated = true`, counter +1 (FR-007);
  - content empty after a `<think>` block: `treeweft.llm.empty = true`, counter +1 (FR-008);
  - an ordinary response: all three attributes present and `false`, no counter change;
  - no `finish_reason`: `treeweft.llm.truncated` absent; no served model:
    `treeweft.llm.model_mismatch` absent (FR-011);
  - a response that is both truncated and a mismatch records both and increments both series;
  - in every case above the value `_chat` returns equals what it returned before this feature,
    including `None` with an error status for null content (FR-010);
  - a failed request sets no condition attributes and increments no condition counter;
  - with the no-op tracer each true condition still increments its counter (edge case:
    tracing disabled).

### Implementation for User Story 2

- [X] T010 [US2] Add `ServedModelBaseline` to `src/treeweft/domain/llm_response.py` with an
  `observe(requested, served) -> bool | None` method. State transition `unset → set`, once per
  requested model; never reset. No lock: the check has no `await` (research R5). Make T008 pass.
- [X] T011 [US2] Add counter `treeweft_llm_response_conditions_total` with labels `operation`
  and `condition` to `src/treeweft/infrastructure/metrics.py`. Create every series at zero at
  import for the same enum-derived operation values as T005 and conditions `model_mismatch`,
  `truncated`, `empty` (fifteen series with today's enum).
- [X] T012 [US2] Extend `_chat` in `src/treeweft/adapters/llm_api/llm_adapter.py`: keep one
  module-level `ServedModelBaseline`; inside the same never-raising block as T006, compute the
  mismatch from `observe(LLM_MODEL, signals.served_model)` and take `truncated` and `empty` from
  the signals; set `treeweft.llm.model_mismatch`, `treeweft.llm.truncated` and
  `treeweft.llm.empty` each to its boolean value when it is not none and leave it unset when it
  is none; increment `metrics.llm_response_conditions_total` once per condition that is true.
  Do not change the extraction lines or the `except` block. Make T009 pass and confirm T004
  still passes.
- [X] T013 [P] [US2] Add a section to `docs/observability-runbook.md` describing the three
  conditions, the first-seen baseline rule and its limits (a wrong model from startup is not
  flagged; a restart forgets the baseline; each worker process keeps its own), the counter, and
  the example PromQL and TraceQL queries from contracts/telemetry.md. State that the feature
  only detects: truncated or empty responses are not retried or kept out of the cache.

**Checkpoint**: US2's acceptance scenarios 1–5 are verified by unit tests.

---

## Phase 5: User Story 3 — A benchmark reader sees how much each arm repeats itself (Priority: P3)

**Goal**: each query and arm records calls per tool, exact repeats, a looped flag and cut-off
markers; the summary, comparison table and rollup report them; old results show `n/a`.

**Independent Test**: a scripted agent calling one tool three times identically and another
twice differently yields two repeated calls, a loop and counts of three and two; token totals,
turns and judge scores for the existing scripted tests are unchanged.

**Depends on**: Phase 2 only. Independent of Phases 3 and 4.

### Tests for User Story 3 (write first)

- [X] T014 [P] [US3] Write failing tests in `tests/unit/test_tool_call_stats.py` for
  `treeweft.domain.benchmark.tool_call_stats`:
  - tool A recorded three times with identical arguments and tool B twice with different
    arguments gives `repeated == 2` (FR-012), `looped is True` (FR-013), `counts_by_tool == {"A": 3, "B": 2}`
    (FR-021);
  - arguments differing only in key order count as the same call; so do nested objects with
    reordered keys;
  - arguments differing in any value, in list order, or in an extra key are different calls
    (FR-014);
  - the same arguments to two different tools are different calls;
  - two identical calls give `repeated == 1` and `looped is False`; `LOOP_THRESHOLD == 3`;
  - an empty tally gives `repeated == 0`, `looped is False`, `counts_by_tool == {}`;
  - invariants: "`sum(counts_by_tool.values())` equals the run's existing `tool_calls`",
    "`repeated` equals `tool_calls` minus the number of distinct calls", "`looped` implies
    `repeated >= 2`";
  - arguments that are not JSON-serialisable by default do not raise.
- [X] T015 [P] [US3] Write failing tests in `tests/unit/test_agent_llm_chat_full.py` for
  `AgentLLM.chat_full` in `treeweft.adapters.benchmark.agent_llm`, mocking `httpx.AsyncClient`:
  - an OpenAI-compatible response returns a `ChatResult` whose `finish_reason` is
    `choices[0].finish_reason`, with `content`, `usage` and `tool_calls` equal to what `chat()`
    returns today;
  - the native Anthropic path returns `finish_reason` from `stop_reason`;
  - a response without a finish reason gives `finish_reason is None`;
  - `chat()` still returns exactly `(content, usage, tool_calls)` and makes one request;
  - `served_models` is still recorded on both paths.
- [X] T016 [US3] Add failing tests to `tests/unit/test_native_tools_loop.py` (and the existing
  ReAct-loop tests; find them with `grep -rn "run_agent(" tests/unit`) using the scripted fake
  LLM already defined there:
  - three identical calls to one tool plus two differing calls to another give
    `repeated_tool_calls == 2`, `looped is True` and the expected `tool_call_counts`
    (acceptance scenario 1);
  - four calls to one search tool with four different queries give a count of four and
    `repeated_tool_calls == 0` (scenario 6);
  - a call with unparseable arguments and a call to an unknown tool appear in neither
    `tool_call_counts` nor `repeated_tool_calls`;
  - the same script gives identical figures with `native_tools=True` and `native_tools=False`
    (scenario 4), and the fake records the same number of LLM and tool calls as before the
    feature (FR-017);
  - a fake exposing `chat_full` that reports a truncation finish reason on one response gives
    `truncated_responses == 1`; the forced final answer is counted too;
  - a fake exposing only `chat()` (the existing fakes) still works and gives
    `truncated_responses == 0`;
  - every existing assertion on `turns`, `tool_calls` and token totals in this file passes
    without edits (SC-006).
- [X] T017 [P] [US3] Add failing tests for the judge (locate the existing ones with
  `grep -rn "judge_answer\|parse_judge_json" tests/unit`; default file
  `tests/unit/test_benchmark.py`): a verdict whose response reports a truncation finish reason
  gives `JudgeScore.truncated is True`; a verdict that is cut off and therefore unparseable
  gives `truncated is True` together with `parse_ok is False` (the main case: a cut-off verdict
  is usually broken JSON); an ordinary one gives `False`; a judge client exposing
  only `chat()` gives `None`; a missing gold answer, an empty candidate and a failed call give
  `None`; `as_dict()` includes `truncated`; scores and `parse_ok` are unchanged in every case.
- [X] T018 [P] [US3] Add failing tests to `tests/unit/test_agent_metrics.py` for `aggregate`:
  - rows carrying the new fields give per-arm `looped_share`, `mean_repeated_tool_calls`,
    `mean_tool_calls_by_tool`, `agent_truncated_queries` and `judge_truncated_queries`
    (FR-015, FR-022), and top-level `loop_threshold == 3` (FR-016);
  - in `mean_tool_calls_by_tool`, "a row that carries `tool_call_counts` but did not call a
    given tool counts as zero for that tool; a row without the field is left out of the mean";
  - arms exposing different tools list only their own tools (scenario 7);
  - rows without the new fields give `None` for every new per-arm field, never `0` (FR-019);
  - every existing key in the summary has the same value with and without the new fields in
    the rows, including win rates and p-values when some rows are marked truncated (FR-024).
- [X] T019 [P] [US3] Add failing tests to `tests/unit/test_rollup.py`:
  - `format_comparison_table` on a summary with the new fields includes rows "Mean turns",
    "Looped queries" and "Mean repeated calls" and a calls-per-tool line for each arm, below
    the three existing rows, which are byte-identical to today's;
  - on a summary without the new fields those rows show `n/a` and the calls-per-tool lines
    show `n/a`;
  - `multi_repo_rollup` carries `grep_looped_share`, `treeweft_looped_share`,
    `grep_mean_repeated_tool_calls`, `treeweft_mean_repeated_tool_calls`,
    `grep_mean_tool_calls_by_tool` and `treeweft_mean_tool_calls_by_tool` per repo, `None` for a
    repo whose summary predates the feature;
  - `format_rollup_table` shows two new columns as `grep / treeweft` with `n/a` for such a
    repo, and every existing column and the pooled row are unchanged;
  - pooled figures for the new columns are computed over the repos that have them.
- [X] T020 [P] [US3] Add failing tests to `tests/unit/test_agentic_runner.py`:
  - `_arm_row` writes `tool_call_counts`, `repeated_tool_calls`, `looped`,
    `agent_truncated_responses` and `agent_truncated`, with and without
    `debug_transcripts` (FR-018), and the values of `tool_call_counts` sum to `tool_calls`;
  - a query where only one arm's agent response was cut off marks only that arm (scenario 9,
    FR-023);
  - the row's `judge` block carries `truncated` (scenario 10);
  - the run summary carries `agent_truncated_responses` and `judge_truncated_responses`
    totals (FR-020).
- [X] T021 [P] [US3] Add a failing test to `tests/unit/test_opik_tracing.py`: for a row carrying
  the new fields the feedback scores include `looped` (0 or 1) and `repeated_tool_calls`; for
  a row without them neither score is emitted and nothing raises.

### Implementation for User Story 3

- [X] T022 [P] [US3] Implement `src/treeweft/domain/benchmark/tool_call_stats.py`:
  `LOOP_THRESHOLD = 3` and a `ToolCallTally` with `record(tool, args)`, `counts_by_tool`,
  `repeated` and `looped`. Call identity is the tool name plus
  `json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)`. Pure, no I/O. Make
  T014 pass.
- [X] T023 [P] [US3] In `src/treeweft/adapters/benchmark/agent_llm.py` add a `ChatResult`
  dataclass (`content`, `usage`, `tool_calls`, `finish_reason`) and
  `async def chat_full(...) -> ChatResult` holding the current body of `chat()`, reading
  `finish_reason` from `choices[0]` on the OpenAI-compatible path and `stop_reason` on the
  native Anthropic path. Reduce `chat()` to a call to `chat_full` returning the same three
  values. Do not store a per-call value on `self`: arms and judges share one client under
  `asyncio.gather` (research R8). Make T015 pass.
- [X] T024 [US3] In `src/treeweft/application/benchmark/agent_loop.py`:
  - add fields to `AgentRunResult` with these defaults: `tool_call_counts` empty mapping,
    `repeated_tool_calls` `0`, `looped` `False`, `truncated_responses` `0`;
  - add a module helper that calls `llm.chat_full(...)` when the client has it and otherwise
    `llm.chat(...)` with an unknown finish reason, and use it at all four `llm.chat` call
    sites in this file;
  - in both loops create one `ToolCallTally` per run and call `record(tool, args)` at the two
    points where `tool_calls += 1` already happens, and nowhere else;
  - count a response as cut off when `is_truncation(finish_reason)` is true, including the
    forced final answer;
  - populate the four new fields on every `AgentRunResult` return path, including the early
    error returns.
  Do not change the messages sent, the order of calls or token accounting. Make T016 pass.
- [X] T025 [P] [US3] Add `truncated: bool | None = None` to `JudgeScore` in
  `src/treeweft/domain/benchmark/judge_schema.py` and include it in `as_dict()`. In
  `src/treeweft/application/benchmark/judge.py` use the same `chat_full`-or-`chat` fallback and
  set `truncated` from `is_truncation(finish_reason)` whenever a judge response was received,
  whatever `parse_ok` turns out to be: `parse_judge_json` returns a `parse_ok=False` score for
  broken JSON without raising, and that score must still carry the marker. Leave it `None`
  only on the three returns where no response was received (missing gold answer, empty
  candidate, call failed twice). Keep the retry and scores exactly as they are. Make T017
  pass.
- [X] T026 [US3] In `src/treeweft/application/benchmark/agentic_runner.py`: add
  `tool_call_counts`, `repeated_tool_calls`, `looped`, `agent_truncated_responses` and
  `agent_truncated` to `_arm_row` unconditionally; after `aggregate(...)` set
  `agg["agent_truncated_responses"]` and `agg["judge_truncated_responses"]` as run-wide totals
  computed from the rows. Leave the per-query progress line and all existing keys unchanged.
  Make T020 pass.
- [X] T027 [US3] In `src/treeweft/domain/benchmark/agent_metrics.py`: add a mean that returns
  `None` when no value is present and use it only for the new fields; in `_arm_means` add
  `looped_share`, `mean_repeated_tool_calls`, `mean_tool_calls_by_tool`,
  `agent_truncated_queries` and `judge_truncated_queries` per contracts/benchmark-results.md;
  in `aggregate` add top-level `loop_threshold` from `LOOP_THRESHOLD`. Do not add the new
  fields to `_ARM_NUMERIC_FIELDS` and do not change the existing `_mean`. Make T018 pass.
- [X] T028 [US3] In `src/treeweft/domain/benchmark/rollup.py`: append the "Mean turns",
  "Looped queries" and "Mean repeated calls" rows and the calls-per-tool lines to
  `format_comparison_table`, printing `n/a` for `None`; add the six per-repo fields to
  `multi_repo_rollup` and the two `grep / treeweft` columns to `format_rollup_table`, with
  pooled values over the repos that have the figures. Existing rows, columns and keys are
  byte-identical. Make T019 pass.
- [X] T029 [P] [US3] In `src/treeweft/adapters/benchmark/opik_tracing.py` add `looped` and
  `repeated_tool_calls` to the per-(query, arm) feedback scores when the row carries them.
  Keep the module best-effort and post-hoc. Make T021 pass.
- [X] T030 [P] [US3] Update `docs/benchmark-eval.md`, section "Reading the output": explain the
  new table rows and calls-per-tool lines; what "looped" means and that the threshold is three
  identical calls; that exact repeats show a stuck agent while calls per tool show
  compensatory searching or reading; that cut-off agent answers and verdicts are counted and
  marked but not excluded; that results from before this change show `n/a`; and that this does
  not start a new baseline epoch.

**Checkpoint**: US3's acceptance scenarios 1–11 are verified by unit tests.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T031 [P] Add an entry to `CHANGELOG.md` under the unreleased section: new `llm.chat` span
  attributes, the two new counters, and the new benchmark result fields; note "additive, no
  API, MCP or index-schema change".
- [X] T032 Run `env -u PYTHONPATH python -m pytest tests/unit -q` and report the pass count
  against the T001 baseline. Run `git diff --stat main -- contracts/` and confirm it is empty.
  Search the changed files for prompt or response text being written to a span or metric and
  confirm there is none.
- [ ] T033 Run the live checks in `specs/004-llm-response-signals/quickstart.md` (Story 1 and
  Story 2 `/metrics` and trace checks, the existing dashboard still rendering, and the optional
  ±5% throughput comparison). First check that the indexer, the LLM endpoint and the trace
  backend are reachable. If the homelab stack is down, do not work around it: report each live
  check as not run and why.
- [X] T034 Re-read spec.md's FR-001 to FR-024 and SC-001 to SC-009 against the finished code and
  list, for each, the test or check that covers it; report any that are covered only by a live
  check that was not run.
- [X] T035 Open a pull request from `004-llm-response-signals` to `main` on `origin` whose
  description reports the verification actually performed, including anything not run. Do not
  merge and do not push to `main`. Do not add a generated-by attribution line to the pull
  request or to commits.

---

## Dependencies & Execution Order

```text
Phase 1 (T001)
   └─> Phase 2 (T002 → T003)
          ├─> Phase 3 / US1 (T004 → T005 → T006; T007 any time after T006's design is fixed)
          │       └─> Phase 4 / US2 (T008, T009 → T010 → T011 → T012; T013)
          └─> Phase 5 / US3 (tests T014–T021 → T022, T023 → T024 → T025 → T026 → T027 → T028; T029, T030)
                                                                  
Phases 3–5 ──> Phase 6 (T031 → T032 → T033 → T034 → T035)
```

- **US1** needs only Phase 2.
- **US2** needs US1: T012 extends the block T006 adds in the same function.
- **US3** needs only Phase 2 (`is_truncation`). It can be built before, after or alongside US1
  and US2.
- Within US3: T024 needs T022 and T023; T026 needs T024 and T025; T027 needs T022 for
  `LOOP_THRESHOLD`; T028 needs T027's field names.

## Parallel opportunities

- After Phase 2, US3 can proceed in parallel with US1 → US2; they share no source file.
- US3 tests T014, T015, T017, T018, T019, T020, T021 are in different files and can be written
  together. T016 is separate because it depends on the fake-LLM shape chosen in T015.
- US3 implementation T022 and T023 are independent; T025 and T029 are independent of T024.
- Docs tasks T007, T013, T030 and T031 touch different files.

Example, US3 tests in one batch:

```text
T014 tests/unit/test_tool_call_stats.py
T015 tests/unit/test_agent_llm_chat_full.py
T017 tests/unit/test_benchmark.py
T018 tests/unit/test_agent_metrics.py
T019 tests/unit/test_rollup.py
T020 tests/unit/test_agentic_runner.py
T021 tests/unit/test_opik_tracing.py
```

## Implementation Strategy

- **MVP**: Phases 1–3 (T001–T007). That alone delivers token cost per operation and the served
  model on every call, and can ship on its own.
- **Increment 2**: Phase 4 adds the three flags on top of the same data.
- **Increment 3**: Phase 5 is independent harness work and can be reviewed separately.
- One pull request is the default (one spec). If review size becomes a problem, split along
  the three increments; each leaves the unit suite green.
- For every implementation task, run its failing test first, then implement, then run the
  whole unit suite before moving on.

## Implementation notes (2026-10-06)

- **T016** landed in a new file, `tests/unit/test_agent_loop_tool_stats.py`, not as an extension
  of `test_native_tools_loop.py`: one script of steps is rendered as ReAct text or as native
  tool calls, so both loops are asserted by the same parametrised tests. The existing loop
  tests pass unmodified.
- **T017** landed in a new file, `tests/unit/test_judge_truncation.py`; there were no existing
  judge tests to extend.
- **T023/T024/T025**: the `chat_full`-or-`chat` fallback is one module-level function,
  `chat_full(llm, ...)`, in `adapters/benchmark/agent_llm.py`, used by both loops and the judge.
- **T026**: the run-wide totals are computed by a small helper, `_truncation_totals(rows)`.
- **T006**: the response body is parsed once into a local; see research R6, "As built".
- **T033 is not done.** No indexer was running on this machine (`localhost:8001` refused the
  connection), so the live `/metrics`, trace, dashboard and throughput checks were not run. The
  metrics exposition was checked in-process instead: `metrics.get_metrics()` emits 10 token
  series and 15 condition series at zero. SC-005 (existing dashboard unchanged) and SC-008
  (throughput within ±5%) therefore rest on the unit tests and on construction, not on a live
  observation.
