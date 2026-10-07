"""Format detection and bounded archive traversal without unsafe extraction APIs."""

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from zipfile import ZipFile

import rarfile
from loguru import logger

from app.config import Settings
from app.models.domain import DocumentData
from app.parser.base import (
    DocumentLimitError,
    ParseResult,
    check_zip_package,
    commit_original,
    is_specification_name,
    safe_member_name,
    summarize,
)
from app.parser.docx import parse_doc, parse_docx
from app.parser.pdf import parse_pdf
from app.parser.xlsx import parse_xls, parse_xlsx


@dataclass
class _Budget:
    members: int = 0
    bytes: int = 0


class DocumentParser:
    """Parse local originals, recording partial failures instead of hiding them."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def parse(self, document: DocumentData) -> DocumentData:
        """Parse a complete downloaded document and its recursively nested members."""
        return self._parse(document.model_copy(deep=True), _Budget(), 0)

    def _parse(self, document: DocumentData, budget: _Budget, depth: int) -> DocumentData:
        document.original_text = ""
        document.summary = ""
        document.children = []
        document.error = None
        try:
            if document.local_path is None or not document.local_path.is_file():
                raise FileNotFoundError("Downloaded document is unavailable")
            kind = self._format(document)
            if kind in {"zip", "rar"}:
                if depth >= self.settings.max_archive_depth:
                    raise DocumentLimitError("Archive exceeds MAX_ARCHIVE_DEPTH")
                return self._archive(document, kind, budget, depth)
            if kind in {"docx", "xlsx"}:
                with ZipFile(document.local_path) as package:
                    members, size = check_zip_package(
                        package,
                        self.settings.max_archive_members - budget.members,
                        self.settings.max_extracted_bytes - budget.bytes,
                    )
                    budget.members += members
                    budget.bytes += size
            if kind == "pdf":
                result = parse_pdf(document.local_path, self.settings)
            elif kind == "docx":
                result = parse_docx(document.local_path)
            elif kind == "doc":
                result = parse_doc(document.local_path, self.settings)
            elif kind == "xlsx":
                result = parse_xlsx(document.local_path, self.settings.max_spreadsheet_cells)
            elif kind == "xls":
                result = parse_xls(document.local_path, self.settings.max_spreadsheet_cells)
            else:
                document.parse_status = "unsupported"
                document.error = f"Unsupported document format: {kind or 'unknown'}"
                return document
            self._apply_result(document, result)
        except Exception as error:
            document.error = f"{type(error).__name__}: {error}"
            document.parse_status = "partial_errors" if document.original_text else "error"
            logger.warning("Document extraction failed for {}: {}", document.name, document.error)
        return document

    def _apply_result(self, document: DocumentData, result: ParseResult) -> None:
        document.original_text = result.text
        document.summary = summarize(result.text, self.settings.summary_characters)
        document.error = "; ".join(result.errors) or None
        document.parse_status = (
            "partial_errors"
            if result.errors and result.text
            else "error"
            if result.errors
            else "success"
        )

    def _format(self, document: DocumentData) -> str:
        assert document.local_path is not None
        with document.local_path.open("rb") as stream:
            signature = stream.read(1024)
        if b"%PDF-" in signature:
            return "pdf"
        if signature.startswith(b"PK\x03\x04") or signature.startswith(b"PK\x05\x06"):
            with ZipFile(document.local_path) as package:
                names = set(package.namelist())
                if "word/document.xml" in names:
                    return "docx"
                if "xl/workbook.xml" in names:
                    return "xlsx"
            return "zip"
        if signature.startswith(b"Rar!\x1a\x07"):
            return "rar"
        suffix = Path(document.name or document.local_path.name).suffix.lower().lstrip(".")
        if signature.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            if suffix in {"xls", "doc"}:
                return suffix
            if document.media_type == "application/vnd.ms-excel":
                return "xls"
            if document.media_type == "application/msword":
                return "doc"
            # The OLE directory carries UTF-16 stream names even with opaque URLs.
            with document.local_path.open("rb") as stream:
                sample = stream.read(self.settings.max_download_bytes)
            if "WordDocument".encode("utf-16-le") in sample:
                return "doc"
            if any(name.encode("utf-16-le") in sample for name in ("Workbook", "Book")):
                return "xls"
        return suffix

    def _archive(
        self, document: DocumentData, kind: str, budget: _Budget, depth: int
    ) -> DocumentData:
        assert document.local_path is not None
        archive = (
            ZipFile(document.local_path)
            if kind == "zip"
            else rarfile.RarFile(document.local_path, errors="strict")
        )
        failures: list[str] = []
        with archive:
            members = archive.infolist()
            if len(members) + budget.members > self.settings.max_archive_members:
                raise DocumentLimitError("Archive exceeds MAX_ARCHIVE_MEMBERS")
            # Preflight the complete directory before writing any entry.
            advertised_bytes = 0
            for member in members:
                safe_member_name(member.filename)
                advertised_bytes += member.file_size
                if kind == "rar" and (member.is_symlink() or getattr(member, "file_redir", None)):
                    raise DocumentLimitError(f"Archive link is forbidden: {member.filename}")
            if kind == "zip":
                check_zip_package(
                    archive,
                    self.settings.max_archive_members - budget.members,
                    self.settings.max_extracted_bytes - budget.bytes,
                )
            if advertised_bytes + budget.bytes > self.settings.max_extracted_bytes:
                raise DocumentLimitError("Archive exceeds MAX_EXTRACTED_BYTES")
            budget.members += len(members)
            for member in members:
                is_directory = member.is_dir()
                if is_directory:
                    continue
                child = DocumentData(
                    url=document.url,
                    name=member.filename,
                    is_specification=document.is_specification
                    or is_specification_name(member.filename),
                    downloaded_at=document.downloaded_at,
                )
                try:
                    with archive.open(member) as source:
                        child.local_path, child.sha256, child.size_bytes = self._save_member(
                            source, member.filename, document.local_path.parent / "members", budget
                        )
                    child = self._parse(child, budget, depth + 1)
                except Exception as error:
                    child.parse_status = "error"
                    child.error = f"{type(error).__name__}: {error}"
                document.children.append(child)
                if child.parse_status != "success":
                    failures.append(f"{child.name}: {child.error or child.parse_status}")
            text = "\n\n".join(
                f"[Archive member: {child.name}]\n{child.original_text}"
                for child in document.children
                if child.original_text
            )
            if not document.children:
                failures.append("Archive contains no readable files")
            self._apply_result(document, ParseResult(text, failures))
        return document

    def _save_member(
        self, source: BinaryIO, name: str, directory: Path, budget: _Budget
    ) -> tuple[Path, str, int]:
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        descriptor, temporary = tempfile.mkstemp(prefix="member-", suffix=".part", dir=directory)
        partial = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as target:
                while chunk := source.read(65536):
                    size += len(chunk)
                    budget.bytes += len(chunk)
                    if size > self.settings.max_download_bytes:
                        raise DocumentLimitError("Archive member exceeds MAX_DOWNLOAD_BYTES")
                    if budget.bytes > self.settings.max_extracted_bytes:
                        raise DocumentLimitError("Archive exceeds MAX_EXTRACTED_BYTES")
                    target.write(chunk)
                    digest.update(chunk)
                target.flush()
                os.fsync(target.fileno())
            suffix = Path(name).suffix.lower()
            if not suffix.isascii() or not suffix[1:].isalnum() or len(suffix) > 12:
                suffix = ".bin"
            destination = directory / f"{digest.hexdigest()}{suffix}"
            commit_original(partial, destination, digest.hexdigest())
            return destination, digest.hexdigest(), size
        finally:
            partial.unlink(missing_ok=True)
