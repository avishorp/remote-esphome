import asyncio
import hashlib
import http
import logging
from pathlib import Path
from typing import cast


from aiohttp import request as aiohttp_request, FormData
from watchdog.events import (
    FileClosedEvent,
    FileClosedNoWriteEvent,
    FileModifiedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer


class _FsEventHandler(FileSystemEventHandler):
    def __init__(
        self,
        target_queue: asyncio.Queue[str | None],
        event_loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._target_queue = target_queue
        self._event_loop = event_loop

    def on_any_event(self, event: FileSystemEvent) -> None:
        if isinstance(event, (FileClosedEvent | FileModifiedEvent)):
            self._event_loop.call_soon_threadsafe(
                self._target_queue.put_nowait,
                str(event.src_path),
            )


class FileSyncService:
    def __init__(self, target_dir: Path, url: str) -> None:
        self._target_dir = target_dir
        self._url = url
        self._change_queue = asyncio.Queue[Path | None]()

    async def __aenter__(self) -> None:
        # First, down-sync remote files
        await self._initial_download()

        # Start an observer on the target directory
        event_handler = _FsEventHandler(self._change_queue, asyncio.get_event_loop())
        self._observer = Observer()
        self._observer.schedule(event_handler, str(self._target_dir), recursive=True)
        self._observer.start()

        # Create a task for uploading changed files
        self._upload_task = asyncio.create_task(self._upload_changes())

    async def __aexit__(self, a, b, c) -> None:
        self._observer.stop()
        self._observer.join()
        self._change_queue.put_nowait(None)
        await self._upload_task

    async def _initial_download(self) -> None:
        # Get a list of files from the remote
        async with aiohttp_request("GET", self._url + "/state") as rsp:
            if rsp.status != http.HTTPStatus.OK:
                raise RuntimeError("Failed to fetch file list")

            file_list = await rsp.json()
            for file_entry in file_list["files"]:
                filename = cast(str, file_entry["name"])
                local_filename = (self._target_dir / filename).absolute()
                if not local_filename.is_relative_to(self._target_dir.absolute()):
                    logging.warning(
                        f"Refusing to sync out-of-tree file {local_filename}"
                    )
                remote_checksum = file_entry["checksum"]

                # Create a directory if necessary
                file_dir = local_filename.parent
                if file_dir != Path() and not file_dir.exists():
                    file_dir.mkdir(parents=True)

                # Compare the checksum of the local and the remote one
                local_checksum = (
                    hashlib.md5(local_filename.read_bytes()).hexdigest()
                    if local_filename.exists()
                    else ""
                )

                if remote_checksum != local_checksum:
                    logging.info(f"Downloading {local_filename}")
                    logging.debug(
                        f"Local checksum {local_checksum}, remote checksum {remote_checksum}",
                    )
                    async with aiohttp_request(
                        "GET", self._url + f"/state?dl={filename}",
                    ) as download_rsp:
                        if rsp.status != http.HTTPStatus.OK:
                            logging.warning(f"Failed to download {filename}")
                        with local_filename.open("wb") as lf:
                            lf.write(await download_rsp.read())
                else:
                    logging.debug(
                        f"File {local_filename} is up-to-date, skipping download",
                    )

    async def _upload_changes(self) -> None:
        while (next_file := await self._change_queue.get()) is not None:
            full_name = Path(next_file)
            relative_name = full_name.relative_to(self._target_dir)
            logging.info(f"Uploading state file {relative_name}")

            # Create form data
            form_data = FormData()
            form_data.add_field(
                "filename", value=str(relative_name), content_type="text/plain",
            )
            form_data.add_field(
                "file",
                full_name.open("rb"),
                filename=full_name.name,
                content_type="application/octet-stream",
            )

            async with aiohttp_request(
                "POST", self._url + "/state", data=form_data,
            ) as response:
                if response.status != http.HTTPStatus.NO_CONTENT:
                    logging.warning(
                        f"Failed to upload state file {relative_name}, status={response.status}",
                    )
