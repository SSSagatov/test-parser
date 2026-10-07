"""Source data and analysis contracts. Missing official values stay missing."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Party(Record):
    name: str | None = None
    bin: str | None = None
    address: str | None = None
    source_id: str | None = None

    @field_validator("bin")
    @classmethod
    def valid_bin(cls, value: str | None) -> str | None:
        if value is not None and (not value.isdigit() or len(value) != 12):
            raise ValueError("BIN must contain exactly 12 digits")
        return value


class DocumentData(Record):
    url: str
    name: str | None = None
    media_type: str | None = None
    local_path: Path | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    original_text: str = ""
    summary: str = ""
    parse_status: str = "pending"
    error: str | None = None
    is_specification: bool = False
    downloaded_at: datetime | None = None
    children: list["DocumentData"] = Field(default_factory=list)


class ValidationResult(Record):
    valid: bool = False
    reasons: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    verified_at: datetime


class RiskResult(Record):
    level: RiskLevel
    reasons: list[str]
    color: str


class LotData(Record):
    source_id: str
    source_url: str
    lot_number: str | None = None
    announcement_id: str | None = None
    announcement_number: str | None = None
    announcement_url: str | None = None
    title: str | None = None
    description: str | None = None
    budget: Decimal | None = Field(default=None, ge=0)
    status: str | None = None
    currency: str | None = None
    publication_date: datetime | None = None
    submission_start: datetime | None = None
    submission_deadline: datetime | None = None
    procurement_method: str | None = None
    customer: Party | None = None
    suppliers: list[Party] = Field(default_factory=list)
    delivery_place: str | None = None
    work_place: str | None = None
    service_location: str | None = None
    location_source_url: str | None = None
    region: str | None = None
    city: str | None = None
    execution_period: str | None = None
    warranty: str | None = None
    certificates: str | None = None
    quantity: Decimal | None = Field(default=None, ge=0)
    unit: str | None = None
    cpv: str | None = None
    technical_characteristics: str | None = None
    installation_requirements: str | None = None
    payment_terms: str | None = None
    additional_conditions: str | None = None
    required_deliverables: str | None = None
    links: list[str] = Field(default_factory=list)
    documents: list[DocumentData] = Field(default_factory=list)
    specification_original: str = ""
    specification_summary: str = ""
    official_page_available: bool = False
    collected_at: datetime
    validation: ValidationResult | None = None
    risk: RiskResult | None = None
    raw: dict = Field(default_factory=dict)

    @field_validator("source_id")
    @classmethod
    def nonempty_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_id must be nonempty")
        return value


class SearchFilters(Record):
    region: str | None = None
    city: str | None = None
    customer: str | None = None
    supplier: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    status: str | None = None
    method: str | None = None
    budget_min: Decimal | None = Field(default=None, ge=0)
    budget_max: Decimal | None = Field(default=None, ge=0)
    keyword: str | None = None
    cpv: str | None = None
    announcement_number: str | None = None
    lot_number: str | None = None

    @field_validator("date_from", "date_to")
    @classmethod
    def filter_date_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Search dates must have an explicit timezone")
        return value

    @model_validator(mode="after")
    def ordered_ranges(self) -> "SearchFilters":
        if self.budget_min is not None and self.budget_max is not None:
            if self.budget_min > self.budget_max:
                raise ValueError("budget_min exceeds budget_max")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from exceeds date_to")
        return self


class ListingPage(Record):
    lots: list[LotData]
    next_cursor: str | None
    source_url: str


class CrawlStatistics(Record):
    pages: int = 0
    processed: int = 0
    saved: int = 0
    invalid: int = 0
    filtered: int = 0
    duplicates: int = 0
    failed: int = 0
