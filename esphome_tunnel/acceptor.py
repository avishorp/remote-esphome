from typing import Callable, Coroutine, Any, cast
from pathlib import Path
import logging
import asyncio
import pickle

from aiohttp import web, WSMsgType

from .header import parse_header, encode_header, WebRequestData, WebResponseData
from .channel import Channel


PROXY_PORT = 6052
TUNNEL_PORT = 7070
_STATIC_DIR = Path(__file__).parent / "static"
_DEFUALT_HTML = _STATIC_DIR / "default.html"


_logger = logging.getLogger("acceptor")


class WebRequestChannel(Channel):
    def __init__(
        self,
        channel_num: int,
        send_fn: Callable[[bytes], Coroutine[None, None, None]],
        response: web.StreamResponse,
        request: web.Request,
    ):
        self._logger = logging.getLogger(f"acceptor.chan{channel_num}")
        self._send_fn = send_fn
        self._request_handler: asyncio.Task | None = None
        self._data_queue = asyncio.Queue[bytes | None]()
        self._response = response
        self._request = request
        self._prepared = False
        self._shutdown = False
        self._closed = asyncio.Event()

    async def send(self, data: bytes) -> None:
        await self._send_fn(data)

    async def on_msg(self, data: bytes) -> None:
        if self._shutdown:
            self._logger.warning("Message received on a shut down channel")
            await self._response.write_eof()
        
        if len(data) == 0:
            raise ConnectionAbortedError()

        try:
            if not self._prepared:
                raw = pickle.loads(data)
                assert isinstance(raw, WebResponseData)
                response_data = cast(WebResponseData, raw)

                self._logger.info(
                    f"Preparing a response with status {response_data.status}"
                )
                self._response.set_status(response_data.status)
                for key, value in response_data.headers.items():
                    self._response.headers.add(key, value)
                await self._response.prepare(self._request)
                self._prepared = True

            else:
                # If the response is already prepared, forward the message into
                # its body

                if len(data) == 0:
                    # Response data exhausted
                    self._logger.debug("End of response data")
                    await self._response.write_eof()
                    await self._close()
                else:
                    self._logger.debug(f"Forwarding response data len={len(data)}")
                    try:
                        await self._response.write(data)
                    except (ConnectionAbortedError, ConnectionResetError):
                        # Client disconnected abruptly
                        await self._close()

        except (ConnectionAbortedError, ConnectionResetError):
            # Channel closed on the client side
            await self.send(b"")
            raise ConnectionAbortedError
    
    async def _close(self) -> None:
        self._closed.set()
        try:
            await self.send(b"")
        except (ConnectionAbortedError, ConnectionResetError):
            pass

    async def on_shutdown(self) -> None:
        self._shutdown = True

    async def wait_closed(self) -> None:
        await self._closed.wait()


class TunnelAcceptor:
    def __init__(self, port: int):
        self._port = port
        self._active_channels: dict[int, WebRequestChannel] = {}
        self._last_channel_number = 0
        self._client_lock = asyncio.Lock()
        self._ws: web.WebSocketResponse | None = None
        self._logger = logging.getLogger("tunnel_acceptor")

    async def start(self):
        app = web.Application()
        app.add_routes([web.get("/tunnel", self._handle_tunnel_connect)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", self._port)
        self._logger.info(f"Accepting tunnel connection on port {self._port}")
        await site.start()

    async def _handle_tunnel_connect(self, request: web.Request):
        self._logger.info(
            f"Tunnel acceptor connected from {request.remote or '<unknown>'}"
        )

        if self._client_lock.locked():
            self._logger.warning(
                "Only one tunnel connection can be accepted, rejecting"
            )
            return web.Response(status=429, text="Only one client can be accepted")

        async with self._client_lock:
            # Create a websocket response
            ws = web.WebSocketResponse()
            self._ws = ws
            await ws.prepare(request)

            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    channel_num, body = parse_header(msg.data)
                    channel = self._active_channels.get(channel_num)
                    if channel is None:
                        self._logger.warning(
                            f"Unsolicited message on channel {channel_num}"
                        )
                        continue

                    try:
                        await channel.on_msg(body)
                    except (ConnectionResetError, ConnectionAbortedError):
                        del self._active_channels[channel_num]

            self._logger.info("Initiator disconnected")
            for chan in self._active_channels.values():
                await chan.on_shutdown()
            self._active_channels = {}
            self._ws = None

        return ws

    def create_channel(self, *args: Any, **kwargs: Any) -> Channel | None:
        ch_number = self._last_channel_number
        self._last_channel_number += 1

        if self._ws is not None:
            # If the tunnel is up, return a new channel object
            async def _sender(data: bytes):
                if self._ws is not None:
                    await self._ws.send_bytes(encode_header(ch_number, data))
                else:
                    raise ConnectionAbortedError()

            ch = WebRequestChannel(ch_number, _sender, *args, **kwargs)
            self._active_channels[ch_number] = ch
            self._logger.debug(f"New channel created num={ch_number}")
            return ch

        else:
            # If the tunnel is down, return None
            self._logger.debug("The tunnel is down, failing to create a channel")
            return None


class WebProxyServer(object):
    def __init__(self, port: int, tunnel: TunnelAcceptor):
        self._port = port
        self._tunnel = tunnel
        self._logger = logging.getLogger("proxy_server")

    async def start(self):
        app = web.Application()

        app.add_routes(
            [
                web.static("/__local", _STATIC_DIR),
                web.route("*", "/{tail:.*}", self._handle_request),
            ]
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", self._port)
        self._logger.info(f"Accepting web requests on port {self._port}")
        await site.start()

    async def _handle_request(self, request: web.Request):
        self._logger.info(f"Request to {request.url}")

        # First, try to create a tunnel channel.
        resp = web.StreamResponse()
        channel = self._tunnel.create_channel(resp, request)

        if channel is None:
            # If a channel could not be created, fallback to the
            # default response.
            r = web.Response(text="Tunnel is down")
            return r

        # Send the request object down the tunnel
        self._logger.debug(f"Sending request object for {request.url}")
        request_data = WebRequestData(
            request.method, str(request.url), request.headers.copy()
        )
        await channel.send(pickle.dumps(request_data))

        async def _relay_request():
            # Relay incoming request data to the tunnel.
            async for chunk in request.content:
                self._logger.debug(f"Forwarding request data len={len(chunk)}")
                await channel.send(chunk)

        # Wait until channel is closed
        #await channel.wait_closed()
        await asyncio.wait([channel.wait_closed(), ])

        self._logger.info("Request completed")
        return resp


def run_acceptor(tunnel_port: int, proxy_port: int):
    async def _runner():
        tunnel = TunnelAcceptor(tunnel_port)
        proxy = WebProxyServer(proxy_port, tunnel)
        t1 = asyncio.create_task(tunnel.start())
        t2 = asyncio.create_task(proxy.start())
        await asyncio.sleep(1000)
        # await asyncio.gather(tunnel.start(), proxy.start(), return_exceptions=True)
        # await asyncio.sleep(2)

    asyncio.run(_runner())
