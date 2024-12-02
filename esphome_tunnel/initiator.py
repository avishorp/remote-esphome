from typing import Callable, Coroutine
from urllib.parse import urlparse, urlunparse
from dataclasses import dataclass
import asyncio
import pickle
import logging

from aiohttp import ClientSession, WSMsgType
from multidict import CIMultiDict

from .header import parse_header, encode_header, WebRequestData, WebResponseData


_logger = logging.getLogger("initiator")


@dataclass
class Channel:
    send: Callable[[bytes], Coroutine[None, None, None]]
    recv: Callable[[], Coroutine[None, None, bytes]]
    logger: logging.Logger


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

                            channel_queue, _ = self._active_channels.get(
                                channel_num, (None, None)
                            )
                            chan_logger = _logger.getChild(f"ch{channel_num}")

                            if channel_queue is None:
                                # New channel - create a channel object
                                _logger.debug(
                                    f"Accepting a new channel ({channel_num})"
                                )
                                channel_queue = asyncio.Queue[bytes | Exception]()
                                await channel_queue.put(body)

                                async def _send(data: bytes):
                                    chan_logger.debug(f"Sending data (len={len(data)})")
                                    await self._outbound_queue.put((channel_num, data))

                                async def _recv():
                                    data = await self._active_channels[channel_num][
                                        0
                                    ].get()
                                    if isinstance(data, Exception):
                                        chan_logger.debug(
                                            f"Exception received: {str(data)}"
                                        )
                                        raise data
                                    else:
                                        chan_logger.debug(
                                            f"Data received (len={len(data)})"
                                        )
                                        if len(data) == 0:
                                            # Cancel the task associated with the channel
                                            self._active_channels[channel_num][
                                                1
                                            ].cancel()
                                            raise ConnectionAbortedError()

                                        return data

                                channel = Channel(_send, _recv, chan_logger)

                                async def _new_channel_task():
                                    try:
                                        await self._handle_new_channel(channel)
                                    except Exception as exc:
                                        channel.logger.exception(exc)
                                    finally:
                                        channel.logger.debug("Closing channel")
                                        del self._active_channels[channel_num]

                                self._active_channels[channel_num] = (
                                    channel_queue,
                                    asyncio.create_task(_new_channel_task()),
                                )

                            else:
                                await channel_queue.put(body)

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

    async def _handle_new_channel(self, channel: Channel):
        channel.logger.debug("Channel created")

        try:
            # Wait for the header
            request_data_raw = await channel.recv()
            if len(request_data_raw) == 0:
                return
            request_data = pickle.loads(request_data_raw)
            if not isinstance(request_data, WebRequestData):
                raise ValueError("Expected request data, but got something else")

            if request_data.is_websocket:
                await self._handle_websocket_channel(request_data, channel)
            else:
                await self._handle_plain_channel(request_data, channel)

        except (pickle.UnpicklingError, ValueError) as exc:
            channel.logger.error(f"Request aborted: {exc}")

        except ConnectionAbortedError:
            pass

    async def _handle_plain_channel(
        self, request_data: WebRequestData, channel: Channel
    ):
        chan_logger = channel.logger

        # Issue a request towards the target
        async def tunnel_to_server():
            try:
                while True:
                    data = await channel.recv()
                    chan_logger.debug(f"Forwarding request data len={len(data)}")
                    yield data
            except (ConnectionAbortedError, ConnectionResetError):
                pass

        url = self._rewrite_url(request_data.url)
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
            await channel.send(pickle.dumps(resp_data))

            # Send the response body to the tunnel
            while True:
                chunk = await resp.content.read(10000)
                if len(chunk) == 0:
                    break
                await channel.send(chunk)

            # Signal the other side that wer'e done with this request
            await channel.send(b"")

        chan_logger.debug("Request completed")

    async def _handle_websocket_channel(
        self, request_data: WebRequestData, channel: Channel
    ):
        chan_logger = channel.logger

        url = self._rewrite_url(request_data.url)
        chan_logger.debug(f"Issuing a WebSocket request to {url}")
        for k, v in request_data.headers.items():
            chan_logger.debug(f"> {k}: {v}")

        async with self._session.ws_connect(
            url,
            headers=request_data.headers,
        ) as resp:

            async def server_to_tunnel():
                async for msg in resp:
                    if len(msg.data) == 0:
                        break
                    await channel.send(pickle.dumps(msg))

            async def tunnel_to_server():
                while True:
                    msg = pickle.loads(await channel.recv())
                    match msg.type:
                        case WSMsgType.BINARY:
                            await resp.send_bytes(msg.data)
                        case WSMsgType.TEXT:
                            await resp.send_str(msg.data)
                        case WSMsgType.PING:
                            await resp.ping(msg.data)
                        case WSMsgType.PONG:
                            await resp.pong(msg.data)
                        case WSMsgType.CLOSE:
                            await resp.close()

            chan_logger.debug("Got WebSocket response")
            resp_data = WebResponseData(0, CIMultiDict())
            await channel.send(pickle.dumps(resp_data))

            await asyncio.wait(
                [
                    asyncio.create_task(server_to_tunnel()),
                    asyncio.create_task(tunnel_to_server()),
                ],
                return_when=asyncio.ALL_COMPLETED,
            )

    def _rewrite_url(self, url: str):
        if self._rewrite_host is not None:
            return urlunparse(urlparse(url)._replace(netloc=self._rewrite_host))
        else:
            return url


def run_initiator(target_url: str, rewrite_host: str | None = None):
    tunnel = TunnelInitiator(target_url, rewrite_host)
    asyncio.run(tunnel.start())
