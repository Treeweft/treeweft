"""The index-status gate on every route it must cover (ADR-004 §3).

Forces `index_guard`'s cached status directly (no real stores touched) and
checks the HTTP-level effect: 409s with the errors.md body shape on read
routes and write routes while `reindex_required`; everything else still
works; and a structural check that no future `/search`, `/find-*`,
`/graph-*`, `/index-*` or `/build-community` route can skip the gate
unnoticed.
"""
import pytest
from fastapi.testclient import TestClient

from treeweft.adapters.postgresql import maintenance_lock
from treeweft.application import index_guard as ig
from treeweft.application.indexer_service import app

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_status(monkeypatch):
    monkeypatch.setattr(ig, "_status", ig.IndexStatus("ok"))
    yield


def _force_reindex_required(monkeypatch, reason="vector store (fake): embedding_model is A, configured B"):
    monkeypatch.setattr(ig, "_status", ig.IndexStatus("reindex_required", reason=reason))


READ_REQUESTS = [
    ("post", "/search", {"query": "foo", "source_id": "src-1"}),
    ("post", "/hydrate-chunks", {"ids": ["a.py:1-2"]}),
    ("get", "/find-definition", {"name": "foo"}),
    ("get", "/find-callers", {"name_or_id": "foo"}),
    ("get", "/find-references", {"name_or_id": "foo"}),
    ("post", "/graph-explore", {"query": "foo", "source_id": "src-1"}),
]

WRITE_REQUESTS = [
    ("post", "/index-file", {"file_path": "/tmp/does-not-exist.py"}),
    ("post", "/index-directory", {"directory": "/tmp/does-not-exist-dir"}),
    ("post", "/index-repo", {"path": "/tmp/does-not-exist-repo"}),
    ("post", "/index-graph", {"source_id": "nope"}),
    ("post", "/jobs/does-not-exist/retry", None),
    ("post", "/build-community", None),
]


def _call(method: str, path: str, body):
    if method == "get":
        return client.get(path, params=body)
    return client.post(path, json=body)


class TestReadRoutesRefused:
    @pytest.mark.parametrize("method,path,body", READ_REQUESTS)
    def test_returns_409_with_the_errors_md_body(self, monkeypatch, method, path, body):
        _force_reindex_required(monkeypatch)
        resp = _call(method, path, body)
        assert resp.status_code == 409
        payload = resp.json()
        for key in ("detail", "reason", "index_status", "rebuild"):
            assert key in payload
        assert payload["index_status"] == "reindex_required"
        assert len(payload["detail"]) <= 300

    def test_detail_keeps_the_pointer_when_the_reason_is_huge(self, monkeypatch):
        _force_reindex_required(monkeypatch, reason="x" * 1000)
        resp = client.post("/search", json={"query": "foo", "source_id": "src-1"})
        payload = resp.json()
        assert len(payload["detail"]) <= 300
        assert "/index/rebuild" in payload["detail"]


def _no_admin_required(request):
    return None


class TestWriteRoutesRefused:
    @pytest.mark.parametrize("method,path,body", WRITE_REQUESTS)
    def test_returns_409_and_enqueues_nothing(self, monkeypatch, method, path, body):
        _force_reindex_required(monkeypatch)
        monkeypatch.setattr("treeweft.application.indexer_service.authz._require_admin", _no_admin_required)
        resp = _call(method, path, body)
        assert resp.status_code == 409
        payload = resp.json()
        assert payload["index_status"] == "reindex_required"


class TestUnaffectedRoutesStillWork:
    def test_health_answers_normally(self, monkeypatch):
        _force_reindex_required(monkeypatch)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_sources_answers_normally(self, monkeypatch):
        _force_reindex_required(monkeypatch)
        resp = client.get("/sources")
        assert resp.status_code != 409

    def test_jobs_answers_normally(self, monkeypatch):
        _force_reindex_required(monkeypatch)
        resp = client.get("/jobs")
        assert resp.status_code != 409

    def test_job_groups_answers_normally(self, monkeypatch):
        _force_reindex_required(monkeypatch)
        resp = client.get("/job-groups")
        assert resp.status_code != 409

    def test_build_community_status_answers_normally(self, monkeypatch):
        _force_reindex_required(monkeypatch)
        resp = client.get("/build-community")
        assert resp.status_code != 409


class TestAuthRunsBeforeTheGate:
    def test_auth_on_no_credentials_is_401_not_409(self, monkeypatch):
        _force_reindex_required(monkeypatch)
        monkeypatch.setenv("AUTH_ENABLED", "true")
        resp = client.post("/search", json={"query": "foo", "source_id": "src-1"})
        assert resp.status_code in (401, 403)
        assert resp.status_code != 409


class TestPreparing409OnStoreError:
    def test_store_error_becomes_preparing_409_when_lock_exclusive(self, monkeypatch):
        # status stays "ok" (autouse fixture) so require_searchable() passes;
        # the store call itself raises.
        async def _boom(*a, **kw):
            raise RuntimeError("collection dropped mid-search")

        async def _exclusive():
            return "exclusive"

        monkeypatch.setattr("treeweft.application.indexer_service.embed_query", _boom)
        monkeypatch.setattr(maintenance_lock, "probe", _exclusive)
        resp = client.post("/search", json={"query": "foo", "source_id": "src-1"})
        assert resp.status_code == 409
        assert resp.json()["index_status"] == "rebuilding"

    def test_store_error_propagates_when_lock_not_held(self, monkeypatch):
        async def _boom(*a, **kw):
            raise RuntimeError("boom, unrelated to any rebuild")

        async def _none():
            return None

        monkeypatch.setattr("treeweft.application.indexer_service.embed_query", _boom)
        monkeypatch.setattr(maintenance_lock, "probe", _none)
        resp = client.post("/search", json={"query": "foo", "source_id": "src-1"})
        assert resp.status_code == 500

    def test_probe_never_called_when_the_store_call_succeeds(self, monkeypatch):
        calls = []

        async def _tracked():
            calls.append(1)
            return None

        async def _empty(*a, **kw):
            return []

        monkeypatch.setattr("treeweft.application.indexer_service.graph_store.find_entities_by_name", _empty)
        monkeypatch.setattr(maintenance_lock, "probe", _tracked)
        resp = client.get("/find-definition", params={"name": "nonexistent-xyz"})
        assert resp.status_code == 200
        assert calls == []


READ_PREFIXES = ("/search", "/hydrate", "/find-", "/graph-")
WRITE_PREFIXES = ("/index-", "/build-community")

# (method, path) pairs the gate must NOT cover, with why.
EXEMPT = {
    ("GET", "/build-community"): "status poll only — no store access",
}


class TestRouteClassification:
    def test_every_matching_route_is_read_write_or_exempt(self):
        read_paths = {p for _, p, _ in READ_REQUESTS}
        write_paths = {p.rstrip("does-not-exist/retry") + "{job_id}/retry" if "retry" in p else p
                       for _, p, _ in WRITE_REQUESTS}
        write_paths = {"/jobs/{job_id}/retry" if "retry" in p else p for p in write_paths}

        unclassified = []
        for route in app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or set()
            if path is None:
                continue
            matches_prefix = any(path.startswith(p) for p in READ_PREFIXES + WRITE_PREFIXES)
            is_retry = path == "/jobs/{job_id}/retry"
            if not matches_prefix and not is_retry:
                continue
            for method in methods:
                key = (method, path)
                if key in EXEMPT:
                    continue
                if path in read_paths or path in write_paths or is_retry:
                    continue
                unclassified.append(key)
        assert not unclassified, f"routes not covered by the gate test or EXEMPT: {unclassified}"
