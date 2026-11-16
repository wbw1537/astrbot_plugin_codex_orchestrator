"""ACL-enforced orchestration over explicitly configured Codex environments."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from .bounds import bounded_text, bounded_value
from .store import Store
from .transport import JsonRpcClient

MAX_EVENT_PAGE = 50
MAX_EVENT_BYTES = 4096
MAX_EVENT_PAGE_BYTES = 65536
MAX_RESULT_TEXT = 8000
MAX_EVIDENCE_ITEMS = 8


@dataclass(frozen=True)
class Actor:
    platform: str
    umo: str
    user_id: str
    conversation_id: str
    role: str = "user"

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.umo}:{self.user_id}"


@dataclass
class Environment:
    name: str
    client: JsonRpcClient
    workspaces: dict[str, str]
    admins: set[str] = field(default_factory=set)
    allowlist: set[str] = field(default_factory=set)
    sandbox: str = "workspace-write"
    approval: str = "on-request"
    generation: int = 1


class Orchestrator:
    def __init__(self, store: Store, environments: dict[str, Environment]):
        self.store, self.environments = store, environments
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._listeners: dict[tuple[str, str], str] = {}
        self._completion: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._final_text: dict[str, str] = {}
        self._early_events: dict[str, list[tuple[str, Any]]] = {}
        self._interaction_waiters: dict[str, asyncio.Future[Any]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._reservation_events: dict[tuple[str, str], asyncio.Event] = {}
        self._uncertain: dict[tuple[str, str], str] = {}
        for env in environments.values():
            env.client.handlers(
                notification=lambda method, params, e=env: self._notification(e, method, params),
                request=lambda method, params, request_id=None, e=env: self._server_request(
                    e, method, params, request_id
                ),
            )

    def _allowed(self, env: Environment, actor: Actor) -> bool:
        return actor.role == "admin" or actor.key in env.admins or actor.key in env.allowlist

    def _check(self, env_name: str, actor: Actor) -> Environment:
        env = self.environments.get(env_name)
        if env is None or not self._allowed(env, actor):
            raise PermissionError("not authorized for this Codex environment")
        return env

    def _owned(self, env_name: str, task_id: str, actor: Actor) -> dict[str, Any]:
        self._check(env_name, actor)
        task = self.store.task(task_id)
        if (
            not task
            or task["env"] != env_name
            or task["actor"] != actor.key
            or task["cid"] != actor.conversation_id
        ):
            raise PermissionError("task not found or not owned by actor")
        return task

    async def submit(
        self,
        env_name: str,
        workspace: str,
        text: str,
        actor: Actor,
        *,
        mode: str = "new",
        thread_id: str | None = None,
        request_key: str | None = None,
    ) -> dict[str, Any]:
        env = self._check(env_name, actor)
        if not text.strip() or len(text) > 32000 or mode not in {"new", "resume", "queue"}:
            raise ValueError("invalid task text or mode")
        if workspace not in env.workspaces:
            raise ValueError("unknown workspace alias")
        if mode == "resume" and not thread_id:
            raise ValueError("resume requires an owned thread")
        if thread_id and mode != "resume":
            raise ValueError("thread_id is valid only with explicit resume mode")
        if thread_id:
            owned = self.store.db.execute(
                "SELECT 1 FROM tasks WHERE env=? AND actor=? AND cid=? "
                "AND workspace=? AND thread_id=?",
                (env_name, actor.key, actor.conversation_id, workspace, thread_id),
            ).fetchone()
            if not owned:
                raise PermissionError("thread is not owned in this workspace")
        task = self.store.create_task(
            env=env_name,
            actor=actor.key,
            platform=actor.platform,
            umo=actor.umo,
            cid=actor.conversation_id,
            workspace=workspace,
            text=text,
            request_key=request_key,
            mode="new" if mode == "queue" else mode,
            parent_thread_id=thread_id,
        )
        if task["status"] != "submitted" or task["thread_id"]:
            return task
        lock = self._locks.setdefault((env_name, workspace), asyncio.Lock())
        reservation_event = self._reservation_events.get((env_name, workspace))
        if reservation_event is None:
            reservation_event = asyncio.Event()
            reservation_event.set()
            self._reservation_events[(env_name, workspace)] = reservation_event
        if not reservation_event.is_set() or lock.locked():
            self.store.update(task["id"], status="queued")
            work = self._run(task["id"], env, actor, workspace, mode, thread_id, lock)
        else:
            work = self._run(task["id"], env, actor, workspace, mode, thread_id, lock)
        job = asyncio.create_task(work)
        self._tasks.add(job)
        job.add_done_callback(self._tasks.discard)
        return self.store.task(task["id"]) or task

    def _effective_policy_matches(self, response: Any, env: Environment, workspace: str) -> bool:
        if not isinstance(response, dict):
            return False
        cwd = env.workspaces[workspace]
        sandbox = response.get("sandbox")
        expected_type = {
            "workspace-write": "workspaceWrite",
            "read-only": "readOnly",
            "danger-full-access": "dangerFullAccess",
        }.get(env.sandbox)
        if response.get("cwd") != cwd or response.get("approvalPolicy") != env.approval:
            return False
        if response.get("approvalsReviewer") != "user" or not isinstance(sandbox, dict):
            return False
        if sandbox.get("type") != expected_type:
            return False
        roots = sandbox.get("writableRoots")
        if env.sandbox == "workspace-write":
            if not isinstance(roots, list) or any(root != cwd for root in roots):
                return False
            # Codex treats the thread cwd as writable implicitly; empty roots is native-realistic.
            if sandbox.get("networkAccess") is not False:
                return False
        return True

    async def _run(
        self,
        task_id: str,
        env: Environment,
        actor: Actor,
        workspace: str,
        mode: str,
        thread_id: str | None,
        lock: asyncio.Lock,
    ) -> None:
        key = (env.name, workspace)
        reservation = self._reservation_events.get(key)
        if reservation is None:
            reservation = asyncio.Event()
            reservation.set()
            self._reservation_events[key] = reservation
        elif key not in self._uncertain and not reservation.is_set():
            reservation.set()
        await reservation.wait()
        async with lock:
            task = self.store.task(task_id)
            if not task or task["status"] == "interrupted":
                return
            self.store.update(task_id, status="running")
            try:
                if mode == "resume":
                    try:
                        value = await env.client.request(
                            "thread/resume",
                            {
                                "threadId": thread_id,
                                "cwd": env.workspaces[workspace],
                                "approvalPolicy": env.approval,
                                "approvalsReviewer": "user",
                                "sandbox": env.sandbox,
                            },
                            timeout=30,
                        )
                    except Exception as exc:
                        self.store.update(
                            task_id, status="failed", error=f"owned thread resume failed: {exc}"
                        )
                        return
                    tid = value.get("thread", {}).get("id") if isinstance(value, dict) else None
                    if tid != thread_id or not self._effective_policy_matches(
                        value, env, workspace
                    ):
                        self.store.update(
                            task_id,
                            status="failed",
                            error="resumed thread identity or effective policy mismatch",
                        )
                        return
                    thread_status = value.get("thread", {}).get("status", {}).get("type")
                    if thread_status != "idle":
                        self.store.update(
                            task_id,
                            status="failed",
                            error="owned native thread is active or unavailable",
                        )
                        return
                else:
                    # Persist dispatch intent before side effects; restart never replays this phase.
                    self.store.update(task_id, thread_start_phase="dispatching")
                    # Native thread/start has no idempotency key. Lost response is never retried.
                    try:
                        value = await env.client.request(
                            "thread/start",
                            {
                                "cwd": env.workspaces[workspace],
                                "approvalPolicy": env.approval,
                                "approvalsReviewer": "user",
                                "sandbox": env.sandbox,
                            },
                            timeout=30,
                        )
                    except Exception as exc:
                        key = (env.name, workspace)
                        self._uncertain[key] = task_id
                        self._reservation_events.setdefault(key, asyncio.Event()).clear()
                        self.store.update(task_id, status="submission_unknown", error=str(exc))
                        return
                    tid = value.get("thread", {}).get("id") if isinstance(value, dict) else None
                    if not tid:
                        self.store.update(
                            task_id,
                            status="submission_unknown",
                            error="thread/start response missing id",
                        )
                        return
                    if not self._effective_policy_matches(value, env, workspace):
                        self.store.update(
                            task_id,
                            status="failed",
                            error="native effective cwd/policy differs from configured policy",
                        )
                        return
                self.store.update(task_id, thread_id=tid)
                self._listeners[(env.name, tid)] = task_id
                waiter = asyncio.get_running_loop().create_future()
                self._completion[task_id] = waiter
                if self.store.task(task_id)["status"] in {"completed", "failed", "interrupted"}:
                    self._completion.pop(task_id, None)
                    return
                self.store.update(task_id, turn_start_phase="dispatching")
                try:
                    turns = await env.client.request(
                        "turn/start",
                        {
                            "threadId": tid,
                            "clientUserMessageId": task_id,
                            "input": [{"type": "text", "text": task["input"]}],
                        },
                        timeout=45,
                    )
                except Exception as exc:
                    key = (env.name, workspace)
                    self._uncertain[key] = task_id
                    self._reservation_events.setdefault(key, asyncio.Event()).clear()
                    self.store.update(task_id, status="submission_unknown", error=str(exc))
                    self._completion.pop(task_id, None)
                    return
                turn = turns.get("turn", {}) if isinstance(turns, dict) else {}
                turn_id = turn.get("id")
                self.store.update(task_id, turn_id=turn_id, turn_start_phase="confirmed")
                current = self.store.task(task_id)
                if current["status"] not in {"completed", "failed", "interrupted"}:
                    self.store.update(task_id, status="running")
                # Apply notifications buffered while turn/start was awaiting its native ID.
                for method, params in self._early_events.pop(task_id, []):
                    event_turn_id = params.get("turnId") or params.get("turn", {}).get("id")
                    if event_turn_id == turn_id:
                        await self._notification(env, method, params)
                if turn.get("status") in {"completed", "failed", "interrupted"}:
                    self._finish(task_id, turn)
                elif not waiter.done():
                    await waiter
                self._completion.pop(task_id, None)
            except Exception as exc:
                self.store.update(task_id, status="submission_unknown", error=str(exc))

    async def _notification(self, env: Environment, method: str, params: Any) -> None:
        if not isinstance(params, dict):
            return
        if method == "serverRequest/resolved":
            native_key = ":".join(
                (
                    env.name,
                    str(env.generation),
                    str(params.get("threadId", "")),
                    str(params.get("requestId")),
                )
            )
            rows = self.store.db.execute(
                "SELECT id FROM interactions WHERE request_id=? AND state='pending'", (native_key,)
            ).fetchall()
            for row in rows:
                self.store.db.execute(
                    "UPDATE interactions SET state='expired' WHERE id=?", (row["id"],)
                )
                fut = self._interaction_waiters.pop(row["id"], None)
                if fut and not fut.done():
                    fut.set_exception(ValueError("native interaction expired"))
            return
        tid = params.get("threadId") or params.get("thread", {}).get("id")
        task_id = self._listeners.get((env.name, tid))
        if not task_id:
            return
        task = self.store.task(task_id)
        if not task:
            return
        turn_id = params.get("turnId") or params.get("turn", {}).get("id")
        if not task["turn_id"]:
            self._early_events.setdefault(task_id, []).append((method, params))
            return
        if turn_id and task["turn_id"] != turn_id:
            return
        self.store.event(task_id, {"method": method, "params": params})
        if method == "item/completed":
            item = params.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agentMessage" and item.get("text"):
                self._final_text[task_id] = str(item["text"])
        if method.endswith("turn/completed"):
            self._finish(task_id, params.get("turn", params))

    def _finish(self, task_id: str, turn: dict[str, Any]) -> None:
        status = turn.get("status")
        if status not in {"completed", "failed", "interrupted"}:
            return
        current = self.store.task(task_id)
        if (
            current
            and current["status"] in {"completed", "failed", "interrupted"}
            and current["result"]
        ):
            return
        canonical_messages = [
            item.get("text", "")
            for item in turn.get("items", [])
            if item.get("type") == "agentMessage" and item.get("phase") == "final_answer"
        ]
        final_text = (
            turn.get("finalText")
            or (canonical_messages[-1] if canonical_messages else "")
            or self._final_text.get(task_id, "")
        )
        final_bytes = len(final_text.encode("utf-8")) if isinstance(final_text, str) else 0
        text_truncated = final_bytes > MAX_RESULT_TEXT
        if text_truncated:
            final_text = bounded_text(final_text, MAX_RESULT_TEXT)
        result = {
            "turn": {key: turn[key] for key in ("id", "status") if key in turn},
            "finalText": final_text,
            "nativeEvidence": self._native_evidence(task_id),
        }
        if turn.get("error") is not None:
            result["turn"]["error"] = self._bounded_value(turn["error"], 2048)
        if text_truncated:
            result["finalTextTruncated"] = True
            result["finalTextOriginalBytes"] = final_bytes
        self.store.finish(task_id, status, result, turn.get("error"))
        key = (current["env"], current["workspace"]) if current else None
        if key:
            if self._uncertain.get(key) == task_id:
                self._uncertain.pop(key, None)
            self._reservation_events.setdefault(key, asyncio.Event()).set()
        waiter = self._completion.get(task_id)
        if waiter and not waiter.done():
            waiter.set_result(result)

    @staticmethod
    def _bounded_value(value: Any, max_bytes: int = 8192) -> Any:
        return bounded_value(value, max_bytes)

    def _native_evidence(self, task_id: str) -> dict[str, Any]:
        """Summarize observed App Server evidence without mixing it into model text."""
        rows = self.store.db.execute(
            "SELECT seq,payload FROM events "
            "WHERE task_id=? AND json_extract(payload,'$.method') IN (?,?,?,?,?) "
            "ORDER BY seq DESC LIMIT ?",
            (
                task_id,
                "turn/plan/updated",
                "turn/diff/updated",
                "thread/tokenUsage/updated",
                "item/completed",
                "item/historySnapshot",
                501,
            ),
        ).fetchall()
        evidence: dict[str, Any] = {}
        command_items: list[dict[str, Any]] = []
        for row in reversed(rows):
            record = json.loads(row["payload"])
            method, params = record.get("method"), record.get("params", {})
            if method == "turn/plan/updated":
                evidence["plan"] = {
                    "turnId": params.get("turnId"),
                    "eventCursor": row["seq"],
                    "explanation": self._bounded_value(params.get("explanation"), 2048),
                    "steps": self._bounded_value(params.get("plan", []), 8192),
                }
            elif method == "turn/diff/updated":
                evidence["diff"] = {
                    "turnId": params.get("turnId"),
                    "eventCursor": row["seq"],
                    "content": self._bounded_value(params.get("diff", ""), 8192),
                }
            elif method == "thread/tokenUsage/updated":
                evidence["usage"] = {
                    "turnId": params.get("turnId"),
                    "eventCursor": row["seq"],
                    "tokenUsage": self._bounded_value(params.get("tokenUsage", {}), 4096),
                }
            elif method in {"item/completed", "item/historySnapshot"}:
                item = params.get("item", {})
                if isinstance(item, dict) and item.get("type") == "commandExecution":
                    observed = {}
                    for key in (
                        "id",
                        "status",
                        "command",
                        "exitCode",
                        "durationMs",
                        "aggregatedOutput",
                    ):
                        if key in item:
                            max_bytes = 1024 if key in {"command", "aggregatedOutput"} else 512
                            observed[key] = self._bounded_value(item[key], max_bytes)
                    command_items.append(
                        {
                            "source": (
                                "native_item_completed"
                                if method == "item/completed"
                                else "native_history_item"
                            ),
                            "eventCursor": row["seq"],
                            "turnId": params.get("turnId"),
                            "observed": observed,
                        }
                    )
        if command_items:
            clipped = command_items[-MAX_EVIDENCE_ITEMS:]
            evidence["commandExecutions"] = clipped
            if len(command_items) > MAX_EVIDENCE_ITEMS:
                evidence["commandExecutionsTruncated"] = True
                evidence["commandExecutionsOmitted"] = len(command_items) - MAX_EVIDENCE_ITEMS
        if len(rows) == 501:
            evidence["evidenceHistoryTruncated"] = True
        return evidence

    async def _server_request(
        self, env: Environment, method: str, params: Any, request_id: Any = None
    ) -> Any:
        if not isinstance(params, dict):
            raise PermissionError("invalid native request")
        tid = params.get("threadId")
        task_id = self._listeners.get((env.name, tid))
        if not task_id:
            raise PermissionError("request is not associated with an owned active task")
        supported = {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "item/tool/requestUserInput",
            "mcpServer/elicitation/request",
        }
        if method not in supported:
            raise PermissionError("unsupported native request")
        if method == "item/tool/requestUserInput" and any(
            question.get("isSecret") is True
            for question in params.get("questions", [])
            if isinstance(question, dict)
        ):
            self.store.event(
                task_id,
                {"warning": "Secret questions require local Codex UI; remote chat was not used."},
            )
            return {"answers": {}}
        serialized = json.dumps(params, sort_keys=True)
        existing = next(
            (
                row
                for row in self.store.db.execute(
                    "SELECT id,state,params,response FROM interactions "
                    "WHERE task_id=? AND method=? ORDER BY rowid DESC",
                    (task_id, method),
                )
                if row["params"] == serialized
            ),
            None,
        )
        if existing and existing["state"] == "resolved":
            return json.loads(existing["response"] or "{}")
        if existing and existing["state"] == "pending":
            interaction_id = existing["id"]
            self.store.db.execute(
                "UPDATE interactions SET generation=?,request_id=? WHERE id=?",
                (
                    env.generation,
                    f"{env.name}:{env.generation}:{tid}:{request_id}"
                    if request_id is not None
                    else None,
                    interaction_id,
                ),
            )
        else:
            interaction_id = str(uuid.uuid4())
            self.store.db.execute(
                "INSERT INTO interactions("
                "id,task_id,generation,method,params,state,response,request_id) "
                "VALUES(?,?,?,?,?,?,NULL,?)",
                (
                    interaction_id,
                    task_id,
                    env.generation,
                    method,
                    serialized,
                    "pending",
                    f"{env.name}:{env.generation}:{tid}:{request_id}"
                    if request_id is not None
                    else None,
                ),
            )
            self.store.event(
                task_id, {"interaction": interaction_id, "method": method, "params": params}
            )
        future = self._interaction_waiters.get(interaction_id)
        if not future or future.done():
            future = asyncio.get_running_loop().create_future()
            self._interaction_waiters[interaction_id] = future
        return await future

    @staticmethod
    def _validate_mcp_content(params: dict[str, Any], content: Any) -> dict[str, Any]:
        if params.get("mode") != "form" or not isinstance(content, dict):
            raise ValueError("only schema-backed MCP form elicitation can be accepted")
        schema = params.get("requestedSchema")
        if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
            raise ValueError("unsupported MCP form schema")
        fields = schema["properties"]
        if set(content) - set(fields) or set(schema.get("required", [])) - set(content):
            raise ValueError("MCP form content has missing or unknown fields")
        for name, value in content.items():
            field = fields[name]
            if not isinstance(field, dict):
                raise ValueError(f"unsupported field schema for {name}")
            supported_keys = {
                "type",
                "title",
                "description",
                "default",
                "enum",
                "oneOf",
                "minimum",
                "maximum",
                "minLength",
                "maxLength",
                "items",
                "minItems",
                "maxItems",
            }
            if set(field) - supported_keys:
                raise ValueError(f"unsupported schema constraint for {name}")
            if field.get("type") not in {"string", "number", "integer", "boolean", "array"}:
                raise ValueError(f"unsupported schema type for {name}")
            if isinstance(field.get("enum"), list) and value not in field["enum"]:
                raise ValueError(f"invalid choice for {name}")
            if isinstance(field.get("oneOf"), list) and value not in [
                x.get("const") for x in field["oneOf"] if isinstance(x, dict)
            ]:
                raise ValueError(f"invalid choice for {name}")
            if field.get("type") == "string":
                if not isinstance(value, str):
                    raise ValueError(f"{name} must be text")
                if len(value) < field.get("minLength", 0) or len(value) > field.get(
                    "maxLength", float("inf")
                ):
                    raise ValueError(f"{name} has invalid text length")
            if field.get("type") in {"number", "integer"}:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or (field.get("type") == "integer" and not isinstance(value, int))
                ):
                    raise ValueError(f"{name} must be numeric")
                if value < field.get("minimum", float("-inf")) or value > field.get(
                    "maximum", float("inf")
                ):
                    raise ValueError(f"{name} is outside its allowed range")
            if field.get("type") == "boolean" and not isinstance(value, bool):
                raise ValueError(f"{name} must be boolean")
            if isinstance(field.get("items"), dict):
                if not isinstance(value, list):
                    raise ValueError(f"{name} must be a list")
                if len(value) < field.get("minItems", 0) or len(value) > field.get(
                    "maxItems", float("inf")
                ):
                    raise ValueError(f"{name} has an invalid number of selections")
                item_enum = field["items"].get("enum")
                if item_enum and any(item not in item_enum for item in value):
                    raise ValueError(f"invalid selection for {name}")
        return content

    async def act(
        self, env_name: str, task_id: str, actor: Actor, action: str, payload: Any = None
    ) -> dict[str, Any]:
        task = self._owned(env_name, task_id, actor)
        env = self.environments[env_name]
        if action == "cancel":
            if task["status"] in {"queued", "submitted"} and not task["thread_id"]:
                self.store.update(
                    task_id, status="interrupted", error="cancelled before native submission"
                )
                return self.task_view(env_name, task_id, actor)
            if not task["thread_id"] or not task["turn_id"]:
                raise ValueError("task has no confirmed active turn")
            await env.client.request(
                "turn/interrupt",
                {"threadId": task["thread_id"], "turnId": task["turn_id"]},
                timeout=10,
            )
            current = self.store.task(task_id)
            if current["status"] not in {"completed", "failed", "interrupted"}:
                self.store.update(task_id, status="cancelling")
        elif action == "steer":
            if not isinstance(payload, dict) or payload.get("expectedTurnId") != task["turn_id"]:
                raise ValueError("steer requires the current expectedTurnId")
            await env.client.request(
                "turn/steer",
                {
                    "threadId": task["thread_id"],
                    "expectedTurnId": task["turn_id"],
                    "input": payload["input"],
                },
            )
        elif action in {"answer", "approve", "deny"}:
            interaction_id = payload.get("interactionId") if isinstance(payload, dict) else None
            row = self.store.db.execute(
                "SELECT * FROM interactions WHERE id=? AND task_id=?", (interaction_id, task_id)
            ).fetchone()
            if not row or row["state"] != "pending" or row["generation"] != env.generation:
                raise ValueError("interaction is expired, already handled, or unknown")
            native = json.loads(row["params"])
            if row["method"] in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                decision = payload.get("decision") if isinstance(payload, dict) else None
                allowed = (
                    {"accept", "acceptForSession", "decline", "cancel"}
                    if row["method"] == "item/commandExecution/requestApproval"
                    else {"accept", "decline", "cancel"}
                )
                offered = native.get("availableDecisions")
                if isinstance(offered, list) and decision not in offered:
                    raise ValueError("decision was not offered by native App Server")
                if decision not in allowed:
                    raise ValueError("unsupported decision")
                result = {"decision": decision}
            elif row["method"] == "item/permissions/requestApproval":
                if action != "deny":
                    raise ValueError("permission grants are not enabled in this preview")
                result = {
                    "permissions": {"fileSystem": {"entries": []}, "network": {"enabled": False}},
                    "scope": "turn",
                }
            elif row["method"] == "item/tool/requestUserInput":
                answers = payload.get("answers") if isinstance(payload, dict) else None
                expected = {
                    q["id"]
                    for q in native.get("questions", [])
                    if isinstance(q, dict) and "id" in q
                }
                if action != "answer" or not isinstance(answers, dict) or set(answers) != expected:
                    raise ValueError("answers must cover exactly the requested question IDs")
                result = {
                    "answers": {
                        k: {"answers": v if isinstance(v, list) else [str(v)]}
                        for k, v in answers.items()
                    }
                }
            elif row["method"] == "mcpServer/elicitation/request":
                choice = payload.get("action") if isinstance(payload, dict) else None
                if action == "deny":
                    choice = "decline"
                if choice not in {"accept", "decline", "cancel"}:
                    raise ValueError("unsupported elicitation choice")
                result = {"action": choice}
                if choice == "accept":
                    result["content"] = self._validate_mcp_content(
                        native, payload.get("content", {})
                    )
            else:
                raise ValueError("unsupported interaction")
            self.store.db.execute(
                "UPDATE interactions SET state='resolved',response=? WHERE id=?",
                (json.dumps(result), interaction_id),
            )
            fut = self._interaction_waiters.pop(interaction_id, None)
            if not fut or fut.done():
                raise ValueError("native interaction is no longer waiting")
            fut.set_result(result)
        else:
            raise ValueError("unsupported action")
        return self.store.task(task_id) or task

    def task_view(
        self,
        env_name: str,
        task_id: str,
        actor: Actor,
        *,
        cursor: int = 0,
        limit: int = 20,
        interaction_offset: int = 0,
        interaction_limit: int = 20,
    ) -> dict[str, Any]:
        task = self._owned(env_name, task_id, actor)
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError("cursor must be a non-negative integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_EVENT_PAGE
        ):
            raise ValueError(f"limit must be between 1 and {MAX_EVENT_PAGE}")
        if (
            isinstance(interaction_offset, bool)
            or not isinstance(interaction_offset, int)
            or interaction_offset < 0
        ):
            raise ValueError("interaction_offset must be a non-negative integer")
        if (
            isinstance(interaction_limit, bool)
            or not isinstance(interaction_limit, int)
            or not 1 <= interaction_limit <= 20
        ):
            raise ValueError("interaction_limit must be between 1 and 20")
        result = self._parse_json_value(task["result"])
        error = self._parse_json_value(task["error"])
        view = {
            k: task[k]
            for k in (
                "id",
                "env",
                "workspace",
                "thread_id",
                "turn_id",
                "status",
                "created",
                "updated",
            )
        }
        view["result"] = result
        view["error"] = self._bounded_value(error, 2048) if error is not None else None
        evidence = self._native_evidence(task_id)
        if evidence:
            view["nativeEvidence"] = evidence
        view["events"] = self.store.events_page(
            task_id,
            cursor=cursor,
            limit=limit,
            max_event_bytes=MAX_EVENT_BYTES,
            max_page_bytes=MAX_EVENT_PAGE_BYTES,
        )
        pending = self.store.db.execute(
            "SELECT id,method,params FROM interactions "
            "WHERE task_id=? AND state='pending' ORDER BY rowid",
            (task_id,),
        ).fetchall()
        view["interactions"] = []
        end = interaction_offset + interaction_limit
        for row in pending[interaction_offset:end]:
            native = json.loads(row["params"])
            item = {
                "id": row["id"],
                "method": row["method"],
                "params": self._bounded_value(native, 2048),
            }
            if isinstance(native.get("availableDecisions"), list):
                supported = {"accept", "decline", "cancel"}
                if row["method"] == "item/commandExecution/requestApproval":
                    supported.add("acceptForSession")
                item["availableDecisions"] = list(
                    dict.fromkeys(
                        value
                        for value in native["availableDecisions"]
                        if isinstance(value, str) and value in supported
                    )
                )
            view["interactions"].append(item)
        more = len(pending) > end
        view["interactionsPage"] = {
            "offset": interaction_offset,
            "nextOffset": end if more else None,
            "hasMore": more,
            "total": len(pending),
        }
        if more:
            view["interactionsTruncated"] = True
            view["interactionsOmitted"] = len(pending) - end
        return view

    @staticmethod
    def _parse_json_value(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

    async def wait_task(
        self,
        env_name: str,
        task_id: str,
        actor: Actor,
        wait_seconds: float = 0,
        *,
        cursor: int = 0,
        limit: int = 20,
        interaction_offset: int = 0,
    ) -> dict[str, Any]:
        self._owned(env_name, task_id, actor)
        if (
            isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, (int, float))
            or not 0 <= wait_seconds <= 30
        ):
            raise ValueError("wait_seconds must be between 0 and 30")
        deadline = asyncio.get_running_loop().time() + float(wait_seconds)
        terminal = {"completed", "failed", "interrupted"}
        while wait_seconds:
            task = self.store.task(task_id)
            if not task or task["status"] in terminal:
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            waiter = self._completion.get(task_id)
            if waiter:
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), remaining)
                except asyncio.TimeoutError:
                    break
            else:
                await asyncio.sleep(min(0.05, remaining))
        return self.task_view(
            env_name,
            task_id,
            actor,
            cursor=cursor,
            limit=limit,
            interaction_offset=interaction_offset,
        )

    def sessions(self, env_name: str, actor: Actor) -> list[dict[str, Any]]:
        self._check(env_name, actor)
        return [
            {"threadId": row["thread_id"], "workspace": row["workspace"], "taskId": row["id"]}
            for row in self.store.db.execute(
                "SELECT * FROM tasks WHERE env=? AND actor=? AND cid=? ORDER BY created DESC",
                (env_name, actor.key, actor.conversation_id),
            )
            if row["thread_id"]
        ]

    async def _hold_recovered(self, task: dict[str, Any], env: Environment) -> None:
        key = (env.name, task["workspace"])
        lock = self._locks.setdefault(key, asyncio.Lock())
        ready = asyncio.Event()

        async def hold() -> None:
            async with lock:
                waiter = asyncio.get_running_loop().create_future()
                self._completion[task["id"]] = waiter
                ready.set()
                await waiter
                self._completion.pop(task["id"], None)

        job = asyncio.create_task(hold())
        self._tasks.add(job)
        job.add_done_callback(self._tasks.discard)
        await ready.wait()

    async def recover(self, env_name: str | None = None) -> None:
        """Reconcile history before restoring writers and known-safe queued work."""
        rows = self.store.db.execute(
            "SELECT * FROM tasks WHERE status IN "
            "('submitted','queued','running','waiting_approval','waiting_input',"
            "'reconnecting','submission_unknown','cancelling') ORDER BY created"
        ).fetchall()
        queued: list[tuple[dict[str, Any], Environment]] = []
        active: list[tuple[dict[str, Any], Environment]] = []
        for raw in rows:
            task = dict(raw)
            env = self.environments.get(task["env"])
            if (env_name and task["env"] != env_name) or not env:
                continue
            key = (env.name, task["workspace"])
            authorized = task["actor"] in env.admins or task["actor"] in env.allowlist
            if (
                task["status"] in {"queued", "submitted"}
                and not task["thread_id"]
                and task["thread_start_phase"] == "not_started"
            ):
                if authorized:
                    queued.append((task, env))
                else:
                    self.store.update(
                        task["id"],
                        status="failed",
                        error="queued task owner is no longer authorized",
                    )
                continue
            self._reservation_events.setdefault(key, asyncio.Event()).clear()
            if not authorized:
                self._uncertain[key] = task["id"]
                self.store.update(
                    task["id"],
                    status="submission_unknown",
                    error="workspace reserved; owner ACL must be restored before reconciliation",
                )
                continue
            if task["thread_id"]:
                active.append((task, env))
            else:
                self._uncertain[key] = task["id"]
                self.store.update(
                    task["id"],
                    status="submission_unknown",
                    error="submission intent requires native reconciliation",
                )
        for task, env in active:
            key = (env.name, task["workspace"])
            try:
                resumed = await env.client.request(
                    "thread/resume",
                    {
                        "threadId": task["thread_id"],
                        "cwd": env.workspaces[task["workspace"]],
                        "approvalPolicy": env.approval,
                        "approvalsReviewer": "user",
                        "sandbox": env.sandbox,
                    },
                )
                if resumed.get("thread", {}).get("id") != task[
                    "thread_id"
                ] or not self._effective_policy_matches(resumed, env, task["workspace"]):
                    raise ValueError("recovered thread identity/effective policy mismatch")
                turns, cursor = [], None
                for _ in range(20):
                    page = await env.client.request(
                        "thread/turns/list",
                        {"threadId": task["thread_id"], "limit": 100, "cursor": cursor},
                    )
                    turns.extend(page.get("data", []))
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                matching = next((turn for turn in turns if turn.get("id") == task["turn_id"]), None)
                if not matching and task["status"] == "submission_unknown":
                    for candidate in turns:
                        items, item_cursor = [], None
                        for _ in range(20):
                            page = await env.client.request(
                                "thread/items/list",
                                {
                                    "threadId": task["thread_id"],
                                    "turnId": candidate.get("id"),
                                    "limit": 100,
                                    "cursor": item_cursor,
                                },
                            )
                            items.extend(page.get("data", []))
                            item_cursor = page.get("nextCursor")
                            if not item_cursor:
                                break
                        if any(
                            item.get("type") == "userMessage" and item.get("clientId") == task["id"]
                            for item in items
                        ):
                            matching = candidate
                            self.store.update(task["id"], turn_id=candidate.get("id"))
                            break
                if matching and matching.get("status") in {"completed", "failed", "interrupted"}:
                    items, cursor = [], None
                    for _ in range(20):
                        page = await env.client.request(
                            "thread/items/list",
                            {
                                "threadId": task["thread_id"],
                                "turnId": matching.get("id"),
                                "limit": 100,
                                "cursor": cursor,
                            },
                        )
                        items.extend(page.get("data", []))
                        cursor = page.get("nextCursor")
                        if not cursor:
                            break
                    # Turns may carry a canonical snapshot even when the item page is empty.
                    items = items or matching.get("items", [])
                    canonical = [
                        item.get("text", "")
                        for item in items
                        if item.get("type") == "agentMessage"
                        and item.get("phase") == "final_answer"
                    ]
                    if canonical:
                        matching["finalText"] = canonical[-1]
                    for item in items:
                        self.store.event(
                            task["id"],
                            {
                                "method": "item/historySnapshot",
                                "params": {
                                    "threadId": task["thread_id"],
                                    "turnId": matching.get("id"),
                                    "item": item,
                                },
                            },
                        )
                    self._finish(task["id"], matching)
                    self._reservation_events.setdefault(key, asyncio.Event()).set()
                    self._uncertain.pop(key, None)
                elif matching:
                    self._listeners[(env.name, task["thread_id"])] = task["id"]
                    self.store.update(task["id"], turn_id=matching.get("id"), status="running")
                    if task["id"] not in self._completion:
                        await self._hold_recovered(task, env)
                elif task["turn_start_phase"] == "not_started":
                    # A confirmed thread with no turn dispatch can safely re-enter the local queue.
                    self.store.update(
                        task["id"],
                        status="queued",
                        mode="resume",
                        parent_thread_id=task["thread_id"],
                        thread_start_phase="confirmed",
                    )
                    self._uncertain.pop(key, None)
                    self._reservation_events.setdefault(key, asyncio.Event()).set()
                    queued.append(
                        (
                            {
                                **task,
                                "status": "queued",
                                "mode": "resume",
                                "parent_thread_id": task["thread_id"],
                            },
                            env,
                        )
                    )
                else:
                    self._uncertain[key] = task["id"]
                    self.store.update(
                        task["id"],
                        status="submission_unknown",
                        error="native history did not prove whether the turn was submitted",
                    )
            except Exception as exc:
                self._uncertain[key] = task["id"]
                self.store.update(task["id"], status="reconnecting", error=str(exc))
        # Queue records are safe to execute only when no ambiguous intent reserves this workspace.
        for task, env in queued:
            key = (env.name, task["workspace"])
            lock = self._locks.setdefault(key, asyncio.Lock())
            actor = Actor(
                task["platform"], task["umo"], task["actor"].split(":", 2)[-1], task["cid"]
            )
            job = asyncio.create_task(
                self._run(
                    task["id"],
                    env,
                    actor,
                    task["workspace"],
                    task["mode"],
                    task["parent_thread_id"],
                    lock,
                )
            )
            self._tasks.add(job)
            job.add_done_callback(self._tasks.discard)

    def mark_disconnected(self, env_name: str) -> None:
        rows = self.store.db.execute(
            "SELECT id,workspace FROM tasks WHERE env=? AND status IN "
            "('running','waiting_approval','waiting_input','cancelling')",
            (env_name,),
        ).fetchall()
        for row in rows:
            key = (env_name, row["workspace"])
            self._reservation_events.setdefault(key, asyncio.Event()).clear()
            self._uncertain[key] = row["id"]
            self.store.update(
                row["id"],
                status="reconnecting",
                error="native transport disconnected; reconciling before queue resume",
            )

    async def replace_client(self, env_name: str, client: JsonRpcClient) -> None:
        env = self.environments[env_name]
        previous = env.client
        if previous is not client:
            await previous.close()
        env.client = client
        env.generation += 1
        client.handlers(
            notification=lambda method, params, e=env: self._notification(e, method, params),
            request=lambda method, params, request_id=None, e=env: self._server_request(
                e, method, params, request_id
            ),
        )

    async def close(self) -> None:
        for job in self._tasks:
            job.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.store.db.execute(
            "UPDATE tasks SET status='reconnecting', "
            "error='plugin shutdown; remote turn may continue',updated=strftime('%s','now') "
            "WHERE status IN ('running','waiting_approval','waiting_input','cancelling')"
        )
        for env in self.environments.values():
            await env.client.close()
