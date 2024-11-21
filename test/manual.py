from random import Random, randint
from pathlib import Path
import asyncio
import hashlib
import threading
import time
import logging


from aiohttp import web, WSMsgType
import lorem.lorem
import lorem
import getport

from esphome_tunnel.acceptor import run_acceptor
from esphome_tunnel.initiator import run_initiator


async def handle_hello(_: web.Request):
    return web.Response(text="Hello world")


async def handle_lorem(request: web.Request):
    rsp = web.StreamResponse()
    await rsp.prepare(request)

    g = lorem.lorem.LoremGenerator()

    try:
        for _ in range(100):
            p = g.gen_paragraph((10, 100), (10, 100), (10, 100))
            await rsp.write(p.encode())
            await asyncio.sleep(0.2)
    except (ConnectionAbortedError, ConnectionResetError):
        pass

    return rsp


async def handle_hash(request: web.Request):
    rsp = web.StreamResponse()
    await rsp.prepare(request)

    md5 = hashlib.md5()
    while True:
        chunk = await request.content.read(10000)
        if len(chunk) == 0:
            break
        md5.update(chunk)

    await rsp.write(f"{md5.hexdigest()}\n".encode())
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

    while True:
        data = await request.content.read(2048)
        if len(data) == 0:
            break

        await response.write(data)

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


def srv(port: int):
    print("running...")
    try:
        logger = logging.getLogger("test_server")
        app = web.Application(logger=logger)
        app.add_routes(
            [
                web.post("/echo_post", handle_post_echo),
                web.get("/echo_ws", handle_post_ws),
                web.get("/gen_random", handle_gen_random),
                web.get("/hello", handle_hello),
                web.get("/lorem", handle_lorem),
                web.post("/hash", handle_hash),
            ]
        )
        web.run_app(app, port=port)

    except Exception as exc:
        logger.exception(exc)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG, format="%(name)s [%(levelname)s]: %(message)s"
    )
    srv_port = getport.get()

    acceptor_thread = threading.Thread(target=run_acceptor, args=(7071, 7072))
    initiator_thread = threading.Thread(
        target=run_initiator, args=("http://localhost:7071/tunnel", "localhost:7076")
    )
    acceptor_thread.start()
    time.sleep(2)
    initiator_thread.start()

    srv(7076)
    acceptor_thread.join()
    initiator_thread.join()
