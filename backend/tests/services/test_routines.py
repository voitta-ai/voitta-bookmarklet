from __future__ import annotations

from pathlib import Path

import pytest

from app.services import routines

@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(routines, "_path", lambda: tmp_path / "routines.json")
    routines._claims.clear()
    return tmp_path

def _every(**kw):
    base = {"mode": "every", "every_min": 30, "start": "09:30", "end": "16:00",
            "days": [0, 1, 2, 3, 4], "prompt": "check"}
    return {**base, **kw}

def _routine(*chimes, **kw):
    return {"name": "Watch", "tz": "America/New_York", "chimes": list(chimes), **kw}

def test_round_trip_assigns_ids():
    saved = routines.save_routines([_routine(_every(), {"mode": "at", "times": ["15:55", "09:35"], "prompt": "x"})])
    assert saved[0]["id"].startswith("r_")
    assert [c["id"][:2] for c in saved[0]["chimes"]] == ["c_", "c_"]
    assert saved[0]["chimes"][1]["times"] == ["09:35", "15:55"]  # sorted
    assert routines.list_routines() == saved

def test_minimum_interval_enforced():
    with pytest.raises(ValueError, match="minimum interval is 10"):
        routines.save_routines([_routine(_every(every_min=5))])
    routines.save_routines([_routine(_every(every_min=10))])

@pytest.mark.parametrize(
    "chime, msg",
    [
        (_every(start="9:30"), "HH:MM"),
        (_every(start="16:00", end="09:00"), "end is before start"),
        (_every(prompt="   "), "prompt is empty"),
        ({"mode": "at", "times": [], "prompt": "x"}, "at least one time"),
        ({"mode": "cron", "prompt": "x"}, "mode must be"),
        (_every(days=[7]), "days must be"),
    ],
)
def test_invalid_chimes(chime, msg):
    with pytest.raises(ValueError, match=msg):
        routines.save_routines([_routine(chime)])

def test_bad_timezone():
    with pytest.raises(ValueError, match="unknown time zone"):
        routines.save_routines([_routine(_every(), tz="Mars/Olympus")])

def test_duplicate_routine_ids_are_reassigned():
    saved = routines.save_routines([_routine(_every(), id="r_same"), _routine(_every(), id="r_same")])
    assert saved[0]["id"] == "r_same" and saved[1]["id"] != "r_same"

def test_claim_first_wins_per_slot():
    assert routines.claim("t1", "r", "c", 1000) is True
    assert routines.claim("t1", "r", "c", 1000) is False
    assert routines.claim("t1", "r", "c", 2000) is True  # next slot
    assert routines.claim("t2", "r", "c", 1000) is True  # other chat
