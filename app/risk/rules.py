"""Explainable requirement-based risk assessment; amount is never an input."""

from datetime import datetime, timedelta

from app.config import Settings
from app.models.domain import LotData, RiskLevel, RiskResult
from app.utils.time import utc_now


def assess_risk(lot: LotData, settings: Settings, now: datetime | None = None) -> RiskResult:
    now = now or utc_now()
    text = " ".join(
        filter(
            None,
            [
                lot.title,
                lot.description,
                lot.specification_original,
                lot.installation_requirements,
                lot.certificates,
                lot.execution_period,
                lot.additional_conditions,
            ],
        )
    ).casefold()
    high: list[str] = []
    if not lot.specification_original.strip():
        high.append("Missing technical specification; requirements cannot be verified")
    pending_documents = list(lot.documents)
    while pending_documents:
        document = pending_documents.pop()
        if document.is_specification and document.parse_status != "success":
            high.append("Critical specification document is missing or incompletely parsed")
            break
        pending_documents.extend(document.children)
    high.extend(
        f"High-complexity requirement: {term}"
        for term in settings.risk_high_terms
        if term.casefold() in text
    )
    if lot.submission_deadline and lot.submission_deadline.tzinfo is None:
        high.append("Submission deadline timezone is unverified")
    elif lot.submission_deadline and now < lot.submission_deadline <= now + timedelta(
        days=settings.strict_deadline_days
    ):
        high.append(f"Submission deadline is within {settings.strict_deadline_days} days")
    if high:
        return RiskResult(level=RiskLevel.HIGH, reasons=high, color="#F8696B")
    medium = [
        f"Special requirement: {term}"
        for term in settings.risk_medium_terms
        if term.casefold() in text
    ]
    if lot.installation_requirements:
        medium.append("Installation requirements are present")
    if lot.certificates:
        medium.append("Certificates are required")
    if medium:
        return RiskResult(
            level=RiskLevel.MEDIUM, reasons=list(dict.fromkeys(medium)), color="#FFEB84"
        )
    if any(term.casefold() in text for term in settings.risk_standard_terms):
        return RiskResult(
            level=RiskLevel.LOW,
            reasons=["Standard product; specification parsed; no detected special requirements"],
            color="#63BE7B",
        )
    return RiskResult(
        level=RiskLevel.MEDIUM,
        reasons=["Specification parsed, but standard-product classification is unverified"],
        color="#FFEB84",
    )
