"""The prompt-version and deployment-pin admin API (ADR-003, research R11).

`GET /prompt-versions` (read) and `PUT /prompt-pins/{operation}` (the
deployment pin, US2). Per-source overrides and the manual refresh endpoint
are added in US3.

On an APIRouter that `indexer_service` includes, registered next to
`routes_auth` and `routes_webhook` (`indexer_service.py:~160`). Nothing here
imports `indexer_service` — `application/prompt_pins.py`'s module docstring
explains why (`indexer_runners` must never import `indexer_service` back;
the same one-way rule applies to every router module).

The read endpoint deliberately lives at `/prompt-versions`, not under
`/sources`: GET requests under `/sources` skip the auth middleware
(`indexer_service.py:157`), which would make `authz._require_admin` see no
`request.state.user` and answer 401 for a genuine admin.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse

from treeweft.adapters.llm_api import prompts
from treeweft.application import index_guard
from treeweft.application import indexer_authz as authz
from treeweft.application import indexer_state as _state
from treeweft.application import prompt_pins
from treeweft.application import prompt_refresh
from treeweft.application import routes_webhook as _routes_webhook
from treeweft.domain.prompt_pins import is_stale

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_POSTGRES_DETAIL = "prompt pins need Postgres (DATABASE_URL)"


def _caller_id(request: Request) -> str | None:
    user = getattr(request.state, "user", None)
    return user.id if user is not None else None


def _no_postgres() -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": _NO_POSTGRES_DETAIL})


class PinRequest(BaseModel):
    version: int


@router.get("/prompt-versions")
async def get_prompt_versions(request: Request):
    authz._require_admin(request)
    if not _state.DATABASE_URL:
        return _no_postgres()

    try:
        rows = await prompt_pins._pin_store.list_all()
    except RuntimeError:
        return _no_postgres()

    deployment_pins: dict[str, dict] = {}
    override_pins: dict[str, dict[str, dict]] = {}
    for row in rows:
        row_dict = row.to_dict()
        if row.scope == "deployment":
            deployment_pins[row.operation] = row_dict
        else:
            override_pins.setdefault(row.operation, {})[row.scope] = row_dict

    operations: dict[str, dict] = {}
    for op, registered in prompts.REGISTRY.items():
        pin_row = deployment_pins.get(op)
        operations[op] = {
            "versions": [
                {"version": pv.version, "notes": pv.notes}
                for pv in sorted(registered.values(), key=lambda pv: pv.version)
            ],
            "latest": prompts.latest(op).version,
            "deployment_pin": (
                {
                    "version": pin_row["version"],
                    "updated_at": pin_row["updated_at"],
                    "updated_by": pin_row["updated_by"],
                }
                if pin_row is not None
                else None
            ),
        }

    sources_out = []
    for source in await _state._source_repo.list_all():
        override_row = override_pins.get("chunk_summary", {}).get(source.id)
        effective_version = prompt_pins.effective("chunk_summary", source.id)
        stale = is_stale(
            source.summary_prompt_version, source.summary_refresh_target, effective_version
        )
        active = await _routes_webhook._find_active_job_for_source(source.id)
        sources_out.append(
            {
                "source_id": source.id,
                "label": source.url or source.path or source.id,
                "chunk_count": source.chunk_count,
                "summary_prompt_version": source.summary_prompt_version,
                "summary_refresh_target": source.summary_refresh_target,
                "override": (
                    {
                        "version": override_row["version"],
                        "updated_at": override_row["updated_at"],
                        "updated_by": override_row["updated_by"],
                    }
                    if override_row is not None
                    else None
                ),
                "effective_version": effective_version,
                "stale": stale,
                "active_job_id": active["job_id"] if active else None,
            }
        )

    return {"operations": operations, "sources": sources_out}


@router.put("/prompt-pins/{operation}")
async def put_prompt_pin(operation: str, req: PinRequest, request: Request, dry_run: bool = False):
    authz._require_admin(request)
    if not _state.DATABASE_URL:
        return _no_postgres()
    if operation not in prompts.REGISTRY:
        raise HTTPException(404, f"unknown operation: {operation}")

    try:
        result = await prompt_pins.set_pin(
            operation, "deployment", req.version,
            updated_by=_caller_id(request), dry_run=dry_run,
        )
    except prompt_pins.UnknownPromptVersionError as exc:
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc), "valid_versions": list(exc.valid_versions)},
        )
    except RuntimeError:
        return _no_postgres()

    return result


@router.put("/prompt-pins/chunk_summary/sources/{source_id}")
async def put_prompt_pin_override(
    source_id: str, req: PinRequest, request: Request, dry_run: bool = False
):
    authz._require_admin(request)
    if not _state.DATABASE_URL:
        return _no_postgres()

    try:
        result = await prompt_pins.set_pin(
            "chunk_summary", source_id, req.version,
            updated_by=_caller_id(request), dry_run=dry_run,
        )
    except prompt_pins.UnknownPromptVersionError as exc:
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc), "valid_versions": list(exc.valid_versions)},
        )
    except prompt_pins.UnknownSourceError:
        raise HTTPException(404, f"unknown source: {source_id}")
    except RuntimeError:
        return _no_postgres()

    return result


@router.put("/prompt-pins/hyde/sources/{source_id}")
async def put_hyde_pin_override(source_id: str, request: Request, dry_run: bool = False):
    """Always refused: HyDE has no per-source overrides (US3 scenario 4)."""
    authz._require_admin(request)
    if not _state.DATABASE_URL:
        return _no_postgres()

    return JSONResponse(
        status_code=400, content={"detail": prompt_pins.HYDE_OVERRIDE_DETAIL}
    )


@router.delete("/prompt-pins/chunk_summary/sources/{source_id}")
async def delete_prompt_pin_override(source_id: str, request: Request, dry_run: bool = False):
    authz._require_admin(request)
    if not _state.DATABASE_URL:
        return _no_postgres()

    try:
        result = await prompt_pins.clear_override(
            source_id, updated_by=_caller_id(request), dry_run=dry_run,
        )
    except (prompt_pins.UnknownSourceError, prompt_pins.NoOverrideError):
        raise HTTPException(404, f"no chunk_summary override for source: {source_id}")
    except RuntimeError:
        return _no_postgres()

    return result


def _log_manual_refresh(
    caller: str | None, source_id: str, previous_version, target_version: int, outcome: str
) -> None:
    logger.warning(
        "event=prompt_manual_refresh caller=%s source_id=%s previous_version=%s "
        "target_version=%s outcome=%s",
        caller, source_id, previous_version, target_version, outcome,
    )


@router.post("/sources/{source_id}/resummarize")
async def resummarize_source(source_id: str, request: Request):
    """A manual, single-source refresh (US3 scenario 5, FR-025).

    Routed as POST specifically so the `/sources` GET auth-skip
    (`indexer_service.py:157`) does not apply to it.
    """
    authz._require_admin(request)
    if not _state.DATABASE_URL:
        return _no_postgres()

    source = await _state._source_repo.get_by_id(source_id)
    if source is None:
        raise HTTPException(404, f"unknown source: {source_id}")

    gate = await index_guard.require_writable()
    if gate is not None:
        return gate

    caller = _caller_id(request)
    target = prompt_pins.effective("chunk_summary", source_id)

    plan = await prompt_refresh.plan_refreshes([source], lambda _sid: target)

    if not plan.enqueued and not plan.deferred:
        result = {"job_id": None, "reason": f"already current at chunk_summary v{target}"}
        _log_manual_refresh(
            caller, source_id, source.summary_prompt_version, target, "already_current"
        )
        return result

    if plan.deferred:
        item = plan.deferred[0]
        result = {"job_id": item.blocking_job_id, "deferred": True}
        _log_manual_refresh(caller, source_id, item.current_version, target, "deferred")
        return result

    plan = await prompt_refresh.enqueue_refreshes(plan, created_by=caller)

    if plan.deferred:
        item = plan.deferred[0]
        result = {"job_id": item.blocking_job_id, "deferred": True}
        _log_manual_refresh(caller, source_id, item.current_version, target, "deferred")
        return result

    item = plan.enqueued[0]
    result = {"job_id": item.job_id, "target_version": target}
    _log_manual_refresh(caller, source_id, item.current_version, target, "enqueued")
    return JSONResponse(status_code=202, content=result)
