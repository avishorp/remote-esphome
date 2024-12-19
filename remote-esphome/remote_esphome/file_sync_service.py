import asyncio
import hashlib
import http
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from aiohttp import FormData
from aiohttp import request as aiohttp_request


@dataclass
class _FileEntry:
    name: Path
    checksum: str


class FileSyncService:
    def __init__(
        self, target_dir: Path, url: str, exclude: re.Pattern = re.compile(r"(?!.)")
    ) -> None:
        self._target_dir = target_dir
        self._url = url
        self._change_queue = asyncio.Queue[Path | None]()
        self._files: list[_FileEntry] = []
        self._exclude = exclude
        self._stop_event = asyncio.Condition()

    async def __aenter__(self) -> None:
        # First, down-sync remote files
        await self._initial_download()

        # Create a task for uploading changed files
        async def _upsync_loop() -> None:
            while True:
                async with self._stop_event:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=10.0)
                        break
                    except TimeoutError:
                        await self._upsync()

        self._upload_task = asyncio.create_task(_upsync_loop())

    async def __aexit__(self, exc_type: object, exc_value: object, exc_tb: object) -> None:
        async with self._stop_event:
            self._stop_event.notify()
        await self._upload_task

    def _list_esphome_files(self) -> list[Path]:
        """Return a list of all the files in the esphome directory.

        Excluded files are filtered out.
        """
        all_files = [f for f in self._target_dir.rglob("*") if f.is_file()]
        return [
            f
            for f in all_files
            if self._exclude.match(str(f.relative_to(self._target_dir))) is None
        ]

    def _file_checksum(self, fn: Path) -> str:
        return hashlib.md5(fn.read_bytes()).hexdigest()

    async def _upsync(self) -> None:
        """Scan tracked files for change and upload them."""
        for fn in self._list_esphome_files():
            # Find the file in the file list
            for tfn in self._files:
                if tfn.name == fn:
                    # File found. Compare checksums.
                    current_checksum = self._file_checksum(fn)
                    if current_checksum != tfn.checksum:
                        # Files differ - upload the new version
                        logging.info(f"Uploading {fn}")
                        await self._upload_file(fn)
                        tfn.checksum = current_checksum
                    break

            else:
                # File not found in the list, it is a new one.
                current_checksum = self._file_checksum(fn)
                await self._upload_file(fn)
                self._files.append(_FileEntry(fn, current_checksum))

        # TODO: Handle deleted files

    async def _initial_download(self) -> None:
        # First, scan the local working directory and keep a list of
        # all the files in it. Files that won't get synced from the server
        # will be deleted.
        initial_file_list = self._list_esphome_files()

        # Get a list of files from the remote
        async with aiohttp_request("GET", self._url + "/state") as rsp:
            if rsp.status != http.HTTPStatus.OK:
                raise RuntimeError("Failed to fetch file list")

            file_list = await rsp.json()
            for file_entry in file_list["files"]:
                filename = cast(str, file_entry["name"])
                local_filename = self._target_dir / filename
                if not local_filename.absolute().is_relative_to(
                    self._target_dir.absolute()
                ):
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
                        "GET",
                        self._url + f"/state?dl={filename}",
                    ) as download_rsp:
                        if rsp.status != http.HTTPStatus.OK:
                            logging.warning(f"Failed to download {filename}")
                        with local_filename.open("wb") as lf:
                            lf.write(await download_rsp.read())
                else:
                    logging.debug(
                        f"File {local_filename} is up-to-date, skipping download",
                    )

                # Add the file to the managed file list
                self._files.append(_FileEntry(local_filename, remote_checksum))

        # Fianlly, delete all the files that are not on the remote server
        files_to_delete = set(initial_file_list).difference(
            [e.name for e in self._files],
        )
        for fn in files_to_delete:
            logging.info(f"Deleting {fn}")
            fn.unlink()

        logging.info("Initial sync completed")

    async def _upload_file(self, fn: Path) -> None:
        relative_name = fn.relative_to(self._target_dir)
        logging.info(f"Uploading state file {relative_name}")

        # Create form data
        form_data = FormData()
        form_data.add_field(
            "filename",
            value=str(relative_name),
            content_type="text/plain",
        )
        form_data.add_field(
            "file",
            fn.open("rb"),
            filename=fn.name,
            content_type="application/octet-stream",
        )

        async with aiohttp_request(
            "POST",
            self._url + "/state",
            data=form_data,
        ) as response:
            if response.status != http.HTTPStatus.NO_CONTENT:
                logging.warning(
                    f"Failed to upload state file {relative_name}, status={response.status}",
                )
