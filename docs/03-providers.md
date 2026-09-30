# Providers

## Supported providers

| Provider | Default model |
|---|---|
| `anthropic` | `claude-sonnet-4-6` |
| `openai` | `gpt-4o` |
| `gemini` | `gemini-2.0-flash-exp` |
| `requesty` | `anthropic/claude-sonnet-5` |

## Requesty

`requesty` goes through the Requesty router's Anthropic-compatible Messages endpoint and reuses the Anthropic adapter (streaming, tool use, inline images, `cache_control`). Model ids are router-qualified: `anthropic/claude-sonnet-5`, `openai/gpt-5.5`, `vertex/gemini-2.5-pro`. The base URL defaults to `https://router.requesty.ai`; set `REQUESTY_BASE_URL` for a private deployment (a trailing `/v1` is tolerated).

## settings.json format

Located at `~/.config/voitta-compute/settings.json`.

```json
{
  "provider": "anthropic",
  "api_keys": {
    "anthropic": "sk-ant-...",
    "openai": "sk-...",
    "gemini": "AIza..."
  },
  "models": {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "gemini": "gemini-2.0-flash-exp"
  },
  "layout": "chat-right",
  "theme": "auto",
  "max_tool_iterations": 25,
  "max_tokens": 24576
}
```

Only `provider` and the matching `api_keys` entry are required. All other fields have defaults.

## Changing provider at runtime

The Settings panel in the widget writes to `settings.json` via `PUT /api/settings`. Changes take effect on the next user message (the agent loop reads settings at the top of each `run_turn()` call).

## Streaming

All three providers stream. The agent loop uses `async for ev in provider.stream(request)` over `BlockStart / BlockDelta / BlockStop / MessageStop / StreamError` events defined in `app/services/llm/stream.py`. Text tokens are forwarded to Chainlit via `cl.Message.stream_token()`.

## Tool results with images

Only Anthropic supports inline image blocks in tool results (also Requesty with Claude models; Requesty drops them for other models). When a screenshot is captured and the active provider is not Anthropic, the agent injects a note: `"N image(s) captured but current provider doesn't accept inline images — switch to Anthropic to view them"`.

## Agent-loop caps

- `max_tool_iterations` — maximum tool-use cycles per user turn (default 25). When hit, the model sees a warning message.
- `max_tokens` — max assistant tokens per LLM call (default 24576). When hit, Chainlit surfaces a truncation warning.

Both are readable/writable from the Settings → Global tab.
