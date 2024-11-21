from typing import Callable, Coroutine, cast
from urllib.parse import urlparse, urlunparse
import asyncio
import pickle
import logging

from aiohttp import ClientSession, WSMsgType

from .header import parse_header, encode_header, WebRequestData, WebResponseData
from .channel import Channel


_logger = logging.getLogger("initiator")


class WebRequestChannel(Channel):
    def __init__(
        self,
        channel_num: int,
        session: ClientSession,
        send_fn: Callable[[bytes], Coroutine[None, None, None]],
        rewrite_host: str | None,
    ):
        self._logger = logging.getLogger(f"initiator.chan{channel_num}")
        self._send_fn = send_fn
        self._session = session
        self._rewrite_host = rewrite_host
        self._request_handler: asyncio.Task | None = None
        self._data_queue = asyncio.Queue[bytes | None]()
        self._closed = asyncio.Event()

    async def send(self, data: bytes) -> None:
        await self._send_fn(data)

    async def on_msg(self, data: bytes) -> None:
        self._logger.debug(f"Message received (len={len(data)})")
        if self._request_handler is None:
            # First message on a channel is a request object
            self._logger.debug("First message received")
            raw = pickle.loads(data)
            assert isinstance(raw, WebRequestData)
            request_data = cast(WebRequestData, raw)
            self._request_handler = asyncio.create_task(
                self._handle_request(request_data)
            )

        else:
            # If a request handler is already set up, put the received data in the receive
            # queue.
            self._data_queue.put_nowait(data)

    async def on_shutdown(self) -> None:
        if self._request_handler is not None and not self._request_handler.done():
            self._request_handler.cancel()
            self._data_queue.put_nowait(None)
        self._request = None

    async def _handle_request(self, request_data: WebRequestData):
        async def data_sender():
            try:
                while True:
                    data = await self._data_queue.get()
                    if data is None or len(data) == 0:
                        break
                    self._logger.debug(f"Forwarding request data len={len(data)}")
                    yield data
            except (ConnectionAbortedError, ConnectionResetError):
                pass

        url = request_data.url
        if self._rewrite_host is not None:
            url = urlunparse(urlparse(url)._replace(netloc=self._rewrite_host))
        self._logger.debug(f"Issuing a {request_data.method} request to {url}")

        async with self._session.request(
            request_data.method,
            url,
            headers=request_data.headers,
            data=data_sender(),
        ) as resp:
            # Send the response back to the request initiator
            self._logger.debug(f"Got response with status {resp.status}")
            resp_data = WebResponseData(resp.status, resp.headers.copy())
            await self.send(pickle.dumps(resp_data))

            while True:
                resp_data = await resp.content.read(10000)
                if len(resp_data) == 0:
                    break
                self._logger.debug(f"Forwarding response data len={len(resp_data)}")
                await self.send(resp_data)

        self._logger.debug("Request closed")
        try:
            await self.send(b"")
        except (ConnectionAbortedError, ConnectionResetError):
            pass

        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()


class TunnelInitiator:
    def __init__(self, url: str, rewrite_host: str | None = None):
        self._url = url
        self._rewrite_host = rewrite_host
        self._active_channels: dict[int, Channel] = {}

    async def start(self):
        _logger.info("Starting tunnel initiator")
        _logger.info(f"Trying to connect {self._url}")
        try:
            async with ClientSession() as session:
                async with session.ws_connect(self._url) as ws:
                    _logger.info("Tunnel connected successfully")

                    async for msg in ws:

                        if msg.type == WSMsgType.BINARY:
                            # Parse the header
                            channel_num, body = parse_header(msg.data)
                            channel = self._active_channels.get(channel_num)

                            if channel is None:
                                # New channel - create a channel object
                                _logger.debug(
                                    f"Accepting a new channel ({channel_num})"
                                )

                                async def _send_channel(data: bytes):
                                    await ws.send_bytes(
                                        encode_header(channel_num, data)
                                    )

                                self._active_channels[channel_num] = WebRequestChannel(
                                    channel_num,
                                    session,
                                    _send_channel,
                                    self._rewrite_host,
                                )

                            # Send the received data to the appropriate channel
                            asyncio.create_task(
                                self._active_channels[channel_num].on_msg(body)
                            )

        except (ConnectionAbortedError, ConnectionResetError):
            pass

        finally:
            # Shut down active channels (if any)
            _logger.info("Tunnel connection closed")
            await asyncio.gather(
                *(chan.on_shutdown() for chan in self._active_channels.values())
            )
            self._active_channels = {}


def run_initiator(target_url: str, rewrite_host: str | None = None):
    tunnel = TunnelInitiator(target_url, rewrite_host)
    asyncio.run(tunnel.start())
