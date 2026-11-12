from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import plugin_events


@pytest.fixture(autouse=True)
def _root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.services.current_user.user_data_root", lambda: tmp_path)
    return tmp_path


def test_append_read_clear_roundtrip():
    assert plugin_events.append("hedgehog", [{"type": "first_ok", "t": 1}, {"type": "heartbeat", "ok": True}]) == 2
    evs = plugin_events.read("hedgehog")
    assert [e["type"] for e in evs] == ["first_ok", "heartbeat"]
    assert all("rt" in e for e in evs)
    plugin_events.clear("hedgehog")
    assert plugin_events.read("hedgehog") == []


def test_caps_count_and_drops_junk(monkeypatch):
    monkeypatch.setattr(plugin_events, "MAX_EVENTS", 5)
    for i in range(3):
        plugin_events.append("hedgehog", [{"type": "x", "i": i}, {"type": "y", "i": i}])
    evs = plugin_events.read("hedgehog")
    assert len(evs) == 5 and evs[-1] == {**evs[-1], "type": "y", "i": 2}
    assert plugin_events.append("hedgehog", ["nope", {"type": "z", "big": "x" * 5000}]) == 0
    assert plugin_events.append("hedgehog", [{"type": "nested", "obj": {"a": 1}}]) == 1
    assert "obj" not in plugin_events.read("hedgehog")[-1]


def test_drops_events_older_than_max_age(monkeypatch):
    plugin_events.append("hedgehog", [{"type": "old"}])
    real = time.time
    monkeypatch.setattr(plugin_events.time, "time", lambda: real() + plugin_events.MAX_AGE_S + 60)
    plugin_events.append("hedgehog", [{"type": "new"}])
    assert [e["type"] for e in plugin_events.read("hedgehog")] == ["new"]


def test_rejects_bad_plugin_names():
    for bad in ("../x", "A", "", "x" * 80):
        with pytest.raises(plugin_events.EventError):
            plugin_events.append(bad, [{"type": "x"}])


def test_routes_desktop(monkeypatch):
    monkeypatch.delenv("VOITTA_GOOGLE_AUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("VOITTA_GOOGLE_AUTH_CLIENT_SECRET", raising=False)
    from app.main import app

    c = TestClient(app)
    r = c.post("/api/plugin-events/hedgehog", json={"events": [{"type": "first_ok"}]})
    assert r.status_code == 200 and r.json() == {"kept": 1}
    # sendBeacon-style: text/plain body with a bare list
    r = c.post("/api/plugin-events/hedgehog", content='[{"type":"logout"}]', headers={"Content-Type": "text/plain"})
    assert r.json() == {"kept": 1}
    assert [e["type"] for e in c.get("/api/plugin-events/hedgehog").json()["events"]] == ["first_ok", "logout"]
    # unapproved site can't write
    assert c.post("/api/plugin-events/hedgehog", json={"events": [{"type": "x"}]},
                  headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.delete("/api/plugin-events/hedgehog").status_code == 200
    assert c.get("/api/plugin-events/hedgehog").json()["events"] == []
