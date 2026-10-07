"""Prometheus metrics for Treeweft indexer.

Metric naming is stable — the Grafana dashboards (assets/grafana/) and the load-test
harnesses (scripts/loadtest_*.py) code against the exact names defined here. Do not rename without
updating those.
"""
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CollectorRegistry
from prometheus_client import ProcessCollector

from treeweft.domain.audit import Operation

registry = CollectorRegistry()

# Expose process-level metrics (process_resident_memory_bytes, process_cpu_*,
# open fds, …) on our custom registry. The default registry isn't scraped by
# GET /metrics, so without this the load-test harness and any RSS/CPU
# dashboard can't observe indexer memory growth.
try:
    ProcessCollector(registry=registry)
except Exception:  # pragma: no cover - platform without /proc
    pass

# Counters
files_processed = Counter(
    "treeweft_files_processed_total",
    "Total files indexed",
    registry=registry,
)
chunks_embedded = Counter(
    "treeweft_chunks_embedded_total",
    "Total chunks embedded and inserted into Milvus",
    registry=registry,
)
embed_errors = Counter(
    "treeweft_embed_errors_total",
    "Total embedding failures",
    registry=registry,
)
job_creates = Counter(
    "treeweft_jobs_created_total",
    "Total indexing jobs submitted",
    ["status"],
    registry=registry,
)
rerank_fallbacks = Counter(
    "treeweft_rerank_fallbacks_total",
    "Searches that degraded to raw vector order because the reranker call "
    "failed (dead RERANKER_URL, timeout). Any nonzero rate means ranking "
    "quality is silently degraded — alert on it.",
    registry=registry,
)
hyde_fallbacks = Counter(
    "treeweft_hyde_fallbacks_total",
    "Searches that asked for HyDE and ran without it. reason=error: no "
    "expansion within LLM_HYDE_TIMEOUT (timeout, provider error or open "
    "circuit breaker); reason=rejected: every answer failed validation; "
    "reason=embed_failed: the expansion could not be embedded. A sustained "
    "rate means search quality is degraded.",
    ["reason"],
    registry=registry,
)
# Export every series from the start, at 0: rate() and increase() miss the
# first increment of a series that did not exist before it.
for _reason in ("error", "rejected", "embed_failed"):
    hyde_fallbacks.labels(reason=_reason)
encoding_fallbacks = Counter(
    "treeweft_encoding_fallbacks_total",
    "Files indexed via UTF-8 replacement fallback (invalid UTF-8 input)",
    registry=registry,
)

# ── LLM response counters ───────────────────────────────────────────

# Label values come from the Operation enum so the two cannot drift; "unknown"
# is what _chat records when a caller names no operation.
LLM_OPERATIONS = tuple(op.value for op in Operation) + ("unknown",)

llm_tokens_total = Counter(
    "treeweft_llm_tokens_total",
    "Tokens the LLM endpoint reported for service chat calls. Not incremented "
    "for a response that omits usage, so a flat series can mean either no "
    "calls or an endpoint that does not report usage.",
    ["operation", "direction"],  # direction: input | output
    registry=registry,
)
for _op in LLM_OPERATIONS:
    for _direction in ("input", "output"):
        llm_tokens_total.labels(operation=_op, direction=_direction)

llm_response_conditions_total = Counter(
    "treeweft_llm_response_conditions_total",
    "Service LLM responses on which a condition was detected. "
    "condition=model_mismatch: the endpoint reported a different served model "
    "from the first one seen for that requested model since startup; "
    "condition=truncated: generation stopped at the token limit; "
    "condition=empty: no usable text came back. Detection only — the call's "
    "result is unchanged. Any model_mismatch means the model moved under a "
    "running service.",
    ["operation", "condition"],
    registry=registry,
)
for _op in LLM_OPERATIONS:
    for _condition in ("model_mismatch", "truncated", "empty"):
        llm_response_conditions_total.labels(operation=_op, condition=_condition)

# ── Incremental-job counters ────────────────────────────────────────

incremental_jobs_total = Counter(
    "treeweft_incremental_jobs_total",
    "Incremental (webhook) indexing jobs by terminal status",
    ["status"],  # done | failed | dead_letter
    registry=registry,
)

dead_letter_jobs_total = Counter(
    "treeweft_dead_letter_jobs_total",
    "Jobs that exhausted all retry attempts and entered dead-letter state",
    registry=registry,
)

webhook_shed_total = Counter(
    "treeweft_webhook_shed_total",
    "Webhook requests rejected with 429 due to queue saturation",
    registry=registry,
)

fleet_refresh_enqueued_total = Counter(
    "treeweft_fleet_refresh_enqueued_total",
    "Re-index jobs enqueued by the staleness-driven fleet auto-refresh loop",
    registry=registry,
)

# Histograms
embed_latency = Histogram(
    "treeweft_embed_latency_seconds",
    "Embed server request latency",
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
    registry=registry,
)
chunk_size = Histogram(
    "treeweft_chunk_size_bytes",
    "Chunk text size in bytes before embedding",
    buckets=(128, 256, 512, 1024, 2048, 4096, 8192, 16384, 65536),
    registry=registry,
)

# ── Incremental-job histograms ─────────────────────────────────────

incremental_job_duration_seconds = Histogram(
    "treeweft_incremental_job_duration_seconds",
    "Wall-clock time for a completed incremental (webhook) index job",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800),
    registry=registry,
)

merge_to_searchable_seconds = Histogram(
    "treeweft_merge_to_searchable_seconds",
    "Time from webhook acceptance to incremental job completion (merge→searchable). "
    "git ls-remote cannot resolve 'commits behind HEAD', so we use index-age "
    "gauges and this histogram for SLO alerting instead.",
    buckets=(5, 15, 30, 60, 120, 300, 600, 1800, 3600),
    registry=registry,
)

# Gauges
active_jobs = Gauge(
    "treeweft_active_jobs",
    "Currently running indexing jobs",
    registry=registry,
)
queue_depth = Gauge(
    "treeweft_queue_depth",
    "Jobs queued awaiting execution",
    registry=registry,
)

# ── Freshness / staleness gauges ───────────────────────────────────

source_index_stale = Gauge(
    "treeweft_source_index_stale",
    "1 if the source's indexed commit SHA differs from current HEAD, else 0. "
    "Only exported for up to FRESHNESS_MAX_SOURCES sources; above that, "
    "see treeweft_sources_stale_total instead. "
    "git ls-remote resolves HEAD SHA, not commit timestamp.",
    ["source_id"],
    registry=registry,
)

source_index_age_seconds = Gauge(
    "treeweft_source_index_age_seconds",
    "Seconds since the source was last indexed (now - indexed_at). "
    "Exported per-source up to FRESHNESS_MAX_SOURCES; use as SLO signal "
    "when combined with treeweft_source_index_stale.",
    ["source_id"],
    registry=registry,
)

sources_stale_total = Gauge(
    "treeweft_sources_stale_total",
    "Aggregate count of stale sources when cardinality exceeds "
    "FRESHNESS_MAX_SOURCES (replaces per-label gauges above that threshold).",
    registry=registry,
)

# ── Queue saturation gauge ─────────────────────────────────────────

queue_saturation = Gauge(
    "treeweft_queue_saturation",
    "Queue fill ratio: depth / MAX_QUEUE_DEPTH, capped at 1.0. "
    "Alert when this approaches 1 — new webhooks will be shed with 429.",
    registry=registry,
)


def get_metrics() -> bytes:
    """Generate Prometheus text format metrics."""
    return generate_latest(registry)
