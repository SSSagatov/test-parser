"""Portable SQLAlchemy schema; PostgreSQL uses native UUID and JSONB storage."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Timestamped:
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Announcement(Timestamped, Base):
    __tablename__ = "announcements"
    source_key: Mapped[str] = mapped_column(String(512), unique=True)
    source_id: Mapped[str | None] = mapped_column(String(255))
    number: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(Text)


class Customer(Timestamped, Base):
    __tablename__ = "customers"
    source_key: Mapped[str] = mapped_column(String(64), unique=True)
    source_id: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(Text)
    bin: Mapped[str | None] = mapped_column(String(12), index=True)
    address: Mapped[str | None] = mapped_column(Text)


class Supplier(Timestamped, Base):
    __tablename__ = "suppliers"
    source_key: Mapped[str] = mapped_column(String(64), unique=True)
    source_id: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(Text)
    bin: Mapped[str | None] = mapped_column(String(12), index=True)
    address: Mapped[str | None] = mapped_column(Text)


class Lot(Timestamped, Base):
    __tablename__ = "lots"
    source_id: Mapped[str] = mapped_column(String(255), unique=True)
    source_url: Mapped[str] = mapped_column(Text)
    lot_number: Mapped[str | None] = mapped_column(String(255), index=True)
    announcement_id: Mapped[UUID | None] = mapped_column(ForeignKey("announcements.id"))
    customer_id: Mapped[UUID | None] = mapped_column(ForeignKey("customers.id"))
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    budget: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    status: Mapped[str | None] = mapped_column(String(255), index=True)
    currency: Mapped[str | None] = mapped_column(String(16))
    publication_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    submission_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    procurement_method: Mapped[str | None] = mapped_column(Text)
    delivery_place: Mapped[str | None] = mapped_column(Text)
    work_place: Mapped[str | None] = mapped_column(Text)
    service_location: Mapped[str | None] = mapped_column(Text)
    execution_period: Mapped[str | None] = mapped_column(Text)
    warranty: Mapped[str | None] = mapped_column(Text)
    certificates: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    unit: Mapped[str | None] = mapped_column(Text)
    cpv: Mapped[str | None] = mapped_column(String(255), index=True)
    technical_characteristics: Mapped[str | None] = mapped_column(Text)
    installation_requirements: Mapped[str | None] = mapped_column(Text)
    payment_terms: Mapped[str | None] = mapped_column(Text)
    additional_conditions: Mapped[str | None] = mapped_column(Text)
    is_valid: Mapped[bool] = mapped_column(default=False, index=True)
    validation_reasons: Mapped[list] = mapped_column(JSON_TYPE, default=list)
    missing_data: Mapped[list] = mapped_column(JSON_TYPE, default=list)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON_TYPE)


class LotSupplier(Timestamped, Base):
    __tablename__ = "lot_suppliers"
    __table_args__ = (UniqueConstraint("lot_id", "supplier_id"),)
    lot_id: Mapped[UUID] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), index=True)
    supplier_id: Mapped[UUID] = mapped_column(ForeignKey("suppliers.id"))


class Document(Timestamped, Base):
    __tablename__ = "documents"
    lot_id: Mapped[UUID] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String(255))
    original_text: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    parse_status: Mapped[str] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    is_specification: Mapped[bool] = mapped_column(default=False)


class Attachment(Timestamped, Base):
    __tablename__ = "attachments"
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), unique=True
    )
    local_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Specification(Timestamped, Base):
    __tablename__ = "specifications"
    lot_id: Mapped[UUID] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), unique=True)
    original_text: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")


class RiskAssessment(Timestamped, Base):
    __tablename__ = "risk_assessments"
    lot_id: Mapped[UUID] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), unique=True)
    level: Mapped[str] = mapped_column(String(16), index=True)
    color: Mapped[str] = mapped_column(String(32))
    reasons: Mapped[list] = mapped_column(JSON_TYPE)


class CrawlRun(Timestamped, Base):
    __tablename__ = "crawl_runs"
    __table_args__ = (Index("ix_crawl_runs_resume", "filter_hash", "status", "created_at"),)
    filter_hash: Mapped[str] = mapped_column(String(64))
    filters: Mapped[dict] = mapped_column(JSON_TYPE)
    source: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="running")
    cursor: Mapped[str | None] = mapped_column(Text)
    statistics: Mapped[dict] = mapped_column(JSON_TYPE, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CrawlRunLot(Timestamped, Base):
    __tablename__ = "crawl_run_lots"
    __table_args__ = (UniqueConstraint("run_id", "lot_id"),)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="CASCADE"), index=True
    )
    lot_id: Mapped[UUID] = mapped_column(ForeignKey("lots.id", ondelete="CASCADE"), index=True)
    # Preserve the record observed in this run when later runs update the master lot.
    payload: Mapped[dict] = mapped_column(JSON_TYPE)
    is_valid: Mapped[bool] = mapped_column(default=False)


class ExportHistory(Timestamped, Base):
    __tablename__ = "export_history"
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("crawl_runs.id"), index=True)
    format: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="completed")


class DuplicateObservation(Timestamped, Base):
    """Invalid duplicate observations preserve the valid canonical lot unchanged."""

    __tablename__ = "duplicate_observations"
    __table_args__ = (UniqueConstraint("run_id", "source_id"),)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[str] = mapped_column(String(255))
    is_valid: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str] = mapped_column(String(64), default="DUPLICATE")
    data: Mapped[dict] = mapped_column(JSON_TYPE)
