"""Read every PDF page; keep native text when OCR fails on a mixed page."""

from pathlib import Path
from threading import Lock

import pdfplumber
import pymupdf
import pytesseract
from PIL import Image

from app.config import Settings
from app.parser.base import DocumentLimitError, ParseResult

# PyMuPDF and pytesseract's executable setting are process-global. All access here
# is serialized, while downloads and non-PDF parsing remain concurrent.
_PDF_LOCK = Lock()


def _ocr_page(page: pymupdf.Page, settings: Settings) -> str:
    scale = settings.ocr_dpi / 72
    pixel_count = page.rect.width * page.rect.height * scale * scale
    if pixel_count > settings.max_pdf_page_pixels:
        raise DocumentLimitError("PDF page exceeds MAX_PDF_PAGE_PIXELS")
    pixmap = page.get_pixmap(dpi=settings.ocr_dpi, colorspace=pymupdf.csRGB, alpha=False)
    with Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples) as image:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_binary
        return pytesseract.image_to_string(
            image, lang=settings.ocr_languages, timeout=settings.ocr_timeout
        ).strip()


def parse_pdf(path: Path, settings: Settings) -> ParseResult:
    """Extract each page and automatically OCR scanned or image-heavy pages.

    An OCR failure yields a partial result with an explicit page error; there is
    no fallback that reports an unread scanned page as successfully extracted.
    """
    result = ParseResult()
    pages: list[str] = []
    with _PDF_LOCK, pymupdf.open(path) as rendered:
        if rendered.needs_pass:
            return ParseResult(errors=["PDF is encrypted and requires a password"])
        try:
            native = pdfplumber.open(path)
        except Exception:
            native = None  # PyMuPDF can still extract all pages from some malformed PDFs.
        try:
            for index, page in enumerate(rendered):
                text = ""
                try:
                    if native is not None:
                        try:
                            text = native.pages[index].extract_text(layout=False) or ""
                        except Exception:
                            text = page.get_text("text", sort=True).strip()
                    if not text.strip():
                        text = page.get_text("text", sort=True).strip()
                    images = page.get_image_info()
                    page_area = max(page.rect.get_area(), 1)
                    coverage = (
                        sum(
                            (pymupdf.Rect(image["bbox"]) & page.rect).get_area() for image in images
                        )
                        / page_area
                    )
                    # Truly empty separator pages need no OCR; vector-drawn text does.
                    has_visual_content = bool(images or text.strip() or page.get_drawings())
                    needs_ocr = (
                        (not text.strip() and has_visual_content)
                        or coverage >= settings.ocr_image_coverage
                        or (images and len(text.strip()) < settings.ocr_min_text_characters)
                    )
                    if needs_ocr:
                        try:
                            recognized = _ocr_page(page, settings)
                            if recognized:
                                # Native text remains available even if recognition changes it.
                                text = (
                                    (text + "\n[OCR]\n" + recognized).strip()
                                    if text.strip()
                                    else recognized
                                )
                            elif images or not text.strip():
                                result.errors.append(
                                    f"Page {index + 1}: OCR produced no readable text"
                                )
                        except Exception as error:
                            result.errors.append(
                                f"Page {index + 1}: OCR failed: {type(error).__name__}: {error}"
                            )
                except Exception as error:
                    result.errors.append(
                        f"Page {index + 1}: extraction failed: {type(error).__name__}: {error}"
                    )
                if text.strip():
                    pages.append(f"[Page {index + 1}]\n{text}")
        finally:
            if native is not None:
                native.close()
    result.text = "\n\n".join(pages)
    if not pages and not result.errors:
        result.errors.append("PDF contains no readable pages")
    return result
