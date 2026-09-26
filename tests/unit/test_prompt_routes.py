"""GET /prompt-versions and PUT /prompt-pins/{operation} (ADR-003, T024).

Fakes every store — PromptPinStore, the source repository, the job store,
job group store and queue — the way test_index_rebuild.py fakes the
rebuild's stores (no real Postgres). A test-only chunk_summary v4 is
monkeypatched into `prompts.REGISTRY`, as `test_summary_versioning.py`
does, so a pin move can be exercised without touching the real registry.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg
import pytest
from fastapi.testclient import TestClient

from treeweft.adapters.llm_api import llm_adapter, prompts
from treeweft.adapters.postgresql.prompt_pin_store import PinRow
from treeweft.application import index_guard as ig
from treeweft.application import indexer_service as idx_svc
from treeweft.application import indexer_state as idx_state
from treeweft.application import prompt_pins
from treeweft.application import prompt_refresh
from treeweft.application import routes_prompts
from treeweft.domain.sources import SourceRecord

app = idx_svc.app


# ---------------------------------------------------------------------------
# A test-only chunk_summary v4, mirroring test_summary_versioning.py
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


def _pin_row(operation: str, scope: str, version: int, updated_by: str | None = None) -> PinRow:
    return PinRow(
        operation=operation, scope=scope, version=version,
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc), updated_by=updated_by,
    )


class FakeSourceRepo:
    def __init__(self, sources: list[SourceRecord]):
        self.by_id = {s.id: s for s in sources}

    async def list_all(self):
        return list(self.by_id.values())

    async def get_by_id(self, source_id):
        return self.by_id.get(source_id)


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
        self.find_active_for_source_calls: list[str] = []
        self.find_active_for_sources_calls: list[list[str]] = []

    async def find_active_for_source(self, source_id):
        self.find_active_for_source_calls.append(source_id)
        d = self.active.get(source_id)
        return _FakeActiveJob(d) if d else None

    async def find_active_for_sources(self, source_ids):
        self.find_active_for_sources_calls.append(list(source_ids))
        return {sid: _FakeActiveJob(self.active[sid]) for sid in source_ids if sid in self.active}

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


def _set_sources(env, monkeypatch, sources: list[SourceRecord]):
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
    def test_get_prompt_versions_requires_admin(self, env, client):
        resp = client.get("/prompt-versions")
        assert resp.status_code in (401, 403)

    def test_put_prompt_pin_requires_admin(self, env, client):
        resp = client.put("/prompt-pins/chunk_summary", json={"version": 3})
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# No Postgres
# ---------------------------------------------------------------------------

class TestNoPostgres:
    def test_get_returns_503(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        monkeypatch.setattr(idx_state, "DATABASE_URL", "")
        resp = client.get("/prompt-versions")
        assert resp.status_code == 503

    def test_put_returns_503(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        monkeypatch.setattr(idx_state, "DATABASE_URL", "")
        resp = client.put("/prompt-pins/chunk_summary", json={"version": 3})
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Authoritative reads: set_pin must reload before deciding no-op/plan, so a
# stale local view can't hide a change another process already made.
# ---------------------------------------------------------------------------

class TestAuthoritativeReadOnSetPin:
    def test_set_pin_reload_prevents_wrong_no_op(self, env, client, monkeypatch, registry_v4):
        """Postgres already holds v3 (say a peer process reverted it), but
        this process's local view is stale and still thinks it's at v4. A
        request to move to v4 must not be treated as a no-op: the real
        current value (v3) differs from the target."""
        _grant_admin(monkeypatch)
        env["pin_store"].rows = [_pin_row("chunk_summary", "deployment", 3), _pin_row("hyde", "deployment", 1)]
        monkeypatch.setattr(
            prompt_pins, "_view",
            prompt_pins.PinView(deployment={"chunk_summary": 4, "hyde": 1}, overrides={}),
        )
        _set_sources(env, monkeypatch, [_source("src-1", version=3, chunk_count=7)])

        resp = client.put("/prompt-pins/chunk_summary", json={"version": 4})
        assert resp.status_code == 200
        body = resp.json()
        assert body["previous_version"] == 3
        assert {i["source_id"] for i in body["enqueued"]} == {"src-1"}
        assert env["pin_store"].upsert_calls == [("chunk_summary", "deployment", 4, "admin-1")]
        assert env["queue"].enqueued != []


# ---------------------------------------------------------------------------
# routes_prompts.py must not report an unrelated RuntimeError as the
# "Postgres not configured" 503 (finding #3).
# ---------------------------------------------------------------------------

class TestUnrelatedRuntimeErrorIsNot503:
    def test_enqueue_runtime_error_is_500_not_503_and_leaks_nothing(
        self, env, client, monkeypatch, registry_v4
    ):
        _grant_admin(monkeypatch)
        _set_sources(env, monkeypatch, [_source("src-1", version=3, chunk_count=1)])

        async def _boom(plan, *, created_by):
            raise RuntimeError("boom unrelated to postgres config")

        monkeypatch.setattr(prompt_refresh, "enqueue_refreshes", _boom)

        resp = client.put("/prompt-pins/chunk_summary", json={"version": 4})
        assert resp.status_code == 500
        assert resp.status_code != 503
        assert routes_prompts._NO_POSTGRES_DETAIL not in resp.text
        assert "boom unrelated to postgres config" not in resp.text


# ---------------------------------------------------------------------------
# GET /prompt-versions
# ---------------------------------------------------------------------------

class TestGetPromptVersions:
    def test_registry_pins_and_sources(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        _set_sources(env, monkeypatch, [
            _source("src-current", version=3),
            _source("src-stale", version=None, target=None),
        ])
        env["job_store"].active["src-current"] = {"job_id": "job-active", "status": "running"}

        resp = client.get("/prompt-versions")
        assert resp.status_code == 200
        body = resp.json()

        assert body["operations"]["chunk_summary"]["latest"] == 3
        assert body["operations"]["chunk_summary"]["deployment_pin"]["version"] == 3
        assert body["operations"]["hyde"]["deployment_pin"]["version"] == 1
        assert body["operations"]["chunk_summary"]["versions"] == [
            {"version": 3, "notes": prompts.REGISTRY["chunk_summary"][3].notes}
        ]

        sources_by_id = {s["source_id"]: s for s in body["sources"]}
        current = sources_by_id["src-current"]
        assert current["chunk_count"] == 100
        assert current["summary_prompt_version"] == 3
        assert current["effective_version"] == 3
        assert current["stale"] is False
        assert current["override"] is None
        assert current["active_job_id"] == "job-active"

        stale = sources_by_id["src-stale"]
        assert stale["summary_prompt_version"] is None
        assert stale["stale"] is False  # never summarized, no target -> not stale
        assert stale["active_job_id"] is None

    def test_source_override_is_reported(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        env["pin_store"].rows.append(_pin_row("chunk_summary", "src-1", 4, updated_by="alice"))
        _set_overrides(monkeypatch, {"src-1": 4})
        _set_sources(env, monkeypatch, [_source("src-1", version=4)])

        resp = client.get("/prompt-versions")
        body = resp.json()
        override = body["sources"][0]["override"]
        assert override == {"version": 4, "updated_at": override["updated_at"], "updated_by": "alice"}

    def test_active_jobs_are_looked_up_in_one_bulk_call(self, env, client, monkeypatch):
        """Finding #4: one query for active jobs across all sources, not one
        `find_active_for_source` call per source (N+1)."""
        _grant_admin(monkeypatch)
        _set_sources(env, monkeypatch, [
            _source("src-a", version=3),
            _source("src-b", version=3),
            _source("src-c", version=3),
        ])
        env["job_store"].active["src-b"] = {"job_id": "job-active", "status": "running"}

        resp = client.get("/prompt-versions")
        assert resp.status_code == 200
        body = resp.json()

        assert env["job_store"].find_active_for_source_calls == []
        assert len(env["job_store"].find_active_for_sources_calls) == 1
        assert set(env["job_store"].find_active_for_sources_calls[0]) == {"src-a", "src-b", "src-c"}

        by_id = {s["source_id"]: s for s in body["sources"]}
        assert by_id["src-b"]["active_job_id"] == "job-active"
        assert by_id["src-a"]["active_job_id"] is None

    def test_effective_version_and_stale_use_the_freshly_read_pins_not_the_local_view(
        self, env, client, monkeypatch
    ):
        """Finding #4: stale/effective_version must reflect the pins this
        request just read from Postgres, not the process-local view, which
        can lag another process's write."""
        _grant_admin(monkeypatch)
        # Postgres already holds chunk_summary=4 (a peer process moved it).
        env["pin_store"].rows = [_pin_row("chunk_summary", "deployment", 4), _pin_row("hyde", "deployment", 1)]
        # This process's local view is stale and still thinks it's at 3.
        monkeypatch.setattr(
            prompt_pins, "_view",
            prompt_pins.PinView(deployment={"chunk_summary": 3, "hyde": 1}, overrides={}),
        )
        _set_sources(env, monkeypatch, [_source("src-1", version=3)])

        resp = client.get("/prompt-versions")
        assert resp.status_code == 200
        body = resp.json()

        source = body["sources"][0]
        assert source["effective_version"] == 4
        assert source["stale"] is True


# ---------------------------------------------------------------------------
# Unknown operation / version
# ---------------------------------------------------------------------------

class TestUnknownOperationAndVersion:
    def test_unknown_operation_is_404(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        resp = client.put("/prompt-pins/not-a-real-op", json={"version": 1})
        assert resp.status_code == 404

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_unknown_version_is_400_with_valid_versions(self, env, client, monkeypatch, registry_v4, dry_run):
        _grant_admin(monkeypatch)
        resp = client.put(
            "/prompt-pins/chunk_summary", params={"dry_run": str(dry_run).lower()}, json={"version": 99}
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"] == "unknown chunk_summary version 99"
        assert sorted(body["valid_versions"]) == [3, 4]
        assert env["pin_store"].upsert_calls == []


# ---------------------------------------------------------------------------
# No-op
# ---------------------------------------------------------------------------

class TestNoOpPin:
    def test_setting_current_version_writes_nothing(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        resp = client.put("/prompt-pins/chunk_summary", json={"version": 3})
        assert resp.status_code == 200
        body = resp.json()
        assert body["enqueued"] == body["deferred"] == body["not_enqueued"] == []
        assert body["group_id"] is None
        assert body["total_chunks"] == 0
        assert env["pin_store"].upsert_calls == []
        assert env["job_store"].upserted == []
        assert env["queue"].enqueued == []


# ---------------------------------------------------------------------------
# Dry run vs real — the table from spec US2 scenario 6 / FR-015 / FR-017
# ---------------------------------------------------------------------------

class TestDryRunMatchesReal:
    def _put(self, client, version, dry_run):
        return client.put(
            "/prompt-pins/chunk_summary", params={"dry_run": str(dry_run).lower()}, json={"version": version}
        )

    def test_overridden_source_is_skipped(self, env, client, monkeypatch, registry_v4):
        _grant_admin(monkeypatch)
        env["pin_store"].rows.append(_pin_row("chunk_summary", "src-override", 3))
        _set_overrides(monkeypatch, {"src-override": 3})
        _set_sources(env, monkeypatch, [_source("src-override", version=3)])

        dry = self._put(client, 4, True).json()
        real = self._put(client, 4, False).json()
        for body in (dry, real):
            assert body["enqueued"] == body["deferred"] == body["not_enqueued"] == []

    def test_already_current_source_is_excluded(self, env, client, monkeypatch, registry_v4):
        _grant_admin(monkeypatch)
        _set_sources(env, monkeypatch, [_source("src-current", version=4)])

        dry = self._put(client, 4, True).json()
        assert dry["enqueued"] == dry["deferred"] == dry["not_enqueued"] == []

    def test_active_job_source_is_deferred(self, env, client, monkeypatch, registry_v4):
        _grant_admin(monkeypatch)
        _set_sources(env, monkeypatch, [_source("src-active", version=3, chunk_count=912)])
        env["job_store"].active["src-active"] = {"job_id": "job-blocking", "status": "running"}

        dry = self._put(client, 4, True).json()
        real = self._put(client, 4, False).json()
        for body in (dry, real):
            assert body["enqueued"] == body["not_enqueued"] == []
            assert len(body["deferred"]) == 1
            item = body["deferred"][0]
            assert item["source_id"] == "src-active"
            assert item["blocking_job_id"] == "job-blocking"
            assert item["current_version"] == 3
            assert item["target_version"] == 4
            assert item["chunk_count"] == 912
        assert env["job_store"].upserted == []
        assert env["queue"].enqueued == []

    def test_pin_back_after_errors_is_enqueued(self, env, client, monkeypatch, registry_v4):
        """FR-015 / spec US2 scenario 6: a refresh toward v4 ended with
        errors (target still set, recorded still 3) while the pin moves
        back to 3 — the source is stale (target set) and lists in enqueued
        even though current_version == target_version == 3."""
        _grant_admin(monkeypatch)
        env["pin_store"].rows = [_pin_row("chunk_summary", "deployment", 4), _pin_row("hyde", "deployment", 1)]
        monkeypatch.setattr(
            prompt_pins, "_view",
            prompt_pins.PinView(deployment={"chunk_summary": 4, "hyde": 1}, overrides={}),
        )
        _set_sources(env, monkeypatch, [_source("src-pinback", version=3, target=4, chunk_count=40)])

        dry = self._put(client, 3, True).json()
        real = self._put(client, 3, False).json()

        assert dry["deferred"] == dry["not_enqueued"] == []
        assert len(dry["enqueued"]) == 1
        assert dry["enqueued"][0]["job_id"] is None
        assert dry["enqueued"][0]["current_version"] == 3
        assert dry["enqueued"][0]["target_version"] == 3

        assert real["deferred"] == real["not_enqueued"] == []
        assert len(real["enqueued"]) == 1
        assert real["enqueued"][0]["job_id"] is not None
        assert real["enqueued"][0]["source_id"] == "src-pinback"
        assert env["queue"].enqueued == [real["enqueued"][0]["job_id"]]

    def test_reindex_required_every_stale_source_is_not_enqueued(self, env, client, monkeypatch, registry_v4):
        _grant_admin(monkeypatch)
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("reindex_required", reason="schema mismatch"))
        _set_sources(env, monkeypatch, [
            _source("src-a", version=3, chunk_count=40),
            _source("src-b", version=3, chunk_count=10),
        ])

        dry = self._put(client, 4, True).json()
        real = self._put(client, 4, False).json()

        for body in (dry, real):
            assert body["enqueued"] == body["deferred"] == []
            assert {i["source_id"] for i in body["not_enqueued"]} == {"src-a", "src-b"}
            for item in body["not_enqueued"]:
                assert "reindex_required" in item["reason"]

        # The pin is still stored even though nothing could be enqueued.
        assert env["pin_store"].upsert_calls == [("chunk_summary", "deployment", 4, "admin-1")]
        assert env["job_store"].upserted == []

    def test_dry_run_writes_nothing_real_call_creates_matching_jobs(self, env, client, monkeypatch, registry_v4):
        _grant_admin(monkeypatch, user_id="pinner")
        _set_sources(env, monkeypatch, [
            _source("src-1", version=3, chunk_count=10),
            _source("src-2", version=3, chunk_count=20),
        ])

        dry_resp = self._put(client, 4, True)
        assert dry_resp.status_code == 200
        dry = dry_resp.json()
        assert dry["dry_run"] is True
        assert {i["source_id"] for i in dry["enqueued"]} == {"src-1", "src-2"}
        assert all(i["job_id"] is None for i in dry["enqueued"])
        assert dry["group_id"] is None
        assert dry["total_chunks"] == 30

        # Nothing written by the dry run.
        assert env["pin_store"].upsert_calls == []
        assert env["job_store"].upserted == []
        assert env["queue"].enqueued == []
        assert env["group_store"].created == []

        real_resp = self._put(client, 4, False)
        assert real_resp.status_code == 200
        real = real_resp.json()
        assert real["dry_run"] is False
        assert real["previous_version"] == 3
        assert real["version"] == 4
        assert real["total_chunks"] == 30
        assert {i["source_id"] for i in real["enqueued"]} == {"src-1", "src-2"}
        assert all(i["job_id"] for i in real["enqueued"])

        assert env["pin_store"].upsert_calls == [("chunk_summary", "deployment", 4, "pinner")]
        assert sorted(env["queue"].enqueued) == sorted(i["job_id"] for i in real["enqueued"])


# ---------------------------------------------------------------------------
# Race: jobs_active_source_uniq
# ---------------------------------------------------------------------------

class TestRaceOnJobsActiveSourceUniq:
    def test_unique_violation_moves_the_item_to_deferred(self, env, client, monkeypatch, registry_v4):
        _grant_admin(monkeypatch)
        _set_sources(env, monkeypatch, [_source("src-race", version=3, chunk_count=5)])
        env["job_store"].boom_for.add("src-race")

        resp = client.put("/prompt-pins/chunk_summary", json={"version": 4})
        assert resp.status_code == 200
        body = resp.json()
        assert body["enqueued"] == []
        assert len(body["deferred"]) == 1
        assert body["deferred"][0]["source_id"] == "src-race"
        assert env["queue"].enqueued == []


# ---------------------------------------------------------------------------
# Job group
# ---------------------------------------------------------------------------

class TestJobGroup:
    def test_two_or_more_jobs_share_one_prompt_refresh_group(self, env, client, monkeypatch, registry_v4):
        _grant_admin(monkeypatch)
        _set_sources(env, monkeypatch, [
            _source("src-1", version=3, chunk_count=1),
            _source("src-2", version=3, chunk_count=1),
        ])
        resp = client.put("/prompt-pins/chunk_summary", json={"version": 4})
        body = resp.json()
        assert body["group_id"] is not None
        assert len(env["group_store"].created) == 1
        assert env["group_store"].created[0]["kind"] == "prompt-refresh"
        assert env["group_store"].created[0]["task_count"] == 2

    def test_single_job_gets_no_deployment_group(self, env, client, monkeypatch, registry_v4):
        _grant_admin(monkeypatch)
        _set_sources(env, monkeypatch, [_source("src-1", version=3, chunk_count=1)])
        resp = client.put("/prompt-pins/chunk_summary", json={"version": 4})
        body = resp.json()
        assert body["group_id"] is None


# ---------------------------------------------------------------------------
# updated_by
# ---------------------------------------------------------------------------

class TestUpdatedBy:
    def test_updated_by_is_the_caller(self, env, client, monkeypatch, registry_v4):
        _grant_admin(monkeypatch, user_id="bob")
        _set_sources(env, monkeypatch, [_source("src-1", version=3)])
        client.put("/prompt-pins/chunk_summary", json={"version": 4})
        assert env["pin_store"].upsert_calls == [("chunk_summary", "deployment", 4, "bob")]


# ---------------------------------------------------------------------------
# FR-026 logging
# ---------------------------------------------------------------------------

class TestPinChangeLogging:
    def test_real_change_logs_caller_operation_scope_versions_and_ids(
        self, env, client, monkeypatch, registry_v4, caplog
    ):
        _grant_admin(monkeypatch, user_id="carol")
        _set_sources(env, monkeypatch, [_source("src-1", version=3, chunk_count=1)])
        with caplog.at_level(logging.INFO, logger="treeweft.application.prompt_pins"):
            client.put("/prompt-pins/chunk_summary", json={"version": 4})

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "carol" in m and "chunk_summary" in m and "deployment" in m
            and "previous_version=3" in m and "version=4" in m and "src-1" in m
            for m in messages
        )

    def test_dry_run_logs_that_it_was_a_dry_run(self, env, client, monkeypatch, registry_v4, caplog):
        _grant_admin(monkeypatch, user_id="carol")
        _set_sources(env, monkeypatch, [_source("src-1", version=3, chunk_count=1)])
        with caplog.at_level(logging.INFO, logger="treeweft.application.prompt_pins"):
            client.put("/prompt-pins/chunk_summary", params={"dry_run": "true"}, json={"version": 4})

        messages = [r.getMessage() for r in caplog.records]
        assert any("dry_run=True" in m for m in messages)


# ---------------------------------------------------------------------------
# HyDE
# ---------------------------------------------------------------------------

class TestHydePin:
    def test_hyde_change_enqueues_nothing_returns_effect_and_clears_cache(self, env, client, monkeypatch):
        _grant_admin(monkeypatch)
        llm_adapter._HYDE_CACHE["some-key"] = "cached hypothetical"

        registry = dict(prompts.REGISTRY)
        registry["hyde"] = {**registry["hyde"], 2: prompts.PromptVersion(
            operation="hyde", version=2, system="v2 test hyde prompt.",
            schema=prompts.FrozenResponseSchema(min_length=1, max_length=8000, forbidden_phrases=()),
            notes="test hyde v2",
        )}
        monkeypatch.setattr(prompts, "REGISTRY", registry)

        resp = client.put("/prompt-pins/hyde", json={"version": 2})
        assert resp.status_code == 200
        body = resp.json()
        assert body["enqueued"] == body["deferred"] == body["not_enqueued"] == []
        assert body["effect"] == "takes effect on the next query"
        assert body["group_id"] is None
        assert llm_adapter._HYDE_CACHE == {}


# ---------------------------------------------------------------------------
# GET /sources additive fields
# ---------------------------------------------------------------------------

class TestSourcesEndpointGainsSummaryFields:
    def test_sources_includes_summary_fields(self, env, client, monkeypatch):
        monkeypatch.setattr(idx_svc, "_state", idx_state)  # no-op; keeps import explicit
        _set_sources(env, monkeypatch, [_source("src-1", version=3, target=None)])
        resp = client.get("/sources")
        assert resp.status_code == 200
        body = resp.json()
        row = next(r for r in body if r["id"] == "src-1")
        assert row["summary_prompt_version"] == 3
        assert row["summary_refresh_target"] is None
        assert row["summary_stale"] is False
