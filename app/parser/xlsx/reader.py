"""Keep every sheet, hidden sheets, formulas, and cached formula values."""

from datetime import datetime
from pathlib import Path

import openpyxl
import xlrd

from app.parser.base import DocumentLimitError, ParseResult


def _string(value: object) -> str:
    if value is None:
        return ""
    return value.isoformat(sep=" ") if isinstance(value, datetime) else str(value)


def parse_xlsx(path: Path, max_cells: int) -> ParseResult:
    output: list[str] = []
    examined = 0
    populated = False
    book = openpyxl.load_workbook(path, read_only=True, data_only=False, keep_links=False)
    values = openpyxl.load_workbook(path, read_only=True, data_only=True, keep_links=False)
    try:
        for sheet in book.worksheets:
            output.append(f"[Sheet: {sheet.title}]")
            cached = values[sheet.title]
            # Ignore incorrect producer-supplied dimensions; walk the actual XML.
            sheet.reset_dimensions()
            cached.reset_dimensions()
            for row, cached_row in zip(sheet.iter_rows(), cached.iter_rows(), strict=True):
                examined += len(row)
                if examined > max_cells:
                    raise DocumentLimitError("Spreadsheet exceeds MAX_SPREADSHEET_CELLS")
                cells = []
                for cell, cached_cell in zip(row, cached_row, strict=True):
                    value = _string(cell.value)
                    if cell.data_type == "f" and cached_cell.value is not None:
                        value += f" [cached value: {_string(cached_cell.value)}]"
                    cells.append(value)
                if any(cells):
                    populated = True
                    output.append("\t".join(cells))
    finally:
        book.close()
        values.close()
    text = "\n".join(output)
    return ParseResult(
        text if populated else "", [] if populated else ["Spreadsheet contains no populated cells"]
    )


def parse_xls(path: Path, max_cells: int) -> ParseResult:
    output: list[str] = []
    examined = 0
    populated = False
    book = xlrd.open_workbook(path, on_demand=True)
    try:
        for name in book.sheet_names():
            sheet = book.sheet_by_name(name)
            examined += sheet.nrows * sheet.ncols
            if examined > max_cells:
                raise DocumentLimitError("Spreadsheet exceeds MAX_SPREADSHEET_CELLS")
            output.append(f"[Sheet: {sheet.name}]")
            for index in range(sheet.nrows):
                cells = []
                for cell in sheet.row(index):
                    value = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        value = xlrd.xldate.xldate_as_datetime(value, book.datemode)
                    cells.append(_string(value))
                if any(cells):
                    populated = True
                    output.append("\t".join(cells))
            book.unload_sheet(name)
    finally:
        book.release_resources()
    return ParseResult(
        "\n".join(output) if populated else "",
        [] if populated else ["Spreadsheet contains no populated cells"],
    )
