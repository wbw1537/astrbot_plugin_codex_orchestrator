# AstrBot Codex Orchestrator / AstrBot Codex 编排插件

English | [简体中文](README.zh-CN.md)

Community preview 0.1 is an AstrBot client for the native Codex App Server. It keeps Codex execution, instructions, skills, MCP and sandbox policy on the configured execution host. It does not bundle Codex CLI or handle upstream model credentials.

## Supported now

- Native App Server JSON-RPC over local stdio and configured WebSocket (`wss`; `ws` only loopback unless an explicit private/tunnel opt-in is set).
- Explicit environment, workspace alias and actor allowlists; admin-only if lists are empty. Each action checks the originating platform, UMO, user and conversation.
- Durable SQLite task and interaction records, request-key deduplication per environment and actor, workspace serialization, task status/result pull, cancellation request state and explicit steering.
- Manual native command/file approval choices, fail-closed permission requests, non-secret tool questions and MCP form responses for the supported primitive/enum/range subset. Unsupported MCP schema constraints are rejected. Secret questions are declined without storing an answer; URL elicitation is decline/cancel only.
- AstrBot `/codex` command and registered agent tools. Completions are saved to the outbox and can be pulled with `codex_task` or `/codex result`.

## Limits

AstrBot 4.29.4 has no stable public continuation API that safely re-enters the original live agent. This preview does not wake another conversation or rewrite history. Completion delivery is durable inbox/outbox plus explicit result pull; message sending depends on a future tested adapter. On restart, remote thread history reconciliation is best effort and never replays a side-effecting start. Stdio servers have weaker recovery than persistent remote servers. Native Codex WebSocket is experimental. A configured endpoint is a trust domain, not a multi-tenant sandbox. Workspace aliases do not sandbox filesystem access; effective native policy must match the configured policy. Remote artifacts are references only, not automatically downloaded.

| Capability | Status |
|---|---|
| Stdio and WebSocket App Server transport | Supported preview |
| ACL, workspace aliases, task status, cancel, steer | Supported preview |
| Native approval and question handling | Supported preview; manual only |
| Durable task/outbox and manual result pull | Supported preview |
| Automatic AstrBot agent re-entry | Deferred; unsupported by tested 4.29.4 public API |
| Goal mode, native queue UI, fork/compact UI, artifact transfer, dynamic tools, multimodal | Deferred |

## Setup

Install from this repository in AstrBot, or use a plugin archive built with `python scripts/build_plugin.py --output dist/astrbot_plugin_codex_orchestrator.zip`. Install `requirements.txt`. Configure at least one environment in the plugin settings. Example below is illustrative; replace paths and identities. Do not place tokens in URLs or this file.

```json
{
  "database": "data/codex-orchestrator.sqlite3",
  "environments": [{
    "name": "dev",
    "transport": "stdio",
    "command": ["codex", "app-server"],
    "workspaces": {"repo": "/workspace/project"},
    "admins": ["platform:umo:user"],
    "allowlist": [],
    "sandbox": "workspace-write",
    "approval": "on-request"
  }]
}
```

For remote WebSocket set `transport: "websocket"`, `url` to an administrator-controlled `wss://` endpoint and optionally `token_file` to a protected file (mode 0600). Private `ws://` requires `private_ws_opt_in: true` and must be protected by a private network or tunnel; it has no TLS. The endpoint token authenticates App Server access; Codex upstream model login remains on the execution host. Never expose the native endpoint publicly without its own authentication and network controls.

Use `/codex environments`, `/codex submit ENV WORKSPACE OBJECTIVE`, `/codex resume ENV WORKSPACE THREAD OBJECTIVE`, `/codex status ENV TASK`, `/codex result ENV TASK`, and `/codex sessions ENV`. Agent tools expose authorized environment/workspace choices, submission (new or owned-thread resume), status, cancel, steer and sessions. `codex_submit` returns a receipt; `codex_task` can wait 0–30 seconds and return structured result/error, native evidence, or pending interactions to its calling agent. Its `cursor`/`limit` arguments page bounded progress events; responses include `nextCursor`, `hasMore`, and visible truncation markers. Each serialized progress entry is capped at 4 KiB and its page at 64 KiB (including truncation metadata); final text is capped at 8,000 UTF-8 bytes. Up to eight recent command observations are summarized, with visible omission markers. Native plan, diff, token usage, and command exit data remain separate under `nativeEvidence`; command exits are observations, not claims that a named check passed. Pending interaction IDs and offered choices are shown in task status. Use `/codex approve ENV TASK INTERACTION [decision]`, `/codex deny ENV TASK INTERACTION`, or `/codex answer ENV TASK INTERACTION JSON` from the owning conversation. For a user-input question, JSON is `{"answers":{"question_id":["value"]}}`; for an MCP form, JSON is `{"action":"accept","content":{"field":"value"}}`. LLM tools cannot answer approvals. Secret questions are declined for local Codex UI handling.

## Development

Python >=3.10. Install development dependencies with `pip install -e ".[test]"`. Run `python -m pytest`, `ruff check .`, `ruff format --check .`, `python -m compileall -q codex_orchestrator main.py`, `python -m build`, and `python scripts/build_plugin.py`. Tests use local fixtures and do not call a model service.

Verified with AstrBot 4.29.4 and Codex CLI 0.162.1. The regression suite has 38 tests. Live WebChat testing also verified task creation, same-thread resume, structured results returning to the Agent, independent file/checksum validation and rejection of cross-conversation reads.

Known issue: AstrBot 4.29.4's decorator-generated tool schemas omit `required` declarations. An Agent can omit a required argument such as `env`, receive a missing-argument error, and need to retry with the field included. Explicit required schemas remain follow-up work.

MIT licensed; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
