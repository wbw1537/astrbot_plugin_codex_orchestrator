"""Loader/handler checks against the verified AstrBot 4.29.4 API surface."""

import asyncio
import importlib.util
import inspect
import json
import re
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
        def llm_tool(name=None, **kwargs):
            def register(fn):
                tools[name or fn.__name__] = fn
                return fn

            return register

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


def test_registered_tools_are_agent_results_with_no_identity_arguments(loaded):
    mod, tools, commands, registration = loaded
    expected = {
        "codex_environments",
        "codex_submit",
        "codex_task",
        "codex_cancel",
        "codex_steer",
        "codex_sessions",
    }
    assert expected <= set(tools)
    assert not set(tools) & {"codex_approve", "codex_answer", "codex_raw_rpc"}
    for name, fn in tools.items():
        assert inspect.iscoroutinefunction(fn) and not inspect.isasyncgenfunction(fn), (
            name + " sends a direct user message instead of a tool result"
        )
        doc = inspect.getdoc(fn) or ""
        args = set(re.findall(r"^\s*(\w+)\((?:string|number|object|array|boolean)\):", doc, re.M))
        assert not args & {"event", "actor", "role", "user_id", "url", "cwd", "approvalPolicy"}, (
            name,
            args,
        )


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
