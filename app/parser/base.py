"""Extraction results preserve partial text and name every incomplete operation."""

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from stat import S_IFMT, S_IFREG
from threading import Lock
from zipfile import ZipFile

_COMMIT_LOCK = Lock()


@dataclass
class ParseResult:
    text: str = ""
    errors: list[str] = field(default_factory=list)


class DocumentLimitError(ValueError):
    """A document exceeded a configured resource or archive safety limit."""


def is_specification_name(name: str | None) -> bool:
    """Recognize explicit specification labels, including archived member names."""
    normalized = (name or "").casefold()
    return any(
        term in normalized
        for term in (
            "техническ",
            "техникалық",
            "techspec",
            "technical specification",
        )
    )


def commit_original(partial: Path, destination: Path, checksum: str) -> None:
    """Publish immutable content, tolerating concurrent downloads of identical bytes.

    Windows cannot replace a file currently being read by another parser. Existing
    content-addressed files are therefore verified and reused instead of replaced.
    """

    def matches() -> bool:
        if not destination.is_file():
            return False
        with destination.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest() == checksum

    # Serialize publication, so two threads cannot both observe a missing target
    # and attempt competing Windows renames while the first reader opens it.
    with _COMMIT_LOCK:
        if matches():
            return
        try:
            os.replace(partial, destination)
        except OSError:
            if not matches():
                raise


def safe_member_name(name: str) -> None:
    """Reject absolute paths, traversal, Windows streams, and ambiguous separators."""
    normalized = name.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (
        not parts
        or normalized.startswith("/")
        or any(part in {"..", "."} or ":" in part or "\x00" in part for part in parts)
    ):
        raise DocumentLimitError(f"Unsafe archive member name: {name!r}")


def check_zip_package(package: ZipFile, max_members: int, max_bytes: int) -> tuple[int, int]:
    """Preflight Office/ZIP members before a library decompresses XML or images."""
    members = package.infolist()
    if len(members) > max_members:
        raise DocumentLimitError("Archive exceeds MAX_ARCHIVE_MEMBERS")
    total = 0
    for member in members:
        safe_member_name(member.filename)
        mode = S_IFMT(member.external_attr >> 16)
        if not member.is_dir() and mode not in {0, S_IFREG}:
            raise DocumentLimitError(f"Archive contains a non-regular file: {member.filename}")
        if member.flag_bits & 1:
            raise DocumentLimitError(f"Encrypted ZIP member: {member.filename}")
        total += member.file_size
        if total > max_bytes:
            raise DocumentLimitError("Archive exceeds MAX_EXTRACTED_BYTES")
    return len(members), total


def summarize(text: str, max_characters: int) -> str:
    """Return a deterministic extract of original text; never invent missing details."""
    compact = " ".join(text.split())
    if len(compact) <= max_characters:
        return compact
    prefix = compact[: max_characters - 1]
    boundary = max(prefix.rfind(". "), prefix.rfind("; "), prefix.rfind("\n"))
    if boundary >= max_characters // 2:
        prefix = prefix[: boundary + 1]
    else:
        prefix = prefix.rsplit(" ", 1)[0] or prefix
    return prefix + "…"
