# Data Model: Prompt Versioning

Phase 1 of [plan.md](plan.md). The decisions behind this model are in [research.md](research.md).

## Entities

### PromptVersion (in code, immutable)

`src/treeweft/adapters/llm_api/prompts.py`

| Field | Type | Rule |
|---|---|---|
| `operation` | `"chunk_summary" \| "hyde"` | |
| `version` | positive int | Unique per operation. Never reused, never edited |
| `system` | str | The bare system prompt. `_NO_THINK_SUFFIX` is appended at call time |
| `schema` | frozen `ResponseSchema` copy | The validator schema this version's output must pass |
| `notes` | str | One-line release note shown in the API and UI |

- Seed versions: `chunk_summary` v3 (today's `_SUMMARY_SYSTEM` and `_SUMMARY_SCHEMA`) and `hyde`
  v1 (today's `_HYDE_SYSTEM` and `_HYDE_SCHEMA`).
- Baseline versions (used without Postgres, R9): `chunk_summary` 3 and `hyde` 1. These constants
  never change.
- Integrity: `tests/unit/fixtures/prompt_hashes.json` holds `sha256` over
  `{"system", "schema"}` for every registered version (R1).

### Pin (Postgres `prompt_pins`, migration 021)

| Column | Type | Rule |
|---|---|---|
| `operation` | TEXT NOT NULL | A registered operation |
| `scope` | TEXT NOT NULL | `'deployment'` or a `source_records.id` |
| `version` | INTEGER NOT NULL | Registered for the operation. Checked by the API, and at startup (fail loud) |
| `updated_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | |
| `updated_by` | TEXT NULL | The caller's user ID. NULL for a startup seed or with auth off |

- `PRIMARY KEY (operation, scope)` and `CHECK (operation <> 'hyde' OR scope = 'deployment')`.
- There is no foreign key on `scope`, because it is polymorphic. The source-delete path deletes
  the override (FR-012).
- Every mutation runs `NOTIFY prompt_pins_changed` in the same transaction.

### Source summary state (Postgres `source_records`, new columns in migration 021)

| Column | Type | Meaning |
|---|---|---|
| `summary_prompt_version` | INTEGER NULL | The chunk-summary version the stored summary vectors were built with. NULL means unknown or never summarized. Backfilled to 3 |
| `summary_refresh_target` | INTEGER NULL | Set while a refresh toward that version is unfinished (FR-015). NULL otherwise |

- `PostgreSourceRepository.save()` upserts an explicit column list that excludes both columns,
  so re-saves never clobber them. They are written only through two dedicated methods:
  - `mark_summary_refresh(source_id, target)`;
  - `record_summary_version(source_id, version | None)`, which sets the version **and** clears
    the target in one `UPDATE`.
- `SourceRecord` (the domain dataclass) gains both fields, read-only from the application's point
  of view.

### PinView (in memory, per process)

`application/prompt_pins.py`. This is an immutable snapshot `{deployment: {op: version},
overrides: {source_id: version}}`, swapped atomically on reload (R3).

## Derived values

```text
effective("hyde")                        = view.deployment["hyde"]            (baseline 1 without Postgres)
effective("chunk_summary", source_id)    = view.overrides.get(source_id, view.deployment["chunk_summary"])
stale(source) = source.summary_refresh_target IS NOT NULL
             OR (source.summary_prompt_version IS NOT NULL
                 AND source.summary_prompt_version != effective("chunk_summary", source.id))
```

The domain helpers `resolve(...)` and `is_stale(...)` live in `src/treeweft/domain/prompt_pins.py`.
They are pure, with no adapter imports (constitution IV).

## Per-source state

```text
                    pin/override moves away
   ┌─────────┐  ───────────────────────────────▶  ┌─────────┐
   │ current │                                     │  stale  │
   │ rec = T │  ◀──────── pin moves back ───────── │ rec ≠ T │
   └─────────┘   (only if no refresh has started)  └─────────┘
        ▲                                              │ refresh starts:
        │ clean finish:                                │ refresh_target := T
        │ rec := T, refresh_target := NULL             ▼
        │                                         ┌────────────┐
        └──────────────────────────────────────── │ refreshing │  (stale for every T)
                                                  │ target set │
             ended with errors / interrupted ────▶│            │
             (target stays set; still stale)      └────────────┘
```

Here `rec` is `summary_prompt_version` and `T` is the effective chunk-summary version.

- A clean **full** index job (file, directory or repo) moves any state to current at the version
  the job used (or to `rec = NULL` when summary vectors are off or unsupported), clearing the
  target (R6).
- A **graph** job and an **incremental** job change nothing.
- `rec = NULL` with the target NULL is "never summarized": never stale, never refreshed
  automatically.

## Refresh job (`jobs` row, `kind = 'resummarize'`)

| Field | Use |
|---|---|
| `source_id` | The source. The one-active-job rule (008) applies |
| `payload.target_version` | Resolved at enqueue, and re-resolved when the job starts if the pin has moved (R10) |
| `total_files` / `processed_files` | Chunk counters (R12) |
| `total_chunks` | Snapshot size |
| `errors` | Chunks whose summary failed transiently. With `errors > 0`, the job ends done with errors and the version is not advanced |
| `message` | `"refreshed N/M chunks to chunk_summary vK"`, or the reason for a no-op |
| `group_id` | A deployment-pin change enqueuing more than one refresh creates one job group (`kind="prompt-refresh"`) for progress. A single refresh is a group of one, as for other jobs |

## Validation rules

| Rule | Where | Error |
|---|---|---|
| Pin version registered | API (dry and real) | 400 `{"detail", "valid_versions"}` |
| No HyDE override | API and table `CHECK` | 400 `{"detail"}` |
| Stored pin registered | Startup | `RuntimeError` naming the pin and the valid versions; startup aborts |
| `prompt_pins` table and columns present | Startup | `RuntimeError` naming migration 021 |
| Source exists (override, manual refresh) | API | 404 |
| Per-request `summary_prompt_version` read override | Not validated | Unregistered tiers (for example the benchmark's 9002) keep working |

## Migration 021 (`021_prompt_versions.sql`)

This is ADR-003 §2's SQL plus `summary_refresh_target`. Every statement is idempotent, because
the runner holds no lock (R9 and issue #28):

- `CREATE TABLE IF NOT EXISTS prompt_pins (…)`;
- `ALTER TABLE source_records ADD COLUMN IF NOT EXISTS summary_prompt_version INTEGER, ADD COLUMN
  IF NOT EXISTS summary_refresh_target INTEGER`;
- `UPDATE source_records SET summary_prompt_version = 3 WHERE summary_prompt_version IS NULL`.

It inserts no pin rows; seeding happens at startup. Its header comment supersedes 005's "bump
`PROMPT_VERSION`" note, because 005 is applied and is never edited.
