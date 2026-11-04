"""Global, per-user HTTP MCP catalog. Builtin deletion disables, not hides."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from app.services.mcp import catalog
from app.services.mcp.registry import list_connectors, refresh_one

router = APIRouter(prefix="/api/mcp")


class ServerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr | None = None
    url: StrictStr | None = None
    url_template: StrictStr | None = None
    tool_prefix: StrictStr | None = None
    enabled: StrictBool | None = None
    token: StrictStr | None = None


class BindingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool | None = None
    token: StrictStr | None = None


def _patch(body: BaseModel) -> dict:
    values = body.model_dump(exclude_unset=True)
    # Only URL fields are nullable (null is the same as clearing the URL).
    if any(v is None and k not in {"url", "url_template"} for k, v in values.items()):
        raise HTTPException(status_code=422, detail="Only URL fields may be null")
    return values


def _mutate(fn, *args) -> dict:
    try:
        fn(*args)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return catalog.wire_catalog()


@router.get("")
async def get_catalog() -> dict:
    return catalog.wire_catalog()


@router.put("/servers/{server_id}")
async def put_server(server_id: str, patch: ServerPatch) -> dict:
    return _mutate(catalog.put_server, server_id, _patch(patch))


@router.delete("/servers/{server_id}")
async def delete_server(server_id: str) -> dict:
    return _mutate(catalog.delete_server, server_id)


@router.put("/servers/{server_id}/plugins/{plugin}")
async def put_binding(server_id: str, plugin: str, patch: BindingPatch) -> dict:
    return _mutate(catalog.put_binding, server_id, plugin, _patch(patch))


@router.post("/servers/{server_id}/refresh")
async def refresh_server(server_id: str) -> dict:
    if server_id not in catalog.servers():
        raise HTTPException(status_code=404, detail="Unknown MCP server")
    for conn in list_connectors():
        if conn.decl.catalog_id == server_id:
            await refresh_one(conn)
    return catalog.wire_catalog()
