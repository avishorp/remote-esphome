from typing import Callable, Coroutine, Any
from pathlib import Path
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
import asyncio
import pickle

from aiohttp import web, WSMsgType, WSMessage

from .header import parse_header, encode_header, WebRequestData, WebResponseData


PROXY_PORT = 6052
TUNNEL_PORT = 7070
_STATIC_DIR = Path(__file__).parent / "static"
_DEFUALT_HTML = _STATIC_DIR / "default.html"


class TunnelDownException(Exception):
    pass


_OutboundDataQueue = asyncio.Queue[tuple[int, bytes]]


@dataclass
class Channel:
    send: Callable[[bytes], Coroutine[None, None, None]]
    recv: Callable[[], Coroutine[None, None, bytes]]
    logger: logging.Logger


class TunnelAcceptor:
    def __init__(self, tunnel_port: int, proxy_port: int, on_connect: Callable[[], Coroutine[None, None, None]] | None = None):
        self._tunnel_port = tunnel_port
        self._proxy_port = proxy_port
        self._client_lock = asyncio.Lock()
        self._tunnel_logger = logging.getLogger("acceptor")
        self._proxy_logger = logging.getLogger("proxy")
        self._outbound_queue: _OutboundDataQueue | None = None
        self._active_channels = dict[int, asyncio.Queue[bytes | Exception]]()
        self._last_channel_num = 0
        self._srv_task: asyncio.Task | None = None
        self._on_connect = on_connect

    async def start(self):
        self._srv_task = asyncio.create_task(self._start())

    async def stop(self):
        assert self._srv_task is not None
        self._srv_task.cancel()

    async def __aenter__(self):
        await self.start()

    async def __aexit__(self, exc_type: Any, exc_value: Any, exc_tb: Any):
        await self.stop()
        
    async def _start(self):
        await asyncio.wait(
            [
                asyncio.create_task(self._start_tunnel_server()),
                asyncio.create_task(self._start_proxy_server()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )

    async def _start_tunnel_server(self):
        app = web.Application()
        app.add_routes([web.get("/tunnel", self._handle_tunnel_connect)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, None, self._tunnel_port)
        self._tunnel_logger.info(
            f"Accepting tunnel connection on port {self._tunnel_port}"
        )
        await site.start()

    async def _start_proxy_server(self):
        app = web.Application()
        app.add_routes(
            [
                web.static("/__local", _STATIC_DIR),
                web.route("*", "/{tail:.*}", self._handle_proxy_request),
            ]
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", self._proxy_port)
        self._proxy_logger.info(f"Accepting web requests on port {self._proxy_port}")
        await site.start()

    async def _handle_tunnel_connect(self, request: web.Request):
        self._tunnel_logger.info(
            f"Tunnel acceptor connected from {request.remote or '<unknown>'}"
        )

        if self._client_lock.locked():
            self._tunnel_logger.warning(
                "Only one tunnel connection can be accepted, rejecting"
            )
            return web.Response(status=429, text="Only one client can be accepted")

        async with self._client_lock:
            # Create a websocket response
            ws = web.WebSocketResponse()
            self._ws = ws
            await ws.prepare(request)
            
            if self._on_connect is not None:
                await self._on_connect()

            # Create an outbound queue
            self._outbound_queue = asyncio.Queue[tuple[int, bytes]]()

            async def _recv_to_queue():
                async for msg in ws:
                    if msg.type == WSMsgType.BINARY:
                        channel_num, body = parse_header(msg.data)
                        if channel_num in self._active_channels:
                            self._active_channels[channel_num].put_nowait(body)
                        else:
                            if len(body) > 0:
                                self._tunnel_logger.warning(
                                    f"Unsolicited message on channel {channel_num}"
                                )

            async def _transmit_to_queue():
                while True:
                    if self._outbound_queue is None:
                        break

                    # Get a data item from the outbound queue, encode it
                    # and send it down the WS channel
                    channel_num, data = await self._outbound_queue.get()
                    await ws.send_bytes(encode_header(channel_num, data))

            # Initiate transmit and receive tasks
            await asyncio.wait(
                [
                    asyncio.create_task(_recv_to_queue()),
                    asyncio.create_task(_transmit_to_queue()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Clear the outbound queue.
            self._outbound_queue = None

            # Insert an exception to all active channels and wait for
            # them to get cleared up
            for ch in self._active_channels.values():
                ch.put_nowait(ConnectionAbortedError())

        return ws

    @asynccontextmanager
    async def _create_channel(self):
        if self._outbound_queue is None:
            raise TunnelDownException()

        # Assign a new number to the channel
        self._last_channel_num += 1
        channel_num = self._last_channel_num

        # Create a data queue
        self._active_channels[channel_num] = asyncio.Queue[bytes | Exception]()

        # Create a channel logger
        chan_logger = self._tunnel_logger.getChild(f"ch{channel_num}")

        async def _send(data: bytes) -> None:
            # If there's no outbound queue, the tunnel is inactive
            if self._outbound_queue is None:
                chan_logger.warning("Sending data to aborted channel")
                raise ConnectionAbortedError()

            chan_logger.debug(f"Sending data (len={len(data)})")
            await self._outbound_queue.put((channel_num, data))

        async def _recv() -> bytes:
            try:
                # Get the queue for this channel, and wait for data
                queue = self._active_channels[channel_num]
                data = await queue.get()

                # If an exception was received, throw it. Otherwise,
                # return the data item
                if isinstance(data, Exception):
                    chan_logger.debug(f"Exception received: {str(data)}")
                    raise data
                else:
                    f"Data received (len={len(data)})"
                    return data

            except KeyError:
                # The channel has been closed for some reason
                raise ConnectionAbortedError()

        yield Channel(_send, _recv, chan_logger)

        # Cleanup
        del self._active_channels[channel_num]

    async def _handle_proxy_request(self, request: web.Request):
        is_websocket = (
            request.headers.get("Upgrade", "").lower() == "websocket"
            and request.headers.get("Connection", "").lower() == "upgrade"
        )
        resp = web.WebSocketResponse() if is_websocket else web.StreamResponse()

        try:
            async with self._create_channel() as channel:
                channel.logger.debug("Channel created")

                # Send the request
                for k, v in request.headers.items():
                    channel.logger.debug(f"> {k}: {v}")
                request_data = WebRequestData(
                    request.method,
                    str(request.url),
                    request.headers.copy(),
                    is_websocket,
                )
                await channel.send(pickle.dumps(request_data))
                channel.logger.debug(f"Request object sent for url={request.url}")

                # Wait for the response header
                resp_header_raw = await channel.recv()
                try:
                    resp_header = pickle.loads(resp_header_raw)
                    if not isinstance(resp_header, WebResponseData):
                        self._proxy_logger.error("Unexpected response from remote side")
                        return resp

                    if is_websocket:
                        assert isinstance(resp, web.WebSocketResponse)
                        await resp.prepare(request)
                        await self._relay_websocket(channel, resp)
                    else:
                        resp.set_status(resp_header.status)
                        resp.headers.add("X-Tunnel-Up", "true")
                        for k, v in resp_header.headers.items():
                            channel.logger.debug(f"< {k}: {v}")
                            resp.headers.add(k, v)
                        await resp.prepare(request)

                        await self._relay_plain_body(channel, request, resp)

                    # Let the remote side know that the client has been closed
                    await channel.send(b"")

                except pickle.UnpicklingError:
                    self._proxy_logger.error("Failed to unpickle response object")
                    return resp

            return resp

        except TunnelDownException:
            # When the tunnel is down, fallback to a static predefined
            # content
            resp.headers.add("X-Tunnel-Up", "false")
            await resp.prepare(request)
            await resp.write("Tunnel is down".encode())

        except (ConnectionAbortedError, ConnectionResetError):
            pass

        return resp

    async def _relay_plain_body(
        self, channel: Channel, request: web.Request, response: web.StreamResponse
    ):
        async def client_to_tunnel():
            # Stream request data to the tunnel
            try:
                while True:
                    chunk = await request.content.read(16 * 1024)
                    if len(chunk) == 0:
                        break
                    await channel.send(chunk)
            except (ConnectionAbortedError, ConnectionResetError):
                pass

        async def tunnel_to_client():
            # Stream response data from the tunnel to the client
            try:
                while True:
                    data = await channel.recv()
                    if len(data) == 0:
                        break
                    await response.write(data)

            except (ConnectionAbortedError, ConnectionResetError):
                pass

        # Create tasks to pass the data between the tunnel and the client
        await asyncio.wait(
            [
                asyncio.create_task(client_to_tunnel()),
                asyncio.create_task(tunnel_to_client()),
            ],
            return_when=asyncio.ALL_COMPLETED,
        )
        pass

    async def _relay_websocket(self, channel: Channel, ws: web.WebSocketResponse):
        async def client_to_tunnel():
            async for msg in ws:
                await channel.send(pickle.dumps(msg))

        async def tunnel_to_client():
            try:
                while True:
                    msg = pickle.loads(await channel.recv())
                    assert isinstance(msg, WSMessage)
                    if len(msg) == 0:
                        break

                    match msg.type:
                        case WSMsgType.BINARY:
                            await ws.send_bytes(msg.data)
                        case WSMsgType.TEXT:
                            await ws.send_str(msg.data)
                        case WSMsgType.PING:
                            await ws.ping(msg.data)
                        case WSMsgType.PONG:
                            await ws.pong(msg.data)
                        case WSMsgType.CLOSE:
                            await ws.close()

            except (ConnectionAbortedError, ConnectionResetError):
                pass

        # Create tasks to pass the data between the tunnel and the client
        await asyncio.wait(
            [
                asyncio.create_task(client_to_tunnel()),
                asyncio.create_task(tunnel_to_client()),
            ],
            return_when=asyncio.ALL_COMPLETED,
        )


def run_acceptor(tunnel_port: int, proxy_port: int):
    async def _runner():
        async with TunnelAcceptor(tunnel_port, proxy_port):
            while True:
                await asyncio.sleep(3600)

    asyncio.run(_runner())
