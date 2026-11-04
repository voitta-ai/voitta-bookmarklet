"""Catalog routes, scoped dispatch, legacy credentials and revocation."""
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from mcp.types import Tool

from app import plugins, settings
from app.routes.mcp_catalog import router
from app.routes.plugins import router as plugins_router
from app.routes.settings import router as settings_router
from app.services import current_user, user_settings
from app.services.mcp import catalog, registry as mcp
from app.tools.registry import ToolCtx, ToolSpec, registry

SID = "voitta-enterprise--vre"


@pytest.fixture
def env(monkeypatch):
    blobs = {}
    marker = current_user.set_current_email(None)
    monkeypatch.setattr(user_settings, "read", lambda: deepcopy(blobs.get(catalog.identity(), {})))
    monkeypatch.setattr(user_settings, "write", lambda blob: blobs.__setitem__(catalog.identity(), deepcopy(blob)))
    monkeypatch.setattr(catalog, "_STATES", {})
    monkeypatch.setattr(catalog, "_TOOLS", {})
    monkeypatch.setattr(mcp, "MCP_CONNECTORS", {})
    monkeypatch.setattr(registry, "_tools", {})
    monkeypatch.setattr(plugins, "_PLUGINS", [
        SimpleNamespace(name="voitta-enterprise", host_patterns=("enterprise.example",)),
        SimpleNamespace(name="hedgehog", host_patterns=("hedgehog.example",)),
    ])
    monkeypatch.setattr(settings, "_codex_signed_in", lambda: False)
    monkeypatch.setattr(settings, "_agent_sdk_status", lambda: {})
    mcp.register_connector(mcp.MCPServerDecl(
        plugin_name="voitta-enterprise", id="vre", url_setting=None,
        token_setting="plugins.voitta-enterprise.mcp.api_key",
        token_map_setting="plugins.voitta-enterprise.mcp.api_keys",
        tool_prefix="vre_", expose_tools=None, host_patterns=["enterprise.example"],
        url_template="{host}/mcp",
    ))
    listed, called = [], []

    async def list_tools(url, token):
        listed.append((catalog.identity(), url, token))
        return [Tool(name="search", description="Search", inputSchema={"type": "object"})]

    async def call_tool(url, token, name, args):
        called.append((catalog.identity(), url, token, name))
        return SimpleNamespace(content=[], is_error=False, structured_content=None)

    monkeypatch.setattr(mcp.mcp_client, "list_tools", list_tools)
    monkeypatch.setattr(mcp.mcp_client, "call_tool", call_tool)
    app = FastAPI()
    app.include_router(router)
    app.include_router(plugins_router)
    app.include_router(settings_router)
    yield SimpleNamespace(blobs=blobs, listed=listed, called=called, app=app)
    current_user.reset_current_email(marker)


@pytest.fixture
async def client(env):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://test") as c:
        yield c


def legacy(env):
    env.blobs[catalog.identity()] = {"plugins": {"voitta-enterprise": {"mcp": {
        "api_key": "old-fallback", "api_keys": {"enterprise.example": "old-host", "second.example": "second-key"},
    }}}}


def conn(plugin="voitta-enterprise"):
    return mcp.list_connectors(plugin)[0]


async def test_builtin_catalog_and_original_names(env, client):
    legacy(env)
    row = (await client.get("/api/mcp")).json()["servers"][0]
    assert row["id"] == SID and row["builtin"]
    assert row["has_token"] is False and row["has_legacy_token"] is True
    assert row["bindings"] == [{"plugin": "voitta-enterprise", "enabled": True, "has_token": False}]
    response = await client.post(f"/api/mcp/servers/{SID}/refresh")
    assert response.status_code == 200
    assert response.json()["servers"][0]["connectors"][0]["tool_names"] == ["vre_search"]
    assert env.listed == [(None, "https://enterprise.example/mcp", "old-host")]
    assert "vre_search" not in registry._tools
    assert registry.get("vre_search") is not None


async def test_token_precedence_preserve_and_clear(env, client):
    legacy(env)
    d = conn().decl
    assert mcp.token_for(d, "https://enterprise.example/mcp") == "old-host"
    assert mcp.token_for(d, "https://other.example/mcp") == "old-fallback"
    root = f"/api/mcp/servers/{SID}"
    assert (await client.put(root, json={"token": "server-secret"})).status_code == 200
    assert mcp.token_for(conn().decl, "enterprise.example") == "server-secret"
    await client.put(root, json={"name": "Renamed"})
    assert mcp.token_for(conn().decl, "enterprise.example") == "server-secret"
    binding = root + "/plugins/voitta-enterprise"
    await client.put(binding, json={"enabled": True, "token": "override-secret"})
    await client.put(binding, json={"enabled": True})
    assert mcp.token_for(conn().decl, "enterprise.example") == "override-secret"
    await client.put(binding, json={"enabled": True, "token": ""})
    assert mcp.token_for(conn().decl, "enterprise.example") == "server-secret"
    await client.put(root, json={"token": ""})
    assert mcp.token_for(conn().decl, "enterprise.example") is None
    assert env.blobs[None]["plugins"]["voitta-enterprise"]["mcp"]["api_key"] == "old-fallback"


async def test_bind_to_hedgehog_is_host_gated_and_no_legacy_token_leak(env, client):
    legacy(env)
    root = f"/api/mcp/servers/{SID}"
    await client.put(root + "/plugins/hedgehog", json={"enabled": True})
    await client.post(root + "/refresh")
    hedge = conn("hedgehog")
    assert hedge.tool_names[0].startswith("vre_hedgehog_")
    assert (None, "https://hedgehog.example/mcp", None) in env.listed
    spec = registry.get(hedge.tool_names[0])
    assert spec not in registry.visible_for_host("enterprise.example")
    assert spec in registry.visible_for_host("hedgehog.example")
    assert (await spec.handler({}, ToolCtx(host="evil.example")))["error"] == "mcp_disabled"
    await spec.handler({}, ToolCtx(host="hedgehog.example"))
    assert env.called[-1][1] == "https://hedgehog.example/mcp"
    await client.put(root + "/plugins/hedgehog", json={"enabled": False})
    assert (await spec.handler({}, ToolCtx(host="hedgehog.example")))["error"] == "mcp_disabled"
    await client.put(root + "/plugins/hedgehog", json={"enabled": True})
    # Re-enabling doesn't revive captured schemas from a revoked binding.
    assert (await spec.handler({}, ToolCtx(host="hedgehog.example")))["error"] == "mcp_disabled"


async def test_fixed_url_edit_invalidates_stale_tools_and_legacy_credentials(env, client):
    legacy(env)
    await client.post(f"/api/mcp/servers/{SID}/refresh")
    old = registry.get("vre_search")
    # An unchanged frontend form must not suppress legacy credentials.
    await client.put(f"/api/mcp/servers/{SID}", json={"name": "Renamed", "url": None, "url_template": "{host}/mcp"})
    assert mcp.token_for(conn().decl, "enterprise.example") == "old-host"
    await client.put(f"/api/mcp/servers/{SID}", json={"url": "https://fixed.example/mcp", "url_template": None})
    assert (await old.handler({}, ToolCtx(host="enterprise.example")))["error"] == "mcp_disabled"
    await client.post(f"/api/mcp/servers/{SID}/refresh")
    assert env.listed[-1] == (None, "https://fixed.example/mcp", None)
    assert mcp.endpoint_for(conn(), "elsewhere.example") == "https://fixed.example/mcp"


async def test_per_user_runtime_and_captured_handlers_are_isolated(env, client):
    a = current_user.set_current_email("alice@example.test")
    root = "/api/mcp/servers/shared"
    await client.put(root, json={"url": "https://alice.example/mcp", "tool_prefix": "shared_", "token": "alice-token"})
    await client.put(root + "/plugins/hedgehog", json={"enabled": True})
    await client.post(root + "/refresh")
    alice = conn("hedgehog")
    captured = registry.get(alice.tool_names[0])
    current_user.reset_current_email(a)
    b = current_user.set_current_email("bob@example.test")
    assert "shared" not in {s["id"] for s in (await client.get("/api/mcp")).json()["servers"]}
    assert registry.get(captured.name) is None
    assert (await captured.handler({}, ToolCtx(host="hedgehog.example")))["error"] == "mcp_disabled"
    await client.put(root, json={"url": "https://bob.example/mcp", "tool_prefix": "shared_", "token": "bob-token"})
    await client.put(root + "/plugins/hedgehog", json={"enabled": True})
    await client.post(root + "/refresh")
    await registry.dispatch(captured.name, {}, ToolCtx(host="hedgehog.example", email="bob@example.test"))
    assert env.called[-1][:3] == ("bob@example.test", "https://bob.example/mcp", "bob-token")
    assert (await captured.handler({}, ToolCtx(host="hedgehog.example")))["error"] == "mcp_disabled"
    current_user.reset_current_email(b)
    a = current_user.set_current_email("alice@example.test")
    await registry.dispatch(captured.name, {}, ToolCtx(host="hedgehog.example", email="alice@example.test"))
    assert env.called[-1][:3] == ("alice@example.test", "https://alice.example/mcp", "alice-token")
    current_user.reset_current_email(a)


async def test_secret_redaction_in_both_apis_and_remote_errors(env, client, monkeypatch):
    legacy(env)
    await client.put(f"/api/mcp/servers/{SID}", json={"token": "global-secret"})
    await client.put(f"/api/mcp/servers/{SID}/plugins/hedgehog", json={"enabled": True, "token": "binding-secret"})

    async def fails(url, token):
        raise RuntimeError(f"401 Authorization: Bearer {token}")

    monkeypatch.setattr(mcp.mcp_client, "list_tools", fails)
    await client.post(f"/api/mcp/servers/{SID}/refresh")
    for path in ("/api/mcp", "/api/settings"):
        response = await client.get(path)
        assert response.status_code == 200
        for secret in ("global-secret", "binding-secret", "old-fallback", "old-host", "second-key"):
            assert secret not in response.text
    redacted = (await client.get("/api/settings")).json()["plugins"]
    assert redacted["voitta-enterprise"]["mcp"]["has_api_keys"] == {"enterprise.example": True, "second.example": True}
    assert redacted["hedgehog"]["mcp_bindings"][SID]["has_token"] is True
    assert env.blobs[None]["plugins"]["voitta-enterprise"]["mcp"]["api_key"] == "old-fallback"


async def test_legacy_host_secret_patch_merges(env, client):
    legacy(env)
    path = "plugins.voitta-enterprise.mcp.api_keys"
    await client.put("/api/settings", json={"dotted": {path: {"enterprise.example": "rotated"}}})
    assert env.blobs[None]["plugins"]["voitta-enterprise"]["mcp"]["api_keys"] == {
        "enterprise.example": "rotated", "second.example": "second-key"}
    await client.put("/api/settings", json={"dotted": {path: {"enterprise.example": ""}}})
    assert env.blobs[None]["plugins"]["voitta-enterprise"]["mcp"]["api_keys"] == {"second.example": "second-key"}
    await client.put("/api/settings", json={"dotted": {path: None}})
    assert "api_keys" not in env.blobs[None]["plugins"]["voitta-enterprise"]["mcp"]


@pytest.mark.parametrize("patch", [
    {"url": "file:///etc/passwd"}, {"url": "https://user:pass@example.com/mcp"},
    {"url": "relative/mcp"}, {"url": "https://host:bad/mcp"},
    {"url_template": "https://{host}.evil.example/mcp"},
    {"url_template": "https://fixed.example/mcp"}, {"tool_prefix": "vre_"},
    {"tool_prefix": "bad-prefix"}, {"token": None}, {"enabled": "yes"},
])
async def test_validation(env, client, patch):
    response = await client.put("/api/mcp/servers/custom", json={"url": "http://localhost:8123/mcp", "tool_prefix": "custom_", **patch})
    assert response.status_code == 422
    assert "custom" not in catalog.servers()


async def test_delete_disable_and_old_refresh_routes(env, client):
    root = f"/api/mcp/servers/{SID}"
    assert (await client.post("/api/plugins/voitta-enterprise/refresh")).status_code == 200
    original = registry.get("vre_search")
    response = await client.delete(root)
    assert response.json()["servers"][0]["enabled"] is False
    assert (await original.handler({}, ToolCtx(host="enterprise.example")))["error"] == "mcp_disabled"
    env.listed.clear()
    await client.post("/api/plugins/refresh-all")
    assert env.listed == []
    await client.put("/api/mcp/servers/custom", json={"url": "https://custom.example", "tool_prefix": "custom_"})
    await client.put("/api/mcp/servers/custom/plugins/hedgehog", json={"enabled": True})
    response = await client.delete("/api/mcp/servers/custom")
    assert {s["id"] for s in response.json()["servers"]} == {SID}
    assert "custom" not in env.blobs[None]["plugins"]["hedgehog"]["mcp_bindings"]
    assert (await client.delete("/api/mcp/servers/missing")).status_code == 404


async def test_collision_never_removes_local_tool(env, client):
    async def handler(args, ctx):
        return "local"
    local = ToolSpec("vre_search", "Local", {}, handler)
    registry.register(local)
    await client.post(f"/api/mcp/servers/{SID}/refresh")
    assert conn().tool_names == []
    await client.delete(f"/api/mcp/servers/{SID}")
    assert registry.get("vre_search") is local


async def test_settings_bypass_disable_is_checked_at_call_time(env, client):
    await client.post(f"/api/mcp/servers/{SID}/refresh")
    spec = registry.get("vre_search")
    # Direct settings edits don't go through the catalog invalidation helper.
    env.blobs[None] = {"mcp": {"servers": {SID: {"enabled": False}}}}
    assert spec not in registry.visible_for_host("enterprise.example")
    assert (await spec.handler({}, ToolCtx(host="enterprise.example")))["error"] == "mcp_disabled"
    assert not env.called
    assert mcp.endpoint_for(conn()) is None  # host-less vre:// resolver also revoked


async def test_fixed_server_defaults_are_per_server_and_extra_hosts_activate(env, client):
    for sid in ("one", "two"):
        await client.put(f"/api/mcp/servers/{sid}", json={
            "url": f"https://{sid}.example/mcp", "url_template": "{host}/mcp",
            "tool_prefix": sid + "_", "token": sid + "-secret",
        })
        await client.put(f"/api/mcp/servers/{sid}/plugins/hedgehog", json={"enabled": True})
    env.blobs[None]["plugins"]["hedgehog"]["extra_hosts"] = ["localhost:8123"]
    await mcp.refresh_all()
    assert (None, "https://one.example/mcp", "one-secret") in env.listed
    assert (None, "https://two.example/mcp", "two-secret") in env.listed
    assert all(url not in {"https://hedgehog.example/mcp", "http://localhost:8123/mcp"}
               for _, url, _ in env.listed)
    first = conn("hedgehog")
    spec = registry.get(first.tool_names[0])
    assert spec in registry.visible_for_host("localhost:8123")
    assert spec not in registry.visible_for_host("localhost:9999")
    await spec.handler({}, ToolCtx(host="localhost:8123"))
    assert env.called[-1][1:3] == ("https://one.example/mcp", "one-secret")


async def test_disable_during_probe_never_registers_stale_schema(env, client, monkeypatch):
    import asyncio
    started, resume = asyncio.Event(), asyncio.Event()

    async def slow_probe(url, token):
        started.set()
        await resume.wait()
        return [Tool(name="search", inputSchema={"type": "object"})]

    monkeypatch.setattr(mcp.mcp_client, "list_tools", slow_probe)
    pending = asyncio.create_task(mcp.refresh_one(conn()))
    await started.wait()
    await client.put(f"/api/mcp/servers/{SID}", json={"enabled": False})
    resume.set()
    await pending
    assert registry.get("vre_search") is None


async def test_call_failures_never_echo_bearer(env, client, monkeypatch, caplog):
    await client.put(f"/api/mcp/servers/{SID}", json={"token": "private-bearer"})
    await client.post(f"/api/mcp/servers/{SID}/refresh")

    async def failing_call(url, token, name, args):
        raise RuntimeError(f"Authorization: Bearer {token}")

    monkeypatch.setattr(mcp.mcp_client, "call_tool", failing_call)
    result = await registry.get("vre_search").handler({}, ToolCtx(host="enterprise.example"))
    assert result["error"] == "mcp_call_failed"
    assert "private-bearer" not in str(result)
    assert "private-bearer" not in caplog.text
