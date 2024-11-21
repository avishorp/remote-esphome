from abc import ABC, abstractmethod


class Channel(ABC):

    @abstractmethod
    async def send(self, data: bytes) -> None: ...

    @abstractmethod
    async def on_msg(self, data: bytes) -> None: ...

    @abstractmethod
    async def on_shutdown(self) -> None: ...

    @abstractmethod
    async def wait_closed(self) -> None: ...
