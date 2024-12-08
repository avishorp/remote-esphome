import sys
import logging

from .acceptor import run_acceptor
from .initiator import run_initiator

logging.basicConfig(level=logging.DEBUG, format="%(name)s [%(levelname)s]: %(message)s")

if sys.argv[1] == "acceptor":
    run_acceptor(7071, 7072)
elif sys.argv[1] == "initiator":
    run_initiator("http://localhost:7071/tunnel", "localhost:7076")
