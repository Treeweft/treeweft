# Treeweft Constitution

## Core Principles

### I. Verification First (NON-NEGOTIABLE)

Every change MUST state, before work starts, the concrete check that will prove it: the command
to run, the test to add, the endpoint to hit, or the metric to inspect. After the work, that check
MUST actually be run and its result reported as it happened, including failures, skipped steps and
partial outcomes. "Done" means verified. When something cannot be verified, the report MUST say so
and why.

Rationale: most of Treeweft's worst defects were silent — the system kept running while doing the
wrong thing — so plausibility is not evidence.

### II. Test-First with a Strict Test Taxonomy

- Every behaviour change or bug fix MUST land with tests, and a regression test MUST be shown to
  fail without the fix before it is trusted.
- **Unit tests** (`tests/unit/`) MUST NOT touch real external infrastructure (databases, vector
  stores, graph stores, message brokers, model servers). They are Detroit-style: mock only external
  services, never internal classes. Embedded, in-process stores on a temporary file (e.g. SQLite)
  are allowed.
- **Integration tests** that exercise real services MUST live under `tests/integration/`, be
  marked `slow`, and skip unless an explicit opt-in environment variable (e.g. `MILVUS_TEST_URI`,
  `NEO4J_TEST_URI`) is set. They MUST create only uniquely prefixed data and remove it afterwards.
- Retrieval **quality** MUST be validated by the benchmark harness, never by unit tests.

Rationale: a fast, service-free unit suite keeps CI trustworthy; real-service behaviour (query
semantics, consistency, schema behaviour) is only proven against the real service.

### III. Evidence-Gated Retrieval Changes

- Any change that could move search quality or agent cost (ranking, rescoring, chunking,
  embedding or reranker models, prompts, response payloads) MUST be backed by a benchmark run
  (see `docs/benchmark-eval.md`) before it becomes a default.
- Unproven ideas MUST ship opt-in (flag or request field), never as defaults. In particular the
  search payload MUST NOT be trimmed by default: the governing metric is mean agent turns judged
  with tokens, and every payload-shrinking idea so far lost to compensatory fetching
  (`docs/token-optimization-rejected.md`).
- Before any long benchmark or agentic run, the actually-served LLM, reranker and embedding models
  and their placement MUST be verified live; configuration files are not evidence.
- Results from incompatible harness eras MUST NOT be mixed in one rollup.

Rationale: retrieval intuitions have repeatedly been wrong; only measured outcomes decide defaults.

### IV. Layered Architecture and Clean Boundaries

- DDD layering: `domain/` holds pure logic and MUST NOT import `adapters/`; `adapters/` implement
  I/O behind the domain's ports; `application/` wires FastAPI; `infrastructure/` holds config and
  DI. New backends MUST implement the existing port surface.
- The MCP server MUST remain a pure HTTP proxy to the indexer; all filesystem, indexing and
  retrieval work lives in the indexer service.
- Startup-path code MUST NOT import backend adapters that require their services' environment
  (e.g. the Milvus and Neo4j stores); it goes through the root shims, so the simple deployment
  profile keeps working.
- External providers are pluggable and MUST be treated neutrally: the LLM is any OpenAI-compatible
  endpoint, and embedding, reranking, vector and graph backends are selected by configuration. No
  design, default or document may assume a single provider.

Rationale: two deployment profiles and several interchangeable backends only stay correct if the
boundaries between them are enforced rather than remembered.

### V. Fail Loud, Never Degrade Silently

- Missing or invalid configuration MUST fail at startup with a message naming the setting, not
  surface later as wrong results.
- A fallback path (e.g. the character chunker, a flat-score reranker fallback, a vector-only search
  without HyDE) MUST be observable — logged or counted — and covered by a test that shows when it
  engages. A change that could route normal traffic into a fallback MUST be treated as a defect.
- Operations that silently match nothing (for example a query keyed on a property the writer never
  sets) MUST be caught by a test that asserts on the effect, not on the query text alone.

Rationale: the recurring failure mode in this codebase is a component that keeps "working" while
quietly doing something else: every file sent to a fallback chunker, recall collapsing behind flat
reranker scores, graph entities dropped by a MATCH that should have been a MERGE.

### VI. Secure by Default

- Every indexer endpoint MUST enforce authorization through the existing authz layer; admin-only
  operations MUST use the admin check. Per-repository read tools MUST use the caller's credentials
  and MUST NOT fall back to a shared service account.
- Indexed content (source files, paths, commit data) is untrusted input. Anything placed in an LLM
  prompt MUST be fenced as data, and anything placed in a query or filter expression MUST be
  escaped or parameterized.
- Secrets MUST NOT be committed; configuration references variable names only. Network-facing
  defaults MUST be the least exposed option (for example the indexer binds loopback by default).

Rationale: Treeweft reads arbitrary repositories and serves them to agents across tenants; both the
content it indexes and the callers it serves must be treated as potentially hostile.

### VII. Versioned Data and Releases

- **The product is CalVer.** Published images are named by release date: `YYYY.M.D` without
  zero padding (`.N` for a same-day re-release), the rolling `YYYY.M`, and `latest`.
- **The source is SemVer.** `pyproject.toml` carries `MAJOR.MINOR.PATCH`. MAJOR is required for
  any breaking change to the indexer HTTP API, the MCP tool surface or the index schema; MINOR
  for additions; PATCH for fixes. The MCP tool surface shares the source version while the MCP
  server remains a thin proxy. Version bumps MUST satisfy the contract-snapshot check against the
  last release; they are not decided by judgement alone.
- **Every release carries both tags on one commit**: the SemVer tag `v<pyproject version>`,
  pushed first, then the CalVer tag, which triggers publishing. The version bump MUST be merged
  before either tag is pushed.
- **The month tag is a stability pin.** A release that bumps the SemVer MAJOR MUST be the first
  release of its calendar month.
- **The index schema has its own integer** (`INDEX_SCHEMA_VERSION`), stamped into each data
  store with the embedding model and vector dimension. A change that invalidates persisted index
  data (the vector-store collection schema, the embedding model or dimension, the graph shape)
  MUST bump it, MUST bump the SemVer MAJOR, MUST be listed as "requires re-index" in the
  changelog, and MUST NOT silently strand or corrupt an existing deployment: a mismatch is
  reported and the index rebuilt deliberately. A summary-prompt change is not a schema change;
  it is versioned and refreshed per ADR-003.
- Postgres schema changes MUST ship as a new numbered migration; applied migrations are never
  edited.

ADR-004 is fully implemented as of 1.1.0: the SemVer release tooling (tags, contract snapshots)
has been in force since 1.0.0 (2026-09-24), and the index schema stamp, reindex-required mode
and the rebuild have been in force since 1.1.0.

Rationale: a date tells operators when a release was cut, but only a compatibility version can
tell software whether two components can talk, or tell an operator that an upgrade forces a
re-index. Deployments hold expensive, long-lived indexes; that cost must be visible before it is
paid.

## Technology and Operational Constraints

- Python 3.11+ with dependencies locked in `uv.lock`; CI runs the unit suite on the supported
  interpreter versions.
- The operational invariants in `CLAUDE.md` ("Invariants") are binding under this constitution.
  They cover, among others: tree-sitter parser handling, Markdown chunking, `LANGUAGE_QUERIES` keys,
  the `MERGE` rules in `store_graph`, the async Neo4j driver API, the explicit vector-store schema,
  result normalization through `chunk_hit_from_milvus`, reranker selection and graph-rescoring
  defaults, and MCP transport configuration.
- The indexer runs on the host, not in Docker, because it reads arbitrary host paths; containers
  reach it through `host.docker.internal`, and `/home` is never mounted into containers.

## Development Workflow and Quality Gates

- All changes land through a pull request from a feature branch to `main`; nothing is pushed to
  `main` directly, and a maintainer merges.
- A pull request is mergeable only when CI is green (unit tests and code scanning) and its
  description reports the verification from Principle I, including anything not verified.
- Architectural decisions (changes to how components fit together, to the contracts between
  them, or to cross-cutting policy) are recorded as ADRs in `docs/` (`adr-NNN-*.md`, with a
  status). Most features are not architectural decisions and need no ADR.
- Documentation that describes changed behaviour (`docs/engineering-notes.md`, runbooks, README)
  MUST be updated in the same pull request.
- When a feature implements an ADR, its Spec Kit artifacts (`specs/`) MUST agree with that ADR;
  where they conflict, the ADR is amended first.

## Governance

This constitution supersedes other development practices. `CLAUDE.md`, `CONTRIBUTING.md` and the
docs hold the detailed operational rules; they MUST NOT contradict it, and when they do, the
conflict is resolved by amending one of them explicitly, not by ignoring either.

Amendments are made by pull request that edits this file, updates the version line, and explains
the change. Versioning follows semantic versioning: MAJOR for removing or redefining a principle
incompatibly, MINOR for adding a principle or materially expanding guidance, PATCH for
clarifications and wording.

Compliance: every Spec Kit plan MUST pass its "Constitution Check" gate, and any justified
exception MUST be recorded in that plan's complexity tracking. Reviewers check pull requests
against these principles.

**Version**: 1.0.1 | **Ratified**: 2026-09-24 | **Last Amended**: 2026-09-25
