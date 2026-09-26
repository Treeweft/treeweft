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

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse

from treeweft.adapters.llm_api import prompts
from treeweft.application import indexer_authz as authz
from treeweft.application import indexer_state as _state
from treeweft.application import prompt_pins
from treeweft.application import routes_webhook as _routes_webhook
from treeweft.domain.prompt_pins import is_stale

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
