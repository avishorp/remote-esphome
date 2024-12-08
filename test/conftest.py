from random import Random, randint
from contextlib import asynccontextmanager
import asyncio
import hashlib

from aiohttp import web, WSMsgType, request as aiohttp_request, ClientSession
from pytest import fixture
import getport
import pytest_asyncio

from remote_esphome import TunnelInitiator, TunnelAcceptor


@pytest_asyncio.fixture
async def test_server():
    async def handle_get_hello(_: web.Request):
        return web.Response(text="Hello world")

    async def handle_random(request: web.Request):
        rsp = web.StreamResponse()
        await rsp.prepare(request)

        seed = int(request.query.get("seed", 1000))
        count = int(request.query.get("count", 4096))
        g = Random(seed)

        n = 0
        try:
            while n < count:
                cl = max(count - n, 256)
                chunk = g.randbytes(cl)
                data = "".join([f"{b:02x}" for b in chunk])
                await rsp.write(data.encode())
                await asyncio.sleep(0.2)
                n += cl
        except (ConnectionAbortedError, ConnectionResetError):
            pass

        return rsp

    async def handle_gen_random(request: web.Request):
        seed = randint(0, 1000000000)
        r = Random(seed)
        response = web.StreamResponse(headers={"X-Seed": str(seed)})
        await response.prepare(request)

        while True:
            try:
                data = r.randbytes(1100)
                await response.write(data)
            except (ConnectionAbortedError, ConnectionResetError):
                break

        return response

    async def handle_post_echo(request: web.Request):
        response = web.StreamResponse()
        response.headers.add("X-Data-Echo", str(request.headers.get("X-Data")))
        await response.prepare(request)
        data_buffer = bytearray()

        async def _sender():
            nonlocal data_buffer

            try:
                while True:
                    if len(data_buffer) < 1000:
                        await asyncio.sleep(0.2)
                    send_chunk_size = randint(1, len(data_buffer))

                    send_chunk = data_buffer[:send_chunk_size]
                    del data_buffer[:send_chunk_size]
                    await response.write(send_chunk)
            except asyncio.CancelledError:
                await response.write(data_buffer)  # Flush
            pass

        sender_task = asyncio.create_task(_sender())

        while True:
            chunk = await request.content.read(2048)
            if len(chunk) == 0:
                break
            data_buffer.extend(chunk)

        sender_task.cancel()
        await sender_task

        return response

    async def handle_post_ws(request: web.Request):
        response = web.WebSocketResponse()
        await response.prepare(request)

        async for msg in response:
            match msg.type:
                case WSMsgType.BINARY:
                    await response.send_bytes(msg.data)

                case WSMsgType.TEXT:
                    await response.send_str(msg.data)

                case WSMsgType.PING:
                    await response.pong()

        await response.close()
        return response

    port = getport.get()
    app = web.Application()
    app.add_routes(
        [
            web.post("/echo", handle_post_echo),
            web.get("/echo_ws", handle_post_ws),
            web.get("/gen_random", handle_gen_random),
            web.get("/hello", handle_get_hello),
            web.get("/random", handle_random),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=port)
    await site.start()

    yield port

    await site.stop()


@fixture
def tunnel():
    @asynccontextmanager
    async def _setup_tunnel(src_port: int):
        tunnel_port = getport.get()
        proxy_port = getport.get()
        tunnel_ready = asyncio.Future()

        async def _set_tunnel_ready():
            tunnel_ready.set_result(None)

        async with TunnelAcceptor(tunnel_port, proxy_port, _set_tunnel_ready):
            await asyncio.sleep(2)
            async with TunnelInitiator(
                f"http://localhost:{tunnel_port}/tunnel",
                rewrite_host=f"localhost:{src_port}",
            ):
                await tunnel_ready
                yield proxy_port

    return _setup_tunnel


@fixture
def check_get_hello():
    async def _worker(port: int):
        async with aiohttp_request("GET", f"http://localhost:{port}/hello") as resp:
            assert resp.status == 200
            assert (await resp.read()).decode() == "Hello world"

    return _worker


@fixture
def check_get_random():
    async def _worker(port: int, count: int = 1024, seed: int = 123):
        gen = Random(seed)
        expected = "".join([f"{b:02x}" for b in gen.randbytes(count)])

        async with aiohttp_request(
            "GET", f"http://localhost:{port}/random?count={count}&seed={seed}"
        ) as resp:
            assert resp.status == 200
            received = await resp.read()
            assert received.decode() == expected

    return _worker


@fixture
def check_post_echo():
    async def _worker(port: int, count: int = 1000000, seed: int = 123):
        gen = Random(seed)
        data = gen.randbytes(count)

        async with aiohttp_request(
            "POST", f"http://localhost:{port}/echo",
            data=data
        ) as resp:
            assert resp.status == 200
            received = await resp.read()
            assert received == data

    return _worker


@fixture
def check_ws_echo():
    async def _worker(port: int, msg_count: int = 10000, seed: int = 123):
        async with ClientSession() as sess:
            async with sess.ws_connect(f"http://localhost:{port}/echo_ws") as ws:
                q = asyncio.Queue[bytes](maxsize=10)

                async def _generate():
                    gen = Random(seed)
                    for _ in range(msg_count):
                        msg_len = gen.randint(10, 10000)
                        msg = gen.randbytes(msg_len)
                        await ws.send_bytes(msg)
                        await q.put(msg)
                    
                    await ws.close()

                asyncio.create_task(_generate())

                async for rmsg in ws:
                    if rmsg.type == WSMsgType.BINARY:
                        assert rmsg.data == await q.get()

    return _worker
