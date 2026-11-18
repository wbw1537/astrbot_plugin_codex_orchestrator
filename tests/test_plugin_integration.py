"""Loader/handler checks against the verified AstrBot 4.29.4 API surface."""

import asyncio
import importlib.util
import inspect
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def loaded(monkeypatch):
    tools = {}
    commands = {}
    registration = {}

    class Star:
        # Verified native base does not persist config on self.
        def __init__(self, context, config=None):
            self.context = context

    class Filter:
        @staticmethod
        def command(name):
            def register(fn):
                commands[name] = fn
                return fn

            return register

    def register(name, author, *args, **kw):
        registration.update(name=name, author=author)
        return lambda cls: cls

    for n in ["astrbot", "astrbot.api"]:
        m = types.ModuleType(n)
        m.__path__ = []
        monkeypatch.setitem(sys.modules, n, m)

    class FunctionTool:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.handler = None
            self.handler_module_path = None

    sys.modules["astrbot.api"].FunctionTool = FunctionTool
    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = Star
    star.register = register
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = Filter
    monkeypatch.setitem(sys.modules, star.__name__, star)
    monkeypatch.setitem(sys.modules, event.__name__, event)
    pkg = types.ModuleType("test_plugin")
    pkg.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, pkg.__name__, pkg)
    spec = importlib.util.spec_from_file_location("test_plugin.main", ROOT / "main.py")
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    return mod, tools, commands, registration


class Manager:
    async def get_curr_conversation_id(self, umo):
        return "actual-conversation-id"


class Context:
    conversation_manager = Manager()

    def __init__(self):
        self.tools = {}

    def add_llm_tools(self, *tools):
        for tool in tools:
            tool.handler_module_path = tool.__module__
            self.tools[tool.name] = tool


class Event:
    unified_msg_origin = "qq-instance:GroupMessage:group-1"
    role = "member"

    def get_sender_id(self):
        return "member-1"

    def get_platform_id(self):
        return "qq-instance"

    def get_session_id(self):
        return "group-1"

    def is_admin(self):
        return False

    def get_message_str(self):
        return "/codex environments"

    def plain_result(self, text):
        return types.SimpleNamespace(text=text)


@pytest.mark.asyncio
async def test_real_star_constructor_contract_and_cleanup(loaded, tmp_path):
    mod, tools, commands, registration = loaded
    plugin = mod.CodexOrchestratorPlugin(
        Context(), {"database": str(tmp_path / "plugin.sqlite"), "environments": []}
    )
    try:
        await plugin.initialize()
        actor = await plugin._actor(Event())
        assert actor.platform == "qq-instance"
        assert actor.role != "admin", "bound is_admin method was treated as true"
        assert actor.conversation_id == "actual-conversation-id", (
            "platform session used as conversation id"
        )
        result = await plugin.llm_environments(Event())
        assert isinstance(result, str) and json.loads(result) == {"environments": []}
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_schema_default", [False, True])
async def test_default_database_uses_plugin_data_and_persists(
    loaded, monkeypatch, tmp_path, use_schema_default
):
    mod, _, _, _ = loaded
    monkeypatch.chdir(tmp_path)
    expected = (
        tmp_path / "data/plugin_data/astrbot_plugin_codex_orchestrator/codex-orchestrator.sqlite3"
    )
    config = {"environments": []}
    if use_schema_default:
        schema = json.loads((ROOT / "_conf_schema.json").read_text())
        config["database"] = schema["database"]["default"]
    assert not expected.parent.exists()
    plugin = mod.CodexOrchestratorPlugin(Context(), config)
    try:
        await plugin.initialize()
        assert expected.is_file()
        assert not (tmp_path / "data/codex-orchestrator.sqlite3").exists()
        task = plugin.store.create_task(
            env="test",
            actor="user",
            platform="test",
            umo="test:session",
            cid="conversation",
            workspace="repo",
            text="persist this task",
            request_key=None,
        )
        plugin.store.finish(task["id"], "completed", {"text": "saved"})
    finally:
        await plugin.terminate()
    reopened = mod.CodexOrchestratorPlugin(Context(), config)
    try:
        await reopened.initialize()
        saved = reopened.store.task(task["id"])
        assert saved["input"] == "persist this task"
        assert saved["status"] == "completed"
        assert json.loads(saved["result"]) == {"text": "saved"}
    finally:
        await reopened.terminate()


@pytest.mark.asyncio
async def test_registered_tools_are_agent_results_with_no_identity_arguments(loaded, tmp_path):
    mod, _, commands, registration = loaded
    context = Context()
    plugin = mod.CodexOrchestratorPlugin(
        context, {"database": str(tmp_path / "tools.sqlite"), "environments": []}
    )
    await plugin.initialize()
    try:
        required = {
            "codex_environments": [],
            "codex_submit": ["env", "workspace", "text"],
            "codex_task": ["env", "task_id"],
            "codex_sessions": ["env"],
            "codex_cancel": ["env", "task_id"],
            "codex_steer": ["env", "task_id", "text"],
        }
        assert set(context.tools) == set(required)
        for name, tool in context.tools.items():
            assert tool.parameters["required"] == required[name]
            assert not set(tool.parameters["properties"]) & {
                "event",
                "actor",
                "role",
                "user_id",
                "url",
                "cwd",
                "approvalPolicy",
            }
            assert tool.handler is None  # framework must not bind self a second time
            assert tool.handler_module_path == mod.__name__
            assert inspect.iscoroutinefunction(tool.call)
        task_schema = context.tools["codex_task"].parameters
        assert task_schema["properties"]["cursor"]["type"] == "integer"
        assert "wait_seconds" not in task_schema["required"]
        # Exercise the same ContextWrapper -> event path the real tool executor uses.
        wrapper = types.SimpleNamespace(context=types.SimpleNamespace(event=Event()))
        value = await context.tools["codex_environments"].call(wrapper)
        assert json.loads(value) == {"environments": []}
    finally:
        await plugin.terminate()


def test_plugin_identity_matches_distribution(loaded):
    import yaml

    mod, tools, commands, registration = loaded
    meta = yaml.safe_load((ROOT / "metadata.yaml").read_text())
    assert registration["name"] == meta["name"], "runtime and package plugin identities differ"
    assert registration["author"] != "OpenAI", "new project incorrectly attributed to OpenAI"
    assert isinstance(json.loads((ROOT / "_conf_schema.json").read_text()), dict)


@pytest.mark.asyncio
async def test_transport_disconnect_reconnects_from_saved_admin_config(
    loaded, monkeypatch, tmp_path
):
    mod, tools, commands, registration = loaded

    class Client:
        def __init__(self):
            self.closed = asyncio.Event()
            self.initialized = False

        def handlers(self, **_):
            pass

        async def initialize(self):
            self.initialized = True
            return {"ok": True}

        async def request(self, method, params=None, **_):
            if method == "thread/resume":
                return {
                    "thread": {"id": params["threadId"]},
                    "cwd": params["cwd"],
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "user",
                    "sandbox": {
                        "type": "workspaceWrite",
                        "writableRoots": [],
                        "networkAccess": False,
                    },
                }
            return {"data": [], "nextCursor": None}

        async def wait_closed(self):
            await self.closed.wait()

        async def close(self):
            self.closed.set()

    clients = [Client(), Client()]

    async def connect(*_args, **_kwargs):
        return clients.pop(0)

    monkeypatch.setattr(mod, "connect_stdio", connect)
    plugin = mod.CodexOrchestratorPlugin(
        Context(),
        {
            "database": str(tmp_path / "reconnect.sqlite"),
            "environments": [
                {
                    "name": "local",
                    "transport": "stdio",
                    "command": ["codex"],
                    "workspaces": {"repo": str(tmp_path)},
                    "admins": [],
                    "allowlist": [],
                }
            ],
        },
    )
    await plugin.initialize()
    initial = plugin.service.environments["local"].client
    initial.closed.set()
    for _ in range(50):
        if plugin.service.environments["local"].client is not initial:
            break
        await asyncio.sleep(0.01)
    assert plugin.service.environments["local"].client is not initial
    assert plugin.service.environments["local"].generation == 2
    await plugin.terminate()


@pytest.mark.asyncio
async def test_command_parser_accepts_argument_only_framework_text(loaded, tmp_path):
    from codex_orchestrator.service import Environment, Orchestrator
    from codex_orchestrator.store import Store

    mod, tools, commands, registration = loaded
    plugin = mod.CodexOrchestratorPlugin(Context(), {})
    owner = await plugin._actor(Event())

    class Client:
        def handlers(self, **_):
            pass

        async def close(self):
            pass

    store = Store(str(tmp_path / "command.sqlite"))
    plugin.store = store
    plugin.service = Orchestrator(
        store, {"dev": Environment("dev", Client(), {"repo": str(tmp_path)}, allowlist={owner.key})}
    )
    event = Event()
    event.get_message_str = lambda: "sessions dev"
    response = await plugin.codex_command(event)
    assert json.loads(response.text) == {"sessions": []}
    await plugin.terminate()


@pytest.fixture
async def chat_task(loaded, tmp_path):
    from codex_orchestrator.service import Environment, Orchestrator
    from codex_orchestrator.store import Store

    mod, _, _, _ = loaded
    plugin = mod.CodexOrchestratorPlugin(Context(), {})
    event = Event()
    owner = await plugin._actor(event)

    class Client:
        def handlers(self, **_):
            pass

        async def close(self):
            pass

    store = Store(str(tmp_path / "chat.sqlite"))
    plugin.store = store
    plugin.service = Orchestrator(
        store, {"dev": Environment("dev", Client(), {"repo": str(tmp_path)}, allowlist={owner.key})}
    )
    receipt = store.create_task(
        env="dev",
        actor=owner.key,
        platform=owner.platform,
        umo=owner.umo,
        cid=owner.conversation_id,
        workspace="repo",
        text="chat regression",
        request_key=None,
    )
    yield plugin, event, store, receipt["id"]
    await plugin.terminate()


def pending_request(store, task_id, number):
    interaction = f"approval-{number}"
    store.db.execute(
        "INSERT INTO interactions(id,task_id,generation,method,params,state) VALUES(?,?,?,?,?,?)",
        (
            interaction,
            task_id,
            1,
            "item/commandExecution/requestApproval",
            json.dumps(
                {
                    "command": "large command " + "中文🌱\n" * 4000,
                    "availableDecisions": ["decline", "cancel"],
                }
            ),
            "pending",
        ),
    )
    return interaction


@pytest.mark.asyncio
async def test_chat_status_keeps_all_approval_ids_after_large_progress(chat_task):
    plugin, event, store, task_id = chat_task
    store.event(
        task_id, {"method": "item/commandExecution/outputDelta", "params": {"delta": "x" * 12000}}
    )
    expected = [pending_request(store, task_id, number) for number in range(23)]
    seen = []
    offset = 0
    while True:
        event.get_message_str = lambda offset=offset: f"/codex status dev {task_id} {offset}"
        response = await plugin.codex_command(event)
        assert len(response.text.encode("utf-8")) <= 3800
        page = json.loads(response.text)
        item = page["interactions"][0]
        seen.append(item["id"])
        assert item["availableDecisions"] == ["decline", "cancel"]
        assert "outputDelta" not in response.text
        if not page["interactionsPage"]["hasMore"]:
            break
        offset = page["interactionsPage"]["nextOffset"]
        assert offset == len(seen)
        assert page["nextStatusCommand"].endswith(f" {offset}")
    assert seen == expected


@pytest.mark.asyncio
async def test_chat_result_unicode_and_escape_pages_are_lossless(chat_task):
    plugin, event, store, task_id = chat_task
    approval = pending_request(store, task_id, 1)
    text = '中文🌱"\\\n\t' * 400
    store.finish(task_id, "completed", {"finalText": text, "turn": {"status": "completed"}})
    chunks = []
    offset = 0
    while True:
        event.get_message_str = lambda offset=offset: f"/codex result dev {task_id} {offset}"
        response = await plugin.codex_command(event)
        assert len(response.text.encode("utf-8")) <= 3800
        page = json.loads(response.text)
        assert page["interactions"][0]["id"] == approval
        result = page["finalText"]
        assert result["offset"] == offset
        chunks.append(result["text"])
        if not result["hasMore"]:
            assert result["nextOffset"] is None
            break
        assert result["nextOffset"] > offset
        offset = result["nextOffset"]
        assert page["nextResultCommand"].endswith(f" {offset}")
    assert len(chunks) > 1
    assert "".join(chunks) == text


@pytest.mark.asyncio
async def test_chat_interaction_pagination_preserves_owner_checks(chat_task):
    plugin, event, store, task_id = chat_task
    pending_request(store, task_id, 1)
    event.get_sender_id = lambda: "another-member"
    event.get_message_str = lambda: f"/codex status dev {task_id} 0"
    response = await plugin.codex_command(event)
    assert "not authorized" in json.loads(response.text)["error"]
    assert "approval-1" not in response.text
    assert "large command" not in response.text


@pytest.mark.asyncio
async def test_chat_followup_does_not_skip_pending_interactions(chat_task):
    from codex_orchestrator.chat import command_response

    plugin, event, store, task_id = chat_task
    for number in range(3):
        pending_request(store, task_id, number)
    actor = await plugin._actor(event)
    # cancel/approve/steer return full task views; compact formatting still pages one at a time.
    view = plugin.service.task_view("dev", task_id, actor)
    page = json.loads(command_response(view))
    assert page["interactions"][0]["id"] == "approval-0"
    assert page["interactionsPage"]["nextOffset"] == 1


@pytest.mark.asyncio
async def test_chat_result_handles_legacy_plain_text_and_invalid_offsets(chat_task):
    plugin, event, store, task_id = chat_task
    store.finish(task_id, "completed", "legacy plain result")
    event.get_message_str = lambda: f"/codex result dev {task_id}"
    response = await plugin.codex_command(event)
    assert json.loads(response.text)["finalText"]["text"] == "legacy plain result"
    event.get_message_str = lambda: f"/codex result dev {task_id} -1"
    response = await plugin.codex_command(event)
    assert "non-negative" in json.loads(response.text)["error"]


@pytest.mark.asyncio
async def test_chat_only_offers_supported_decisions_for_file_approval(chat_task):
    plugin, event, store, task_id = chat_task
    interaction = pending_request(store, task_id, 1)
    store.db.execute(
        "UPDATE interactions SET method=?,params=? WHERE id=?",
        (
            "item/fileChange/requestApproval",
            json.dumps({"availableDecisions": ["acceptForSession", "decline"]}),
            interaction,
        ),
    )
    event.get_message_str = lambda: f"/codex status dev {task_id}"
    page = json.loads((await plugin.codex_command(event)).text)
    assert page["interactions"][0]["availableDecisions"] == ["decline"]
    with pytest.raises(ValueError, match="unsupported decision"):
        await plugin.service.act(
            "dev",
            task_id,
            await plugin._actor(event),
            "approve",
            {"interactionId": interaction, "decision": "acceptForSession"},
        )
