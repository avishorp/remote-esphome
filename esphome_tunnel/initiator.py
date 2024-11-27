from typing import Callable, Coroutine
from urllib.parse import urlparse, urlunparse
import asyncio
import pickle
import logging

from aiohttp import ClientSession, WSMsgType

from .header import parse_header, encode_header, WebRequestData, WebResponseData


_logger = logging.getLogger("initiator")


class TunnelInitiator:
    def __init__(self, url: str, rewrite_host: str | None = None):
        self._url = url
        self._rewrite_host = rewrite_host
        self._active_channels: dict[
            int, tuple[asyncio.Queue[bytes | Exception], asyncio.Task]
        ] = {}
        self._outbound_queue = asyncio.Queue[tuple[int, bytes]]()

    async def start(self):
        _logger.info("Starting tunnel initiator")
        _logger.info(f"Trying to connect {self._url}")
        send_task = None
        try:
            async with ClientSession() as self._session:
                async with self._session.ws_connect(self._url) as ws:
                    _logger.info("Tunnel connected successfully")

                    self._outbound_queue = asyncio.Queue[tuple[int, bytes]]()

                    async def sender():
                        try:
                            while True:
                                channel_num, data = await self._outbound_queue.get()
                                await ws.send_bytes(encode_header(channel_num, data))
                        except (ConnectionResetError, ConnectionAbortedError):
                            pass

                    send_task = asyncio.create_task(sender())

                    async for msg in ws:

                        if msg.type == WSMsgType.BINARY:
                            # Parse the header
                            channel_num, body = parse_header(msg.data)
                            channel, _ = self._active_channels.get(
                                channel_num, (None, None)
                            )
                            chan_logger = _logger.getChild(f"ch{channel_num}")

                            if channel is None:
                                # New channel - create a channel object
                                _logger.debug(
                                    f"Accepting a new channel ({channel_num})"
                                )
                                channel = asyncio.Queue[bytes | Exception]()
                                await channel.put(body)

                                async def send(data: bytes):
                                    chan_logger.debug(f"Sending data (len={len(data)})")
                                    await self._outbound_queue.put((channel_num, data))

                                async def recv():
                                    data = await self._active_channels[channel_num][
                                        0
                                    ].get()
                                    if isinstance(data, Exception):
                                        chan_logger.debug(
                                            f"Exception received: {str(data)}"
                                        )
                                        raise data
                                    else:
                                        if len(data) == 0:
                                            # Cancel the task associated with the channel
                                            chan_logger.debug(
                                                f"Data received (len={len(data)})"
                                            )
                                            self._active_channels[channel_num][
                                                1
                                            ].cancel()

                                        return data

                                def close():
                                    chan_logger.debug("Closing channel")
                                    del self._active_channels[channel_num]

                                channel_task = asyncio.create_task(
                                    self._handle_channel(chan_logger, send, recv, close)
                                )
                                self._active_channels[channel_num] = (
                                    channel,
                                    channel_task,
                                )

                            else:
                                await channel.put(body)

        except (ConnectionAbortedError, ConnectionResetError):
            pass

        finally:
            # Shut down active channels (if any)
            _logger.info("Tunnel connection closed")

            # Cancel the send task (if it's still active)
            if send_task is not None:
                send_task.cancel()

            # Shut down any active channel
            for _, channel_task in self._active_channels.values():
                channel_task.cancel()

            self._active_channels = {}

    async def _handle_channel(
        self,
        chan_logger: logging.Logger,
        send: Callable[[bytes], Coroutine[None, None, None]],
        recv: Callable[[], Coroutine[None, None, bytes]],
        close: Callable[[], None],
    ):
        chan_logger.debug("Channel created")

        try:
            # Wait for the header
            request_data_raw = await recv()
            request_data = pickle.loads(request_data_raw)
            if not isinstance(request_data, WebRequestData):
                raise ValueError("Expected request data, but got something else")

            # Issue a request towards the target
            async def tunnel_to_server():
                try:
                    while True:
                        data = await recv()
                        if data is None or len(data) == 0:
                            break
                        chan_logger.debug(f"Forwarding request data len={len(data)}")
                        yield data
                except (ConnectionAbortedError, ConnectionResetError):
                    pass

            url = request_data.url
            if self._rewrite_host is not None:
                url = urlunparse(urlparse(url)._replace(netloc=self._rewrite_host))
            chan_logger.debug(f"Issuing a {request_data.method} request to {url}")
            for k, v in request_data.headers.items():
                chan_logger.debug(f"> {k}: {v}")

            async with self._session.request(
                request_data.method,
                url,
                headers=request_data.headers,
                data=tunnel_to_server(),
            ) as resp:
                # Send the response back to the request initiator
                chan_logger.debug(f"Got response with status {resp.status}")
                for k, v in resp.headers.items():
                    chan_logger.debug(f"< {k}: {v}")

                resp_data = WebResponseData(resp.status, resp.headers.copy())
                await send(pickle.dumps(resp_data))

                # Send the response body to the tunnel
                while True:
                    chunk = await resp.content.read(10000)
                    if len(chunk) == 0:
                        break
                    await send(chunk)

                # Signal the other side that wer'e done with this request
                await send(b"")

            chan_logger.debug("Request completed")

        except (pickle.UnpicklingError, ValueError) as exc:
            chan_logger.error(f"Request aborted: {exc}")

        finally:
            close()


def run_initiator(target_url: str, rewrite_host: str | None = None):
    tunnel = TunnelInitiator(target_url, rewrite_host)
    asyncio.run(tunnel.start())
