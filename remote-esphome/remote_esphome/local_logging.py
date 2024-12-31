import os
import logging
from typing import Any

TRACE = 5
DEBUG = logging.DEBUG
ERROR = logging.ERROR
INFO = logging.INFO
WARNING = logging.WARNING
error = logging.error
exception = logging.exception
info = logging.info
warning = logging.warning
    
__all__ = [
    "DEBUG",
    "ERROR",
    "INFO",
    "TRACE",
    "WARNING",
    "Logger",
    "error",
    "exception",
    "getLogger",
    "info",
    "setup_logging",
    "trace",
    "warning",
]


class _LoggerWithTrace(logging.Logger):
    def trace(self, msg: str, *args, **kwargs) -> None: ...

Logger = _LoggerWithTrace


def setup_logging() -> None:
    """Set the logging system up.

    Should be called once before any other logging function.
    """
    logging.addLevelName(TRACE, "TRACE")
    loglevel = logging.getLevelNamesMapping().get(os.environ.get("LOGLEVEL", "").upper(), INFO)
    logging.basicConfig(
        level=loglevel, format="%(asctime)s %(levelname)-9s (%(name)s) %(message)s",
    )


def trace(s, msg: str, *args: Any, **kwargs: Any) -> None:
    """Log a message with the 'trace' level.

    Args:
        msg (str): _description_
    """
    logging.log(TRACE, msg, *args, **kwargs)


def _bound_trace(self: _LoggerWithTrace, msg: str, *args: Any, **kwargs: Any) -> None:
    logging.log(TRACE, msg, *args, **kwargs)


setattr(logging.Logger, "trace", _bound_trace)

def getLogger(name: str) -> Logger:
    return logging.getLogger(name)
