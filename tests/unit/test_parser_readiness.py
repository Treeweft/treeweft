"""Tree-sitter parser readiness (#100): the check, the 409 gate, the runner
failure, once-per-language ERROR logging, and the prefetch CLI.

Every test mocks `tree_sitter_language_pack.get_language` / `download`; none
touches the network.
"""
import asyncio
import logging

import pytest
import tree_sitter_language_pack as pack
from fastapi.testclient import TestClient

from treeweft.adapters.tree_sitter import indexer as ts_indexer
from treeweft.adapters.tree_sitter import parsers
from treeweft.application import index_guard
from treeweft.application import indexer_runners as runners
from treeweft.application import indexer_state as state
from treeweft.application.indexer_service import app
from treeweft.domain.jobs import Job, JobStatus

pytestmark = pytest.mark.real_parser_check

client = TestClient(app, raise_server_exceptions=False)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(parsers, "_clock", c)
    return c


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    parsers.reset_cache()
    parsers.reset_download_log()
    monkeypatch.setenv("TREEWEFT_PARSER_RETRY_SECONDS", "30")
    yield
    parsers.reset_cache()
    parsers.reset_download_log()


def _mock_get_language(monkeypatch, bad=(), calls=None):
    def fake(lang):
        if calls is not None:
            calls.append(lang)
        if lang in bad:
            raise pack.DownloadError(f"cannot fetch {lang}")
        return object()

    monkeypatch.setattr(parsers._pack, "get_language", fake)
    return fake


class TestCheckParsers:
    def test_success_is_cached_for_the_process(self, monkeypatch, clock):
        calls = []
        _mock_get_language(monkeypatch, calls=calls)
        assert parsers.check_parsers().ok
        n = len(calls)
        assert n > 0
        clock.now += 10_000
        assert parsers.check_parsers().ok
        assert len(calls) == n

    def test_failure_cached_for_ttl_then_rechecked(self, monkeypatch, clock):
        calls = []
        _mock_get_language(monkeypatch, bad={"python"}, calls=calls)
        first = parsers.check_parsers()
        assert not first.ok
        assert first.failures["python"].startswith("DownloadError: cannot fetch python")
        n = len(calls)
        clock.now += 29
        parsers.check_parsers()
        assert len(calls) == n
        clock.now += 2
        _mock_get_language(monkeypatch, calls=calls)  # outage over
        assert parsers.check_parsers().ok
        assert len(calls) > n

    def test_busy_check_answers_from_cache_instead_of_waiting(self, monkeypatch, clock):
        _mock_get_language(monkeypatch, bad={"python"})
        first = parsers.check_parsers()
        clock.now += 31  # cache expired: a new check would re-download
        calls = []
        _mock_get_language(monkeypatch, calls=calls)
        assert parsers._lock.acquire(blocking=False)  # another thread is checking
        try:
            assert parsers.check_parsers() is first
        finally:
            parsers._lock.release()
        assert calls == []

    def test_force_bypasses_the_cache(self, monkeypatch, clock):
        calls = []
        _mock_get_language(monkeypatch, calls=calls)
        parsers.check_parsers()
        n = len(calls)
        parsers.check_parsers(force=True)
        assert len(calls) == 2 * n

    def test_async_variant_does_not_run_on_the_event_loop_thread(self, monkeypatch):
        import threading

        seen = []
        monkeypatch.setattr(parsers, "check_parsers", lambda: seen.append(threading.get_ident()) or parsers.ParserReadiness(True))
        asyncio.run(parsers.check_parsers_async())
        assert seen and seen[0] != threading.get_ident()


class TestRequireParsers:
    def test_ok_returns_none(self, monkeypatch):
        _mock_get_language(monkeypatch)
        assert asyncio.run(index_guard.require_parsers()) is None

    def test_409_body_shape(self, monkeypatch):
        _mock_get_language(monkeypatch, bad={"python", "go"})
        resp = asyncio.run(index_guard.require_parsers())
        assert resp.status_code == 409
        import json

        body = json.loads(resp.body)
        assert body["reason"] == "parsers_unavailable"
        assert body["index_status"] == "parsers_unavailable"
        assert set(body["languages"]) == {"python", "go"}
        assert "DownloadError" in body["languages"]["python"]
        assert body["detail"].startswith("Tree-sitter parsers unavailable for: go, python.")
        assert "retries automatically" in body["detail"]
        assert len(body["detail"]) <= 300


class TestRoutes:
    def test_index_file_refused_when_a_parser_is_missing(self, monkeypatch):
        _mock_get_language(monkeypatch, bad={"python"})
        resp = client.post("/index-file", json={"file_path": "/tmp/nope.py"})
        assert resp.status_code == 409
        body = resp.json()
        assert body["reason"] == "parsers_unavailable"
        assert "python" in body["languages"]

    @pytest.mark.parametrize(
        "path, payload",
        [
            ("/index-directory", {"directory": "/tmp"}),
            ("/index-repo", {"path": "/tmp"}),
            ("/index-graph", {"source_id": "src-1"}),
        ],
    )
    def test_other_parsing_routes_refused(self, monkeypatch, path, payload):
        _mock_get_language(monkeypatch, bad={"python"})
        resp = client.post(path, json=payload) if payload is not None else client.post(path)
        assert resp.status_code == 409, resp.text
        assert resp.json()["reason"] == "parsers_unavailable"

    @staticmethod
    def _store_with(monkeypatch, job):
        class _Store:
            async def get(self, job_id):
                return job

        monkeypatch.setattr(state, "_job_store", _Store())

    def test_retry_refused_for_a_parsing_job(self, monkeypatch):
        _mock_get_language(monkeypatch, bad={"python"})
        self._store_with(monkeypatch, Job(id="j1", kind="file", status=JobStatus.DEAD_LETTER))
        resp = client.post("/jobs/j1/retry")
        assert resp.status_code == 409
        assert resp.json()["reason"] == "parsers_unavailable"

    def test_retry_of_resummarize_is_not_gated_on_parsers(self, monkeypatch):
        _mock_get_language(monkeypatch, bad={"python"})
        # "running" is not retryable, so the route 409s on status, after the
        # point where the parser gate would have refused.
        self._store_with(monkeypatch, Job(id="r1", kind="resummarize", status=JobStatus.RUNNING))
        resp = client.post("/jobs/r1/retry")
        assert resp.status_code == 409
        assert "can only retry" in resp.json()["detail"]

    def test_rebuild_refused_but_dry_run_is_not(self, monkeypatch):
        _mock_get_language(monkeypatch, bad={"python"})
        monkeypatch.setattr("treeweft.application.indexer_service.authz._require_admin", lambda r: None)
        resp = client.post("/index/rebuild")
        assert resp.status_code == 409
        assert resp.json()["reason"] == "parsers_unavailable"
        dry = client.post("/index/rebuild", params={"dry_run": "true"})
        assert dry.json().get("reason") != "parsers_unavailable"

    def test_build_community_is_not_gated_on_parsers(self, monkeypatch):
        _mock_get_language(monkeypatch, bad={"python"})
        monkeypatch.setattr("treeweft.application.indexer_service.authz._require_admin", lambda r: None)

        async def no_lock(mode):
            return None  # route stops here, before any backfill starts

        monkeypatch.setattr("treeweft.application.indexer_service.maintenance_lock.acquire", no_lock)
        resp = client.post("/build-community")
        assert resp.json()["index_status"] == "rebuilding"  # got past every gate


class TestRunner:
    @staticmethod
    def _file_job(tmp_path, monkeypatch, ran):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")

        async def fake_run(jd, target):
            ran.append(target)

        monkeypatch.setattr(runners, "_run_index_file_job", fake_run)
        persisted = []

        async def fake_persist(jd):
            persisted.append(dict(jd))

        monkeypatch.setattr(runners, "_persist_job", fake_persist)
        monkeypatch.setattr(state, "_jobs", {})
        return Job(id="j1", kind="file", source_path=str(f)), persisted

    def test_file_job_fails_with_parsers_unavailable(self, monkeypatch, tmp_path):
        _mock_get_language(monkeypatch, bad={"python"})
        ran = []
        job, persisted = self._file_job(tmp_path, monkeypatch, ran)
        with pytest.raises(parsers.ParsersUnavailableError, match="parsers_unavailable: python"):
            asyncio.run(runners.dispatch_job(job))
        assert ran == []
        assert persisted[-1]["status"] == "failed"
        assert persisted[-1]["error"].startswith("parsers_unavailable: python: DownloadError")
        assert persisted[-1]["finished_at"]

    def test_file_job_runs_when_parsers_load(self, monkeypatch, tmp_path):
        _mock_get_language(monkeypatch)
        ran = []
        job, persisted = self._file_job(tmp_path, monkeypatch, ran)
        asyncio.run(runners.dispatch_job(job))
        assert len(ran) == 1
        assert persisted == []

    def test_close_without_await_closes_the_inner_runner(self, monkeypatch, tmp_path):
        """lifecycle startup recovery probes dispatch_job() and close()s the
        result unawaited; that must close the real runner coroutine too."""
        import warnings

        ran = []
        job, _ = self._file_job(tmp_path, monkeypatch, ran)
        probe = runners.dispatch_job(job)
        inner = probe._coro
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            probe.close()
        assert inner.cr_frame is None  # closed, so no "never awaited" warning
        assert ran == []

    def test_resummarize_is_not_gated(self, monkeypatch):
        _mock_get_language(monkeypatch, bad={"python"})
        ran = []

        async def fake_run(jd):
            ran.append(1)

        monkeypatch.setattr(runners, "_run_resummarize_job", fake_run)
        monkeypatch.setattr(state, "_jobs", {})
        asyncio.run(runners.dispatch_job(Job(id="r1", kind="resummarize", source_id="s")))
        assert ran == [1]


class TestDownloadFailureLogging:
    def test_error_logged_once_per_language(self, monkeypatch, caplog):
        def boom(lang):
            raise pack.DownloadError("HTTP 503 from github")

        monkeypatch.setattr(ts_indexer, "get_language", boom)
        with caplog.at_level(logging.DEBUG, logger=parsers.logger.name):
            c1 = ts_indexer._make_chunker("python")
            c2 = ts_indexer._make_chunker("python")
        assert type(c1).__name__ == "TokenChunker" and type(c2).__name__ == "TokenChunker"
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "HTTP 503 from github" in errors[0].getMessage()
        warnings_ = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings_) == 1  # the repeat stays visible, below ERROR


class TestCli:
    def test_check_only_makes_no_download_call(self, monkeypatch, capsys):
        _mock_get_language(monkeypatch)
        monkeypatch.setattr(parsers._pack, "download", lambda l: pytest.fail("download called"))
        assert parsers.main([]) == 0
        assert "cache_dir" in capsys.readouterr().out

    def test_check_only_exit_1_on_failure(self, monkeypatch, capsys):
        _mock_get_language(monkeypatch, bad={"go"})
        assert parsers.main([]) == 1
        assert "go" in capsys.readouterr().err

    def test_prefetch_retries_n_times_then_exits_1(self, monkeypatch):
        _mock_get_language(monkeypatch, bad={"go"})
        attempts, sleeps = [], []

        def failing(langs):
            attempts.append(1)
            raise pack.DownloadError("offline")

        monkeypatch.setattr(parsers._pack, "download", failing)
        assert parsers.main(["--prefetch", "--attempts", "4"], sleep=sleeps.append) == 1
        assert len(attempts) == 4
        assert sleeps == [2, 4, 8]

    def test_prefetch_exit_0_when_a_retry_succeeds(self, monkeypatch):
        _mock_get_language(monkeypatch)
        attempts, sleeps = [], []

        def flaky(langs):
            attempts.append(1)
            if len(attempts) < 3:
                raise pack.DownloadError("offline")
            return len(langs)

        monkeypatch.setattr(parsers._pack, "download", flaky)
        assert parsers.main(["--prefetch"], sleep=sleeps.append) == 0
        assert len(attempts) == 3
        assert sleeps == [2, 4]
