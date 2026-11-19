"""Task lifecycle regressions using a fixture at the native server boundary."""

import asyncio

import pytest

from codex_orchestrator.service import Actor, Environment, Orchestrator
from codex_orchestrator.store import Store


class NativeServer:
    def __init__(self, early=False, broaden=False, reviewer_override=None):
        self.calls = []
        self.notify = None
        self.human = None
        self.thread = 0
        self.turn = 0
        self.early = early
        self.broaden = broaden
        self.reviewer_override = reviewer_override

    def handlers(self, *, notification=None, request=None):
        self.notify = notification
        self.human = request

    async def request(self, method, params=None, **kwargs):
        self.calls.append((method, params))
        if method == "thread/start":
            self.thread += 1
            return {
                "thread": {"id": f"thread-{self.thread}", "status": {"type": "idle"}, "turns": []},
                "cwd": params["cwd"],
                "approvalPolicy": "on-request",
                "approvalsReviewer": self.reviewer_override or params["approvalsReviewer"],
                "sandbox": {
                    "type": "dangerFullAccess" if self.broaden else "workspaceWrite",
                    "writableRoots": [],
                    "networkAccess": False,
                    "excludeTmpdirEnvVar": False,
                    "excludeSlashTmp": False,
                },
            }
        if method == "turn/start":
            self.turn += 1
            t = {"id": f"turn-{self.turn}", "status": "inProgress", "items": []}
            if self.early:
                await self.notify(
                    "item/completed",
                    {
                        "threadId": params["threadId"],
                        "turnId": t["id"],
                        "item": {
                            "id": "message-1",
                            "type": "agentMessage",
                            "text": "canonical final",
                            "phase": "final_answer",
                        },
                    },
                )
                await self.notify(
                    "turn/completed",
                    {"threadId": params["threadId"], "turn": dict(t, status="completed")},
                )
            return {"turn": t}
        if method == "turn/interrupt":
            return {}
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        if method == "thread/resume":
            return {
                "thread": {"id": params["threadId"], "status": {"type": "idle"}, "turns": []},
                "cwd": params.get("cwd", "/tmp/project"),
                "approvalPolicy": "on-request",
                "approvalsReviewer": self.reviewer_override or params["approvalsReviewer"],
                "sandbox": {
                    "type": "workspaceWrite",
                    "writableRoots": [],
                    "networkAccess": False,
                    "excludeTmpdirEnvVar": False,
                    "excludeSlashTmp": False,
                },
            }
        if method in {"thread/turns/list", "thread/items/list"}:
            return {"data": [], "nextCursor": None}
        raise AssertionError("unexpected RPC " + method)

    async def close(self):
        pass


async def make(tmp_path, *, reviewer="auto_review", **kwargs):
    client = NativeServer(**kwargs)
    store = Store(str(tmp_path / "tasks.sqlite"))
    a = Actor("bot", "bot:GroupMessage:one", "alice", "cid-1")
    b = Actor("bot", "bot:GroupMessage:one", "bob", "cid-1")
    env = Environment(
        "dev",
        client,
        {"repo": "/tmp/project", "other": "/tmp/other"},
        allowlist={a.key, b.key},
        approvals_reviewer=reviewer,
    )
    return Orchestrator(store, {"dev": env}), client, store, a, b


@pytest.mark.asyncio
async def test_request_key_is_scoped_to_actor(tmp_path):
    svc, server, store, a, b = await make(tmp_path)
    try:
        first = await svc.submit("dev", "repo", "alice private task", a, request_key="same-key")
        second = await svc.submit("dev", "repo", "bob task", b, request_key="same-key")
        assert second["id"] != first["id"], "request_key collision returned another user task"
    finally:
        await svc.close()
        store.close()


@pytest.mark.asyncio
async def test_workspace_exclusivity_lasts_until_terminal(tmp_path):
    svc, server, store, a, b = await make(tmp_path)
    try:
        await svc.submit("dev", "repo", "first writes", a)
        await asyncio.sleep(0.02)
        await svc.submit("dev", "repo", "second writes", a)
        await asyncio.sleep(0.02)
        starts = [p for m, p in server.calls if m == "turn/start"]
        assert len(starts) == 1, "two active writers started in one workspace"
    finally:
        await svc.close()
        store.close()


@pytest.mark.asyncio
async def test_completion_before_submit_response_is_not_lost(tmp_path):
    svc, server, store, a, b = await make(tmp_path, early=True)
    try:
        task = await svc.submit("dev", "repo", "fast task", a)
        await asyncio.sleep(0.03)
        result = svc.task_view("dev", task["id"], a)
        assert result["status"] == "completed", "early native terminal notification lost"
        assert "canonical final" in str(result), "canonical agentMessage text lost"
    finally:
        await svc.close()
        store.close()


@pytest.mark.asyncio
async def test_steer_uses_native_steer_not_turn_start(tmp_path):
    svc, server, store, a, b = await make(tmp_path)
    try:
        task = await svc.submit("dev", "repo", "work", a)
        await asyncio.sleep(0.02)
        task = store.task(task["id"])
        await svc.act(
            "dev",
            task["id"],
            a,
            "steer",
            {
                "expectedTurnId": task["turn_id"],
                "input": [{"type": "text", "text": "also check docs"}],
            },
        )
        assert server.calls[-1][0] == "turn/steer", "steering sent as turn/start"
    finally:
        await svc.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_drift", [{"broaden": True}, {"reviewer_override": "user"}])
async def test_native_policy_mismatch_stops_before_turn(tmp_path, policy_drift):
    svc, server, store, a, b = await make(tmp_path, **policy_drift)
    try:
        receipt = await svc.submit("dev", "repo", "work", a)
        await asyncio.sleep(0.02)
        assert store.task(receipt["id"])["status"] == "failed"
        assert not any(method == "turn/start" for method, _ in server.calls)
    finally:
        await svc.close()
        store.close()


@pytest.mark.asyncio
async def test_native_early_result_retains_confirmed_turn_id(tmp_path):
    svc, server, store, a, b = await make(tmp_path, early=True)
    try:
        task = await svc.submit("dev", "repo", "fast task", a)
        await asyncio.sleep(0.03)
        result = svc.task_view("dev", task["id"], a)
        assert result["turn_id"] == "turn-1", (
            "canonical terminal lost the confirmed native turn identity"
        )
    finally:
        await svc.close()
        store.close()


@pytest.mark.asyncio
async def test_duplicate_terminal_has_one_outbox_record(tmp_path):
    svc, server, store, a, b = await make(tmp_path)
    try:
        task = await svc.submit("dev", "repo", "work", a)
        await asyncio.sleep(0.02)
        for _ in range(2):
            await server.notify(
                "turn/completed",
                {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed", "items": []},
                },
            )
        n = store.db.execute(
            "select count(*) from outbox where task_id=?", (task["id"],)
        ).fetchone()[0]
        assert n == 1, "replayed terminal event created duplicate result deliveries"
    finally:
        await svc.close()
        store.close()


@pytest.mark.asyncio
async def test_stale_completion_before_response_does_not_finish_new_turn(tmp_path):
    svc, server, store, a, b = await make(tmp_path)
    original = server.request

    async def stale(method, params=None, **kw):
        if method == "turn/start":
            await server.notify(
                "turn/completed",
                {
                    "threadId": params["threadId"],
                    "turn": {"id": "previous-turn", "status": "completed"},
                },
            )
        return await original(method, params, **kw)

    server.request = stale
    try:
        task = await svc.submit("dev", "repo", "new work", a)
        await asyncio.sleep(0.03)
        view = svc.task_view("dev", task["id"], a)
        assert view["status"] == "running" and view["turn_id"] == "turn-1", (
            "old turn terminal finalized a new submission"
        )
    finally:
        await svc.close()
        store.close()


@pytest.mark.asyncio
async def test_cancel_terminal_before_rpc_ack_is_not_overwritten(tmp_path):
    svc, server, store, a, b = await make(tmp_path)
    original = server.request

    async def cancel_early(method, params=None, **kw):
        if method == "turn/interrupt":
            await server.notify(
                "turn/completed",
                {
                    "threadId": params["threadId"],
                    "turn": {"id": params["turnId"], "status": "interrupted"},
                },
            )
        return await original(method, params, **kw)

    server.request = cancel_early
    try:
        task = await svc.submit("dev", "repo", "work", a)
        await asyncio.sleep(0.02)
        await svc.act("dev", task["id"], a, "cancel")
        assert svc.task_view("dev", task["id"], a)["status"] == "interrupted", (
            "cancel receipt overwrote confirmed terminal state"
        )
    finally:
        await svc.close()
        store.close()


@pytest.mark.asyncio
async def test_queued_task_can_be_cancelled_before_remote_start(tmp_path):
    svc, server, store, a, b = await make(tmp_path)
    try:
        await svc.submit("dev", "repo", "first", a)
        await asyncio.sleep(0.02)
        second = await svc.submit("dev", "repo", "second", a)
        await asyncio.sleep(0.01)
        await svc.act("dev", second["id"], a, "cancel")
        await server.notify(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        )
        await asyncio.sleep(0.03)
        assert svc.task_view("dev", second["id"], a)["status"] == "interrupted"
        assert len([x for x in server.calls if x[0] == "turn/start"]) == 1, (
            "cancelled queued task still executed"
        )
    finally:
        await svc.close()
        store.close()


@pytest.mark.asyncio
async def test_native_offered_decisions_are_enforced(tmp_path):
    svc, server, store, a, b = await make(tmp_path)
    pending = None
    try:
        task = await svc.submit("dev", "repo", "work", a)
        await asyncio.sleep(0.02)
        pending = asyncio.create_task(
            server.human(
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "cmd-1",
                    "availableDecisions": ["decline"],
                },
            )
        )
        await asyncio.sleep(0.02)
        interactions = svc.task_view("dev", task["id"], a)["interactions"]
        assert len(interactions) == 1
        with pytest.raises((ValueError, PermissionError)):
            await svc.act(
                "dev",
                task["id"],
                a,
                "approve",
                {"interactionId": interactions[0]["id"], "decision": "accept"},
            )
    finally:
        if pending:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await svc.close()
        store.close()


@pytest.mark.asyncio
async def test_recovery_follows_history_pagination(tmp_path):
    svc, server, store, a, b = await make(tmp_path)
    task = await svc.submit("dev", "repo", "work", a)
    await asyncio.sleep(0.02)
    await svc.close()
    store.close()
    replacement = NativeServer()
    original = replacement.request

    async def pages(method, params=None, **kw):
        if method == "thread/turns/list":
            if params.get("cursor") is None:
                return {
                    "data": [{"id": "other-turn", "status": "completed", "items": []}],
                    "nextCursor": "page-2",
                }
            assert params["cursor"] == "page-2"
            return {
                "data": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [
                            {
                                "id": "m1",
                                "type": "agentMessage",
                                "text": "recovered final",
                                "phase": "final_answer",
                            },
                            {
                                "id": "cmd-recovered",
                                "type": "commandExecution",
                                "command": "pytest -q",
                                "status": "completed",
                                "exitCode": 0,
                            },
                        ],
                    }
                ],
                "nextCursor": None,
            }
        return await original(method, params, **kw)

    replacement.request = pages
    reopened = Store(str(tmp_path / "tasks.sqlite"))
    env = Environment(
        "dev",
        replacement,
        {"repo": "/tmp/project", "other": "/tmp/other"},
        allowlist={a.key, b.key},
    )
    restarted = Orchestrator(reopened, {"dev": env})
    try:
        await restarted.recover()
        view = restarted.task_view("dev", task["id"], a)
        assert view["status"] == "completed", (
            "recovery falsely terminalized a task missing only from first history page"
        )
        assert view["result"]["finalText"] == "recovered final"
        command = view["result"]["nativeEvidence"]["commandExecutions"][0]
        assert command["source"] == "native_history_item"
        assert command["observed"]["exitCode"] == 0
    finally:
        await restarted.close()
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewer", ["auto_review", "user"])
async def test_resume_calls_native_resume_before_new_input(tmp_path, reviewer):
    svc, server, store, a, b = await make(tmp_path, reviewer=reviewer)
    try:
        await svc.submit("dev", "repo", "first", a)
        await asyncio.sleep(0.02)
        await server.notify(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        )
        await asyncio.sleep(0.01)
        boundary = len(server.calls)
        await svc.submit("dev", "repo", "follow up", a, mode="resume", thread_id="thread-1")
        await asyncio.sleep(0.02)
        calls = [m for m, p in server.calls[boundary:]]
        assert "thread/resume" in calls, (
            "owned stored session was not loaded/validated before reuse"
        )
        assert calls.index("thread/resume") < calls.index("turn/start")
        assert [
            params["approvalsReviewer"]
            for method, params in server.calls
            if method in {"thread/start", "thread/resume"}
        ] == [reviewer, reviewer]
    finally:
        await svc.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("update_applied", [True, False])
async def test_resume_rechecks_the_reviewer_before_starting_a_turn(tmp_path, update_applied):
    svc, server, store, actor, _ = await make(tmp_path, reviewer="user")
    try:
        await svc.submit("dev", "repo", "first", actor)
        await asyncio.sleep(0.02)
        await server.notify(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        )
        client_request = server.request

        async def request(method, params=None, **kwargs):
            if method == "thread/settings/update":
                server.calls.append((method, params))
                if update_applied:
                    server.reviewer_override = params["approvalsReviewer"]
                return {}
            return await client_request(method, params, **kwargs)

        server.reviewer_override = "user"
        server.request = request
        svc.environments["dev"].approvals_reviewer = "auto_review"
        boundary = len(server.calls)
        receipt = await svc.submit(
            "dev", "repo", "follow up", actor, mode="resume", thread_id="thread-1"
        )
        await asyncio.sleep(0.02)
        calls = server.calls[boundary:]
        assert calls[1] == (
            "thread/settings/update",
            {"threadId": "thread-1", "approvalsReviewer": "auto_review"},
        )
        assert [method for method, _ in calls[:3]] == [
            "thread/resume",
            "thread/settings/update",
            "thread/resume",
        ]
        if update_applied:
            assert calls[3][0] == "turn/start"
        else:
            assert len(calls) == 3
            assert store.task(receipt["id"])["status"] == "failed"
    finally:
        await svc.close()
        store.close()


@pytest.mark.asyncio
async def test_native_resolved_request_is_scoped_to_environment(tmp_path):
    svc, left, store, a, b = await make(tmp_path)
    right = NativeServer()
    other_env = Environment("other", right, {"repo": "/tmp/other-project"}, allowlist={a.key})
    svc.environments["other"] = other_env
    right.handlers(
        notification=lambda m, p: svc._notification(other_env, m, p),
        request=lambda m, p, request_id=None: svc._server_request(other_env, m, p, request_id),
    )
    waits = []
    try:
        one = await svc.submit("dev", "repo", "left", a)
        two = await svc.submit("other", "repo", "right", a)
        await asyncio.sleep(0.03)
        for server in [left, right]:
            waits.append(
                asyncio.create_task(
                    server.human(
                        "item/fileChange/requestApproval",
                        {"threadId": "thread-1", "turnId": "turn-1", "itemId": "patch-1"},
                        7,
                    )
                )
            )
        await asyncio.sleep(0.02)
        await right.notify("serverRequest/resolved", {"threadId": "thread-1", "requestId": 7})
        left_pending = svc.task_view("dev", one["id"], a)["interactions"]
        right_pending = svc.task_view("other", two["id"], a)["interactions"]
        assert len(left_pending) == 1 and len(right_pending) == 0, (
            "RPC IDs from different environments were conflated"
        )
    finally:
        for w in waits:
            w.cancel()
        await asyncio.gather(*waits, return_exceptions=True)
        await svc.close()
        store.close()


@pytest.mark.asyncio
async def test_recovered_running_task_keeps_workspace_reserved(tmp_path):
    svc, server, store, a, b = await make(tmp_path)
    await svc.submit("dev", "repo", "active first", a)
    await asyncio.sleep(0.02)
    await svc.close()
    store.close()
    replacement = NativeServer()
    original = replacement.request

    async def running(method, params=None, **kw):
        if method == "thread/turns/list":
            return {
                "data": [{"id": "turn-1", "status": "inProgress", "items": []}],
                "nextCursor": None,
            }
        return await original(method, params, **kw)

    replacement.request = running
    reopened = Store(str(tmp_path / "tasks.sqlite"))
    env = Environment(
        "dev",
        replacement,
        {"repo": "/tmp/project", "other": "/tmp/other"},
        allowlist={a.key, b.key},
    )
    restarted = Orchestrator(reopened, {"dev": env})
    try:
        await asyncio.wait_for(restarted.recover(), 1)
        await restarted.submit("dev", "repo", "new writer", a)
        await asyncio.sleep(0.03)
        assert not any(m == "turn/start" for m, p in replacement.calls), (
            "recovered running writer did not reserve workspace"
        )
    finally:
        await restarted.close()
        reopened.close()


@pytest.mark.asyncio
async def test_queued_task_survives_restart_and_starts_after_reconcile(tmp_path):
    svc, server, store, a, b = await make(tmp_path)
    await svc.submit("dev", "repo", "first", a)
    await asyncio.sleep(0.02)
    await svc.submit("dev", "repo", "queued second", a)
    await asyncio.sleep(0.01)
    await svc.close()
    store.close()
    replacement = NativeServer()
    original = replacement.request

    async def finished(method, params=None, **kw):
        if method == "thread/turns/list":
            return {
                "data": [{"id": "turn-1", "status": "completed", "items": []}],
                "nextCursor": None,
            }
        return await original(method, params, **kw)

    replacement.request = finished
    reopened = Store(str(tmp_path / "tasks.sqlite"))
    env = Environment(
        "dev",
        replacement,
        {"repo": "/tmp/project", "other": "/tmp/other"},
        allowlist={a.key, b.key},
    )
    restarted = Orchestrator(reopened, {"dev": env})
    try:
        await asyncio.wait_for(restarted.recover(), 1)
        await asyncio.sleep(0.05)
        starts = [p for m, p in replacement.calls if m == "turn/start"]
        assert len(starts) == 1 and starts[0]["input"][0]["text"] == "queued second", (
            "persisted local queue was silently stranded"
        )
    finally:
        await restarted.close()
        reopened.close()


@pytest.mark.asyncio
async def test_terminal_and_delivery_record_are_atomic(tmp_path):
    svc, server, store, a, b = await make(tmp_path)
    try:
        task = await svc.submit("dev", "repo", "work", a)
        await asyncio.sleep(0.02)
        store.db.execute(
            "CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox "
            "BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END"
        )
        try:
            await server.notify(
                "turn/completed",
                {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
            )
        except Exception:
            pass
        row = store.task(task["id"])
        n = store.db.execute(
            "select count(*) from outbox where task_id=?", (task["id"],)
        ).fetchone()[0]
        assert row["status"] != "completed" or n == 1, (
            "terminal result committed while its durable delivery was lost"
        )
    finally:
        await svc.close()
        store.close()


@pytest.mark.asyncio
async def test_failed_native_turn_preserves_structured_error(tmp_path):
    svc, server, store, a, b = await make(tmp_path)
    try:
        task = await svc.submit("dev", "repo", "work", a)
        await asyncio.sleep(0.02)
        await server.notify(
            "turn/completed",
            {
                "threadId": "thread-1",
                "turn": {
                    "id": "turn-1",
                    "status": "failed",
                    "error": {
                        "message": "fixture authorization failed",
                        "codexErrorInfo": "unauthorized",
                    },
                },
            },
        )
        view = svc.task_view("dev", task["id"], a)
        assert view["status"] == "failed" and "fixture authorization failed" in str(view["error"])
    finally:
        await svc.close()
        store.close()
