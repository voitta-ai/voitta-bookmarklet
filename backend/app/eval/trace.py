"""Durable, sequence-numbered event log for one eval run (JSONL + status file)."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.eval.config import SCHEMA_VERSION
from app.eval.redact import Redactor

TERMINAL = ("run.completed", "run.failed", "run.cancelled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Trace:
    """Appends events to ``runs/<run_id>.jsonl``, fsync'd per event, and keeps
    ``runs/<run_id>.json`` (status, final output, error) current."""

    def __init__(
        self, runs_dir: Path, run_id: str, session_id: str, probe_id: str,
        config_digest: str, redactor: Redactor, *, production: bool = True,
    ) -> None:
        self.run_id = run_id
        self._ids = {"run_id": run_id, "session_id": session_id, "probe_id": probe_id}
        self._digest = config_digest
        self._redactor = redactor
        self._log = runs_dir / f"{run_id}.jsonl"
        self._status_path = runs_dir / f"{run_id}.json"
        self._seq = 0
        self._lock = threading.Lock()
        self.status: dict[str, Any] = {
            **self._ids, "status": "running", "final_output": None, "error": None,
            # Labels the result itself, so a status poll alone can tell an
            # ablation run (test-only system prompt) from a production one.
            "config_digest": config_digest, "production": production,
        }
        self._write_status()

    def emit(
        self, type_: str, payload: dict[str, Any], *,
        call_id: str | None = None, parent_span_id: str | None = None,
    ) -> dict[str, Any]:
        clean, hits = self._redactor.scrub(payload)
        with self._lock:
            self._seq += 1
            event = {
                "schema_version": SCHEMA_VERSION,
                "event_id": uuid.uuid4().hex,
                "sequence": self._seq,
                "timestamp": _now(),
                **self._ids,
                "config_digest": self._digest,
                "parent_span_id": parent_span_id,
                "call_id": call_id,
                "type": type_,
                "payload": clean,
                "redactions": hits,
            }
            with open(self._log, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
        return event

    def finish(self, type_: str, payload: dict[str, Any]) -> None:
        if self.status["status"] != "running":
            return  # exactly one terminal event per run
        self.emit(type_, payload)
        status = {"run.completed": "completed", "run.failed": "failed",
                  "run.cancelled": "cancelled"}[type_]
        clean, _ = self._redactor.scrub(payload)
        self.status.update(
            status=status,
            final_output=clean.get("final_output"),
            error=clean.get("error"),
        )
        self._write_status()

    def _write_status(self) -> None:
        tmp = self._status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.status, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, self._status_path)


def read_events(runs_dir: Path, run_id: str, after: int = 0) -> list[dict[str, Any]]:
    path = runs_dir / f"{run_id}.jsonl"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            ev = json.loads(line)
            if ev["sequence"] > after:
                events.append(ev)
    return events


def read_status(runs_dir: Path, run_id: str) -> dict[str, Any] | None:
    path = runs_dir / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def fail_abandoned(runs_dir: Path) -> int:
    """Startup supervisor: a run still marked ``running`` was cut off by a
    crash or restart. Append a terminal ``run.failed`` and mark it failed, so
    an incomplete run never reads as anything but failed."""
    count = 0
    for status_path in runs_dir.glob("*.json"):
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("status") != "running":
            continue
        log = runs_dir / f"{status['run_id']}.jsonl"
        last_seq = 0
        last: dict[str, Any] | None = None
        if log.is_file():
            lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                last = json.loads(lines[-1])
                last_seq = last["sequence"]
        if last is not None and last["type"] in TERMINAL:
            # Crashed after the terminal event but before the status update:
            # repair the status from the log, never add a second terminal.
            payload = last.get("payload") or {}
            status.update(status=last["type"].split(".", 1)[1],
                          final_output=payload.get("final_output"),
                          error=payload.get("error"))
            status_path.write_text(json.dumps(status), encoding="utf-8")
            continue
        event = {
            "schema_version": SCHEMA_VERSION, "event_id": uuid.uuid4().hex,
            "sequence": last_seq + 1, "timestamp": _now(),
            "run_id": status["run_id"], "session_id": status["session_id"],
            "probe_id": status["probe_id"], "config_digest": None,
            "parent_span_id": None, "call_id": None, "type": "run.failed",
            "payload": {"error": {"kind": "abandoned",
                                  "message": "server stopped before the run finished"}},
            "redactions": [],
        }
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
        status.update(status="failed", error=event["payload"]["error"])
        status_path.write_text(json.dumps(status), encoding="utf-8")
        count += 1
    return count
