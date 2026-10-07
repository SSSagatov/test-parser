"""HTML contracts verified against real public portal snapshots."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import Settings
from app.crawler.api.official import SearchWindowExceeded, SourceSchemaError
from app.parser.html.legacy import (
    add_announcement,
    document_modals,
    money,
    parse_document_links,
    parse_legacy_listing,
    parse_lot_detail,
)

FIXTURES = Path(__file__).parent / "fixtures" / "official"
SEARCH = "https://old.goszakup.gov.kz/ru/search/lots"


def html(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def settings():
    return Settings(_env_file=None)


@pytest.fixture
def listing(settings):
    return parse_legacy_listing(html("legacy_lots.html"), SEARCH, settings)


def test_complete_first_page_and_pagination(listing):
    assert len(listing.lots) == 50
    assert listing.lots[0].lot_number == "81508857-ЗЦП2"
    assert listing.lots[0].source_id == "43519574"
    assert listing.lots[0].announcement_number == "17746286-1"
    assert listing.lots[0].title == "Катетер"
    assert listing.lots[0].currency == "KZT"
    assert listing.next_cursor.endswith("count_record=50&page=2")


def test_real_details_do_not_infer_delivery_from_customer_address(listing, settings):
    lot = parse_lot_detail(html("legacy_lot_detail.html"), listing.lots[0], settings)
    lot = add_announcement(html("legacy_general.html"), lot, settings)
    assert lot.budget == Decimal("15000.00")
    assert lot.customer.bin == "160240030664"
    assert lot.description == "ВАЗОФИКС ЦЕРТО №G16"
    assert lot.submission_start.astimezone(UTC) == datetime(2026, 10, 8, 4, tzinfo=UTC)
    assert lot.delivery_place is None and lot.location_source_url is None
    assert "Юр. адрес организатора" in lot.raw["legacy_announcement_fields"]
    assert lot.specification_original == "" and lot.cpv is None


def test_detail_identity_mismatch_is_rejected(listing, settings):
    with pytest.raises(SourceSchemaError, match="does not match"):
        parse_lot_detail(html("legacy_lot_modal.html"), listing.lots[0], settings)


def test_missing_schema_is_not_a_successful_empty_page(settings):
    with pytest.raises(SourceSchemaError, match="results table"):
        parse_legacy_listing("<html><body>Maintenance</body></html>", SEARCH, settings)


def test_legacy_window_does_not_silently_finish(settings):
    with pytest.raises(SearchWindowExceeded, match="completeness"):
        parse_legacy_listing(
            html("legacy_lots.html"), SEARCH + "?count_record=50&page=200", settings
        )


def test_real_document_modals_and_lot_scoping(listing):
    lot = listing.lots[0]
    groups = document_modals(html("legacy_documents.html"), lot.announcement_id)
    assert ("125", "Техническая спецификация") in groups
    documents = parse_document_links(
        html("legacy_spec_modal.html"), lot.announcement_url, lot.lot_number
    )
    assert len(documents) == 1
    assert documents[0].name == "techspec_17746286_43519574.pdf"
    assert documents[0].is_specification
    assert not parse_document_links(
        html("legacy_spec_modal.html"), lot.announcement_url, "not-this-lot"
    )
    with pytest.raises(SourceSchemaError, match="identifiers"):
        document_modals(html("legacy_documents.html"), "wrong-announcement")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "unknown", "100-200"])
def test_invalid_amounts_are_not_silently_guessed(value):
    with pytest.raises(SourceSchemaError):
        money(value)
