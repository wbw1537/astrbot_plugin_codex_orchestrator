import asyncio
import json

import pytest

from codex_orchestrator.service import Actor, Environment, Orchestrator
from codex_orchestrator.store import Store


class FakeClient:
    def __init__(self):
        self.calls = []

    def handlers(self, **handlers):
        self.handler = handlers

    async def request(self, method, params=None, timeout=60):
        self.calls.append((method, params))
        if method in {"thread/start", "thread/resume"}:
            return {
                "thread": {"id": params.get("threadId", "th-1")},
                "cwd": "/work/repo",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
            }
        if method == "turn/start":
            return {"turn": {"id": "tu-1", "status": "inProgress"}}
        if method == "turn/interrupt":
            return {"ok": True}
        return {"data": []}

    async def close(self):
        pass


@pytest.fixture
def setup(tmp_path):
    actor = Actor("test", "group:g", "user:1", "cid-1")
    other = Actor("test", "group:g", "user:2", "cid-1")
    client = FakeClient()
    env = Environment(
        "dev",
        client,
        {"repo": "/work/repo", "other": "/work/other"},
        admins=set(),
        allowlist={actor.key},
    )
    store = Store(str(tmp_path / "state.sqlite3"))
    return Orchestrator(store, {"dev": env}), actor, other, client, store


@pytest.mark.asyncio
async def test_acl_submit_complete_and_owner_isolation(setup):
    service, actor, other, client, store = setup
    with pytest.raises(PermissionError):
        await service.submit("dev", "repo", "task", other)
    receipt = await service.submit("dev", "repo", "task", actor, request_key="event-1")
    assert receipt["status"] in {"submitted", "running"}
    await asyncio.sleep(0.03)
    task = store.task(receipt["id"])
    assert task["thread_id"] == "th-1" and task["turn_id"] == "tu-1"
    with pytest.raises(PermissionError):
        service.task_view("dev", task["id"], other)
    await service._notification(
        service.environments["dev"],
        "turn/completed",
        {
            "threadId": "th-1",
            "turnId": "tu-1",
            "turn": {"id": "tu-1", "status": "completed", "finalText": "done"},
        },
    )
    assert service.task_view("dev", task["id"], actor)["status"] == "completed"
    assert store.db.execute("select count(*) from outbox").fetchone()[0] == 1
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_unrelated_turn_ignored_and_cancel_requires_native_identity(setup):
    service, actor, _, client, store = setup
    receipt = await service.submit("dev", "repo", "task", actor)
    await asyncio.sleep(0.02)
    await service._notification(
        service.environments["dev"],
        "turn/completed",
        {"threadId": "th-1", "turnId": "stale", "turn": {"id": "stale", "status": "completed"}},
    )
    assert store.task(receipt["id"])["status"] == "running"
    await service.act("dev", receipt["id"], actor, "cancel")
    assert store.task(receipt["id"])["status"] == "cancelling"
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_hot_reconnect_reuses_active_runner_without_lock_deadlock(setup):
    service, actor, _, _, store = setup
    receipt = await service.submit("dev", "repo", "work", actor)
    await asyncio.sleep(0.02)
    replacement = FakeClient()

    async def active_resume(method, params=None, timeout=60):
        replacement.calls.append((method, params))
        if method == "thread/resume":
            return {
                "thread": {
                    "id": params["threadId"],
                    "status": {"type": "active", "activeFlags": []},
                },
                "cwd": "/work/repo",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
            }
        if method == "thread/turns/list":
            return {"data": [{"id": "tu-1", "status": "inProgress"}], "nextCursor": None}
        return {"turn": {"id": "tu-1", "status": "inProgress"}}

    replacement.request = active_resume
    service.mark_disconnected("dev")
    await service.replace_client("dev", replacement)
    await asyncio.wait_for(service.recover("dev"), 0.5)
    assert any(method == "thread/resume" for method, _ in replacement.calls)
    await replacement.handler["notification"](
        "turn/completed",
        {"threadId": "th-1", "turnId": "tu-1", "turn": {"id": "tu-1", "status": "completed"}},
    )
    await asyncio.sleep(0.02)
    assert service.task_view("dev", receipt["id"], actor)["status"] == "completed"
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_resume_refuses_native_thread_active_in_another_client(setup):
    service, actor, _, client, store = setup
    await service.submit("dev", "repo", "work", actor)
    await asyncio.sleep(0.02)
    await service._notification(
        service.environments["dev"],
        "turn/completed",
        {"threadId": "th-1", "turnId": "tu-1", "turn": {"id": "tu-1", "status": "completed"}},
    )
    await asyncio.sleep(0.01)
    client.calls.clear()

    async def active(method, params=None, timeout=60):
        client.calls.append((method, params))
        if method == "thread/resume":
            return {
                "thread": {"id": "th-1", "status": {"type": "active", "activeFlags": []}},
                "cwd": "/work/repo",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
            }
        return {"turn": {"id": "unexpected"}}

    client.request = active
    second = await service.submit("dev", "repo", "new turn", actor, mode="resume", thread_id="th-1")
    await asyncio.sleep(0.02)
    assert store.task(second["id"])["status"] == "failed"
    assert not any(method == "turn/start" for method, _ in client.calls)
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_secret_native_question_is_not_stored_and_mcp_form_is_validated(setup):
    service, actor, _, _, store = setup
    task = await service.submit("dev", "repo", "ask", actor)
    await asyncio.sleep(0.02)
    request = service.environments["dev"].client.handler["request"]
    result = await request(
        "item/tool/requestUserInput",
        {
            "threadId": "th-1",
            "turnId": "tu-1",
            "itemId": "secret-1",
            "isBlocking": True,
            "questions": [
                {
                    "id": "password",
                    "header": "Password",
                    "question": "Enter secret",
                    "isSecret": True,
                }
            ],
        },
    )
    assert result == {"answers": {}}
    assert service.task_view("dev", task["id"], actor)["interactions"] == []
    assert store.db.execute("select count(*) from interactions").fetchone()[0] == 0
    mcp = {
        "threadId": "th-1",
        "turnId": "tu-1",
        "mode": "form",
        "message": "Choose",
        "requestedSchema": {
            "type": "object",
            "properties": {"color": {"type": "string", "enum": ["blue", "green"]}},
            "required": ["color"],
        },
    }
    waiting = asyncio.create_task(request("mcpServer/elicitation/request", mcp))
    await asyncio.sleep(0.01)
    interaction = service.task_view("dev", task["id"], actor)["interactions"][0]
    with pytest.raises(ValueError, match="invalid choice"):
        await service.act(
            "dev",
            task["id"],
            actor,
            "answer",
            {"interactionId": interaction["id"], "action": "accept", "content": {"color": "red"}},
        )
    await service.act(
        "dev",
        task["id"],
        actor,
        "answer",
        {"interactionId": interaction["id"], "action": "accept", "content": {"color": "blue"}},
    )
    assert await waiting == {"action": "accept", "content": {"color": "blue"}}
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_recovery_reserves_workspace_even_when_owner_acl_was_revoked(tmp_path):
    admin = Actor("test", "group:g", "admin", "cid-1", "admin")
    original = FakeClient()
    env = Environment("dev", original, {"repo": "/work/repo"})
    store = Store(str(tmp_path / "revoked.sqlite"))
    service = Orchestrator(store, {"dev": env})
    task = await service.submit("dev", "repo", "active task", admin)
    await asyncio.sleep(0.02)
    await service.close()
    store.close()

    replacement = FakeClient()

    async def active_history(method, params=None, timeout=60):
        replacement.calls.append((method, params))
        if method == "thread/resume":
            return {
                "thread": {"id": "th-1", "status": {"type": "active", "activeFlags": []}},
                "cwd": "/work/repo",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
            }
        if method == "thread/turns/list":
            return {"data": [{"id": "tu-1", "status": "inProgress"}], "nextCursor": None}
        return {"data": []}

    replacement.request = active_history
    reopened = Store(str(tmp_path / "revoked.sqlite"))
    env2 = Environment("dev", replacement, {"repo": "/work/repo"})
    restarted = Orchestrator(reopened, {"dev": env2})
    await restarted.recover()
    # Admin role still permits a new request, but the prior task cannot be reconciled
    # without reauthorization; its uncertain workspace reservation blocks execution.
    queued = await restarted.submit("dev", "repo", "second writer", admin)
    await asyncio.sleep(0.02)
    assert reopened.task(task["id"])["status"] == "submission_unknown"
    assert reopened.task(queued["id"])["status"] == "queued"
    assert not any(method == "turn/start" for method, _ in replacement.calls)
    await restarted.close()
    reopened.close()


@pytest.mark.asyncio
async def test_resume_thread_is_bound_to_original_workspace(setup):
    service, actor, _, _, store = setup
    await service.submit("dev", "repo", "work", actor)
    await asyncio.sleep(0.02)
    await service._notification(
        service.environments["dev"],
        "turn/completed",
        {"threadId": "th-1", "turnId": "tu-1", "turn": {"id": "tu-1", "status": "completed"}},
    )
    await asyncio.sleep(0.01)
    with pytest.raises(PermissionError, match="workspace"):
        await service.submit("dev", "other", "work", actor, mode="resume", thread_id="th-1")
    with pytest.raises(ValueError, match="explicit resume"):
        await service.submit("dev", "repo", "work", actor, mode="new", thread_id="th-1")
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_task_view_returns_structured_native_evidence_and_owned_event_pages(setup):
    service, actor, other, _, store = setup
    receipt = await service.submit("dev", "repo", "inspect and verify", actor)
    await asyncio.sleep(0.02)
    env = service.environments["dev"]
    notices = [
        ("turn/plan/updated", {"explanation": "Inspecting", "plan": [{"step": "run checks"}]}),
        ("turn/diff/updated", {"diff": "diff contents"}),
        ("thread/tokenUsage/updated", {"tokenUsage": {"total": 17, "input": 12, "output": 5}}),
        (
            "item/completed",
            {
                "item": {
                    "id": "cmd-item",
                    "type": "commandExecution",
                    "status": "completed",
                    "command": "pytest -q",
                    "exitCode": 0,
                    "aggregatedOutput": "2 passed",
                }
            },
        ),
    ]
    for method, details in notices:
        await service._notification(
            env,
            method,
            {
                "threadId": "th-1",
                "turnId": "tu-1",
                **details,
            },
        )
    await service._notification(
        env,
        "turn/completed",
        {
            "threadId": "th-1",
            "turnId": "tu-1",
            "turn": {"id": "tu-1", "status": "completed", "finalText": "The checks passed."},
        },
    )

    page = service.task_view("dev", receipt["id"], actor, limit=2)
    assert isinstance(page["result"], dict)
    assert page["result"]["finalText"] == "The checks passed."
    evidence = page["result"]["nativeEvidence"]
    assert evidence["plan"]["steps"] == [{"step": "run checks"}]
    assert evidence["diff"]["content"] == "diff contents"
    assert evidence["usage"]["tokenUsage"]["total"] == 17
    command = evidence["commandExecutions"][0]
    assert command["source"] == "native_item_completed"
    assert command["observed"]["exitCode"] == 0
    assert "verifiedChecks" not in evidence and "testsPassed" not in evidence
    assert len(page["events"]["items"]) == 2 and page["events"]["hasMore"]

    next_page = service.task_view(
        "dev", receipt["id"], actor, cursor=page["events"]["nextCursor"], limit=2
    )
    assert next_page["events"]["items"][0]["cursor"] > page["events"]["nextCursor"]
    with pytest.raises(PermissionError):
        service.task_view("dev", receipt["id"], other, cursor=-100)
    with pytest.raises(ValueError):
        service.task_view("dev", receipt["id"], actor, limit=51)
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_task_event_page_clips_large_progress_and_parses_structured_error(setup):
    service, actor, _, _, store = setup
    receipt = await service.submit("dev", "repo", "large progress", actor)
    await asyncio.sleep(0.02)
    env = service.environments["dev"]
    await service._notification(
        env,
        "turn/diff/updated",
        {
            "threadId": "th-1",
            "turnId": "tu-1",
            "diff": "x" * 12000,
        },
    )
    page = service.task_view("dev", receipt["id"], actor, limit=5)
    diff_event = next(item for item in page["events"]["items"] if item.get("truncated"))
    assert diff_event["originalBytes"] > 4096
    assert diff_event["preview"].endswith("…")
    encoded = json.dumps(page, ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= 65536
    await service._notification(
        env,
        "turn/completed",
        {
            "threadId": "th-1",
            "turnId": "tu-1",
            "turn": {
                "id": "tu-1",
                "status": "failed",
                "error": {"message": "native failure", "code": 7},
            },
        },
    )
    failed = service.task_view("dev", receipt["id"], actor)
    assert failed["status"] == "failed"
    assert failed["error"] == {"message": "native failure", "code": 7}
    assert failed["result"]["turn"]["error"] == {"message": "native failure", "code": 7}
    await service.close()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fragment", ["中文🌱", '\\"\n\t\x00'])
async def test_multibyte_and_escaped_progress_stays_within_serialized_bounds(setup, fragment):
    from codex_orchestrator.service import MAX_EVENT_BYTES, MAX_EVENT_PAGE_BYTES, MAX_RESULT_TEXT

    service, actor, _, _, store = setup
    receipt = await service.submit("dev", "repo", "bounded progress", actor)
    await asyncio.sleep(0.02)
    env = service.environments["dev"]
    text = fragment * 20000
    for index in range(25):
        await service._notification(
            env,
            "turn/diff/updated",
            {"threadId": "th-1", "turnId": "tu-1", "diff": text, "index": index},
        )
    await service._notification(
        env,
        "turn/completed",
        {
            "threadId": "th-1",
            "turnId": "tu-1",
            "turn": {"id": "tu-1", "status": "completed", "finalText": text},
        },
    )
    view = service.task_view("dev", receipt["id"], actor, limit=50)
    assert len(view["result"]["finalText"].encode("utf-8")) <= MAX_RESULT_TEXT
    assert view["result"]["finalTextTruncated"]
    assert view["result"]["finalTextOriginalBytes"] == len(text.encode("utf-8"))
    diff = view["nativeEvidence"]["diff"]["content"]
    assert diff["truncated"]
    assert len(json.dumps(diff).encode("utf-8")) <= 8192
    seen = []
    while True:
        page = view["events"]
        assert len(json.dumps(page).encode("utf-8")) <= MAX_EVENT_PAGE_BYTES
        assert all(
            len(json.dumps(item).encode("utf-8")) <= MAX_EVENT_BYTES for item in page["items"]
        )
        seen.extend(item["cursor"] for item in page["items"])
        if not page["hasMore"]:
            break
        view = service.task_view("dev", receipt["id"], actor, cursor=page["nextCursor"], limit=50)
    assert len(seen) >= 26 and seen == sorted(set(seen))
    await service.close()
    store.close()
