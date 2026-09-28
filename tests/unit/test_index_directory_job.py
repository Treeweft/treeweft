"""`/index-directory` jobs build the code graph and summaries (#51).

The directory job had its own chunk-and-embed loop, which never called
`_graph_index_file` and never summarized, yet finalized the source as
`graph_indexed=True`. It now indexes each file through `_process_file`, the
same path as `/index-repo`.

Runs the real job against fakes for the stores, the embedder and the LLM,
recording what the job writes.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from treeweft.application import indexer_runners as svc
from treeweft.application import indexer_state as _state
from treeweft.application import prompt_pins

pytestmark = pytest.mark.asyncio


class _FakeSourceRepo:
    def __init__(self):
        self.saved: list = []
        self.summary_versions: list[tuple[str, int | None]] = []

    async def save(self, record):
        self.saved.append(record)

    async def get_by_id(self, source_id):
        return None

    async def record_summary_version(self, source_id, version):
        self.summary_versions.append((source_id, version))

    async def set_graph_indexed(self, *args, **kwargs):
        return None


class _Writes:
    """What the job wrote, by store."""

    def __init__(self):
        self.graph: dict[str, list[str]] = {}  # file_path -> entity names
        self.chunks: list[dict] = []
        self.summary_vectors: list = []


@pytest.fixture
def writes(monkeypatch):
    w = _Writes()

    async def store_graph(entities, relationships, source_id=None):
        for e in entities:
            w.graph.setdefault(e["file_path"], []).append(e["name"])

    async def insert_chunks(chunks, embeddings, summary_embeddings=None):
        w.chunks.extend(chunks)
        w.summary_vectors.extend(summary_embeddings or [None] * len(chunks))

    async def embed(texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    monkeypatch.setattr(svc.graph_store, "store_graph", store_graph)
    monkeypatch.setattr(svc.graph_store, "upsert_source", AsyncMock())
    monkeypatch.setattr(svc, "insert_chunks", insert_chunks)
    monkeypatch.setattr(svc, "init_collection", AsyncMock())
    monkeypatch.setattr(svc, "embed", embed)
    monkeypatch.setattr(svc, "_reset_source", AsyncMock())
    monkeypatch.setattr(svc, "_persist_job", AsyncMock())
    monkeypatch.setattr(svc, "_persist_job_sync", lambda job: None)
    monkeypatch.setattr(svc.community, "run_post_index_signals", AsyncMock(return_value=0))
    monkeypatch.setattr(_state, "_job_file_error_store", AsyncMock())
    monkeypatch.setattr(prompt_pins, "effective", lambda op, source_id=None: 4)
    return w


@pytest.fixture
def source_repo(monkeypatch):
    repo = _FakeSourceRepo()
    monkeypatch.setattr(_state, "_source_repo", repo)
    return repo


@pytest.fixture
def summaries_off(monkeypatch):
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", False)


def _tree(root, files: dict[str, str]):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


PY_A = "class Parser:\n    def parse(self, text):\n        return text.split()\n"
PY_B = "def main():\n    return Parser().parse('a b')\n"


async def _run(tmp_path, pattern="**/*"):
    job = svc._new_job("directory", "src-dir", str(tmp_path))
    await svc._run_index_directory_job(job, str(tmp_path), pattern)
    return job


async def test_every_file_goes_to_the_graph(tmp_path, writes, source_repo, summaries_off):
    _tree(tmp_path, {"a.py": PY_A, "pkg/b.py": PY_B})

    job = await _run(tmp_path)

    assert job["status"] == "done", job.get("error")
    assert set(writes.graph) == {str(tmp_path / "a.py"), str(tmp_path / "pkg" / "b.py")}
    assert "Parser" in writes.graph[str(tmp_path / "a.py")]
    assert "main" in writes.graph[str(tmp_path / "pkg" / "b.py")]
    assert writes.chunks, "chunks were not inserted"


async def test_source_is_recorded_as_graph_indexed_only_because_it_was(
    tmp_path, writes, source_repo, summaries_off
):
    _tree(tmp_path, {"a.py": PY_A})

    await _run(tmp_path)

    assert writes.graph
    assert source_repo.saved[-1].graph_indexed is True


async def test_pattern_limits_the_files(tmp_path, writes, source_repo, summaries_off):
    _tree(tmp_path, {"a.py": PY_A, "notes.md": "# Notes\n\nSome text.\n", "b.go": "package b\n"})

    job = await _run(tmp_path, pattern="**/*.py")

    assert job["total_files"] == 1
    assert set(writes.graph) == {str(tmp_path / "a.py")}
    assert {c["file_path"] for c in writes.chunks} == {str(tmp_path / "a.py")}


async def test_pattern_matches_files_at_the_top_level(tmp_path, writes, source_repo, summaries_off):
    """`Path.rglob("**/*.py")` includes top-level files; so must the filter."""
    _tree(tmp_path, {"top.py": PY_A, "deep/er/x.py": PY_B})

    job = await _run(tmp_path, pattern="**/*.py")

    assert job["total_files"] == 2


async def test_hidden_directories_are_skipped_as_for_a_repo(tmp_path, writes, source_repo, summaries_off):
    """The same file selection as /index-repo: hidden directories (.venv,
    .git) are skipped; a hidden file is indexed like any other."""
    _tree(tmp_path, {"a.py": PY_A, ".venv/lib/x.py": PY_B, ".hidden.py": PY_B})

    job = await _run(tmp_path)

    assert job["total_files"] == 2
    assert set(writes.graph) == {str(tmp_path / "a.py"), str(tmp_path / ".hidden.py")}


async def test_chunks_are_summarized_and_the_version_recorded(tmp_path, writes, source_repo, monkeypatch):
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", True)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: True)

    async def summaries(chunks, *, version):
        return [(f"summary of {c['file_path']}", "generated") for c in chunks]

    monkeypatch.setattr(svc, "_summaries_for_chunks", summaries)
    _tree(tmp_path, {"a.py": PY_A})

    job = await _run(tmp_path)

    assert job["status"] == "done", job.get("error")
    assert writes.summary_vectors and all(v is not None for v in writes.summary_vectors)
    assert source_repo.summary_versions == [("src-dir", 4)]


async def test_a_file_that_fails_does_not_stop_the_others(tmp_path, writes, source_repo, summaries_off, monkeypatch):
    _tree(tmp_path, {"a.py": PY_A, "b.py": PY_B})
    real = svc.indexer.parse_and_chunk

    def parse_and_chunk(path):
        if path.endswith("a.py"):
            raise ValueError("unparseable")
        return real(path)

    monkeypatch.setattr(svc.indexer, "parse_and_chunk", parse_and_chunk)

    job = await _run(tmp_path)

    assert job["errors"] == 1
    assert set(writes.graph) == {str(tmp_path / "b.py")}
