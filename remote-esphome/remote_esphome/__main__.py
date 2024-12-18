import asyncio
import logging
import sys
from pathlib import Path

from .acceptor import run_acceptor
from .initiator import run_initiator

logging.basicConfig(level=logging.INFO, format="%(name)s [%(levelname)s]: %(message)s")


_USAGE = """
python3 -m remote_esphome acceptor <tunnel_port> <proxy_port>

  - or -

python3 -m remote_esphome initiator <acceptor URL> <ESPHome Dashboard Dir>
"""


async def _amain() -> None:
    if sys.argv[1] == "acceptor":
        await run_acceptor(7071, 7072, Path("acceptor_workdir"))
    elif sys.argv[1] == "initiator":
        await run_initiator(sys.argv[2], Path("initiator_workdir"))


if __name__ == "__main__":
    asyncio.run(_amain())
