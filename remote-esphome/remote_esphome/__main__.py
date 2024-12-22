import asyncio
import logging
import sys
from pathlib import Path

from .acceptor import run_acceptor
from .initiator import run_initiator

logging.basicConfig(level=logging.INFO, format="%(name)s [%(levelname)s]: %(message)s")


_USAGE = """
python3 -m remote_esphome addon <tunnel_port> <proxy_port> <working dir>

  - or -

python3 -m remote_esphome worker <working dir> <acceptor URL>
"""


async def _amain() -> None:
    if sys.argv[1] == "addon":
        tunnel_port = int(sys.argv[2])
        proxy_port = int(sys.argv[3])
        workdir = Path(sys.argv[4])
        await run_acceptor(tunnel_port, proxy_port, workdir)
    elif sys.argv[1] == "worker":
        addon_url = sys.argv[3]
        workdir = Path(sys.argv[2])
        await run_initiator(addon_url, workdir)


if __name__ == "__main__":
    asyncio.run(_amain())
