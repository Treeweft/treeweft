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
WRITE_PREFIXES = ("/index-", "/index/", "/build-community")

# (method, path) pairs the gate must NOT cover, with why.
EXEMPT = {
    ("GET", "/build-community"): "status poll only — no store access",
    ("POST", "/index/rebuild"): "its own conflict/lock semantics — covered by test_index_rebuild.py",
}


def _flat_routes(routes=None, prefix=""):
    """Every (method, path) the app serves, descending into routers mounted
    with `app.include_router` (they appear in `app.routes` as path-less
    `_IncludedRouter` entries in current FastAPI, so a plain walk misses them)."""
    out = set()
    for route in app.routes if routes is None else routes:
        path = getattr(route, "path", None)
        if path is not None:
            for method in getattr(route, "methods", None) or set():
                if method not in ("HEAD", "OPTIONS"):
                    out.add((method, prefix + path))
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            ctx = getattr(route, "include_context", None)
            out |= _flat_routes(inner.routes, prefix + (getattr(ctx, "prefix", "") or ""))
    return out


def test_flat_routes_sees_included_routers():
    """Guards the helper itself: routes from the auth, webhook and prompts
    routers must be visible, or every classification below is blind to them."""
    routes = _flat_routes()
    assert ("POST", "/webhook") in routes
    assert ("POST", "/auth/login") in routes
    assert ("POST", "/sources/{source_id}/resummarize") in routes


class TestRouteClassification:
    def test_every_matching_route_is_read_write_or_exempt(self):
        read_paths = {p for _, p, _ in READ_REQUESTS}
        write_paths = {p.rstrip("does-not-exist/retry") + "{job_id}/retry" if "retry" in p else p
                       for _, p, _ in WRITE_REQUESTS}
        write_paths = {"/jobs/{job_id}/retry" if "retry" in p else p for p in write_paths}

        unclassified = []
        for method, path in sorted(_flat_routes()):
            matches_prefix = any(path.startswith(p) for p in READ_PREFIXES + WRITE_PREFIXES)
            is_retry = path == "/jobs/{job_id}/retry"
            if not matches_prefix and not is_retry:
                continue
            key = (method, path)
            if key in EXEMPT:
                continue
            if path in read_paths or path in write_paths or is_retry:
                continue
            unclassified.append(key)
        assert not unclassified, f"routes not covered by the gate test or EXEMPT: {unclassified}"


# ── parser gate (#95 / #102 / #105) ────────────────────────────────────────
#
# Every write route whose job parses code must call
# `index_guard.require_parsers()` before the job is queued, or a
# parser outage silently builds a structure-less index on TokenChunker
# windows. Each route is either parser-gated (and proven so by an HTTP call)
# or parser-exempt with a reason.

PARSER_GATED_REQUESTS = [
    ("POST", "/index-file", {"file_path": "/tmp/does-not-exist.py"}),
    # Non-existent paths: the parser gate answers first; if it ever regresses
    # the handler 404s instead of walking a real directory.
    ("POST", "/index-directory", {"directory": "/tmp/does-not-exist-dir"}),
    ("POST", "/index-repo", {"path": "/tmp/does-not-exist-repo"}),
    ("POST", "/index-graph", {"source_id": "src-1"}),
    ("POST", "/jobs/{job_id}/retry", None),  # needs a non-resummarize job in the store
    ("POST", "/index/rebuild", None),  # real (non-dry-run) rebuild only
]

# (method, path) write routes that are NOT parser-gated, with why.
PARSER_EXEMPT = {
    ("POST", "/build-community"): "graph-signal backfill over already-indexed data — parses no code",
    ("GET", "/build-community"): "status poll only — parses no code",
    ("POST", "/webhook"): (
        "deliberately ungated (#102): a 409 would drop the push (forges don't redeliver "
        "4xx); the queued job hits the runner-side parser check and dead-letters instead"
    ),
    ("POST", "/sources/{source_id}/resummarize"): "re-summarizes stored chunks — parses no code",
}

# Write routes outside WRITE_PREFIXES that queue (or could queue) index work,
# so their parser-gate status is an explicit, reviewed decision.
PARSER_SCOPE_EXTRA = {
    "/jobs/{job_id}/retry",
    "/webhook",
    "/sources/{source_id}/resummarize",
}


class TestParserGateClassification:
    def test_every_write_route_is_parser_gated_or_exempt(self):
        gated = {(m, p) for m, p, _ in PARSER_GATED_REQUESTS}
        assert not gated & set(PARSER_EXEMPT), "route is both parser-gated and parser-exempt"

        unclassified = [
            key for key in sorted(_flat_routes())
            if (key[1].startswith(WRITE_PREFIXES) or key[1] in PARSER_SCOPE_EXTRA)
            and key not in gated and key not in PARSER_EXEMPT
        ]
        assert not unclassified, (
            f"write routes neither parser-gated nor parser-exempt: {sorted(unclassified)}. "
            "If the route's job parses code, call index_guard.require_parsers() before the job is "
            "queued and add it to PARSER_GATED_REQUESTS; otherwise add a reasoned "
            "PARSER_EXEMPT entry in tests/unit/test_index_gate_routes.py."
        )

    def test_gated_and_exempt_entries_are_real_routes(self):
        real = _flat_routes()
        stale = [k for k in {(m, p) for m, p, _ in PARSER_GATED_REQUESTS} | set(PARSER_EXEMPT) if k not in real]
        assert not stale, f"classified routes that no longer exist: {stale}"


@pytest.mark.real_parser_check
class TestParserGatedRoutesRefused:
    @pytest.fixture(autouse=True)
    def _parsers_down(self, monkeypatch):
        import tree_sitter_language_pack as pack

        from treeweft.adapters.tree_sitter import parsers

        def get_language(lang):
            if lang == "python":
                raise pack.DownloadError("mocked: no network")
            return object()

        parsers.reset_cache()
        monkeypatch.setattr(parsers, "TSParser", lambda language: object())
        monkeypatch.setattr(parsers._pack, "get_language", get_language)
        monkeypatch.setattr(parsers._pack, "downloaded_languages", lambda: list(parsers._languages()))
        monkeypatch.setattr("treeweft.application.indexer_service.authz._require_admin", _no_admin_required)
        yield
        parsers.reset_cache()

    @pytest.mark.parametrize("method,path,body", PARSER_GATED_REQUESTS, ids=[p for _, p, _ in PARSER_GATED_REQUESTS])
    def test_parser_gated_route_returns_409(self, monkeypatch, method, path, body):
        if path == "/jobs/{job_id}/retry":
            from treeweft.application import indexer_state as state
            from treeweft.domain.jobs import Job, JobStatus

            job = Job(id="j1", kind="file", status=JobStatus.DEAD_LETTER)

            class _Store:
                async def get(self, job_id):
                    return job

            monkeypatch.setattr(state, "_job_store", _Store())
            path = "/jobs/j1/retry"
        resp = client.request(method, path, json=body) if body is not None else client.request(method, path)
        assert resp.status_code == 409, resp.text
        payload = resp.json()
        assert payload["reason"] == "parsers_unavailable"
        assert "python" in payload["languages"]
