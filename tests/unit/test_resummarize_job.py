"""The `resummarize` job runner (T022, research R10, R12; FR-014, FR-015,
FR-018; SC-002, SC-003).

A fake vector store stands in for `treeweft.retriever`'s four new shim
functions (accessed by the runner at call time, since other agents are still
adding them), a fake source repo stands in for Postgres, and summaries are
faked at `llm.cache_get_many`/`llm.summarize_with_cache` so no real LLM or
vector store is touched. No file is parsed, no code is embedded, and the
graph is never touched.
"""
from __future__ import annotations

import pytest

import treeweft.retriever as retriever_module
from treeweft.application import indexer_runners as svc
from treeweft.application import indexer_state as _state
from treeweft.application import prompt_pins


def _job(source_id: str, target: int, **extra) -> dict:
    job = {
        "job_id": f"j-{source_id}",
        "id": f"j-{source_id}",
        "source_id": source_id,
        "kind": "resummarize",
        "status": "queued",
        "errors": 0,
        "payload": {"target_version": target},
        "total_files": 0,
        "processed_files": 0,
        "total_chunks": 0,
        "message": "",
    }
    job.update(extra)
    return job


def _rows(n: int, source_id: str = "src-1") -> list[dict]:
    return [
        {
            "id": i,
            "chunk_text": f"chunk-{i} body",
            "file_path": f"file{i}.py",
            "language": "python",
            "source_id": source_id,
        }
        for i in range(1, n + 1)
    ]


class _FakeSourceRepo:
    def __init__(self, call_order=None, existence=True, record=None):
        self._call_order = call_order if call_order is not None else []
        if isinstance(existence, bool):
            self._existence_seq = None
            self._existence_always = existence
        else:
            self._existence_seq = list(existence)
            self._existence_always = False
        self._record = record
        self.marked: list[tuple[str, int | None]] = []
        self.recorded: list[tuple[str, int | None]] = []

    async def get_by_id(self, source_id):
        if self._existence_seq is not None:
            exists = self._existence_seq.pop(0) if self._existence_seq else False
        else:
            exists = self._existence_always
        if not exists:
            return None
        return self._record if self._record is not None else object()

    async def mark_summary_refresh(self, source_id, target):
        self._call_order.append("mark")
        self.marked.append((source_id, target))

    async def record_summary_version(self, source_id, version):
        self._call_order.append("record")
        self.recorded.append((source_id, version))


class _FakeStore:
    def __init__(self, call_order, rows, count_override=None):
        self._call_order = call_order
        self._rows = {r["id"]: dict(r) for r in rows}
        self._count_override = count_override
        self.snapshot_calls = 0
        self.fetch_batches: list[list] = []
        self.write_batches: list[tuple[list, list]] = []
        self.count_calls = 0

    async def snapshot_source_row_ids(self, source_id):
        self.snapshot_calls += 1
        return [rid for rid, r in self._rows.items() if r["source_id"] == source_id]

    async def fetch_rows(self, ids):
        self.fetch_batches.append(list(ids))
        return [dict(self._rows[i]) for i in ids]

    async def write_summary_vectors(self, rows, vectors):
        self._call_order.append("write")
        self.write_batches.append(([r["id"] for r in rows], list(vectors)))

    async def count_source_rows(self, source_id):
        self.count_calls += 1
        if self._count_override is not None:
            return self._count_override
        return sum(1 for r in self._rows.values() if r["source_id"] == source_id)


class _FakeJobFileErrorStore:
    def __init__(self):
        self.calls: list[dict] = []

    async def record(self, **kwargs):
        self.calls.append(kwargs)


class _FakeLLM:
    def __init__(self, fail_texts=(), reject_texts=()):
        self.cache: dict[tuple[str, int], str] = {}
        self.fail_texts = set(fail_texts)
        self.reject_texts = set(reject_texts)
        self.generate_calls: list[str] = []
        self.generate_versions: list[int] = []

    async def cache_get_many(self, sha1s, prompt_version, include_rejected=False):
        out = {}
        for k in sha1s:
            key = (k, prompt_version)
            if key in self.cache:
                v = self.cache[key]
                if v or include_rejected:
                    out[k] = v
        return out

    async def summarize_with_cache(self, chunk_text, language, file_path, *, version):
        self.generate_calls.append(chunk_text)
        self.generate_versions.append(version)
        key = (svc.llm.chunk_cache_key(chunk_text), version)
        if chunk_text in self.fail_texts:
            return None, "error"
        if chunk_text in self.reject_texts:
            self.cache[key] = ""
            return None, "rejected"
        summary = f"Summary: {chunk_text}"
        self.cache[key] = summary
        return summary, "generated"


async def _fake_embed(texts):
    return [[0.1, 0.2] for _ in texts]


def _setup(
    monkeypatch,
    *,
    rows,
    target=5,
    source_id="src-1",
    pin=None,
    existence=True,
    embed_batch_size=None,
    count_override=None,
    fail_texts=(),
    reject_texts=(),
):
    call_order: list[str] = []
    repo = _FakeSourceRepo(call_order, existence=existence)
    store = _FakeStore(call_order, rows, count_override=count_override)
    job_errors = _FakeJobFileErrorStore()
    fake_llm = _FakeLLM(fail_texts=fail_texts, reject_texts=reject_texts)

    monkeypatch.setattr(_state, "_source_repo", repo)
    monkeypatch.setattr(_state, "_job_file_error_store", job_errors)
    monkeypatch.setattr(
        retriever_module, "snapshot_source_row_ids", store.snapshot_source_row_ids, raising=False,
    )
    monkeypatch.setattr(retriever_module, "fetch_rows", store.fetch_rows, raising=False)
    monkeypatch.setattr(
        retriever_module, "write_summary_vectors", store.write_summary_vectors, raising=False,
    )
    monkeypatch.setattr(
        retriever_module, "count_source_rows", store.count_source_rows, raising=False,
    )
    monkeypatch.setattr(
        prompt_pins, "effective", lambda op, source_id=None: pin if pin is not None else target,
    )
    monkeypatch.setattr(svc, "embed", _fake_embed)
    monkeypatch.setattr(svc.llm, "cache_get_many", fake_llm.cache_get_many)
    monkeypatch.setattr(svc.llm, "summarize_with_cache", fake_llm.summarize_with_cache)
    if embed_batch_size is not None:
        monkeypatch.setattr(svc, "EMBED_BATCH_SIZE", embed_batch_size)

    job = _job(source_id, target)
    return job, store, repo, job_errors, fake_llm, call_order


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", True)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: True)


# ---------------------------------------------------------------------------
# Step 0: no-op check runs first
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_noop_disabled_summary_vectors_is_done_and_untouched(monkeypatch):
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", False)

    def _boom():
        raise AssertionError("summary_vectors_supported must not be called when disabled")

    monkeypatch.setattr(svc, "summary_vectors_supported", _boom)
    repo = _FakeSourceRepo()
    monkeypatch.setattr(_state, "_source_repo", repo)

    job = _job("src-1", 5)
    await svc._run_resummarize_job(job)

    assert job["status"] == "done"
    assert job["message"] == "summary vectors disabled"
    assert repo.marked == []
    assert repo.recorded == []


@pytest.mark.asyncio
async def test_noop_unsupported_store_is_done_and_untouched(monkeypatch):
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: False)
    monkeypatch.setenv("VECTOR_STORE", "chromadb")
    repo = _FakeSourceRepo()
    monkeypatch.setattr(_state, "_source_repo", repo)

    job = _job("src-1", 5)
    await svc._run_resummarize_job(job)

    assert job["status"] == "done"
    assert job["message"] == "not supported by chromadb"
    assert repo.marked == []
    assert repo.recorded == []


# ---------------------------------------------------------------------------
# The mark is set before the first write
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mark_set_before_first_write(monkeypatch):
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=_rows(3), target=5,
    )
    await svc._run_resummarize_job(job)

    assert "mark" in call_order and "write" in call_order
    assert call_order.index("mark") < call_order.index("write")


# ---------------------------------------------------------------------------
# Snapshot once; batches follow EMBED_BATCH_SIZE
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_once_and_batches_follow_embed_batch_size(monkeypatch):
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=_rows(5), target=5, embed_batch_size=2,
    )
    await svc._run_resummarize_job(job)

    assert store.snapshot_calls == 1
    assert [len(b) for b in store.fetch_batches] == [2, 2, 1]
    assert job["status"] == "done"


# ---------------------------------------------------------------------------
# Target version and cache hits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summaries_use_target_version_and_cached_chunk_makes_no_llm_call(monkeypatch):
    rows = _rows(2)
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=rows, target=9,
    )
    cached_text = rows[0]["chunk_text"]
    fake_llm.cache[(svc.llm.chunk_cache_key(cached_text), 9)] = "Cached summary."

    await svc._run_resummarize_job(job)

    assert cached_text not in fake_llm.generate_calls
    assert set(fake_llm.generate_versions) <= {9}
    assert job["status"] == "done"


# ---------------------------------------------------------------------------
# A rejected summary passes None and is not an error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rejected_summary_passes_none_and_is_not_an_error(monkeypatch):
    rows = _rows(2)
    reject_text = rows[0]["chunk_text"]
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=rows, target=5, reject_texts={reject_text},
    )
    await svc._run_resummarize_job(job)

    assert job["errors"] == 0
    assert job["status"] == "done"
    ids, vectors = store.write_batches[0]
    assert vectors[ids.index(rows[0]["id"])] is None
    assert job_errors.calls == []


# ---------------------------------------------------------------------------
# A clean run records the version and progress reaches the total
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clean_run_records_version_and_progress_reaches_total(monkeypatch):
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=_rows(4), target=7,
    )
    await svc._run_resummarize_job(job)

    assert job["status"] == "done"
    assert repo.recorded == [("src-1", 7)]
    assert job["processed_files"] == 4
    assert job["total_chunks"] == 4
    assert job["message"] == "refreshed 4/4 chunks to chunk_summary v7"


# ---------------------------------------------------------------------------
# A transient error: done with errors == 1, a job_file_errors row, no
# record_summary_version
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transient_error_done_with_errors_and_file_error_row(monkeypatch):
    rows = _rows(3)
    fail_text = rows[1]["chunk_text"]
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=rows, target=5, fail_texts={fail_text},
    )
    await svc._run_resummarize_job(job)

    assert job["status"] == "done"
    assert job["errors"] == 1
    assert repo.recorded == []
    assert len(job_errors.calls) == 1
    assert job_errors.calls[0]["file_path"] == rows[1]["file_path"]
    assert job_errors.calls[0]["error_kind"] == "exception"


# ---------------------------------------------------------------------------
# A re-run after that failure makes LLM calls only for the failed chunk
# (SC-003)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rerun_after_failure_only_calls_llm_for_the_failed_chunk(monkeypatch):
    rows = _rows(3)
    fail_text = rows[1]["chunk_text"]
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=rows, target=5, fail_texts={fail_text},
    )
    await svc._run_resummarize_job(job)
    assert job["errors"] == 1
    assert set(fake_llm.generate_calls) == {r["chunk_text"] for r in rows}

    fake_llm.fail_texts.clear()
    fake_llm.generate_calls.clear()
    job2 = _job("src-1", 5)
    await svc._run_resummarize_job(job2)

    assert fake_llm.generate_calls == [fail_text]
    assert job2["errors"] == 0
    assert job2["status"] == "done"
    assert repo.recorded == [("src-1", 5)]


# ---------------------------------------------------------------------------
# A count mismatch after the loop fails the job, naming both counts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_count_mismatch_fails_job_naming_both_counts(monkeypatch):
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=_rows(3), target=5, count_override=2,
    )
    await svc._run_resummarize_job(job)

    assert job["status"] == "failed"
    assert "3" in job["error"]
    assert "2" in job["error"]
    assert repo.recorded == []


# ---------------------------------------------------------------------------
# A source deleted mid-job ends done with "source deleted", no further
# writes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_source_deleted_mid_job_done_no_further_writes(monkeypatch):
    # One existence check up front (the already-current short-circuit), then
    # each successfully-written batch checks existence three times (top of
    # loop, immediately before the write, immediately after it) -- batch 1
    # sees the source present the whole way, batch 2's top-of-loop check
    # finds it gone before ever fetching.
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=_rows(4), target=5, embed_batch_size=2,
        existence=[True, True, True, True, False],
    )
    await svc._run_resummarize_job(job)

    assert job["status"] == "done"
    assert job["message"] == "source deleted"
    assert len(store.write_batches) == 1
    assert store.count_calls == 0
    assert repo.recorded == []


# ---------------------------------------------------------------------------
# Fix 4: source deleted between fetch and write must not write (Milvus
# upsert would resurrect the row under a stale source_id)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_source_deleted_between_fetch_and_write_skips_write(monkeypatch):
    # existence: already-current check, top-of-loop, then gone by the
    # pre-write re-check.
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=_rows(2), target=5, existence=[True, True, False],
    )
    await svc._run_resummarize_job(job)

    assert job["status"] == "done"
    assert job["message"] == "source deleted"
    assert store.write_batches == []
    assert repo.recorded == []


# ---------------------------------------------------------------------------
# Fix 4: source deleted right after a write must delete the just-written
# batch (best-effort, whole-source) and stop -- no next batch.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_source_deleted_right_after_write_deletes_batch_and_stops(monkeypatch):
    # existence: already-current check, top-of-loop, pre-write, then gone by
    # the post-write re-check.
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=_rows(4), target=5, embed_batch_size=2,
        existence=[True, True, True, False],
    )
    deleted: list[str] = []
    monkeypatch.setattr(svc, "delete_chunks_by_source", lambda sid: deleted.append(sid))

    await svc._run_resummarize_job(job)

    assert job["status"] == "done"
    assert job["message"] == "source deleted"
    assert len(store.write_batches) == 1
    assert deleted == ["src-1"]
    assert repo.recorded == []


# ---------------------------------------------------------------------------
# Fix 1: a transient error writes back the row's existing summary_vector,
# not None -- a rejection (deterministic) still gets None.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_error_outcome_preserves_existing_summary_vector(monkeypatch):
    rows = _rows(2)
    rows[0]["summary_vector"] = [9.9, 9.9]
    fail_text = rows[0]["chunk_text"]
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=rows, target=5, fail_texts={fail_text},
    )
    await svc._run_resummarize_job(job)

    ids, vectors = store.write_batches[0]
    assert vectors[ids.index(rows[0]["id"])] == [9.9, 9.9]
    assert job["status"] == "done"


# ---------------------------------------------------------------------------
# Fix 1: every chunk erroring ends the job failed, not done -- the version
# is not recorded and the refresh mark is left set.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_chunks_errored_ends_failed_not_done(monkeypatch):
    rows = _rows(2)
    fail_texts = {r["chunk_text"] for r in rows}
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=rows, target=5, fail_texts=fail_texts,
    )
    await svc._run_resummarize_job(job)

    assert job["status"] == "failed"
    assert repo.recorded == []
    assert repo.marked == [("src-1", 5)]


# ---------------------------------------------------------------------------
# Fix 3: the effective view is refreshed before the target is re-resolved,
# so a worker in another process with a stale view can't override the job's
# explicit target.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reload_called_before_resolving_target(monkeypatch):
    order: list[str] = []

    async def _reload():
        order.append("reload")

    def _effective(op, source_id=None):
        order.append("effective")
        return 5

    monkeypatch.setattr(prompt_pins, "reload", _reload)
    monkeypatch.setattr(prompt_pins, "effective", _effective)
    repo = _FakeSourceRepo()
    monkeypatch.setattr(_state, "_source_repo", repo)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: False)
    monkeypatch.setenv("VECTOR_STORE", "milvus")

    job = _job("src-1", 5)
    await svc._run_resummarize_job(job)

    assert order == ["reload", "effective"]


# ---------------------------------------------------------------------------
# Fix 3: a source already cleanly at the target ends done without marking
# or writing anything.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_already_current_at_target_ends_done_without_writes(monkeypatch):
    from types import SimpleNamespace

    record = SimpleNamespace(summary_prompt_version=5, summary_refresh_target=None)
    repo = _FakeSourceRepo(record=record)
    monkeypatch.setattr(_state, "_source_repo", repo)
    monkeypatch.setattr(prompt_pins, "effective", lambda op, source_id=None: 5)

    def _boom(*a, **k):
        raise AssertionError("must not touch the vector store once already current")

    monkeypatch.setattr(retriever_module, "snapshot_source_row_ids", _boom, raising=False)

    job = _job("src-1", 5)
    await svc._run_resummarize_job(job)

    assert job["status"] == "done"
    assert job["message"] == "already current at chunk_summary v5"
    assert repo.marked == []
    assert repo.recorded == []


# ---------------------------------------------------------------------------
# A pin moved while the job was queued is re-resolved at start, and the
# payload is updated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_moved_while_queued_reresolves_and_updates_payload(monkeypatch):
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=_rows(2), target=3, pin=8,
    )
    await svc._run_resummarize_job(job)

    assert job["payload"]["target_version"] == 8
    assert repo.marked == [("src-1", 8)]
    assert repo.recorded == [("src-1", 8)]
    assert job["message"] == "refreshed 2/2 chunks to chunk_summary v8"


# ---------------------------------------------------------------------------
# No parse, code-embedding or graph function is called (SC-002)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_parse_code_embed_or_graph_calls(monkeypatch):
    job, store, repo, job_errors, fake_llm, call_order = _setup(
        monkeypatch, rows=_rows(3), target=5,
    )

    def _boom(*a, **k):
        raise AssertionError("must not be called by a resummarize job")

    monkeypatch.setattr(svc.indexer, "parse_and_chunk", _boom)
    monkeypatch.setattr(svc, "insert_chunks", _boom)
    monkeypatch.setattr(svc, "init_collection", _boom)
    monkeypatch.setattr(svc, "_graph_index_file", _boom)
    monkeypatch.setattr(svc.graph_store, "upsert_source", _boom)
    monkeypatch.setattr(svc.graph_store, "delete_entities_by_file", _boom)

    await svc._run_resummarize_job(job)

    assert job["status"] == "done"
