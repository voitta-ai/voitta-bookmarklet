"""``/api/eval/v1`` routes. Mounted only when the eval API is enabled."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel

from app.eval import runner
from app.eval.config import tenant_for_token
from app.eval.trace import read_events, read_status

router = APIRouter(prefix="/api/eval/v1")


def _tenant(authorization: str = Header(default="")) -> str:
    scheme, _, token = authorization.partition(" ")
    tenant = tenant_for_token(token) if scheme.lower() == "bearer" and token else None
    if tenant is None:
        raise HTTPException(status_code=401, detail="bearer token required")
    return tenant


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except runner.EvalError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


class SessionIn(BaseModel):
    provider: str | None = None
    model: str | None = None


class TurnIn(BaseModel):
    input: str
    probe_id: str
    idempotency_key: str
    parent_run_id: str | None = None
    timeout_s: int | None = None


@router.get("/capabilities")
async def capabilities(tenant: str = Depends(_tenant)) -> dict[str, Any]:
    return runner.capabilities()


@router.post("/sessions")
async def create_session(body: SessionIn, tenant: str = Depends(_tenant)) -> dict[str, Any]:
    session = _call(runner.create_session, tenant, body.provider, body.model)
    return {"session_id": session.id, "config_digest": session.config_digest,
            "config": session.config}


@router.post("/sessions/{session_id}/turns")
async def create_turn(session_id: str, body: TurnIn,
                      tenant: str = Depends(_tenant)) -> dict[str, Any]:
    session = _call(runner.get_session, tenant, session_id)
    try:
        run = await runner.start_turn(
            session, input_text=body.input, probe_id=body.probe_id,
            idempotency_key=body.idempotency_key, parent_run_id=body.parent_run_id,
            timeout_s=body.timeout_s,
        )
    except runner.EvalError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"run_id": run.id, "status": run.trace.status["status"]}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, tenant: str = Depends(_tenant)) -> dict[str, Any]:
    run = runner.get_run(tenant, run_id)
    status = run.trace.status if run else read_status(runner.runs_dir_for(tenant), run_id)
    if status is None:
        raise HTTPException(status_code=404, detail="no such run")
    return status


@router.get("/runs/{run_id}/events")
async def get_events(run_id: str, after: int = 0,
                     tenant: str = Depends(_tenant)) -> Response:
    runs_dir = runner.runs_dir_for(tenant)
    if runner.get_run(tenant, run_id) is None and read_status(runs_dir, run_id) is None:
        raise HTTPException(status_code=404, detail="no such run")
    body = "".join(json.dumps(e, ensure_ascii=False) + "\n"
                   for e in read_events(runs_dir, run_id, after))
    return Response(content=body, media_type="application/x-ndjson")


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, tenant: str = Depends(_tenant)) -> dict[str, Any]:
    _call(runner.close_session, tenant, session_id)
    return {"closed": session_id}
