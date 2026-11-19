# AstrBot Codex Orchestrator

English | [简体中文](README.zh-CN.md)

Community preview **0.1.4** lets an AstrBot Agent delegate tasks to the native
Codex App Server in a configured execution environment. Codex owns execution,
instructions, skills, MCP and sandbox policy. The plugin does not bundle Codex
CLI or move its upstream model credentials into AstrBot.

## Features and limits

- Native JSON-RPC over local subprocess stdio or authenticated WebSocket.
- Administrator-controlled endpoints, workspace aliases and actor allowlists;
  admin-only by default. Task reads, resume, steer, cancel and approvals check
  the original user and conversation as well as current environment permission.
- Durable SQLite task/interaction/result records, workspace serialization,
  explicit same-thread resume and steer, native completion and command evidence.
- Native Codex auto-review for eligible approval requests, with manual approval
  configurable per environment. Non-secret questions and a bounded subset of MCP
  forms can still reach the original user. Unsupported and secret interactions
  fail closed.
- Native progress events are pushed to the plugin. `codex_task` can wait up to
  30 seconds; later results remain available through an explicit query.

Automatic Agent re-entry after a wait/restart and automatic IM outbox sending
are **not implemented**. Native WebSocket is experimental; stdio has weaker
recovery than a persistent server. Ambiguous starts are never blindly replayed.
Artifacts are references only. An endpoint is an execution trust domain, not a
multi-tenant sandbox. Workspace aliases are not filesystem enforcement; the
plugin checks effective native policy before proceeding.

Verified baseline: **AstrBot 4.29.4, Codex CLI 0.162.1, Python >=3.10**. Other
AstrBot/Codex versions have not received the same live validation.
For native auto-review, use Codex App Server 0.169.1 or a compatible version.

## 1. Prepare the Codex execution host

Install Codex CLI using the [official guide](https://learn.chatgpt.com/docs/codex/cli)
on the host that should run tasks and authenticate as its
service user. An existing native login can be reused:

```sh
codex --version
codex login status
# If needed, complete the interactive native login:
codex login
mkdir -p "$HOME/codex-project"
```

Choose an existing project/worktree instead of the example directory if desired.
Its absolute path must be accessible to this Codex user. Keep upstream login
files on this host; never copy native auth files into AstrBot.

For a remote execution host, create a separate App Server access token:

```sh
umask 077
mkdir -p "$HOME/.config/codex-astrbot"
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > "$HOME/.config/codex-astrbot/server.token"
chmod 600 "$HOME/.config/codex-astrbot/server.token"
```

Run the native server as this user. Replace the example address with this host's
actual private interface address:

```sh
codex app-server \
  --listen ws://192.168.10.20:14550 \
  --ws-auth capability-token \
  --ws-token-file "$HOME/.config/codex-astrbot/server.token"
```

Keep the terminal running for the trial. For persistence, supervise this exact
command as the same user; do not use a second login/service identity by accident.
Use `127.0.0.1` only when the AstrBot process shares that network namespace.
Non-loopback `ws` requires the explicit private-network opt-in below and carries
unencrypted traffic. Use it only on a trusted private network/existing tunnel;
never add a public forward. For `wss`, supply your own authenticated TLS endpoint
that preserves WebSocket upgrades and the Authorization header.

Securely place a copy of **only this endpoint token** where the AstrBot service
can read it, with mode 0600 and a private parent directory. This token gives
native execution access as the Codex user. Do not paste it into chat or a URL.

## 2. Install and configure AstrBot

Install from this GitHub repository through AstrBot's plugin manager. AstrBot
installs `requirements.txt`; manual installations must install it in AstrBot's
Python environment. No AstrBot image change is required if dependencies exist.

Start with the plugin's empty environment list. Then configure it through the
plugin settings JSON editor, if available. If the UI cannot edit object lists,
wait for current tasks to finish, disable this plugin, and edit its dedicated
configuration file:

```text
data/config/astrbot_plugin_codex_orchestrator_config.json
```

Replace its contents with the following example and re-enable/reload the plugin.
This is **not** AstrBot's main `cmd_config.json`. Replace addresses and paths:

```json
{
  "database": "data/plugin_data/astrbot_plugin_codex_orchestrator/codex-orchestrator.sqlite3",
  "environments": [{
    "name": "dev",
    "host_label": "devbox",
    "transport": "websocket",
    "url": "ws://192.168.10.20:14550",
    "private_ws_opt_in": true,
    "token_file": "/AstrBot/data/plugin_data/astrbot_plugin_codex_orchestrator/server.token",
    "workspaces": {"repo": "/home/your-user/codex-project"},
    "admins": [],
    "allowlist": [],
    "sandbox": "workspace-write",
    "approval": "on-request",
    "approvals_reviewer": "auto_review"
  }]
}
```

Empty ACL lists permit AstrBot administrators only. Ordinary-user entries must
match the full actor key `platform_id:UMO:user_id` exactly; UMO itself contains
separators. Do not allow a whole group using UMO alone. Keep admin-only access
for the first test. Every task/result still belongs to its original actor/cid.

`host_label` is an optional, non-secret label for the machine running Codex.
The Agent tools include it and the transport in `execution`. Workspace aliases
select project working directories on the Codex side.

Starting in 0.1.4, an omitted `approvals_reviewer` defaults to `auto_review` on
both new and saved environments. Set it to `user` to use AstrBot's manual
approval flow. For environments shared with other users, review the allowlist
before upgrading or pin `user` if you require manual sign-off.
With the default `workspace-write` and `on-request` policy, ordinary commands
stay sandboxed with network access off. Codex evaluates eligible requests for
specific commands or extra access, such as an SSH command. An approved command
may run with the execution account's existing host permissions. Auto-review
can deny a request. Let current tasks finish before changing this setting.
When an older idle thread is resumed, the plugin can update its reviewer
before running another turn.

### Docker placement

| Item | Where the path is resolved |
|---|---|
| `workspaces.repo` | On the Codex execution host |
| `token_file` | Inside the AstrBot process/container |
| `database` | AstrBot working directory; use persistent `data/plugin_data/astrbot_plugin_codex_orchestrator/` |
| stdio `command` | Inside the AstrBot process/container |

For a bind `HOST_DATA:/AstrBot/data`, the example token belongs at
`HOST_DATA/plugin_data/astrbot_plugin_codex_orchestrator/server.token` on the AstrBot host and appears at
`/AstrBot/data/plugin_data/astrbot_plugin_codex_orchestrator/server.token` inside the container. Ensure the
AstrBot UID can read it. Do not copy native Codex login files. `127.0.0.1` inside
the container is not the remote host or the Docker host.

The database parent directory is created automatically on first startup.
Version 0.1.2 changes the default database location to the plugin-specific data
directory above. Saved `database` overrides are preserved. For an existing
installation, stop the plugin and back up/migrate the existing SQLite database
before updating that setting; changing only the path creates an empty task store.

### Optional local stdio

If Codex CLI and its native login are available to the AstrBot process itself,
replace the environment with:

```json
{
  "name": "local",
  "transport": "stdio",
  "command": ["codex", "app-server"],
  "workspaces": {"repo": "/workspace/project"},
  "admins": [],
  "allowlist": [],
  "sandbox": "workspace-write",
  "approval": "on-request",
  "approvals_reviewer": "auto_review"
}
```

A CLI installed only on the Docker host does not become available inside the
AstrBot container. Use the remote WebSocket setup when the environments differ.

## 3. Submit and inspect a task

In an administrator conversation that already has an AstrBot conversation:

```text
/codex environments
/codex submit dev repo Create hello.txt, write hello from AstrBot, then read it back to verify.
/codex status dev TASK_ID
/codex result dev TASK_ID
```

Or ask the Agent to use `codex_environments`, `codex_submit`, then
`codex_task(env="dev", task_id="...", wait_seconds=30)`. Explicit schemas declare
required arguments, integer cursors and wait/page limits. Other tools are
`codex_sessions`, `codex_steer` and `codex_cancel`. When a pending interaction
appears, `codex_task` returns early. It gives the Agent the approval or answer
command and any answer format to relay to the original user. The task stays
active while the user decides. After responding, the user can run the returned
status command or send a new message asking the Agent to check. If
`codex_cancel` returns `cancelling`, `codex_task` reports when the task reaches
`interrupted`.

`status` shows one pending interaction per page, ahead of verbose progress.
`result` pages the final text. Follow the returned `nextStatusCommand` or
`nextResultCommand`; their OFFSET is a pending-interaction index or a final-text
character offset respectively. After answering an interaction, refresh status
from offset 0 because the pending list has changed. Responses stay valid JSON
within 3,800 bytes.
Agent tools retain structured plan/diff/usage/command observations and bounded
progress; `cursor`/`limit` page events and `interaction_offset` pages interactions.
Observed command exit codes are evidence, not a claim that a named check passed.

Use these commands in the original user/conversation for any interaction
that the App Server sends to the user:

```text
/codex approve dev TASK_ID INTERACTION_ID
/codex deny dev TASK_ID INTERACTION_ID
/codex approve dev TASK_ID INTERACTION_ID acceptForSession
/codex answer dev TASK_ID INTERACTION_ID {"answers":{"question_id":["value"]}}
```

Use only a decision actually offered by the server. MCP form answers use
`{"action":"accept","content":{"field":"value"}}`. Secret questions must be
handled locally. Truncated details are visibly marked: inspect more through
`codex_task` and decline if you cannot review enough context. The plugin declines
permission grant requests routed to its manual interaction flow. Cancellation
is confirmed by the native terminal state. It leaves file edits in place.

Resume with `/codex resume dev repo THREAD_ID OBJECTIVE`, steer with
`/codex steer dev TASK_ID TEXT`, and cancel with `/codex cancel dev TASK_ID`.
Resume requires an owned session in the same workspace and conversation.

## Troubleshooting

| Symptom | Check |
|---|---|
| Plugin fails to connect | Server is running; address is reachable from AstrBot's namespace; port/transport correct |
| HTTP 401 | Both endpoint token files contain the same token; do not use a model API key |
| Token file missing/permission error | Container path, bind mount, service UID and 0600 mode |
| No environment / permission denied | Actual AstrBot administrator role or exact actor allowlist |
| Incomplete conversation identity | Send a normal message first so AstrBot establishes a current conversation |
| Old task cannot be read after switching conversation | Ownership includes cid; return to the original conversation |
| `codex` executable missing under stdio | CLI must exist in AstrBot's process environment, including its container |
| Effective policy mismatch | Native policy is broader/different; investigate server configuration instead of bypassing the check |
| Task completed but no later chat reply | Ask the Agent to query `codex_task`; automatic late re-entry is deferred |

## Development

```sh
pip install -e ".[test]"
python -m pytest
ruff check .
ruff format --check .
python -m build
python scripts/build_plugin.py --output dist/astrbot_plugin_codex_orchestrator.zip
```

Tests use local fixtures and do not call a model service. Live validation also
covered new tasks, same-thread resume, results returning to the Agent, independent
file/checksum checks and cross-conversation rejection. Version 0.1.1 additionally
verifies actual AstrBot FunctionTool registration/schema/context handling in an
isolated process. Live outage/re-entry guarantees are not implied.

MIT licensed; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
