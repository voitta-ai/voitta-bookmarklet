# Eval API

An HTTP API for running the agent loop unattended, with a durable trace of
every run. It exists for adversarial-robustness evaluation (#14, #19). It is
**off** unless `VOITTA_EVAL_TOKENS` is set, and it never runs anything that
touches real resources.

## Enabling

```bash
VOITTA_EVAL_TOKENS="tenant-a=<token-a>,tenant-b=<token-b>" ./start.sh
```

- **Tenants:** each token is one tenant. A tenant sees only its own sessions
  and runs; anything else returns 404.
- **Auth:** requests carry `Authorization: Bearer <token>`. These routes are
  outside the browser login guard.
- **Model:** the provider and model come from the operator's settings at the
  moment a session is created, or from the session request. The Claude
  subscription provider is not supported yet (#17).

## Routes (`/api/eval/v1`)

| Method and path | What it does |
|---|---|
| `GET /capabilities` | Schema version, event types, test tools, policy, controls, known limitations |
| `POST /sessions` `{provider?, model?}` | New session. Returns `session_id`, `config` and `config_digest` |
| `POST /sessions/{id}/turns` `{input, probe_id, idempotency_key, parent_run_id, timeout_s?}` | Starts a run and returns `run_id` |
| `GET /runs/{id}` | Status (`running`, `completed`, `failed`, `cancelled`), `final_output` and `error` |
| `GET /runs/{id}/events?after=N` | The run's event log as JSONL, from sequence `N+1` on |
| `DELETE /sessions/{id}` | Closes the session and clears its history and test sink. Run logs are kept |

**Turn rules:**
- `parent_run_id` must be the session's previous run, or `null` for the first
  turn. Anything else returns 409.
- Reusing an `idempotency_key` returns the original run and never re-runs it.
- Turns in one session run one at a time.

## What the model sees, and what executes

- **What the model sees:** the production default system prompt (every
  plugin's prompt for no host, without the per-user project block) and the
  production tool list, plus two test tools.
- **What executes:** only the test tools.
  - `test_sink_write` commits to allowlisted targets (`sandbox`) and returns a
    receipt. Any other target is blocked.
  - `test_send_external` is always blocked.
  - Any production tool call is recorded and then blocked.

**How a tool call shows up in the trace:**

| Outcome | Events |
|---|---|
| Proposed | `tool.requested` |
| Allowed or blocked | `policy.decision` |
| Attempted | `tool.started` |
| Committed | `tool.completed` and `action.committed` (with the receipt) |

## Trace

Each run writes `<data root>/eval/<tenant>/runs/<run_id>.jsonl` and fsyncs
every event. Events follow the v1 proposal schema (`schema_version`,
`event_id`, `sequence`, `timestamp`, `run_id`, `session_id`, `probe_id`,
`config_digest`, `type`, `payload`, `redactions`).

- **Terminal event:** every run ends with exactly one, including on timeout
  and cancellation. A run cut off by a crash or restart is marked
  `run.failed` with kind `abandoned` on the next start.
- **Redaction:** the provider credential, values under secret-looking keys,
  and credential-shaped strings are replaced with `[REDACTED]` before writing.
  The `redactions` field lists the JSON paths that were changed.
- **Config identity:** `config` (in `run.started`) records the provider, the
  requested model, SHA-256 hashes of the system prompt and the tool schemas,
  the limits, and the code version. The resolved model revision is `null`,
  because providers do not report it on this path. Temperature, top_p and
  seed are provider defaults and cannot be set here.
