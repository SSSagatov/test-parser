"""Parse observed official HTML by semantic table headers and field labels."""

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from app.config import Settings
from app.crawler.api.official import SearchWindowExceeded, SourceSchemaError, source_datetime
from app.models.domain import DocumentData, ListingPage, LotData, Party


def text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def money(value: str | None) -> Decimal | None:
    if not value or value.strip() in {"-", "—"}:
        return None
    cleaned = re.sub(r"[\s\u00a0]", "", value).replace(",", ".")
    try:
        result = Decimal(cleaned)
    except InvalidOperation as exc:
        raise SourceSchemaError(f"Invalid official numeric value: {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise SourceSchemaError("Official amount/quantity must be finite and nonnegative")
    return result


def fields_from_html(html: str) -> dict[str, str]:
    """Extract label/value rows and readonly announcement form controls."""
    soup = BeautifulSoup(html, "lxml")
    fields = {}
    for row in soup.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) == 2 and cells[0].name == "th" and cells[1].name == "td":
            for history in cells[1].select(".btn-select-history, script, style"):
                history.decompose()
            fields[text(cells[0]).rstrip(":")] = text(cells[1])
    for group in soup.select(".form-group"):
        label, value = group.find("label"), group.select_one("input[readonly]")
        if label and value:
            fields[text(label).rstrip(":")] = str(value.get("value", "")).strip()
    return fields


def page_links(html: str, url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    content = soup.select_one("#main-wrapper") or soup.select_one(".page-inner") or soup
    return list(
        dict.fromkeys(
            urljoin(url, str(anchor["href"]))
            for anchor in content.select("a[href]")
            if not str(anchor["href"]).startswith(("#", "javascript:", "mailto:", "tel:"))
        )
    )


def parse_legacy_listing(html: str, url: str, settings: Settings) -> ListingPage:
    soup = BeautifulSoup(html, "lxml")
    table = next(
        (
            candidate
            for candidate in soup.select("table")
            if "Наименование объявления" in [text(th) for th in candidate.select("thead th")]
            and "Статус" in [text(th) for th in candidate.select("thead th")]
        ),
        None,
    )
    if table is None:
        raise SourceSchemaError(
            "Official lot results table was not found; response is not a verified empty search"
        )
    headers = [text(th) for th in table.select("thead th")]
    currency = "KZT" if any("тг." in header for header in headers) else None
    lots = []
    for row in table.select("tbody > tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) == 1 and re.search(
            r"Ничего не найдено|Нет данных|Нет записей", text(cells[0]), re.I
        ):
            continue
        if len(cells) != len(headers):
            raise SourceSchemaError("Official results row has an unexpected number of cells")
        announcement = row.select_one('a[href*="/announce/index/"]')
        direct = row.select_one('a[href*="/subpriceoffer/index/"]')
        history = row.select_one(".btn-select-history[data-lot-id][data-trb-buy]")
        if not announcement or (not direct and not history):
            raise SourceSchemaError("Official lot row lacks its verified identifiers")
        announcement_url = urljoin(url, str(announcement["href"]))
        announcement_id = urlsplit(announcement_url).path.rstrip("/").split("/")[-1]
        lot_id = (
            str(history["data-lot-id"])
            if history
            else urlsplit(str(direct["href"])).path.rstrip("/").split("/")[-1]
        )
        source_url = urljoin(url, str(direct["href"])) if direct else announcement_url + "?tab=lots"
        lot_number = text(cells[0])
        number_match = re.match(r"\d+-\d+", text(announcement))
        customer_node = cells[1].find("small")
        customer_name = re.sub(r"^Заказчик:\s*", "", text(customer_node)).strip() or None
        lots.append(
            LotData(
                source_id=lot_id,
                source_url=source_url,
                lot_number=lot_number,
                announcement_id=announcement_id,
                announcement_number=number_match.group() if number_match else None,
                announcement_url=announcement_url,
                title=text(direct) if direct else None,
                budget=money(text(cells[4])),
                quantity=money(text(cells[3])),
                currency=currency,
                procurement_method=text(cells[5]) or None,
                status=text(cells[6]) or None,
                customer=Party(name=customer_name) if customer_name else None,
                collected_at=datetime.now(UTC),
                links=[source_url, announcement_url],
                raw={
                    "legacy_listing": dict(
                        zip(headers, (text(cell) for cell in cells), strict=True)
                    )
                },
            )
        )
    current_page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
    page_size_node = soup.select_one('select[onchange*="ajax_set_count_record"] option[selected]')
    effective_size = int(page_size_node["value"]) if page_size_node else settings.page_size
    following = []
    for anchor in soup.select(".pagination a[href]"):
        href = urljoin(url, str(anchor["href"]))
        parsed = urlsplit(href)
        if parsed.path != urlsplit(url).path or parsed.scheme not in {"http", "https"}:
            continue
        number = parse_qs(parsed.query).get("page", [])
        if number and int(number[0]) == current_page + 1:
            following.append(href)
    next_cursor = following[0] if following else None
    if (
        not next_cursor
        and lots
        and len(lots) >= effective_size
        and current_page * effective_size >= settings.public_result_window
    ):
        raise SearchWindowExceeded(
            "Legacy search reached a full terminal result window; completeness is unverified. Use OWS or narrower filters."
        )
    return ListingPage(lots=lots, next_cursor=next_cursor, source_url=url)


def parse_lot_detail(html: str, lot: LotData, settings: Settings) -> LotData:
    fields = fields_from_html(html)
    number = fields.get("Лот №") or fields.get("Номер лота")
    if number != lot.lot_number:
        raise SourceSchemaError("Rendered lot page does not match the expected official lot number")
    values = lot.model_dump()
    customer = Party(
        name=fields.get("Наименование заказчика") or (lot.customer.name if lot.customer else None),
        bin=fields.get("БИН заказчика") or None,
        address=fields.get("Адрес заказчика") or None,
    )
    delivery = fields.get("Место поставки товара, КАТО") or fields.get("Место поставки") or None
    work_place = fields.get("Место выполнения работ") or None
    service_place = fields.get("Место оказания услуг") or None
    values.update(
        title=fields.get("Наименование ТРУ") or fields.get("Наименование") or lot.title,
        description=fields.get("Дополнительная характеристика")
        or fields.get("Краткая характеристика")
        or lot.description,
        customer=customer,
        status=fields.get("Статус лота") or lot.status,
        budget=money(fields.get("Запланированная сумма"))
        if fields.get("Запланированная сумма")
        else lot.budget,
        quantity=money(fields.get("Количество")) if fields.get("Количество") else lot.quantity,
        unit=fields.get("Единица измерения") or None,
        delivery_place=delivery,
        work_place=work_place,
        service_location=service_place,
        location_source_url=lot.source_url if delivery or work_place or service_place else None,
        execution_period=fields.get("Срок поставки ТРУ") or None,
        additional_conditions=fields.get("Условия поставки ИНКОТЕРМС") or None,
        submission_start=source_datetime(fields.get("Дата начала приема заявок"), settings.timezone)
        or lot.submission_start,
        submission_deadline=source_datetime(
            fields.get("Дата окончания приема заявок"), settings.timezone
        )
        or lot.submission_deadline,
        warranty=fields.get("Гарантия") or None,
        certificates=fields.get("Сертификаты") or None,
        payment_terms=("Авансовый платеж: " + fields["Размер авансового платежа %"] + "%")
        if fields.get("Размер авансового платежа %")
        else None,
        official_page_available=True,
        links=list(dict.fromkeys([*lot.links, *page_links(html, lot.source_url)])),
        raw={**lot.raw, "legacy_lot_fields": fields},
    )
    return LotData.model_validate(values)


def add_announcement(html: str, lot: LotData, settings: Settings) -> LotData:
    fields = fields_from_html(html)
    if fields.get("Номер объявления") != lot.announcement_number:
        raise SourceSchemaError("Announcement page does not match the expected announcement number")
    values = lot.model_dump()
    values.update(
        publication_date=source_datetime(
            fields.get("Дата публикации объявления"), settings.timezone
        ),
        submission_start=source_datetime(fields.get("Срок начала приема заявок"), settings.timezone)
        or lot.submission_start,
        submission_deadline=source_datetime(
            fields.get("Срок окончания приема заявок"), settings.timezone
        )
        or lot.submission_deadline,
        procurement_method=fields.get("Способ проведения закупки") or lot.procurement_method,
        raw={**lot.raw, "legacy_announcement_fields": fields},
    )
    return LotData.model_validate(values)


def document_modals(html: str, announcement_id: str) -> list[tuple[str, str]]:
    """Read only observed document controls; do not invoke arbitrary onclick code."""
    soup, result = BeautifulSoup(html, "lxml"), []
    for button in soup.select(
        '[onclick*="actionModalShowFiles"], .actionModalShowFiles[data-group]'
    ):
        match = re.fullmatch(
            r"\s*actionModalShowFiles\(\s*(\d+)\s*,\s*(\d+)\s*\);?\s*",
            str(button.get("onclick", "")),
        )
        identifier = match.group(1) if match else button.get("data-id")
        group = match.group(2) if match else button.get("data-group")
        if identifier != announcement_id or not group or not str(group).isdigit():
            raise SourceSchemaError("Document control identifiers do not match the announcement")
        row = button.find_parent("tr")
        name = text(row.find("td")) if row else ""
        result.append((str(group), name))
    return list(dict.fromkeys(result))


def parse_document_links(
    html: str, url: str, lot_number: str, group_name: str = ""
) -> list[DocumentData]:
    soup, documents = BeautifulSoup(html, "lxml"), {}
    for anchor in soup.select("a[href]"):
        target = urljoin(url, str(anchor["href"]))
        path = urlsplit(target).path.casefold()
        if not (
            "/files/download_file/" in path or re.search(r"\.(pdf|docx?|xlsx?|zip|rar)$", path)
        ):
            continue
        row, table = anchor.find_parent("tr"), anchor.find_parent("table")
        if row and table:
            headings = [text(cell) for cell in table.select("tr th")]
            if "Номер лота" in headings:
                index = headings.index("Номер лота")
                cells = row.find_all("td", recursive=False)
                if index >= len(cells):
                    raise SourceSchemaError("File table has no lot association cell")
                scope = text(cells[index])
                if scope and scope != lot_number:
                    continue
        name = text(anchor) or urlsplit(target).path.rsplit("/", 1)[-1]
        descriptor = f"{group_name} {name}".casefold()
        documents[target] = DocumentData(
            url=target,
            name=name,
            is_specification=any(
                term in descriptor for term in ("техническ", "техникалық", "techspec")
            ),
        )
    return list(documents.values())
