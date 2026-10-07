"""Adapters for verified official interfaces; see docs/portal-research.md.

All traffic uses the injected transport, including robots checks. Public search
has a finite result window. Authenticated OWS uses keyset pagination for bulk
collection. Unsupported search dimensions are evaluated by the engine against
enriched records, never converted to guessed portal query parameters.
"""

import asyncio
from collections import OrderedDict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.config.settings import Settings
from app.models.domain import DocumentData, ListingPage, LotData, Party, SearchFilters


class Transport(Protocol):
    """Transport owns URL authorization, robots, retries and rate limiting."""

    async def get(self, url: str, params: dict | None = None) -> httpx.Response: ...

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response: ...


class SourceSchemaError(ValueError):
    """An official response no longer conforms to the verified schema."""


class SearchWindowExceeded(ValueError):
    """A search cannot continue completely within the portal's result window."""


class UnsupportedFilter(ValueError):
    """A requested classification cannot be verified by this source."""


def source_datetime(value: Any, timezone: str) -> datetime | None:
    """Decode epoch seconds or ISO dates without depending on machine time zone."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise SourceSchemaError("Boolean is not a source timestamp")
    if isinstance(value, (int, float, Decimal)):
        return datetime.fromtimestamp(float(value), UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=ZoneInfo(timezone)) if parsed.tzinfo is None else parsed


def _text(value: Any) -> str | None:
    return str(value).strip() or None if value is not None else None


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("name_ru") or value.get("nameRu") or value.get("name"))
    return _text(value)


def _joined(values: Any) -> str | None:
    return "; ".join(dict.fromkeys(text for value in values if (text := _text(value)))) or None


def _number(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _records(value: Any, *, context: str) -> list[dict]:
    if isinstance(value, dict):
        value = value.get("results", value.get("result"))
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SourceSchemaError(f"Expected records in {context}")
    return value


def _party(value: Any) -> Party | None:
    if not isinstance(value, dict) or not value:
        return None
    address = value.get("address")
    if isinstance(address, dict):
        address = _joined(address.get(key) for key in ("city", "street", "house", "office"))
    return Party(
        name=_name(value),
        bin=_text(value.get("bin") or value.get("iin_bin")),
        source_id=_text(value.get("id") or value.get("pid")),
        address=_text(address),
    )


def _customer(values: list[dict]) -> Party | None:
    """Avoid assigning one organization's BIN to a combined multi-customer lot."""
    parties = [party for value in values if (party := _party(value))]
    if not parties:
        return None
    unique = {(party.bin, party.name): party for party in parties}
    if len(unique) == 1:
        return next(iter(unique.values()))
    return Party(name=_joined(party.name for party in unique.values()))


def _specification(name: str | None) -> bool:
    normalized = (name or "").casefold()
    return any(
        term in normalized
        for term in ("техническ", "техникалық", "techspec", "technical specification")
    )


def public_documents(groups: Any, lot_id: str) -> list[DocumentData]:
    """Associate lot-specific files only with their stated lot; keep common files."""
    documents: dict[str, DocumentData] = {}
    for group in _records(groups, context="announcement documents"):
        group_name = _name(group.get("group"))
        for item in group.get("files") or []:
            linked_lot = item.get("lot")
            if linked_lot and str(linked_lot.get("id")) != lot_id:
                continue
            file = item.get("file") or {}
            url = file.get("file_url")
            if not url:
                continue
            name = file.get("filename") or item.get("original_name") or group_name
            documents[url] = DocumentData(
                url=url,
                name=name,
                is_specification=_specification(group_name) or _specification(name),
            )
    return list(documents.values())


class OfficialPublicSource:
    """Anonymous JSON adapter discovered from the official public frontend.

    Robots currently disallows /api/ on zakup.gov.kz. With the default transport
    policy this adapter is unavailable and automatic mode selects the allowed
    legacy UI. Its availability is checked at runtime, never assumed.
    """

    name = "public"

    def __init__(self, settings: Settings, transport: Transport):
        self.settings, self.transport = settings, transport
        self.base = (
            urljoin(settings.portal_url.rstrip("/") + "/", settings.public_api_path).rstrip("/")
            + "/"
        )
        self._system_id: str | None = None
        self._cache: OrderedDict[str, tuple[dict, list[dict], list[dict]]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}

    async def _json(self, url: str, params: dict | None = None) -> Any:
        response = await self.transport.get(url, params=params)
        response.raise_for_status()
        return response.json(parse_float=Decimal)

    async def _params(self, filters: SearchFilters) -> dict:
        if filters.cpv:
            raise UnsupportedFilter(
                "The official portal exposes ENSTRU, not CPV; CPV cannot be verified or substituted"
            )
        if self._system_id is None:
            url = urljoin(self.base, "../dicts/systems/")
            systems = _records(await self._json(url), context="system dictionary")
            matches = [item for item in systems if _name(item) == self.settings.public_system_name]
            if len(matches) != 1:
                raise SourceSchemaError(
                    "Configured procurement system is not uniquely present in official dictionary"
                )
            self._system_id = str(matches[0]["id"])
        params: dict[str, Any] = {
            "system_id": self._system_id,
            "limit": self.settings.page_size,
            "offset": 0,
        }
        mapping = {
            "q": filters.keyword,
            "announcement_number": filters.announcement_number,
            "lot_number": filters.lot_number,
            "total_price__gte": filters.budget_min,
            "total_price__lte": filters.budget_max,
        }
        params.update({key: str(value) for key, value in mapping.items() if value is not None})
        # Publication dates are local predicates. offer_start_date is a different
        # event, so applying the date filter to it would silently omit valid lots.
        return params

    def _cursor(self, value: str) -> str:
        url = urljoin(self.base, value)
        parsed, expected = urlsplit(url), urlsplit(urljoin(self.base, "lots/"))
        if (parsed.scheme, parsed.netloc, parsed.path) != (
            expected.scheme,
            expected.netloc,
            expected.path,
        ):
            raise SourceSchemaError("Pagination cursor points outside the official lots endpoint")
        params = parse_qs(parsed.query)
        offset = int(params.get("offset", ["0"])[0])
        limit = int(params.get("limit", [str(self.settings.page_size)])[0])
        if offset < 0 or limit <= 0:
            raise SourceSchemaError("Invalid pagination offset or limit")
        if offset + limit > self.settings.public_result_window:
            raise SearchWindowExceeded(
                f"Public search cannot page beyond {self.settings.public_result_window} results; "
                "use authenticated OWS keyset pagination or narrower verified filters. Crawl is incomplete."
            )
        return url

    async def fetch_page(self, filters: SearchFilters, cursor: str | None = None) -> ListingPage:
        params = await self._params(filters) if cursor is None else None
        url = self._cursor(cursor) if cursor else urljoin(self.base, "lots/")
        payload = await self._json(url, params)
        if not isinstance(payload, dict) or "results" not in payload:
            raise SourceSchemaError("Public lots response has no results list")
        rows = _records(payload, context="public lots")
        next_cursor = payload.get("next")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise SourceSchemaError("Invalid public next cursor")
        if next_cursor and not rows:
            raise SourceSchemaError("Empty public page advertises a following page")
        return ListingPage(
            lots=[self.map_listing(item) for item in rows], next_cursor=next_cursor, source_url=url
        )

    def map_listing(self, item: dict) -> LotData:
        """Preserve missing titles/specifications instead of filling from ENSTRU."""
        if not item.get("id") or not item.get("announcement_id"):
            raise SourceSchemaError("Official lot is missing its lot or announcement identifier")
        lot_id, announcement_id = str(item["id"]), str(item["announcement_id"])
        announcement_url = f"{self.settings.portal_url.rstrip('/')}/announcement/{announcement_id}"
        source_url = f"{announcement_url}#lot-{lot_id}"
        delivery = _joined(_name(place) for place in item.get("delivery_addresses") or [])
        return LotData(
            source_id=lot_id,
            source_url=source_url,
            lot_number=_text(item.get("lot_number")),
            announcement_id=announcement_id,
            announcement_number=_text(item.get("announcement_number")),
            announcement_url=announcement_url,
            title=_name(item),
            description=_text(item.get("description_ru")),
            budget=_number(item.get("total_price")),
            status=_name(item.get("status")) or item.get("status_name"),
            publication_date=source_datetime(
                item.get("announcement_publish_date"), self.settings.timezone
            ),
            submission_start=source_datetime(item.get("offer_start_date"), self.settings.timezone),
            submission_deadline=source_datetime(item.get("offer_end_date"), self.settings.timezone),
            procurement_method=_text(item.get("purchase_method_name")),
            customer=_customer(item.get("customers") or []),
            delivery_place=delivery,
            location_source_url=source_url if delivery else None,
            quantity=_number(item.get("quantity")),
            collected_at=datetime.now(UTC),
            raw={"listing": item},
            links=[source_url, announcement_url],
        )

    async def _announcement(self, identifier: str) -> tuple[dict, list[dict], list[dict]]:
        if identifier in self._cache:
            self._cache.move_to_end(identifier)
            return self._cache[identifier]

        async def fetch() -> tuple[dict, list[dict], list[dict]]:
            root = urljoin(self.base, f"announcements/{identifier}/")
            announcement, lots, documents = await asyncio.gather(
                self._json(root),
                self._json(root + "lots/"),
                self._json(root + "general-files/"),
            )
            if not isinstance(announcement, dict) or str(announcement.get("id")) != identifier:
                raise SourceSchemaError("Announcement identity does not match requested page")
            result = (
                announcement,
                _records(lots, context="announcement lots"),
                _records(documents, context="documents"),
            )
            self._cache[identifier] = result
            while len(self._cache) > self.settings.api_announcement_cache_size:
                self._cache.popitem(last=False)
            return result

        task = self._inflight.get(identifier)
        if task is None:
            task = asyncio.create_task(fetch())
            self._inflight[identifier] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._inflight.pop(identifier, None)

    async def enrich(self, lot: LotData) -> LotData:
        announcement, lots, groups = await self._announcement(str(lot.announcement_id))
        matching = [item for item in lots if str(item.get("id")) == lot.source_id]
        if len(matching) != 1:
            raise SourceSchemaError("Listed lot is absent or duplicated in announcement detail")
        detail = matching[0]
        plans = _records(detail.get("plan_items") or [], context="lot plan items")
        delivery = _joined(
            place.get("kato_full_name_ru")
            or place.get("kato_full_name")
            or (place.get("kato") or {}).get("full_name_ru")
            or (place.get("kato") or {}).get("full_name")
            for plan in plans
            for place in plan.get("deliveries") or []
        )
        documents = public_documents(groups, lot.source_id)
        page = await self.transport.get(lot.source_url.split("#", 1)[0])
        page.raise_for_status()
        values = lot.model_dump()
        values.update(
            title=_name(detail) or _joined(_name(plan) for plan in plans) or lot.title,
            description=_text(detail.get("description"))
            or _joined(
                plan.get("extra_description_ru")
                or plan.get("extra_description")
                or plan.get("description_ru")
                for plan in plans
            )
            or lot.description,
            status=_name(detail.get("status")) or lot.status,
            budget=_number(detail.get("total_price"))
            if detail.get("total_price") is not None
            else lot.budget,
            publication_date=source_datetime(
                announcement.get("publish_date"), self.settings.timezone
            ),
            submission_start=source_datetime(
                announcement.get("offer_start_date"), self.settings.timezone
            ),
            submission_deadline=source_datetime(
                announcement.get("offer_end_date"), self.settings.timezone
            ),
            procurement_method=_name(announcement.get("purchase_method")) or lot.procurement_method,
            customer=_customer([plan["organization"] for plan in plans if plan.get("organization")])
            or lot.customer,
            delivery_place=delivery or lot.delivery_place,
            location_source_url=lot.source_url if delivery or lot.delivery_place else None,
            execution_period=_joined(
                plan.get("delivery_deadline_ru") or plan.get("delivery_deadline") for plan in plans
            ),
            unit=_joined(_name(plan.get("measure")) for plan in plans),
            documents=documents,
            links=list(dict.fromkeys([*lot.links, *(document.url for document in documents)])),
            official_page_available=bool(page.content),
            raw={
                **lot.raw,
                "announcement": announcement,
                "detail": detail,
                "document_groups": groups,
            },
        )
        supplier = _party(announcement.get("supplier"))
        if supplier:
            values["suppliers"] = [supplier]
        if len(plans) == 1 and plans[0].get("count") is not None:
            values["quantity"] = _number(plans[0]["count"])
        return LotData.model_validate(values)


# Field spelling and relationships are from the official v3 schema, not inferred
# from the unrelated aggregated public portal's identifiers.
OWS_QUERY = """
query LotsPage($filter: LotsFiltersInput, $limit: Int, $after: Int) {
  Lots(filter: $filter, limit: $limit, after: $after) {
    id lotNumber nameRu descriptionRu amount count customerId customerBin customerNameRu
    trdBuyId trdBuyNumberAnno isDeleted systemId
    RefLotsStatus { nameRu }
    RefBuyTradeMethods { nameRu }
    Plans {
      id nameRu extraDescRu supplyDateRu count refEnstruCode
      RefUnits { nameRu }
      PlansKato { fullDeliveryPlaceNameRu isDeleted }
    }
    Files { filePath originalName nameRu }
    TrdBuy {
      id numberAnno publishDate startDate endDate repeatStartDate repeatEndDate
      biinSupplier RefBuyStatus { nameRu } RefTradeMethods { nameRu }
      Files { filePath originalName nameRu }
    }
  }
}
"""


class OwsSource:
    """Official token-authenticated v3 GraphQL source with keyset pagination."""

    name = "ows"

    def __init__(self, settings: Settings, transport: Transport):
        self.settings, self.transport = settings, transport
        if not settings.ows_token or not settings.ows_token.get_secret_value():
            raise ValueError(
                "OWS_TOKEN is required; request initial access from the Electronic Finance Center"
            )

    async def fetch_page(self, filters: SearchFilters, cursor: str | None = None) -> ListingPage:
        if filters.cpv:
            raise UnsupportedFilter("OWS exposes ENSTRU, not CPV; CPV cannot be substituted")
        source_filter: dict[str, Any] = {}
        if filters.keyword:
            source_filter["nameDescriptionRu"] = filters.keyword
        if filters.lot_number:
            source_filter["lotNumber"] = filters.lot_number
        if filters.announcement_number:
            source_filter["trdBuyNumberAnno"] = filters.announcement_number
        if filters.customer and filters.customer.isdigit() and len(filters.customer) == 12:
            source_filter["customerBin"] = filters.customer
        # Region/city, supplier, publication date, status, method and money are
        # conservative engine predicates on the complete enriched lot.
        after = int(cursor) if cursor is not None else None
        response = await self.transport.request(
            "POST",
            self.settings.ows_url,
            headers={"Authorization": f"Bearer {self.settings.ows_token.get_secret_value()}"},
            json={
                "query": OWS_QUERY,
                "variables": {
                    "filter": source_filter,
                    "limit": self.settings.page_size,
                    "after": after,
                },
            },
        )
        response.raise_for_status()
        payload = response.json(parse_float=Decimal)
        if payload.get("errors"):
            messages = "; ".join(
                str(error.get("message", "Unknown error")) for error in payload["errors"]
            )
            raise SourceSchemaError(f"Official OWS GraphQL errors: {messages}")
        rows = _records((payload.get("data") or {}).get("Lots"), context="OWS Lots")
        page_info = (payload.get("extensions") or {}).get("pageInfo") or {}
        next_cursor = None
        more = page_info.get("hasNextPage", bool(rows))
        if more:
            if not rows:
                raise SourceSchemaError("OWS returned no rows but claims a next page")
            last_id = page_info.get("lastId", rows[-1].get("id"))
            if last_id is None or str(last_id) == cursor:
                raise SourceSchemaError("OWS keyset cursor did not advance")
            next_cursor = str(last_id)
        return ListingPage(
            lots=[self.map_lot(row) for row in rows],
            next_cursor=next_cursor,
            source_url=self.settings.ows_url,
        )

    def map_lot(self, item: dict) -> LotData:
        if not item.get("id") or not item.get("trdBuyId"):
            raise SourceSchemaError("OWS lot is missing an identifier")
        lot_id, announcement_id = str(item["id"]), str(item["trdBuyId"])
        base = self.settings.legacy_portal_url.rstrip("/")
        url = f"{base}/ru/subpriceoffer/index/{announcement_id}/{lot_id}"
        announcement_url = f"{base}/ru/announce/index/{announcement_id}"
        announcement = item.get("TrdBuy") or {}
        plans = item.get("Plans") or []
        delivery = _joined(
            place.get("fullDeliveryPlaceNameRu")
            for plan in plans
            for place in plan.get("PlansKato") or []
            if not place.get("isDeleted")
        )
        documents: dict[str, DocumentData] = {}
        for file in [*(item.get("Files") or []), *(announcement.get("Files") or [])]:
            path = file.get("filePath")
            if not path:
                continue
            # Preserve the official absolute URL. Relative OWS storage paths do
            # not establish a download origin and must not be guessed.
            if urlsplit(path).scheme not in {"http", "https"}:
                raise SourceSchemaError(
                    "OWS document has a relative storage path without a verified download origin"
                )
            name = file.get("originalName") or file.get("nameRu")
            documents[path] = DocumentData(
                url=path,
                name=name,
                is_specification=_specification(file.get("nameRu")) or _specification(name),
            )
        status = _name(item.get("RefLotsStatus"))
        if item.get("isDeleted"):
            status = "Удален"
        repeated = status and "дополн" in status.casefold()
        return LotData(
            source_id=lot_id,
            source_url=url,
            lot_number=item.get("lotNumber"),
            announcement_id=announcement_id,
            announcement_number=item.get("trdBuyNumberAnno"),
            announcement_url=announcement_url,
            title=item.get("nameRu"),
            description=item.get("descriptionRu"),
            budget=_number(item.get("amount")),
            quantity=_number(item.get("count")),
            status=status,
            publication_date=source_datetime(
                announcement.get("publishDate"), self.settings.timezone
            ),
            submission_start=source_datetime(
                announcement.get("repeatStartDate") if repeated else announcement.get("startDate"),
                self.settings.timezone,
            ),
            submission_deadline=source_datetime(
                announcement.get("repeatEndDate") if repeated else announcement.get("endDate"),
                self.settings.timezone,
            ),
            procurement_method=_name(item.get("RefBuyTradeMethods"))
            or _name(announcement.get("RefTradeMethods")),
            customer=Party(
                name=item.get("customerNameRu"),
                bin=item.get("customerBin"),
                source_id=_text(item.get("customerId")),
            ),
            suppliers=[Party(bin=announcement["biinSupplier"])]
            if announcement.get("biinSupplier")
            else [],
            delivery_place=delivery,
            location_source_url=url if delivery else None,
            execution_period=_joined(plan.get("supplyDateRu") for plan in plans),
            unit=_joined(_name(plan.get("RefUnits")) for plan in plans),
            documents=list(documents.values()),
            links=[url, announcement_url, *documents],
            collected_at=datetime.now(UTC),
            raw={"ows": item},
        )

    async def enrich(self, lot: LotData) -> LotData:
        page = await self.transport.get(lot.source_url)
        page.raise_for_status()
        values = lot.model_dump()
        values["official_page_available"] = bool(page.content)
        return LotData.model_validate(values)
