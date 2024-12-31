import asyncio
import sys
import os
from pathlib import Path

from remote_esphome.acceptor import run_acceptor
from remote_esphome.initiator import run_initiator
import remote_esphome.local_logging as logging


_USAGE = """
python3 -m remote_esphome addon <tunnel_port> <proxy_port> <working dir>

  - or -

python3 -m remote_esphome worker <working dir> <acceptor URL>
"""


async def _amain() -> None:
    try:
        if sys.argv[1] == "addon":
            tunnel_port = int(sys.argv[2])
            proxy_port = int(sys.argv[3])
            workdir = Path(sys.argv[4])
            await run_acceptor(tunnel_port, proxy_port, workdir)
        elif sys.argv[1] == "worker":
            workdir = Path(sys.argv[2])
            addon_url = sys.argv[3]
            await run_initiator(addon_url, workdir)
        else:
            sys.stderr.write("Invalid command line\n")
            sys.stderr.write(_USAGE)
            sys.exit(1)

    except (IndexError, ValueError):
        sys.stderr.write(_USAGE)
        sys.exit(1)


if __name__ == "__main__":
    logging.setup_logging()
    asyncio.run(_amain())
