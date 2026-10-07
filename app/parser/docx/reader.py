"""Extract Word body, nested tables, headers, footers, and note text."""

import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

from app.config import Settings
from app.parser.base import ParseResult, check_zip_package
from docx import Document
from docx.oxml.ns import qn


def _text(element: ET.Element) -> str:
    pieces = []
    for node in element.iter():
        if node.tag == qn("w:t"):
            pieces.append(node.text or "")
        elif node.tag == qn("w:tab"):
            pieces.append("\t")
        elif node.tag in {qn("w:br"), qn("w:cr")}:
            pieces.append("\n")
    return "".join(pieces)


def _blocks(element: ET.Element) -> list[str]:
    output: list[str] = []
    for block in element:
        if block.tag == qn("w:p"):
            output.append(_text(block))
        elif block.tag == qn("w:tbl"):
            for row in block:
                if row.tag == qn("w:tr"):
                    output.append(
                        "\t".join(
                            " / ".join(_blocks(cell)) for cell in row if cell.tag == qn("w:tc")
                        )
                    )
        elif block.tag not in {qn("w:sectPr"), qn("w:tcPr"), qn("w:tblPr"), qn("w:tblGrid")}:
            # Content controls and revision containers may wrap normal paragraphs.
            output.extend(_blocks(block))
    return output


def parse_docx(path: Path) -> ParseResult:
    document = Document(path)
    output = _blocks(document.element.body)
    seen_parts = set()
    for section in document.sections:
        for label, part in (
            ("Header", section.header),
            ("Footer", section.footer),
            ("First page header", section.first_page_header),
            ("First page footer", section.first_page_footer),
            ("Even page header", section.even_page_header),
            ("Even page footer", section.even_page_footer),
        ):
            if part.part.partname not in seen_parts:
                seen_parts.add(part.part.partname)
                content = "\n".join(_blocks(part._element)).strip()
                if content:
                    output.append(f"[{label}]\n{content}")
    with ZipFile(path) as package:
        for name in ("word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"):
            if name in package.namelist():
                root = ET.fromstring(package.read(name))
                content = "\n".join(_blocks(root)).strip()
                if content:
                    output.append(f"[{Path(name).stem}]\n{content}")
    text = "\n".join(output).strip()
    return ParseResult(text, [] if text else ["Word document contains no readable text"])


def parse_doc(path: Path, settings: Settings) -> ParseResult:
    """Convert legacy DOC in an isolated LibreOffice profile without a shell."""
    with tempfile.TemporaryDirectory(prefix="procurement-doc-") as directory:
        workspace = Path(directory)
        profile = workspace / "profile"
        command = [
            settings.libreoffice_binary,
            f"-env:UserInstallation={profile.as_uri()}",
            "--headless",
            "--nologo",
            "--nodefault",
            "--norestore",
            "--convert-to",
            "docx",
            "--outdir",
            str(workspace),
            str(path.resolve()),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.ocr_timeout,
            check=False,
        )
        converted = workspace / (path.stem + ".docx")
        if completed.returncode or not converted.is_file():
            detail = (completed.stderr or completed.stdout).strip()
            raise ValueError(f"LibreOffice DOC conversion failed: {detail}")
        with ZipFile(converted) as package:
            check_zip_package(package, settings.max_archive_members, settings.max_extracted_bytes)
        return parse_docx(converted)
