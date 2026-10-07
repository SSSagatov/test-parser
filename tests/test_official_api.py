"""Offline contract tests using reduced, real official responses captured 2026-10-08."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import Settings
from app.crawler.api.official import (
    OfficialPublicSource,
    OwsSource,
    SearchWindowExceeded,
    SourceSchemaError,
    UnsupportedFilter,
    public_documents,
    source_datetime,
)
from app.models.domain import SearchFilters

FIXTURES = Path(__file__).parent / "fixtures" / "official"


def captured(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def settings():
    return Settings(_env_file=None)


@pytest.fixture
async def transport():
    calls = []

    def handle(request):
        calls.append(request)
        path = request.url.path
        if path.endswith("/dicts/systems/"):
            return httpx.Response(200, json=captured("systems.json"))
        if path == "/api/core/api/public/lots/":
            payload = captured("public_lots.json")
            if request.url.params.get("offset") == "1":
                payload.update(results=[], next=None)
            return httpx.Response(200, json=payload)
        if path == "/api/core/api/public/announcements/40745436/":
            return httpx.Response(200, json=captured("announcement.json"))
        if path.endswith("/40745436/lots/"):
            return httpx.Response(200, json=captured("announcement_lots.json"))
        if path.endswith("/40745436/general-files/"):
            return httpx.Response(200, json=captured("announcement_files.json"))
        if path == "/announcement/40745436":
            return httpx.Response(200, text="<html><div id='root'></div></html>")
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        yield client, calls


async def test_follow_official_next_cursor(transport, settings):
    client, calls = transport
    source = OfficialPublicSource(settings, client)
    first = await source.fetch_page(SearchFilters(keyword="кабинет"))
    assert first.lots[0].lot_number == "88101676-ОК1"
    assert first.lots[0].title is None  # ENSTRU is not a substitute for title/specification.
    assert first.lots[0].source_url.endswith("/announcement/40745436#lot-61182602")
    assert calls[1].url.params["system_id"] == "1"
    assert calls[1].url.params["q"] == "кабинет"
    second = await source.fetch_page(SearchFilters(), first.next_cursor)
    assert not second.lots and second.next_cursor is None
    assert calls[-1].url.params["offset"] == "1"


async def test_enrich_real_lot_scopes_delivery_and_documents(transport, settings):
    client, calls = transport
    source = OfficialPublicSource(settings, client)
    listing = source.map_listing(captured("public_lots.json")["results"][0])
    enriched, repeated = await asyncio.gather(source.enrich(listing), source.enrich(listing))
    assert enriched.title == "Кабинет учебный"
    assert enriched.customer.bin == "970840003360"
    assert enriched.budget == Decimal("17241379.31")
    assert enriched.submission_start == datetime(2026, 10, 9, 4, tzinfo=UTC)
    assert enriched.submission_deadline == datetime(2026, 10, 16, 4, 30, tzinfo=UTC)
    assert enriched.delivery_place == "Карагандинская область, г.Темиртау"
    assert enriched.execution_period.startswith("45 календарных")
    assert enriched.unit == "Комплект"
    assert enriched.quantity == 1
    assert enriched.official_page_available
    assert enriched.specification_original == ""
    assert enriched.cpv is None and enriched.currency is None
    assert len(enriched.documents) == 5
    specs = [doc for doc in enriched.documents if doc.is_specification]
    assert [doc.name for doc in specs] == ["techspec_17712609_43519528.pdf"]
    assert repeated.documents == enriched.documents
    assert sum(str(request.url).endswith("/40745436/lots/") for request in calls) == 1


def test_document_association_excludes_another_lots_specification():
    groups = captured("announcement_files.json")
    # A real record queried using a different ID must retain only common files.
    documents = public_documents(groups, "not-the-official-lot-id")
    assert len(documents) == 3
    assert not any(document.is_specification for document in documents)


async def test_result_window_fails_loudly_before_request(transport, settings):
    client, calls = transport
    source = OfficialPublicSource(settings, client)
    with pytest.raises(SearchWindowExceeded, match="incomplete"):
        await source.fetch_page(SearchFilters(), source.base + "lots/?offset=10000&limit=1")
    assert calls == []


async def test_cursor_cannot_escape_official_endpoint(transport, settings):
    client, calls = transport
    source = OfficialPublicSource(settings, client)
    with pytest.raises(SourceSchemaError, match="outside"):
        await source.fetch_page(SearchFilters(), "https://example.com/lots/?offset=1")
    assert calls == []


async def test_cpv_is_not_silently_treated_as_enstru(transport, settings):
    client, calls = transport
    with pytest.raises(UnsupportedFilter, match="ENSTRU"):
        await OfficialPublicSource(settings, client).fetch_page(SearchFilters(cpv="30200000"))
    assert calls == []


def test_status_dictionary_enabled_flag_does_not_mean_open(transport, settings):
    source = OfficialPublicSource(settings, None)
    lot = source.map_listing(captured("public_lots.json")["results"][0])
    assert lot.status == "Опубликован"
    assert lot.validation is None
    assert not lot.official_page_available


def test_datetime_uses_source_timezone_and_preserves_missing():
    assert source_datetime(1791518400, "Asia/Almaty") == datetime(2026, 10, 9, 4, tzinfo=UTC)
    assert (
        source_datetime("2026-10-09 09:00:00", "Asia/Almaty").utcoffset().total_seconds() == 18000
    )
    assert source_datetime(None, "Asia/Almaty") is None
    with pytest.raises(SourceSchemaError):
        source_datetime(True, "Asia/Almaty")
    with pytest.raises(ValueError):
        source_datetime("not-an-official-date", "Asia/Almaty")


async def test_ows_requires_token_and_rejects_graphql_errors(settings):
    with pytest.raises(ValueError, match="OWS_TOKEN"):
        OwsSource(settings, None)
    settings.ows_token = SecretStr("unit-test-token")
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"errors": [{"message": "Access denied"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(SourceSchemaError, match="Access denied"):
            await OwsSource(settings, client).fetch_page(SearchFilters(), "43519528")
    payload = json.loads(requests[0].content)
    assert payload["variables"]["after"] == 43519528
    assert requests[0].headers["Authorization"] == "Bearer unit-test-token"


async def test_ows_empty_response_terminates_without_invented_records(settings):
    settings.ows_token = SecretStr("unit-test-token")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": {"Lots": []}})
        )
    ) as client:
        page = await OwsSource(settings, client).fetch_page(SearchFilters())
    assert page.lots == [] and page.next_cursor is None
