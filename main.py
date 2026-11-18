"""AstrBot entrypoint. Core modules remain importable without AstrBot installed."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

try:  # package-relative loading is AstrBot's normal data.plugins.<name>.main mode
    from .codex_orchestrator.chat import command_response, task_response
    from .codex_orchestrator.service import Actor, Environment, Orchestrator
    from .codex_orchestrator.store import Store
    from .codex_orchestrator.tool_schemas import tool_definitions
    from .codex_orchestrator.transport import connect_stdio, connect_websocket
except ImportError:  # direct module mode for standalone tests
    from codex_orchestrator.chat import command_response, task_response
    from codex_orchestrator.service import Actor, Environment, Orchestrator
    from codex_orchestrator.store import Store
    from codex_orchestrator.tool_schemas import tool_definitions
    from codex_orchestrator.transport import connect_stdio, connect_websocket

try:
    from astrbot.api import FunctionTool
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star, register
except ImportError:  # allows metadata and core tests without AstrBot

    @dataclass
    class FunctionTool:
        name: str
        description: str
        parameters: dict
        handler: Any = None
        handler_module_path: str | None = None

    class Context:
        pass

    class Star:
        def __init__(self, context: Any, config: Any = None):
            self.context, self.config = context, config or {}

    class AstrMessageEvent:
        pass

    class _Filter:
        @staticmethod
        def command(_name: str):
            return lambda fn: fn

    filter = _Filter()

    def register(*_args: Any, **_kwargs: Any):
        return lambda cls: cls


class _CodexTool(FunctionTool):
    """Public FunctionTool override; framework context remains the actor authority."""

    def __init__(self, *, callback, **schema):
        super().__init__(**schema)
        self._callback = callback

    async def call(self, context, **kwargs):
        return await self._callback(context.context.event, **kwargs)


@register("astrbot_plugin_codex_orchestrator", "wbw1537", "Codex App Server orchestration", "0.1.2")
class CodexOrchestratorPlugin(Star):
    def __init__(self, context: Any, config: Any = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}

    async def initialize(self) -> None:
        cfg = self.config if isinstance(self.config, dict) else {}
        self.store = Store(
            cfg.get(
                "database",
                "data/plugin_data/astrbot_plugin_codex_orchestrator/codex-orchestrator.sqlite3",
            )
        )
        self.clients = []
        self._environment_configs = {}
        self._stopping = False
        self._watchers = set()
        environments = {}
        try:
            for row in cfg.get("environments", []):
                if row.get("transport") == "stdio":
                    client = await connect_stdio(row["command"])
                else:
                    client = await connect_websocket(
                        row["url"],
                        token_file=row.get("token_file"),
                        private_opt_in=bool(row.get("private_ws_opt_in")),
                    )
                self.clients.append(client)
                self._environment_configs[row["name"]] = dict(row)
                await client.initialize()
                environments[row["name"]] = Environment(
                    row["name"],
                    client,
                    row.get("workspaces", {}),
                    set(row.get("admins", [])),
                    set(row.get("allowlist", [])),
                    row.get("sandbox", "workspace-write"),
                    row.get("approval", "on-request"),
                )
            self.service = Orchestrator(self.store, environments)
            await self.service.recover()
            self._register_tools()
            for name in environments:
                watcher = asyncio.create_task(self._watch_environment(name))
                self._watchers.add(watcher)
                watcher.add_done_callback(self._watchers.discard)
        except BaseException:
            for client in self.clients:
                await client.close()
            self.store.close()
            raise

    def _register_tools(self) -> None:
        tools = []
        for definition in tool_definitions():
            callback = getattr(self, definition.pop("callback"))
            tools.append(_CodexTool(callback=callback, **definition))
        self.context.add_llm_tools(*tools)

    async def _watch_environment(self, name: str) -> None:
        delay = 0.5
        while not self._stopping:
            current = self.service.environments[name].client
            wait_closed = getattr(current, "wait_closed", None)
            if wait_closed is None:
                return
            await wait_closed()
            if self._stopping:
                return
            self.service.mark_disconnected(name)
            config = self._environment_configs[name]
            while not self._stopping:
                client = None
                installed = False
                try:
                    if config.get("transport") == "stdio":
                        client = await connect_stdio(config["command"])
                    else:
                        client = await connect_websocket(
                            config["url"],
                            token_file=config.get("token_file"),
                            private_opt_in=bool(config.get("private_ws_opt_in")),
                        )
                    await client.initialize()
                    await self.service.replace_client(name, client)
                    installed = True
                    await self.service.recover(name)
                    delay = 0.5
                    break
                except asyncio.CancelledError:
                    if client and not installed:
                        await client.close()
                    raise
                except Exception:
                    if client and not installed:
                        await client.close()
                    if installed:
                        self.service.mark_disconnected(name)
                    if client:
                        await client.close()
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)

    async def terminate(self) -> None:
        self._stopping = True
        for watcher in getattr(self, "_watchers", set()):
            watcher.cancel()
        await asyncio.gather(*getattr(self, "_watchers", set()), return_exceptions=True)
        if hasattr(self, "service"):
            await self.service.close()
        if hasattr(self, "store"):
            self.store.close()

    async def _actor(self, event: Any) -> Actor:
        # Actor identity and cid come from AstrBot event/context, never tool arguments.
        user = event.get_sender_id() if callable(getattr(event, "get_sender_id", None)) else None
        umo = getattr(event, "unified_msg_origin", None)
        platform = (
            event.get_platform_id() if callable(getattr(event, "get_platform_id", None)) else None
        )
        manager = getattr(self.context, "conversation_manager", None)
        cid = await manager.get_curr_conversation_id(umo) if manager and umo else None
        is_admin = getattr(event, "is_admin", False)
        role = (
            "admin"
            if (is_admin() if callable(is_admin) else getattr(event, "role", "") == "admin")
            else "user"
        )
        if not all(isinstance(x, str) and x for x in (user, umo, cid, platform)):
            raise PermissionError(
                "AstrBot did not provide a complete actor and conversation identity"
            )
        return Actor(platform, umo, user, cid, role)

    async def tool_codex_environments(self, event: Any) -> dict[str, Any]:
        actor = await self._actor(event)
        visible = []
        for name, env in self.service.environments.items():
            if self.service._allowed(env, actor):
                visible.append(
                    {
                        "name": name,
                        "workspaces": sorted(env.workspaces),
                        "transport": "connected"
                        if not getattr(env.client, "_closed", asyncio.Event()).is_set()
                        else "disconnected",
                        "capabilities": ["submit", "status", "cancel", "steer", "sessions"],
                    }
                )
        return {"environments": visible}

    async def tool_codex_submit(
        self,
        event: Any,
        env: str,
        workspace: str,
        text: str,
        mode: str = "new",
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.service.submit(
            env,
            workspace,
            text,
            await self._actor(event),
            mode=mode,
            thread_id=thread_id,
            request_key=getattr(event, "message_id", None),
        )

    async def tool_codex_task(self, event: Any, env: str, task_id: str) -> dict[str, Any]:
        return self.service.task_view(env, task_id, await self._actor(event))

    async def tool_codex_action(
        self, event: Any, env: str, task_id: str, action: str, payload: Any = None
    ) -> dict[str, Any]:
        return await self.service.act(env, task_id, await self._actor(event), action, payload)

    async def tool_codex_sessions(self, event: Any, env: str) -> dict[str, Any]:
        return {"sessions": self.service.sessions(env, await self._actor(event))}

    async def llm_environments(self, event: AstrMessageEvent):
        """List environments this actor may access."""
        return json.dumps(await self.tool_codex_environments(event), ensure_ascii=False)

    async def llm_submit(
        self,
        event: AstrMessageEvent,
        env: str,
        workspace: str,
        text: str,
        mode: str = "new",
        thread_id: str = "",
    ):
        """Submit an objective and return its receipt.

        Use codex_task to wait for results or interactions.

        Args:
            env(string): Configured environment name
            workspace(string): Configured workspace alias
            text(string): Objective and acceptance criteria
            mode(string): Either new or resume
            thread_id(string): Owned native thread ID required for resume
        """
        result = await self.tool_codex_submit(event, env, workspace, text, mode, thread_id or None)
        return json.dumps(result, ensure_ascii=False, default=str)

    async def llm_task(
        self,
        event: AstrMessageEvent,
        env: str,
        task_id: str,
        wait_seconds: float = 0,
        cursor: int = 0,
        limit: int = 20,
        interaction_offset: int = 0,
    ):
        """Read an owned task, optionally waiting briefly for a result and progress page.

        Args:
            env(string): Configured environment name
            task_id(string): Task UUID returned by codex_submit
            wait_seconds(number): Wait at most 30 seconds for completion (0 returns immediately)
            cursor(number): Event cursor from the previous task result (default 0)
            limit(number): Maximum progress events in this page (1 to 50)
            interaction_offset(number): Pending-interaction pagination offset
        """
        result = await self.service.wait_task(
            env,
            task_id,
            await self._actor(event),
            wait_seconds,
            cursor=cursor,
            limit=limit,
            interaction_offset=interaction_offset,
        )
        return json.dumps(result, ensure_ascii=False, default=str)

    async def llm_sessions(self, event: AstrMessageEvent, env: str):
        """List owned native Codex sessions.

        Args:
            env(string): Configured environment name
        """
        result = self.service.sessions(env, await self._actor(event))
        return json.dumps({"sessions": result}, ensure_ascii=False)

    async def llm_cancel(self, event: AstrMessageEvent, env: str, task_id: str):
        """Request cancellation of an owned task.

        Args:
            env(string): Configured environment name
            task_id(string): Owned task UUID
        """
        result = await self.service.act(env, task_id, await self._actor(event), "cancel")
        return json.dumps(result, ensure_ascii=False, default=str)

    async def llm_steer(self, event: AstrMessageEvent, env: str, task_id: str, text: str):
        """Add input to the current owned turn.

        Args:
            env(string): Configured environment name
            task_id(string): Owned task UUID
            text(string): Additional text for the current turn
        """
        actor = await self._actor(event)
        task = self.service.task_view(env, task_id, actor)
        result = await self.service.act(
            env,
            task_id,
            actor,
            "steer",
            {"expectedTurnId": task["turn_id"], "input": [{"type": "text", "text": text}]},
        )
        return json.dumps(result, ensure_ascii=False, default=str)

    @filter.command("codex")
    async def codex_command(self, event: AstrMessageEvent) -> Any:
        """Text command surface for task status, results and human interactions."""
        raw = event.get_message_str() if callable(getattr(event, "get_message_str", None)) else ""
        raw = raw.strip()
        if raw.startswith("/"):
            command, _, remainder = raw.partition(" ")
            message = remainder if command.lstrip("/").split("@", 1)[0] == "codex" else raw
        elif raw == "codex" or raw.startswith("codex "):
            message = raw[5:].strip()
        else:
            message = raw
        action, _, rest = message.partition(" ")
        try:
            actor = await self._actor(event)
            if not action or action == "environments":
                response = await self.tool_codex_environments(event)
            elif action == "submit":
                env, workspace, objective = rest.split(maxsplit=2)
                response = await self.service.submit(env, workspace, objective, actor)
            elif action == "resume":
                env, workspace, thread_id, objective = rest.split(maxsplit=3)
                response = await self.service.submit(
                    env, workspace, objective, actor, mode="resume", thread_id=thread_id
                )
            elif action in {"status", "result"}:
                parts = rest.split()
                if len(parts) not in {2, 3}:
                    raise ValueError("expected ENV TASK [OFFSET]")
                env, task_id = parts[:2]
                offset = int(parts[2]) if len(parts) == 3 else 0
                if offset < 0:
                    raise ValueError("offset must be non-negative")
                response = self.service.task_view(
                    env,
                    task_id,
                    actor,
                    interaction_offset=offset if action == "status" else 0,
                    interaction_limit=1,
                )
                return event.plain_result(task_response(response, action, offset))
            elif action == "sessions":
                response = {"sessions": self.service.sessions(rest.strip(), actor)}
            elif action == "cancel":
                env, task_id = rest.split(maxsplit=1)
                response = await self.service.act(env, task_id, actor, "cancel")
            elif action == "steer":
                env, task_id, text = rest.split(maxsplit=2)
                task = self.service.task_view(env, task_id, actor)
                response = await self.service.act(
                    env,
                    task_id,
                    actor,
                    "steer",
                    {"expectedTurnId": task["turn_id"], "input": [{"type": "text", "text": text}]},
                )
            elif action in {"approve", "deny"}:
                args = rest.split(maxsplit=3)
                env, task_id, interaction_id = args[:3]
                decision = (
                    args[3] if len(args) > 3 else ("accept" if action == "approve" else "decline")
                )
                response = await self.service.act(
                    env,
                    task_id,
                    actor,
                    action,
                    {"interactionId": interaction_id, "decision": decision},
                )
            elif action == "answer":
                env, task_id, interaction_id, answer_json = rest.split(maxsplit=3)
                payload = json.loads(answer_json)
                if not isinstance(payload, dict):
                    raise ValueError("answer payload must be a JSON object")
                payload["interactionId"] = interaction_id
                response = await self.service.act(env, task_id, actor, "answer", payload)
            else:
                response = {
                    "usage": (
                        "/codex environments | submit ENV WORKSPACE OBJECTIVE | "
                        "resume ENV WORKSPACE THREAD OBJECTIVE | status ENV TASK [OFFSET] | "
                        "result ENV TASK [OFFSET] | sessions ENV | cancel ENV TASK | "
                        "steer ENV TASK TEXT | "
                        "approve ENV TASK INTERACTION [decision] | deny ENV TASK INTERACTION | "
                        "answer ENV TASK INTERACTION JSON"
                    )
                }
            text = command_response(response)
        except Exception as exc:
            text = command_response({"error": str(exc)})
        return event.plain_result(text)
