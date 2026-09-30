"""History replay and engine selection for the subscription brain.

* ``_transcript_rows`` — a reopened conversation must show its tool calls (with
  inputs and results), not just the prose between them, and must hide the
  engine's own ``<synthetic>`` replies ("No response requested.").
* ``engine_cli_path`` — the brain must run the newest engine on the machine,
  or a newly released model fails with "Claude Code X does not support this
  model" even though an up-to-date ``claude`` is installed.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.agent_sdk import config
from app.services.agent_sdk.runtime import _build_options, _explain
from app.services.agent_sdk.sessions import _transcript_rows
from app.tools.registry import ToolCtx


def _sm(role: str, content, model: str | None = None):
    msg = {"role": role, "content": content}
    if model:
        msg["model"] = model
    return SimpleNamespace(message=msg)


def test_transcript_keeps_tool_calls_and_drops_synthetic():
    rows = _transcript_rows([
        _sm("user", "make a resume"),
        _sm("assistant", [
            {"type": "text", "text": "Reading the profile."},
            {"type": "tool_use", "id": "t1", "name": "mcp__voitta__linkedin_read_profile",
             "input": {"profile_id": "x"}},
        ]),
        _sm("user", [{"type": "tool_result", "tool_use_id": "t1",
                      "content": [{"type": "text", "text": "{\"ok\": true}"}]}]),
        _sm("assistant", [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}]),
        _sm("user", [{"type": "text", "text": "Continue from where you left off."}]),
        _sm("assistant", [{"type": "text", "text": "No response requested."}], model="<synthetic>"),
        _sm("assistant", [{"type": "text", "text": "Done."}]),
    ])
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "tool", "user", "assistant"]
    read = rows[2]
    assert read["name"] == "linkedin_read_profile"
    assert read["input"] == '{"profile_id": "x"}'
    assert read["output"] == '{"ok": true}' and read["is_error"] is False
    # No result ever arrived: shown as a failed call, not a spinner.
    assert rows[3]["is_error"] is True
    assert all("No response requested" not in (r.get("text") or "") for r in rows)


def test_engine_prefers_newer_system_install(monkeypatch, tmp_path):
    system = tmp_path / "claude"
    system.write_text("")
    monkeypatch.setattr(config, "_bundled_engine", lambda: ("/bundled/claude", (2, 1, 277)))
    monkeypatch.setattr(config, "_cli_path_cached", lambda: str(system))

    monkeypatch.setattr(config, "_binary_version", lambda *_: (2, 1, 283))
    assert config.engine_cli_path() == str(system)

    monkeypatch.setattr(config, "_binary_version", lambda *_: (2, 1, 277))
    assert config.engine_cli_path() == "/bundled/claude"

    monkeypatch.setattr(config, "_binary_version", lambda *_: None)
    assert config.engine_cli_path() == "/bundled/claude"


def test_options_isolate_user_settings():
    opts = _build_options(
        system="s", model=None, resume=None, can_use_tool=None, cli_path="/x/claude",
        ctx=ToolCtx(session_id="s", host=None, email="t@example.com", extras={}),
    )
    # None would mean "load every source", including ~/.claude/CLAUDE.md.
    assert opts.setting_sources == []
    assert str(opts.cli_path) == "/x/claude"


def test_engine_too_old_error_is_actionable():
    text = _explain(
        "API Error: 400 Claude Code 2.1.277 does not support this model; version "
        "2.1.280 or newer is required. Run 'claude update', or update the Claude "
        "desktop app, then try again.",
        "claude-opus-5-5",
    )
    assert "claude-opus-5-5" in text and "2.1.280" in text and "Settings" in text
    assert _explain("something else", None) == "something else"
