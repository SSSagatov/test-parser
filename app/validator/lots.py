from datetime import datetime

from app.config import Settings
from app.models.domain import LotData, ValidationResult
from app.utils.time import utc_now
from app.utils.urls import official_url
from app.validator.location import verify_pavlodar


def is_active(status: str | None, settings: Settings) -> bool:
    if not status:
        return False
    normalized = " ".join(status.casefold().split())
    if any(term.casefold() in normalized for term in settings.inactive_status_fragments):
        return False
    return normalized in {value.casefold() for value in settings.active_statuses}


def validate_lot(
    lot: LotData,
    settings: Settings,
    *,
    duplicate: bool = False,
    pavlodar: bool = False,
    now: datetime | None = None,
) -> ValidationResult:
    now = now or utc_now()
    reasons: list[str] = []
    required_evidence = (
        "lot_number",
        "announcement_number",
        "title",
        "description",
        "budget",
        "status",
        "currency",
        "publication_date",
        "submission_start",
        "submission_deadline",
        "procurement_method",
        "delivery_place",
        "execution_period",
        "warranty",
        "certificates",
        "quantity",
        "unit",
        "technical_characteristics",
        "installation_requirements",
        "payment_terms",
        "additional_conditions",
        "required_deliverables",
    )
    missing = [field for field in required_evidence if getattr(lot, field) in (None, "")]
    for field in ("name", "bin", "address"):
        if lot.customer is None or not getattr(lot.customer, field):
            missing.append(f"customer_{field}")
    if not lot.official_page_available or not official_url(lot.source_url, settings.official_hosts):
        reasons.append("OFFICIAL_PAGE_UNAVAILABLE")
    if not is_active(lot.status, settings):
        reasons.append("STATUS_NOT_ACTIVE")
    announcement = (
        lot.raw.get("announcement")
        or lot.raw.get("TrdBuy")
        or (lot.raw.get("ows") or {}).get("TrdBuy")
        or {}
    )
    announcement_status = announcement.get("status") or announcement.get("RefBuyStatus")
    if isinstance(announcement_status, dict):
        announcement_status = announcement_status.get("name_ru") or announcement_status.get(
            "nameRu"
        )
    if announcement_status and any(
        term.casefold() in str(announcement_status).casefold()
        for term in settings.inactive_status_fragments
    ):
        reasons.append("ANNOUNCEMENT_NOT_ACTIVE")
    if lot.raw.get("isDeleted") or (lot.raw.get("ows") or {}).get("isDeleted"):
        reasons.append("SOURCE_DELETED")
    if not lot.submission_start or not lot.submission_deadline:
        reasons.append("SUBMISSION_WINDOW_NOT_VERIFIED")
    elif lot.submission_start.tzinfo is None or lot.submission_deadline.tzinfo is None:
        reasons.append("SUBMISSION_TIMEZONE_NOT_VERIFIED")
    elif not lot.submission_start <= now < lot.submission_deadline:
        reasons.append("SUBMISSION_CLOSED")
    if duplicate:
        reasons.append("DUPLICATE")
    if pavlodar and (reason := verify_pavlodar(lot, settings)):
        reasons.append(reason)
    if not lot.specification_original:
        missing.append("technical_specification")
    if any(doc.parse_status not in {"success"} for doc in lot.documents):
        missing.append("document_text_incomplete")
    return ValidationResult(
        valid=not reasons, reasons=reasons, missing_data=missing, verified_at=now
    )
