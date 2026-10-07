"""Bounded asynchronous downloads followed by complete attachment extraction."""

import asyncio
import hashlib
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit

from loguru import logger

from app.config import Settings
from app.models.domain import DocumentData
from app.parser.base import commit_original
from app.parser.documents import DocumentParser
from app.utils.time import utc_now


class DownloadTransport(Protocol):
    async def download(self, url: str, destination: Path, max_bytes: int) -> tuple[str, int]:
        """Stream URL into destination, enforcing allowlist on every redirect and limits."""
        ...


class DocumentProcessor:
    """Keep local source files and extraction outcomes even when individual files fail."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.download_concurrency)
        self._parser = DocumentParser(settings)

    async def process_many(
        self, documents: Sequence[DocumentData], transport: DownloadTransport
    ) -> list[DocumentData]:
        # Process bounded batches: never schedule one task per attachment in a crawl.
        output = []
        for offset in range(0, len(documents), self.settings.download_concurrency):
            output.extend(
                await asyncio.gather(
                    *(
                        self.process(document, transport)
                        for document in documents[
                            offset : offset + self.settings.download_concurrency
                        ]
                    )
                )
            )
        return output

    async def process(self, document: DocumentData, transport: DownloadTransport) -> DocumentData:
        document = document.model_copy(deep=True)
        document.local_path = None
        document.sha256 = None
        document.size_bytes = None
        document.original_text = ""
        document.summary = ""
        document.children = []
        document.error = None
        document.downloaded_at = None
        document.parse_status = "pending"
        partial: Path | None = None
        async with self._semaphore:
            try:
                parsed = urlsplit(document.url)
                allowed = {host.lower().rstrip(".") for host in self.settings.official_hosts}
                if (
                    parsed.scheme not in {"http", "https"}
                    or (parsed.hostname or "").lower().rstrip(".") not in allowed
                    or parsed.username
                    or parsed.password
                    or parsed.port not in {None, 80, 443}
                ):
                    raise ValueError("Document URL is outside OFFICIAL_HOSTS")
                directory = self.settings.download_folder.resolve()
                directory.mkdir(parents=True, exist_ok=True)
                descriptor, name = tempfile.mkstemp(
                    prefix="download-", suffix=".part", dir=directory
                )
                os.close(descriptor)
                partial = Path(name)
                claimed_hash, claimed_size = await transport.download(
                    document.url, partial, self.settings.max_download_bytes
                )
                actual_size = partial.stat().st_size
                if actual_size > self.settings.max_download_bytes:
                    raise ValueError("Download exceeds MAX_DOWNLOAD_BYTES")
                actual_hash = await asyncio.to_thread(self._checksum, partial)
                if claimed_size != actual_size or claimed_hash != actual_hash:
                    raise ValueError(
                        "Downloaded file size or SHA256 does not match transport result"
                    )
                suffix = Path(document.name or unquote(parsed.path)).suffix.lower()
                if not suffix.isascii() or not suffix[1:].isalnum() or len(suffix) > 12:
                    suffix = ".bin"
                destination = directory / f"{actual_hash}{suffix}"
                await asyncio.to_thread(commit_original, partial, destination, actual_hash)
                document.local_path = destination
                document.sha256 = actual_hash
                document.size_bytes = actual_size
                document.downloaded_at = utc_now()
                document = await asyncio.to_thread(self._parser.parse, document)
                logger.info(
                    "Downloaded document {} ({} bytes): {}",
                    document.url,
                    actual_size,
                    document.parse_status,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                document.parse_status = "error"
                document.error = f"{type(error).__name__}: {error}"
                logger.warning("Document download failed {}: {}", document.url, document.error)
            finally:
                if partial is not None:
                    partial.unlink(missing_ok=True)
        return document

    @staticmethod
    def _checksum(path: Path) -> str:
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
