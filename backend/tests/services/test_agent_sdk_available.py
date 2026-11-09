"""The subscription brain is offered when *either* engine exists.

Regression: on the Docker server the only engine is the one bundled in the
claude_agent_sdk wheel (no system `claude`), and is_available() only looked
for a system install — so "Claude (subscription)" vanished from Settings.
"""

from __future__ import annotations

import pytest

from app.services.agent_sdk import config


@pytest.mark.parametrize("bundled, system, want", [
    (("/x/_bundled/claude", (2, 1, 319)), None, True),   # Docker image
    (None, "/usr/local/bin/claude", True),               # desktop with Claude Code installed
    (("/x/_bundled/claude", (2, 1, 319)), "/usr/local/bin/claude", True),
    (None, None, False),
])
def test_is_available(monkeypatch, bundled, system, want):
    monkeypatch.setattr(config, "_bundled_engine", lambda: bundled)
    monkeypatch.setattr(config, "_cli_path_cached", lambda: system)
    assert config.is_available() is want


def test_unavailable_without_the_sdk(monkeypatch):
    import importlib.util

    monkeypatch.setattr(config, "_bundled_engine", lambda: ("/x/claude", (2, 1, 319)))
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "claude_agent_sdk" else real(name, *a))
    assert config.is_available() is False
