"""Small bounded asynchronous JSON-RPC transport for Codex App Server."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

from . import __version__

MAX_FRAME = 2 * 1024 * 1024
MAX_PENDING = 128


async def _raise_unsupported() -> Any:
    raise RpcError("unsupported server request")


class RpcError(RuntimeError):
    pass


class JsonRpcClient:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_frame: int = MAX_FRAME,
    ):
        self.reader, self.writer, self.max_frame = reader, writer, max_frame
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._requests: dict[str, Callable[[str, Any], Awaitable[Any]]] = {}
        self._notify: Callable[[str, Any], Awaitable[None]] | None = None
        self._counter = 0
        self._closed = asyncio.Event()
        self._dispatch: set[asyncio.Task[Any]] = set()
        self._session: Any = None
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task = asyncio.create_task(self._read_loop())

    def handlers(
        self,
        *,
        notification: Callable[[str, Any], Awaitable[None]] | None = None,
        request: Callable[[str, Any], Awaitable[Any]] | None = None,
    ) -> None:
        self._notify = notification
        if request:
            self._requests["*"] = request

    async def _send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, separators=(",", ":")).encode()
        if len(data) > self.max_frame:
            raise RpcError("outgoing frame exceeds limit")
        async with self._write_lock:
            sent = self.writer.write(data + b"\n")
            if inspect.isawaitable(sent):
                await sent
            drained = self.writer.drain()
            if inspect.isawaitable(drained):
                await drained

    async def request(self, method: str, params: Any = None, *, timeout: float = 60) -> Any:
        if self._closed.is_set():
            raise RpcError("transport closed")
        if len(self._pending) >= MAX_PENDING:
            raise RpcError("too many pending RPC requests")
        self._counter += 1
        ident = self._counter
        fut = asyncio.get_running_loop().create_future()
        self._pending[ident] = fut
        try:
            await self._send({"id": ident, "method": method, "params": params or {}})
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(ident, None)

    async def notify(self, method: str, params: Any = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def _read_loop(self) -> None:
        error: BaseException = RpcError("transport closed")
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    break
                if len(line) > self.max_frame:
                    raise RpcError("incoming frame exceeds limit")
                msg = json.loads(line)
                if "id" in msg and "method" not in msg:
                    fut = self._pending.get(msg["id"])
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(RpcError(str(msg["error"])))
                        else:
                            fut.set_result(msg.get("result"))
                elif "method" in msg and "id" in msg:
                    handler = self._requests.get("*")
                    if len(self._dispatch) >= MAX_PENDING:
                        await self._send(
                            {
                                "id": msg["id"],
                                "error": {
                                    "code": -32000,
                                    "message": "too many pending server requests",
                                },
                            }
                        )
                        continue
                    if handler is None:
                        task = asyncio.create_task(
                            self._handle_request(lambda _method, _params: _raise_unsupported(), msg)
                        )
                    else:
                        task = asyncio.create_task(self._handle_request(handler, msg))
                    self._dispatch.add(task)
                    task.add_done_callback(self._dispatch.discard)
                elif "method" in msg and self._notify:
                    await self._notify(msg["method"], msg.get("params"))
        except BaseException as exc:
            error = exc
        finally:
            self._closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RpcError(str(error)))
            self.writer.close()

    async def _handle_request(
        self, handler: Callable[[str, Any], Awaitable[Any]], msg: dict[str, Any]
    ) -> None:
        try:
            try:
                signature = inspect.signature(handler)
                accepts_id = len(signature.parameters) >= 3 or any(
                    p.kind == inspect.Parameter.VAR_POSITIONAL
                    for p in signature.parameters.values()
                )
            except (TypeError, ValueError):
                accepts_id = False
            result = await (
                handler(msg["method"], msg.get("params"), msg["id"])
                if accepts_id
                else handler(msg["method"], msg.get("params"))
            )
            await self._send({"id": msg["id"], "result": result})
        except Exception as exc:
            await self._send({"id": msg["id"], "error": {"code": -32000, "message": str(exc)}})

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def initialize(self) -> Any:
        value = await self.request(
            "initialize",
            {
                "clientInfo": {"name": "astrbot-codex-orchestrator", "version": __version__},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.notify("initialized", {})
        return value

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (AttributeError, OSError):
            pass
        if self._session:
            await self._session.close()
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), 2)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        if not self._reader_task.done():
            self._reader_task.cancel()
        for task in self._dispatch:
            task.cancel()
        await asyncio.gather(self._reader_task, *self._dispatch, return_exceptions=True)


async def connect_stdio(
    command: list[str], *, env: dict[str, str] | None = None, cwd: str | None = None
) -> JsonRpcClient:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        cwd=cwd,
        limit=MAX_FRAME + 1,
    )
    assert proc.stdout and proc.stdin
    client = JsonRpcClient(proc.stdout, proc.stdin)
    client._process = proc
    return client


async def connect_websocket(
    url: str, *, token_file: str | None = None, private_opt_in: bool = False
) -> JsonRpcClient:
    import aiohttp

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"ws", "wss"} or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("endpoint must be ws(s) URL without credentials/query/fragment")
    if (
        parsed.scheme == "ws"
        and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        and not private_opt_in
    ):
        raise ValueError("non-loopback ws requires explicit private/tunnel opt-in")
    headers = {}
    if token_file:
        stat = os.stat(token_file)
        if stat.st_mode & 0o077:
            raise ValueError("token file must not be accessible by group/others")
        token = open(token_file, encoding="utf-8").read().strip()
        if token:
            headers["Authorization"] = "Bearer " + token
    session = aiohttp.ClientSession(headers=headers)
    try:
        ws = await session.ws_connect(url, max_msg_size=MAX_FRAME)
    except BaseException:
        await session.close()
        raise

    class WSReader:
        async def readline(self) -> bytes:
            item = await ws.receive()
            if item.type.name != "TEXT":
                return b""
            return item.data.encode() + b"\n"

    class WSWriter:
        async def write(self, data: bytes) -> None:
            await ws.send_str(data.decode().rstrip("\n"))

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            asyncio.create_task(ws.close())

        async def wait_closed(self) -> None:
            await ws.close()

    client = JsonRpcClient(WSReader(), WSWriter())
    client._session = session
    return client
