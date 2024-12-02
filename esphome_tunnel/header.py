from dataclasses import dataclass
import struct

from multidict import CIMultiDict

HEADER_FORMAT = "4sI"
HEADER_MAGIC = b"\x34\xa8\x02\x77"


def parse_header(data: bytes) -> tuple[int, bytes]:
    magic, chan = struct.unpack_from(HEADER_FORMAT, data)
    if magic != HEADER_MAGIC:
        raise RuntimeError("Invalid message")

    return (chan, data[8:])


def encode_header(chan: int, data: bytes) -> bytes:
    return struct.pack(HEADER_FORMAT, HEADER_MAGIC, chan) + data


@dataclass
class WebRequestData:
    method: str
    url: str
    headers: CIMultiDict[str]
    is_websocket: bool


@dataclass
class WebResponseData:
    status: int
    headers: CIMultiDict[str]
