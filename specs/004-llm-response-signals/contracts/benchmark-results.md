# Contract: Benchmark Result Fields

Fields this feature adds to the agentic benchmark's outputs. Every existing field keeps its
name, meaning and value. Readers of older results must treat a missing new field as "not
measured", never as zero.

## Per-query rows file: `arms.<arm>`

| Field | Type | Meaning |
|---|---|---|
| `tool_call_counts` | object, tool name → int | executed calls per tool in this run, for every tool the arm exposes; a tool never called is present with 0 |
| `repeated_tool_calls` | int | executed calls whose tool and arguments equal an earlier call's |
| `looped` | bool | some call was made 3 or more times with the same arguments |
| `agent_truncated_responses` | int or null | agent responses in this run that stopped at the token limit; null when no response reported a finish reason |
| `agent_truncated` | bool or null | `agent_truncated_responses > 0`; null when that count is null |

`arms.<arm>.judge` gains:

| Field | Type | Meaning |
|---|---|---|
| `truncated` | bool or null | the verdict response stopped at the token limit; null when unknown or when no judge call was made |

Invariant: the values in `tool_call_counts` sum to the existing `tool_calls`.

All fields are written whether or not `--debug-transcripts` is set.

## Summary file: `<arm>` block

| Field | Type | Meaning |
|---|---|---|
| `looped_share` | number or null | fraction of this arm's queries with `looped` true |
| `mean_repeated_tool_calls` | number or null | mean of `repeated_tool_calls` |
| `mean_tool_calls_by_tool` | object or null | tool name → mean executed calls per query |
| `agent_truncated_queries` | int or null | queries with `agent_truncated` true |
| `judge_truncated_queries` | int or null | queries with `judge.truncated` true |

A value is null when no row for the arm carries the underlying field. In
`mean_tool_calls_by_tool`, a row that carries `tool_call_counts` but did not call a given tool
counts as zero for that tool; a row without the field is left out of the mean.

All existing `mean_*` fields, win rates and p-values are computed as before and include every
query, marked or not.

## Summary file: top level

| Field | Type | Meaning |
|---|---|---|
| `loop_threshold` | int | identical calls needed for `looped`; 3 |
| `agent_truncated_responses` | int | agent responses cut off, all arms and queries |
| `judge_truncated_responses` | int | judge verdicts cut off, all arms and queries |

## Comparison table (grep vs treeweft)

Rows added below the existing three. Existing rows are unchanged.

```text
| Mean turns | 6.20 | 4.10 | — | — |
| Looped queries | 8.0% | 2.0% | — | — |
| Mean repeated calls | 0.42 | 0.10 | — | — |

Calls per tool (mean per query)

- grep: glob 0.8, grep 3.1, read_file 2.4
- treeweft: read_file 1.2, search_code 1.7
```

A null value prints as `n/a`. Each arm lists only its own tools, in name order; an arm that
measured the figure but called no tool prints `none`.

## Multi-repo rollup

Each per-repo entry gains, for both arms:

| Field | Type |
|---|---|
| `grep_looped_share`, `treeweft_looped_share` | number or null |
| `grep_mean_repeated_tool_calls`, `treeweft_mean_repeated_tool_calls` | number or null |
| `grep_mean_tool_calls_by_tool`, `treeweft_mean_tool_calls_by_tool` | object or null |

The rollup table gains two columns after the existing ones, `looped g/t` and `repeats g/t`,
each shown as `grep / treeweft`. The `pooled` block gains `grep_looped_share`,
`treeweft_looped_share`, `grep_mean_repeated_tool_calls` and
`treeweft_mean_repeated_tool_calls`, weighted by query count, or null when no repo has them. Per-tool means are in the rollup data only. A cell from before this feature
shows `n/a` in the new columns and is otherwise unaffected. Pooled totals for the new columns
are computed over the repos that have the figures.

## Experiment tracking (opt-in)

Each (query, arm) trace gains two feedback scores, `looped` (0 or 1) and
`repeated_tool_calls`, written post-hoc from the finished rows like the existing scores.

## Compatibility

- Additive only. No existing key is removed, renamed or recomputed.
- This does not start a new harness era; results before and after remain comparable on every
  existing figure.
- `scripts/agentic_smoke_check.py` reads none of the new fields.
