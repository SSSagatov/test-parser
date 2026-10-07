"""Constructed domain inputs exercise policy boundaries; no procurement records are fabricated."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Settings
from app.models.domain import DocumentData, LotData, Party, RiskLevel, SearchFilters
from app.risk.rules import assess_risk
from app.validator.filters import matches
from app.validator.location import verify_pavlodar
from app.validator.lots import is_active, validate_lot

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def lot() -> LotData:
    return LotData(
        source_id="constructed-validator-input",
        source_url="https://zakup.gov.kz/__constructed_validator_input__",
        lot_number="constructed-lot-number",
        announcement_number="constructed-announcement-number",
        title="Бумага: constructed parser test",
        status="Прием заявок",
        submission_start=NOW - timedelta(days=2),
        submission_deadline=NOW + timedelta(days=10),
        publication_date=NOW - timedelta(days=3),
        delivery_place="г. Павлодар",
        location_source_url="https://zakup.gov.kz/__constructed_location_input__",
        specification_original="Бумага: constructed requirements text, no procurement data.",
        official_page_available=True,
        collected_at=NOW,
    )


def test_complete_open_lot_validates(lot: LotData, settings: Settings) -> None:
    result = validate_lot(lot, settings, pavlodar=True, now=NOW)
    assert result.valid
    assert not result.reasons
    assert result.verified_at == NOW


@pytest.mark.parametrize("field", ["delivery_place", "work_place", "service_location"])
def test_pavlodar_accepts_only_explicit_official_places(
    lot: LotData, settings: Settings, field: str
) -> None:
    lot.delivery_place = None
    setattr(lot, field, "Казахстан, г. Павлодар, constructed test location")
    assert verify_pavlodar(lot, settings) is None


@pytest.mark.parametrize(
    "place",
    [
        None,
        "Павлодарская область",
        "Павлодарский район, с. Павлодарское",
        "Павлодар",
        "г. Павлодарское",
        "г. Павлодар-2",
        "г. Экибастуз",
    ],
)
def test_pavlodar_never_infers_city_from_region_or_customer(
    lot: LotData, settings: Settings, place: str | None
) -> None:
    lot.delivery_place = place
    lot.customer = Party(name="Constructed customer", address="г. Павлодар")
    lot.city = "Павлодар"
    lot.region = "Павлодарская область"
    assert verify_pavlodar(lot, settings) == "LOCATION_NOT_VERIFIED"
    assert not matches(lot, SearchFilters(city="Pavlodar"), settings)


@pytest.mark.parametrize(
    "source",
    [
        None,
        "https://example.com/source",
        "https://zakup.gov.kz.example.com/source",
        "ftp://zakup.gov.kz/source",
        "https://user:pass@zakup.gov.kz/source",
        "https://zakup.gov.kz:8443/source",
    ],
)
def test_location_requires_safe_official_evidence(
    lot: LotData, settings: Settings, source: str | None
) -> None:
    lot.location_source_url = source
    assert verify_pavlodar(lot, settings) == "LOCATION_NOT_VERIFIED"


@pytest.mark.parametrize("status", ["Отменен", "Завершен", "Итоги", "cancelled", "completed"])
def test_inactive_status_overrides_configured_active_value(
    lot: LotData, settings: Settings, status: str
) -> None:
    settings.active_statuses.append(status)
    lot.status = status
    assert not is_active(status, settings)
    assert "STATUS_NOT_ACTIVE" in validate_lot(lot, settings, now=NOW).reasons


def test_published_real_portal_status_is_active(settings: Settings) -> None:
    assert is_active("Опубликован", settings)


@pytest.mark.parametrize(
    "raw",
    [
        {"announcement": {"status": "Отменен"}},
        {"announcement": {"status": {"name_ru": "Отменен"}}},
        {"ows": {"TrdBuy": {"RefBuyStatus": {"nameRu": "Отменен"}}}},
    ],
)
def test_cancelled_announcement_invalidates_active_lot(
    lot: LotData, settings: Settings, raw: dict
) -> None:
    lot.raw = raw
    assert not validate_lot(lot, settings, now=NOW).valid


@pytest.mark.parametrize("field", ["submission_start", "submission_deadline"])
def test_missing_window_boundary_is_not_assumed_open(
    lot: LotData, settings: Settings, field: str
) -> None:
    setattr(lot, field, None)
    result = validate_lot(lot, settings, now=NOW)
    assert not result.valid
    assert "SUBMISSION_WINDOW_NOT_VERIFIED" in result.reasons
    assert field in result.missing_data


@pytest.mark.parametrize("field", ["submission_start", "submission_deadline"])
def test_naive_source_time_is_explicitly_rejected(
    lot: LotData, settings: Settings, field: str
) -> None:
    setattr(lot, field, getattr(lot, field).replace(tzinfo=None))
    result = validate_lot(lot, settings, now=NOW)
    assert not result.valid
    assert "SUBMISSION_TIMEZONE_NOT_VERIFIED" in result.reasons


def test_submission_start_inclusive_deadline_exclusive(lot: LotData, settings: Settings) -> None:
    lot.submission_start = NOW
    assert validate_lot(lot, settings, now=NOW).valid
    lot.submission_deadline = NOW
    result = validate_lot(lot, settings, now=NOW)
    assert not result.valid
    assert "SUBMISSION_CLOSED" in result.reasons


def test_duplicate_and_unavailable_page_both_have_reasons(lot: LotData, settings: Settings) -> None:
    lot.official_page_available = False
    result = validate_lot(lot, settings, duplicate=True, now=NOW)
    assert not result.valid
    assert {"DUPLICATE", "OFFICIAL_PAGE_UNAVAILABLE"} <= set(result.reasons)


def test_incomplete_document_is_reported_even_with_partial_text(
    lot: LotData, settings: Settings
) -> None:
    lot.documents = [
        DocumentData(
            url=lot.source_url,
            parse_status="partial_errors",
            original_text="Constructed partial text",
            is_specification=True,
            error="Constructed OCR failure",
        )
    ]
    validation = validate_lot(lot, settings, now=NOW)
    assert "document_text_incomplete" in validation.missing_data
    assert assess_risk(lot, settings, now=NOW).level == RiskLevel.HIGH


def test_failed_specification_inside_generic_archive_remains_critical(
    lot: LotData, settings: Settings
) -> None:
    lot.documents = [
        DocumentData(
            url=lot.source_url,
            name="constructed-documents.zip",
            parse_status="partial_errors",
            children=[
                DocumentData(
                    url=lot.source_url,
                    name="Техническая спецификация.pdf",
                    parse_status="error",
                    is_specification=True,
                    error="Constructed OCR failure",
                )
            ],
        )
    ]
    risk = assess_risk(lot, settings, now=NOW)
    assert risk.level == RiskLevel.HIGH
    assert any("Critical specification document" in reason for reason in risk.reasons)


def test_risk_is_identical_at_different_budgets(lot: LotData, settings: Settings) -> None:
    lot.budget = Decimal("1")
    small = assess_risk(lot, settings, now=NOW)
    lot.budget = Decimal("1000000000000")
    large = assess_risk(lot, settings, now=NOW)
    assert small == large
    assert small.level == RiskLevel.LOW


@pytest.mark.parametrize(
    "field,value",
    [
        ("installation_requirements", "Constructed installation constraint"),
        ("certificates", "Constructed certificate constraint"),
    ],
)
def test_explicit_special_requirements_raise_risk(
    lot: LotData, settings: Settings, field: str, value: str
) -> None:
    setattr(lot, field, value)
    assert assess_risk(lot, settings, now=NOW).level == RiskLevel.MEDIUM


@pytest.mark.parametrize(
    "text",
    ["", "Строительство: constructed requirement", "Сложное оборудование: constructed requirement"],
)
def test_missing_or_complex_specification_is_high_risk(
    lot: LotData, settings: Settings, text: str
) -> None:
    lot.specification_original = text
    assert assess_risk(lot, settings, now=NOW).level == RiskLevel.HIGH


def test_short_deadline_is_high_risk(lot: LotData, settings: Settings) -> None:
    lot.submission_deadline = NOW + timedelta(days=settings.strict_deadline_days)
    assert assess_risk(lot, settings, now=NOW).level == RiskLevel.HIGH


def test_invalid_naive_deadline_can_still_be_risk_assessed(
    lot: LotData, settings: Settings
) -> None:
    lot.submission_deadline = lot.submission_deadline.replace(tzinfo=None)
    assert assess_risk(lot, settings, now=NOW).level != RiskLevel.LOW


def test_identifiers_match_exactly_and_budget_zero_is_not_missing(
    lot: LotData, settings: Settings
) -> None:
    lot.budget = Decimal(0)
    assert matches(
        lot, SearchFilters(lot_number=lot.lot_number, budget_min=0, budget_max=0), settings
    )
    assert not matches(lot, SearchFilters(lot_number="constructed"), settings)
    assert not matches(lot, SearchFilters(budget_min=1), settings)
    lot.budget = None
    assert not matches(lot, SearchFilters(budget_max=1), settings)


def test_filters_use_specification_keyword_and_reject_missing_dates(
    lot: LotData, settings: Settings
) -> None:
    assert matches(lot, SearchFilters(keyword="requirements"), settings)
    assert matches(
        lot, SearchFilters(date_from=lot.publication_date, date_to=lot.publication_date), settings
    )
    lot.publication_date = None
    assert not matches(lot, SearchFilters(date_from=NOW), settings)


def test_supplier_filter_requires_actual_supplier_evidence(
    lot: LotData, settings: Settings
) -> None:
    assert not matches(lot, SearchFilters(supplier="Constructed supplier"), settings)
    lot.suppliers = [Party(name="Constructed supplier")]
    assert matches(lot, SearchFilters(supplier="constructed SUPPLIER"), settings)
