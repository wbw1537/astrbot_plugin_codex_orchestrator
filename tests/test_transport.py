import asyncio
import json

import pytest

from codex_orchestrator.transport import JsonRpcClient, RpcError


async def test_pending_human_request_does_not_block_rpc_and_disconnect_closes_future():
    async def handle(reader, writer):
        json.loads(await reader.readline())
        writer.write(
            b'{"id":91,"method":"item/commandExecution/requestApproval","params":{"threadId":"x"}}\n'
        )
        await writer.drain()
        query = json.loads(await reader.readline())
        writer.write(json.dumps({"id": query["id"], "result": {"ok": True}}).encode() + b"\n")
        await writer.drain()
        await reader.readline()  # approval response after human answer
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    release = asyncio.Event()

    async def request_handler(method, params):
        await release.wait()
        return {"decision": "decline"}

    client = JsonRpcClient(reader, writer)
    client.handlers(request=request_handler)
    pending = asyncio.create_task(client.request("thread/list", timeout=1))
    await asyncio.sleep(0.02)
    query = asyncio.create_task(client.request("thread/list", timeout=1))
    assert await query == {"ok": True}
    release.set()
    await asyncio.sleep(0.02)
    with pytest.raises(RpcError):
        await pending
    with pytest.raises(RpcError):
        await client.request("slow", timeout=1)
    await client.close()
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_frame_limit_rejects_outgoing():
    reader = asyncio.StreamReader()

    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    client = JsonRpcClient(reader, Writer(), max_frame=10)
    with pytest.raises(RpcError, match="outgoing frame"):
        await client.notify("a-very-long-notification")
    await client.close()


@pytest.mark.asyncio
async def test_stdio_child_handshake_and_cleanup():
    import sys

    from codex_orchestrator.transport import connect_stdio

    code = "\n".join(
        [
            "import sys,json",
            "for line in sys.stdin:",
            " m=json.loads(line)",
            " if m.get('method')=='initialize': r={'ok':True}",
            " elif m.get('method')=='thread/list': r={'data':[]}",
            " else: continue",
            " sys.stdout.write(json.dumps({'id':m['id'],'result':r})+chr(10));sys.stdout.flush()",
        ]
    )
    client = await connect_stdio([sys.executable, "-u", "-c", code])
    assert (await asyncio.wait_for(client.initialize(), 2))["ok"]
    assert await client.request("thread/list", timeout=2) == {"data": []}
    proc = client._process
    await client.close()
    assert proc and proc.returncode is not None


@pytest.mark.asyncio
async def test_websocket_loopback_handshake():
    from aiohttp import web

    from codex_orchestrator.transport import connect_websocket

    async def ws_handler(request):
        ws = web.WebSocketResponse(max_msg_size=2 * 1024 * 1024)
        await ws.prepare(request)
        async for item in ws:
            if item.type.name == "TEXT":
                msg = json.loads(item.data)
                if msg.get("method") == "initialize":
                    await ws.send_json({"id": msg["id"], "result": {"protocolVersion": "test"}})
                elif msg.get("method") == "thread/list":
                    await ws.send_json({"id": msg["id"], "result": {"data": []}})
        return ws

    app = web.Application()
    app.router.add_get("/", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = await connect_websocket(f"ws://127.0.0.1:{port}/")
    assert (await client.initialize())["protocolVersion"] == "test"
    assert await client.request("thread/list") == {"data": []}
    await client.close()
    await runner.cleanup()
