"""Find lot-specific technical specifications and extract their complete text."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import logging
from pathlib import Path
import random
import re
from threading import Lock
import time
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from legacy_doc import LegacyDocError, extract_text as extract_legacy_doc
import requests

from .api import APIError, AuthError


LOG = logging.getLogger(__name__)
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_XML_BYTES = 50 * 1024 * 1024
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
TECH_NAME = re.compile(r"техн(?:ическ|икалы|икалық)|тех\.?\s*задан|тех\.?\s*тапсыр|(?:^|[\W_])тс(?:[\W_]|$)", re.I)
NON_SPEC_NAME = re.compile(r"квалификационн|біліктілік", re.I)
INVALID_EXCEL_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(frozen=True)
class DocumentText:
    text: str
    status: str


def _is_technical(document):
    title = str(document.get("nameRu") or "") + " " + str(document.get("nameKz") or "")
    filename = str(document.get("originalName") or "")
    return (not NON_SPEC_NAME.search(title)
            and (bool(TECH_NAME.search(title + " " + filename))
                 or filename.casefold().startswith("techspec_")))


def specification_files(lot):
    """Use lot files and announcement techspec files named for this exact lot ID."""
    lot_id = str(lot.get("id") or "")
    selected = []
    for document in lot.get("Files") or []:
        if _is_technical(document):
            selected.append(document)
    announcement_files = (lot.get("TrdBuy") or {}).get("Files") or []
    for document in announcement_files:
        name = str(document.get("originalName") or "")
        if lot_id and _is_technical(document) and re.search(rf"(?<!\d){re.escape(lot_id)}(?!\d)", name):
            selected.append(document)
    if not selected:
        shared = [document for document in announcement_files
                  if _is_technical(document)
                  and not re.search(r"\d{8,}", str(document.get("originalName") or ""))]
        if len(shared) == 1:
            selected.append(shared[0])
    unique = {}
    for document in selected:
        url = document.get("filePath")
        if isinstance(url, str) and url:
            # The same document can appear under both lot and announcement paths.
            unique.setdefault(document.get("id") or url, document)
    return list(unique.values())


def _word_text(archive):
    names = archive.namelist()
    ordered = (["word/document.xml"] +
               sorted(name for name in names if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)) +
               [name for name in ("word/footnotes.xml", "word/endnotes.xml") if name in names])
    blocks = []
    for name in ordered:
        if name not in names:
            continue
        if archive.getinfo(name).file_size > MAX_XML_BYTES:
            raise ValueError("Слишком большой XML внутри DOCX")
        root = ET.fromstring(archive.read(name))
        paragraphs = []
        for paragraph in root.iter(WORD_NS + "p"):
            pieces = []
            for node in paragraph.iter():
                if node.tag == WORD_NS + "t":
                    pieces.append(node.text or "")
                elif node.tag == WORD_NS + "tab":
                    pieces.append("\t")
                elif node.tag in (WORD_NS + "br", WORD_NS + "cr"):
                    pieces.append("\n")
            line = "".join(pieces).strip()
            if line:
                paragraphs.append(line)
        if paragraphs:
            blocks.append("\n".join(paragraphs))
    return "\n\n".join(blocks)


def _spreadsheet_text(content):
    book = load_workbook(BytesIO(content), read_only=True, data_only=True)
    try:
        sections = []
        for sheet in book:
            lines = ["Лист: " + sheet.title]
            for row in sheet.iter_rows(values_only=True):
                values = [str(value) if value is not None else "" for value in row]
                if any(values):
                    lines.append("\t".join(values))
            sections.append("\n".join(lines))
        return "\n\n".join(sections)
    finally:
        book.close()


def extract_document_text(content, *, ocr_page=None, diagnostics=None):
    """Return every readable page of PDF, DOCX, XLSX or legacy DOC."""
    diagnostics = diagnostics if diagnostics is not None else []
    if content.startswith(b"%PDF-"):
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ValueError("PDF защищён паролем") from exc
        pages = []
        for index, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            if not page_text.strip() and ocr_page is not None:
                try:
                    page_text = ocr_page(content, index)
                    if page_text.strip():
                        diagnostics.append("OCR")
                    else:
                        diagnostics.append(f"Страница {index + 1}: OCR не распознал текст")
                except Exception as exc:
                    diagnostics.append(f"Страница {index + 1}: OCR не выполнен ({exc})")
            pages.append(page_text)
        text = "\n\n".join(pages)
    elif content.startswith(b"PK\x03\x04"):
        with ZipFile(BytesIO(content)) as archive:
            names = set(archive.namelist())
            if "word/document.xml" in names:
                text = _word_text(archive)
            elif "xl/workbook.xml" in names:
                text = _spreadsheet_text(content)
            else:
                raise ValueError("Неподдерживаемый формат ZIP-вложения")
    elif content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        text = extract_legacy_doc(content).text
    else:
        raise ValueError("Неподдерживаемый формат вложения")
    text = INVALID_EXCEL_TEXT.sub("", text).strip()
    if not text:
        raise ValueError("; ".join(x for x in diagnostics if x != "OCR") or
                         "Нет текстового слоя; для скана требуется OCR")
    return text


class SpecificationExtractor:
    def __init__(self, config):
        self.config = config
        self.cache_dir = Path(config.output_dir) / ".spec_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._ocr_lock = Lock()
        self._ocr_engine = None

    def _ocr_page(self, content, index):
        # OCR engines are initialized lazily and used by one worker at a time.
        with self._ocr_lock:
            if self._ocr_engine is None:
                from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
                model_dir = self.cache_dir.parent / ".ocr_models"
                model_dir.mkdir(parents=True, exist_ok=True)
                self._ocr_engine = RapidOCR(params={
                    "Global.model_root_dir": str(model_dir.resolve()),
                    "Global.log_level": "warning",
                    "Rec.lang_type": LangRec.CYRILLIC,
                    "Rec.ocr_version": OCRVersion.PPOCRV5,
                    "Rec.model_type": ModelType.MOBILE,
                })
            import pymupdf
            with pymupdf.open(stream=content, filetype="pdf") as pdf:
                image = pdf[index].get_pixmap(dpi=180).tobytes("png")
            result = self._ocr_engine(image)
            return "\n".join(result.txts if result.txts is not None else [])

    def _download(self, url):
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname != "ows.goszakup.gov.kz"
                or not parsed.path.startswith("/download/") or parsed.username or parsed.password):
            raise ValueError("Недопустимый адрес файла техспецификации")
        # This endpoint returns HTTP 415 when Accept is application/octet-stream.
        headers = {"Authorization": f"Bearer {self.config.token}"}
        for attempt in range(self.config.max_retries + 1):
            try:
                with requests.get(url, headers=headers, stream=True, allow_redirects=False,
                                  timeout=self.config.request_timeout) as response:
                    if response.status_code in (401, 403):
                        raise AuthError("API отклонил токен при загрузке техспецификации")
                    if response.status_code in (429, 500, 502, 503, 504):
                        if attempt == self.config.max_retries:
                            raise APIError(f"HTTP {response.status_code} при загрузке техспецификации")
                    else:
                        response.raise_for_status()
                        data = bytearray()
                        for chunk in response.iter_content(chunk_size=65536):
                            data.extend(chunk)
                            if len(data) > MAX_DOCUMENT_BYTES:
                                raise ValueError("Файл больше 25 МБ")
                        return bytes(data)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == self.config.max_retries:
                    raise APIError("Ошибка сети при загрузке техспецификации") from exc
            time.sleep(min(10, 2 ** attempt + random.random()))
        raise AssertionError("unreachable")

    def _extract_one(self, url):
        cache_path = self.cache_dir / (sha256(url.encode("utf-8")).hexdigest() + ".json")
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("url") == url and cached.get("text"):
                    return DocumentText(cached["text"], cached.get("status") or "Извлечено")
            except (OSError, ValueError, TypeError):
                pass
        try:
            diagnostics = []
            text = extract_document_text(self._download(url), ocr_page=self._ocr_page,
                                         diagnostics=diagnostics)
            errors = [item for item in diagnostics if item != "OCR"]
            status = ("Не полностью: " + "; ".join(errors) if errors else
                      "Извлечено (OCR)" if "OCR" in diagnostics else "Извлечено")
        except AuthError:
            raise
        except (APIError, requests.HTTPError, ValueError, BadZipFile, OSError,
                PdfReadError, ET.ParseError, EOFError, RuntimeError, LegacyDocError) as exc:
            LOG.debug("Не удалось извлечь техспецификацию %s: %s", url, exc)
            return DocumentText("", str(exc))
        if not errors:
            temp = cache_path.with_suffix(".tmp")
            temp.write_text(json.dumps({"url": url, "text": text, "status": status},
                                       ensure_ascii=False), encoding="utf-8")
            temp.replace(cache_path)
        return DocumentText(text, status)

    def for_lots(self, lots):
        documents_by_lot = {lot["id"]: specification_files(lot) for lot in lots}
        urls = {document["filePath"] for docs in documents_by_lot.values() for document in docs}
        extracted = {}
        if urls:
            with ThreadPoolExecutor(max_workers=self.config.max_concurrent_requests) as pool:
                futures = {pool.submit(self._extract_one, url): url for url in urls}
                for future in as_completed(futures):
                    extracted[futures[future]] = future.result()
        result = {}
        for lot in lots:
            docs = documents_by_lot[lot["id"]]
            if not docs:
                result[lot["id"]] = DocumentText("", "Файл техспецификации не найден")
                continue
            pieces = []
            errors = []
            for document in docs:
                item = extracted[document["filePath"]]
                if item.text:
                    label = document.get("originalName") or document.get("nameRu") or str(document.get("id"))
                    pieces.append(f"=== {label} ===\n{item.text}")
                    if item.status.startswith("Не полностью"):
                        errors.append(f"{label}: {item.status}")
                else:
                    errors.append(f"{document.get('originalName') or document.get('id')}: {item.status}")
            status = ("Не полностью: " + "; ".join(errors) if errors else
                      "Извлечено (OCR)" if any("OCR" in extracted[d["filePath"]].status for d in docs)
                      else "Извлечено")
            if any(document not in (lot.get("Files") or []) and
                   not re.search(rf"(?<!\d){re.escape(str(lot['id']))}(?!\d)",
                                 str(document.get("originalName") or "")) for document in docs):
                status += "; общий файл объявления — проверьте применимость к лоту"
            result[lot["id"]] = DocumentText("\n\n".join(pieces), status)
        return result
