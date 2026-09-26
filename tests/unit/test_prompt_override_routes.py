"""Per-source prompt overrides and manual refresh (ADR-003, T033).

`PUT`/`DELETE /prompt-pins/chunk_summary/sources/{id}`, the always-refused
`PUT /prompt-pins/hyde/sources/{id}`, and `POST /sources/{id}/resummarize`.
Fakes every store the way `test_prompt_routes.py` does (PromptPinStore, the
source repository, the job store, job group store and queue) — no real
Postgres. A test-only chunk_summary v4 is monkeypatched into
`prompts.REGISTRY`, as `test_prompt_routes.py` does.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg
import pytest
from fastapi.testclient import TestClient

from treeweft.adapters.llm_api import prompts
from treeweft.adapters.postgresql.prompt_pin_store import PinRow
from treeweft.application import index_guard as ig
from treeweft.application import indexer_service as idx_svc
from treeweft.application import indexer_state as idx_state
from treeweft.application import prompt_pins
from treeweft.domain.sources import SourceRecord

app = idx_svc.app


# ---------------------------------------------------------------------------
# A test-only chunk_summary v4, mirroring test_prompt_routes.py
# ---------------------------------------------------------------------------

_V4 = prompts.PromptVersion(
    operation="chunk_summary",
    version=4,
    system="v4 test system prompt.",
    schema=prompts.FrozenResponseSchema(min_length=1, max_length=10, forbidden_phrases=("nope4",)),
    notes="test v4",
)


@pytest.fixture
def registry_v4(monkeypatch):
    registry = dict(prompts.REGISTRY)
    registry["chunk_summary"] = {**registry["chunk_summary"], 4: _V4}
    monkeypatch.setattr(prompts, "REGISTRY", registry)
    return _V4


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakePinStore:
    def __init__(self, rows=None):
        self.rows: list[PinRow] = list(rows or [])
        self.upsert_calls: list[tuple] = []
        self.delete_calls: list[tuple] = []
        self.delete_overrides_calls: list[str] = []

    async def list_all(self):
        return list(self.rows)

    async def upsert(self, operation, scope, version, updated_by):
        self.upsert_calls.append((operation, scope, version, updated_by))
        row = PinRow(
            operation=operation, scope=scope, version=version,
            updated_at=datetime.now(timezone.utc), updated_by=updated_by,
        )
        self.rows = [r for r in self.rows if not (r.operation == operation and r.scope == scope)]
        self.rows.append(row)
        return row

    async def delete(self, operation, scope):
        self.delete_calls.append((operation, scope))
        before = len(self.rows)
        self.rows = [r for r in self.rows if not (r.operation == operation and r.scope == scope)]
        return len(self.rows) != before

    async def delete_overrides_for_source(self, source_id):
        self.delete_overrides_calls.append(source_id)
        before = len(self.rows)
        self.rows = [r for r in self.rows if not (r.scope == source_id and r.scope != "deployment")]
        return before - len(self.rows)


def _pin_row(operation: str, scope: str, version: int, updated_by: str | None = None) -> PinRow:
    return PinRow(
        operation=operation, scope=scope, version=version,
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc), updated_by=updated_by,
    )


class FakeSourceRepo:
    def __init__(self, sources: list[SourceRecord]):
        self.by_id = {s.id: s for s in sources}
        self.deleted: list[str] = []

    async def list_all(self):
        return list(self.by_id.values())

    async def get_by_id(self, source_id):
        return self.by_id.get(source_id)

    async def delete(self, source_id):
        self.deleted.append(source_id)
        self.by_id.pop(source_id, None)


class _FakeActiveJob:
    def __init__(self, d: dict):
        self._d = d

    def to_dict(self) -> dict:
        return self._d


class FakeJobStore:
    def __init__(self):
        self.active: dict[str, dict] = {}
        self.upserted: list = []
        self.boom_for: set[str] = set()

    async def find_active_for_source(self, source_id):
        d = self.active.get(source_id)
        return _FakeActiveJob(d) if d else None

    async def upsert(self, job):
        if job.source_id in self.boom_for:
            raise asyncpg.UniqueViolationError(
                'duplicate key value violates unique constraint "jobs_active_source_uniq"'
            )
        self.upserted.append(job)


class FakeJobGroupStore:
    def __init__(self):
        self.created: list[dict] = []
        self._n = 0

    async def create(self, label, kind, created_by=None, task_count=0):
        self._n += 1
        gid = f"grp-{self._n}"
        self.created.append(
            {"id": gid, "label": label, "kind": kind, "created_by": created_by, "task_count": task_count}
        )
        return gid


class FakeQueue:
    def __init__(self):
        self.enqueued: list[str] = []

    async def enqueue(self, job_id):
        self.enqueued.append(job_id)


def _source(id_, *, version=3, target=None, chunk_count=100, url="https://example/repo"):
    return SourceRecord(
        id=id_, path="", url=url, branch="main",
        chunk_count=chunk_count, summary_prompt_version=version, summary_refresh_target=target,
    )


class _FakeUser:
    def __init__(self, id_: str):
        self.id = id_


def _grant_admin(monkeypatch, user_id: str = "admin-1"):
    def _require_admin(request):
        request.state.user = _FakeUser(user_id)
        return request.state.user

    monkeypatch.setattr(idx_svc.authz, "_require_admin", _require_admin)


@pytest.fixture
def env(monkeypatch):
    pin_store = FakePinStore([_pin_row("chunk_summary", "deployment", 3), _pin_row("hyde", "deployment", 1)])
    source_repo = FakeSourceRepo([])
    job_store = FakeJobStore()
    group_store = FakeJobGroupStore()
    queue = FakeQueue()

    monkeypatch.setattr(prompt_pins, "_pin_store", pin_store)
    monkeypatch.setattr(prompt_pins, "_source_repo", source_repo)
    monkeypatch.setattr(idx_state, "_source_repo", source_repo)
    monkeypatch.setattr(idx_state, "_job_store", job_store)
    monkeypatch.setattr(idx_state, "_job_group_store", group_store)
    monkeypatch.setattr(idx_state, "_job_queue", queue)
    monkeypatch.setattr(idx_state, "DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(
        prompt_pins, "_view",
        prompt_pins.PinView(deployment={"chunk_summary": 3, "hyde": 1}, overrides={}),
    )
    monkeypatch.setattr(ig, "_status", ig.IndexStatus("ok"))
    return {
        "pin_store": pin_store, "source_repo": source_repo, "job_store": job_store,
        "group_store": group_store, "queue": queue,
    }


def _set_sources(env, sources: list[SourceRecord]):
    env["source_repo"].by_id = {s.id: s for s in sources}


def _set_overrides(monkeypatch, overrides: dict[str, int]):
    view = prompt_pins._view
    monkeypatch.setattr(
        prompt_pins, "_view",
        prompt_pins.PinView(deployment=dict(view.deployment), overrides=dict(overrides)),
    )


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Admin gate
# ---------------------------------------------------------------------------

class TestAdminRequired:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("put", "/prompt-pins/chunk_summary/sources/src-1"),
            ("delete", "/prompt-pins/chunk_summary/sources/src-1"),
            ("put", "/prompt-pins/hyde/sources/src-1"),
            ("post", "/sources/src-1/resummarize"),
        ],
    )
    def test_requires_admin(self, env, client, method, path):
        kwargs = {"json": {"version": 3}} if method == "put" else {}
        resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# PUT /prompt-pins/chunk_summary/sources/{source_id}
# ---------------------------------------------------------------------------

class TestOverridePut:
    def test_dry_run_then_real_only_that_source(self, env, client, monkeypatch, registry_v4):
        _grant_admin(monkeypatch)
        _set_sources(env, [
            _source("src-1", version=3, chunk_count=10),
            _source("src-2", version=3, chunk_count=20),
        ])

        dry = client.put(
            "/prompt-pins/chunk_summary/sources/src-1",
            params={"dry_run": "true"}, json={"version": 4},
        )
        assert dry.status_code == 200
        dry_body = dry.json()
        assert dry_body["dry_run"] is True
        assert dry_body["scope"] == "src-1"
        assert {i["source_id"] for i in dry_body["enqueued"]} == {"src-1"}
        assert dry_body["total_chunks"] == 10
        assert env["pin_store"].upsert_calls == []

        real = client.put("/prompt-pins/chunk_summary/sources/src-1", json={"version": 4})
        assert real.status_code == 200
        real_body = real.json()
        assert real_body["dry_run"] is False
        assert {i["source_id"] for i in real_body["enqueued"]} == {"src-1"}
        assert env["pin_store"].upsert_calls == [("chunk_summary", "src-1", 4, "admin-1")]
        assert env["queue"].enqueued == [real_body["enqueued"][0]["job_id"]]

    def test_unknown_version_is_400_with_valid_versions(self, env, client, monkeypatch, registry_v4):
        _grant_admin(monkeypatch)
        _set_sources(env, [_source("src-1", version=3)])
        resp = client.put("/prompt-pins/chunk_summary/sources/src-1", json={"version": 99})
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"] == "unknown chunk_summary version 99"
        assert sorted(body["valid_versions"]) == [3, 4]
        assert env["pin_store"].upsert_calls == []

    def test_unknown_source_is_404(self, env, client, monkeypatch, registry_v4):
        _grant_admin(monkeypatch)
        resp = client.put("/prompt-pins/chunk_summary/sources/does-not-exist", json={"version": 4})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /prompt-pins/hyde/sources/{source_id} — always refused
# ---------------------------------------------------------------------------

class TestHydeOverridePut:
    @pytest.mark.parametrize("dry_run", [False, True])
    def test_always_400(self, env, client, monkeypatch, dry_run):
        _grant_admin(monkeypatch)
        resp = client.put(
            "/prompt-pins/hyde/sources/src-1",
            params={"dry_run": str(dry_run).lower()}, json={"version": 1},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == (
            "hyde pins are deployment-wide; per-source overrides are not supported"
        )
        assert env["pin_store"].upsert_calls == []


# ---------------------------------------------------------------------------
# DELETE /prompt-pins/chunk_summary/sources/{source_id}
# ---------------------------------------------------------------------------

class TestOverrideDelete:
    def test_clear_returns_deployment_pin_as_version(self, env, client, monkeypatch, registry_v4):
        _grant_admin(monkeypatch)
        env["pin_store"].rows.append(_pin_row("chunk_summary", "src-1", 4, updated_by="alice"))
        _set_overrides(monkeypatch, {"src-1": 4})
        _set_sources(env, [_source("src-1", version=4)])

        resp = client.delete("/prompt-pins/chunk_summary/sources/src-1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == 3
        assert body["previous_version"] == 4
        assert env["pin_store"].delete_calls == [("chunk_summary", "src-1")]
        assert {i["source_id"] for i in body["enqueued"]} == {"src-1"}

    def test_no_job_when_already_at_deployment_pin(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        env["pin_store"].rows.append(_pin_row("chunk_summary", "src-1", 3, updated_by="alice"))
        _set_overrides(monkeypatch, {"src-1": 3})
        _set_sources(env, [_source("src-1", version=3)])

        resp = client.delete("/prompt-pins/chunk_summary/sources/src-1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == 3
        assert body["enqueued"] == body["deferred"] == body["not_enqueued"] == []
        assert env["queue"].enqueued == []

    def test_404_without_override(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        _set_sources(env, [_source("src-1", version=3)])
        resp = client.delete("/prompt-pins/chunk_summary/sources/src-1")
        assert resp.status_code == 404

    def test_404_unknown_source(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        resp = client.delete("/prompt-pins/chunk_summary/sources/does-not-exist")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# A deployment-pin change skips an overridden source
# ---------------------------------------------------------------------------

class TestDeploymentChangeSkipsOverriddenSource:
    def test_override_skipped_plain_source_enqueued(self, env, client, monkeypatch, registry_v4):
        _grant_admin(monkeypatch)
        _set_overrides(monkeypatch, {"src-override": 4})
        _set_sources(env, [
            _source("src-override", version=3, chunk_count=5),
            _source("src-plain", version=3, chunk_count=6),
        ])

        resp = client.put("/prompt-pins/chunk_summary", json={"version": 4})
        assert resp.status_code == 200
        body = resp.json()
        all_ids = {i["source_id"] for i in (body["enqueued"] + body["deferred"] + body["not_enqueued"])}
        assert all_ids == {"src-plain"}


# ---------------------------------------------------------------------------
# POST /sources/{source_id}/resummarize
# ---------------------------------------------------------------------------

class TestResummarize:
    def test_stale_enqueues_202(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        _set_sources(env, [_source("src-1", version=3, chunk_count=7)])
        monkeypatch.setattr(
            prompt_pins, "_view",
            prompt_pins.PinView(deployment={"chunk_summary": 4, "hyde": 1}, overrides={}),
        )

        resp = client.post("/sources/src-1/resummarize")
        assert resp.status_code == 202
        body = resp.json()
        assert body["target_version"] == 4
        assert body["job_id"] is not None
        assert env["queue"].enqueued == [body["job_id"]]

    def test_current_returns_200_already_current(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        _set_sources(env, [_source("src-1", version=3)])
        resp = client.post("/sources/src-1/resummarize")
        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] is None
        assert body["reason"] == "already current at chunk_summary v3"

    def test_active_job_returns_200_deferred(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        _set_sources(env, [_source("src-1", version=3, chunk_count=7)])
        monkeypatch.setattr(
            prompt_pins, "_view",
            prompt_pins.PinView(deployment={"chunk_summary": 4, "hyde": 1}, overrides={}),
        )
        env["job_store"].active["src-1"] = {"job_id": "job-active", "status": "running"}

        resp = client.post("/sources/src-1/resummarize")
        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == "job-active"
        assert body["deferred"] is True
        assert env["queue"].enqueued == []

    def test_not_writable_returns_gate_409(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        _set_sources(env, [_source("src-1", version=3, chunk_count=7)])
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("reindex_required", reason="schema mismatch"))

        resp = client.post("/sources/src-1/resummarize")
        assert resp.status_code == 409
        assert "schema mismatch" in resp.json()["detail"]
        assert env["queue"].enqueued == []

    def test_unknown_source_404(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        resp = client.post("/sources/does-not-exist/resummarize")
        assert resp.status_code == 404

    def test_refresh_target_set_counts_as_stale_even_at_recorded_version(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        _set_sources(env, [_source("src-1", version=3, target=3, chunk_count=9)])
        resp = client.post("/sources/src-1/resummarize")
        assert resp.status_code == 202
        body = resp.json()
        assert body["job_id"] is not None
        assert body["target_version"] == 3


# ---------------------------------------------------------------------------
# DELETE /sources/{id} deletes the override row
# ---------------------------------------------------------------------------

class TestSourceDeleteRemovesOverride:
    @pytest.mark.asyncio
    async def test_delete_source_deletes_its_override_row(self, env, monkeypatch, mocker):
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        mocker.patch.object(idx_svc, "delete_chunks_by_source")
        mocker.patch.object(idx_svc.graph_store, "delete_source", new=mocker.AsyncMock())
        mocker.patch.object(idx_svc.graph_store, "delete_source_communities", new=mocker.AsyncMock())

        env["pin_store"].rows.append(_pin_row("chunk_summary", "src-1", 4, updated_by="alice"))
        _set_sources(env, [_source("src-1", version=4)])

        result = await idx_svc.remove_source("src-1", mocker.Mock())
        assert result["status"] == "deleted"
        assert env["pin_store"].delete_overrides_calls == ["src-1"]


# ---------------------------------------------------------------------------
# FR-026 logging
# ---------------------------------------------------------------------------

class TestLoggingFR026:
    def test_override_change_logs_caller_source_versions(self, env, client, monkeypatch, registry_v4, caplog):
        _grant_admin(monkeypatch, user_id="carol")
        _set_sources(env, [_source("src-1", version=3, chunk_count=1)])
        with caplog.at_level(logging.INFO, logger="treeweft.application.prompt_pins"):
            client.put("/prompt-pins/chunk_summary/sources/src-1", json={"version": 4})

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "carol" in m and "src-1" in m and "previous_version=None" in m and "version=4" in m
            for m in messages
        )

    def test_override_clear_logs_caller_source_versions(self, env, client, monkeypatch, caplog):
        _grant_admin(monkeypatch, user_id="dave")
        env["pin_store"].rows.append(_pin_row("chunk_summary", "src-1", 4, updated_by="dave"))
        _set_overrides(monkeypatch, {"src-1": 4})
        _set_sources(env, [_source("src-1", version=4)])
        with caplog.at_level(logging.INFO, logger="treeweft.application.prompt_pins"):
            client.delete("/prompt-pins/chunk_summary/sources/src-1")

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "dave" in m and "src-1" in m and "previous_version=4" in m and "version=3" in m
            for m in messages
        )

    def test_manual_refresh_logs_caller_source_and_target_version(self, env, client, monkeypatch, caplog):
        _grant_admin(monkeypatch, user_id="erin")
        _set_sources(env, [_source("src-1", version=3, chunk_count=1)])
        monkeypatch.setattr(
            prompt_pins, "_view",
            prompt_pins.PinView(deployment={"chunk_summary": 4, "hyde": 1}, overrides={}),
        )
        with caplog.at_level(logging.INFO, logger="treeweft.application.routes_prompts"):
            client.post("/sources/src-1/resummarize")

        messages = [r.getMessage() for r in caplog.records]
        assert any("erin" in m and "src-1" in m and "target_version=4" in m for m in messages)
