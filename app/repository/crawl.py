"""Batch persistence with lot snapshots and checkpoint committed in one transaction."""

import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import (
    Announcement,
    Attachment,
    CrawlRun,
    CrawlRunLot,
    Customer,
    Document,
    DuplicateObservation,
    ExportHistory,
    Lot,
    LotSupplier,
    RiskAssessment,
    Specification,
    Supplier,
    utcnow,
)
from app.models.domain import CrawlStatistics, DocumentData, LotData, Party, SearchFilters


@dataclass(frozen=True)
class RunState:
    id: UUID
    cursor: str | None
    statistics: CrawlStatistics
    status: str
    created_at: datetime


def _state(run: CrawlRun) -> RunState:
    return RunState(
        run.id,
        run.cursor,
        CrawlStatistics.model_validate(run.statistics),
        run.status,
        run.created_at,
    )


def _party_key(party: Party) -> str:
    if party.bin:
        identity = f"bin:{party.bin}"
    elif party.source_id:
        identity = f"id:{party.source_id}"
    else:
        identity = "name-address:" + json.dumps([party.name, party.address], ensure_ascii=False)
    return hashlib.sha256(identity.encode()).hexdigest()


def _announcement_key(lot: LotData) -> str | None:
    if lot.announcement_id:
        return f"id:{lot.announcement_id}"
    if lot.announcement_number:
        return f"number:{lot.announcement_number}"
    if lot.announcement_url:
        return "url:" + hashlib.sha256(lot.announcement_url.encode()).hexdigest()
    return None


def _stamp(**values) -> dict:
    now = utcnow()
    return {"id": uuid4(), "created_at": now, "updated_at": now, **values}


class CrawlRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def start_run(
        self, filters: SearchFilters, resume: bool = False, source: str = ""
    ) -> RunState:
        payload = filters.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps([source, payload], sort_keys=True).encode()).hexdigest()
        async with self.session_factory.begin() as session:
            if resume:
                query = (
                    select(CrawlRun)
                    .where(
                        CrawlRun.filter_hash == digest,
                        CrawlRun.status.in_(["running", "paused", "failed", "interrupted"]),
                    )
                    .order_by(CrawlRun.created_at.desc())
                    .limit(1)
                )
                run = await session.scalar(query)
                if run is None:
                    raise LookupError(
                        "No unfinished crawl matches these filters and source; start without --resume"
                    )
                run.status, run.error, run.finished_at = "running", None, None
            else:
                run = CrawlRun(filters=payload, filter_hash=digest, source=source, statistics={})
                session.add(run)
            await session.flush()
            state = _state(run)
        logger.info("Crawl run {} started; resume={}", state.id, resume)
        return state

    async def get_run(self, run_id: UUID) -> RunState:
        async with self.session_factory() as session:
            run = await session.get(CrawlRun, run_id)
            if run is None:
                raise LookupError(f"Unknown crawl run: {run_id}")
            return _state(run)

    async def finish_run(
        self, run_id: UUID, status: str = "completed", error: str | None = None
    ) -> None:
        if status not in {"completed", "failed", "paused", "interrupted"}:
            raise ValueError(f"Unsupported terminal status: {status}")
        async with self.session_factory.begin() as session:
            result = await session.execute(
                update(CrawlRun)
                .where(CrawlRun.id == run_id)
                .values(status=status, error=error, finished_at=utcnow())
            )
            if not result.rowcount:
                raise LookupError(f"Unknown crawl run: {run_id}")
        logger.info("Crawl run {} finished: {}", run_id, status)

    @staticmethod
    async def _upsert(session: AsyncSession, model, rows: list[dict], unique: list[str]) -> dict:
        if not rows:
            return {}
        table = model.__table__
        factory = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
        # Bound statement sizes also fit SQLite's conservative parameter limits.
        chunk_size = max(1, min(100, 900 // len(rows[0])))
        identities = {}
        for offset in range(0, len(rows), chunk_size):
            statement = factory(table).values(rows[offset : offset + chunk_size])
            mutable = {
                name: getattr(statement.excluded, name)
                for name in rows[0]
                if name not in {"id", "created_at", *unique}
            }
            statement = statement.on_conflict_do_update(
                index_elements=unique, set_=mutable
            ).returning(table.c.id, *(table.c[name] for name in unique))
            result = await session.execute(statement)
            for row in result:
                key = row[1] if len(unique) == 1 else tuple(row[1:])
                identities[key] = row[0]
        return identities

    async def save_batch(
        self,
        run_id: UUID,
        lots: Sequence[LotData],
        next_cursor: str | None,
        statistics: CrawlStatistics,
        duplicate_source_ids: Sequence[str] = (),
    ) -> int:
        """Save a complete successful page (or a resumable offset) before advancing its cursor.

        Invalid validated records are retained with their reasons. Repeated source IDs
        update existing records and the run snapshot, rather than creating duplicates.
        """
        unique_lots = {lot.source_id: lot for lot in lots}
        if any(lot.validation is None for lot in unique_lots.values()):
            raise ValueError("Every lot must be validated before persistence")
        async with self.session_factory.begin() as session:
            # Serialize commits to this run, keeping record changes and cursor atomic.
            run = await session.scalar(
                select(CrawlRun).where(CrawlRun.id == run_id).with_for_update()
            )
            if run is None:
                raise LookupError(f"Unknown crawl run: {run_id}")
            if run.status != "running":
                raise ValueError(f"Cannot save to {run.status} run")
            parties: dict[type, dict[str, dict]] = {Customer: {}, Supplier: {}}
            announcements = {}
            for lot in unique_lots.values():
                for model, values in (
                    (Customer, [lot.customer] if lot.customer else []),
                    (Supplier, lot.suppliers),
                ):
                    for party in values:
                        key = _party_key(party)
                        parties[model][key] = _stamp(source_key=key, **party.model_dump())
                key = _announcement_key(lot)
                if key:
                    announcements[key] = _stamp(
                        source_key=key,
                        source_id=lot.announcement_id,
                        number=lot.announcement_number,
                        url=lot.announcement_url,
                    )
            customer_ids = await self._upsert(
                session, Customer, list(parties[Customer].values()), ["source_key"]
            )
            supplier_ids = await self._upsert(
                session, Supplier, list(parties[Supplier].values()), ["source_key"]
            )
            announcement_ids = await self._upsert(
                session, Announcement, list(announcements.values()), ["source_key"]
            )
            rows = []
            scalar_names = (
                "source_id",
                "source_url",
                "lot_number",
                "title",
                "description",
                "budget",
                "status",
                "currency",
                "publication_date",
                "submission_deadline",
                "procurement_method",
                "delivery_place",
                "work_place",
                "service_location",
                "execution_period",
                "warranty",
                "certificates",
                "quantity",
                "unit",
                "cpv",
                "technical_characteristics",
                "installation_requirements",
                "payment_terms",
                "additional_conditions",
                "collected_at",
            )
            for lot in unique_lots.values():
                values = {name: getattr(lot, name) for name in scalar_names}
                rows.append(
                    _stamp(
                        **values,
                        customer_id=customer_ids.get(_party_key(lot.customer))
                        if lot.customer
                        else None,
                        announcement_id=announcement_ids.get(_announcement_key(lot)),
                        is_valid=lot.validation.valid,
                        validation_reasons=lot.validation.reasons,
                        missing_data=lot.validation.missing_data,
                        payload=lot.model_dump(mode="json"),
                    )
                )
            lot_ids = await self._upsert(session, Lot, rows, ["source_id"])
            ids = list(lot_ids.values())
            if ids:
                for model in (LotSupplier, Specification, RiskAssessment):
                    await session.execute(delete(model).where(model.lot_id.in_(ids)))
                document_ids = select(Document.id).where(Document.lot_id.in_(ids))
                await session.execute(
                    delete(Attachment).where(Attachment.document_id.in_(document_ids))
                )
                await session.execute(delete(Document).where(Document.lot_id.in_(ids)))
            snapshots, links, specs, risks, docs, attachments = [], [], [], [], [], []
            for lot in unique_lots.values():
                lot_id = lot_ids[lot.source_id]
                snapshots.append(
                    _stamp(
                        run_id=run_id,
                        lot_id=lot_id,
                        payload=lot.model_dump(mode="json"),
                        is_valid=lot.validation.valid,
                    )
                )
                for supplier_key in {_party_key(party) for party in lot.suppliers}:
                    links.append(_stamp(lot_id=lot_id, supplier_id=supplier_ids[supplier_key]))
                specs.append(
                    _stamp(
                        lot_id=lot_id,
                        original_text=lot.specification_original,
                        summary=lot.specification_summary,
                    )
                )
                if lot.risk:
                    risks.append(
                        _stamp(
                            lot_id=lot_id,
                            level=lot.risk.level.value,
                            reasons=lot.risk.reasons,
                            color=lot.risk.color,
                        )
                    )
                self._documents(lot_id, lot.documents, docs, attachments)
            await self._upsert(session, CrawlRunLot, snapshots, ["run_id", "lot_id"])
            for model, data in (
                (LotSupplier, links),
                (Specification, specs),
                (RiskAssessment, risks),
                (Document, docs),
                (Attachment, attachments),
            ):
                if data:
                    await session.execute(model.__table__.insert(), data)
            await self._save_duplicate_observations(session, run_id, duplicate_source_ids)
            run.cursor = next_cursor
            run.statistics = statistics.model_dump(mode="json")
        logger.info("Committed {} lots and checkpoint for run {}", len(unique_lots), run_id)
        return len(unique_lots)

    @classmethod
    def _documents(
        cls,
        lot_id: UUID,
        documents: list[DocumentData],
        docs: list,
        attachments: list,
        parent_id: UUID | None = None,
    ) -> None:
        for document in documents:
            row = _stamp(
                lot_id=lot_id,
                parent_id=parent_id,
                url=document.url,
                name=document.name,
                media_type=document.media_type,
                original_text=document.original_text,
                summary=document.summary,
                parse_status=document.parse_status,
                error=document.error,
                is_specification=document.is_specification,
            )
            docs.append(row)
            attachments.append(
                _stamp(
                    document_id=row["id"],
                    local_path=str(document.local_path) if document.local_path else None,
                    sha256=document.sha256,
                    size_bytes=document.size_bytes,
                    downloaded_at=document.downloaded_at,
                )
            )
            cls._documents(lot_id, document.children, docs, attachments, row["id"])

    async def stream_lots(
        self, run_id: UUID | None = None, valid_only: bool = False, batch_size: int = 500
    ) -> AsyncIterator[LotData]:
        """Keyset pagination avoids retaining a 100,000-row result or a long transaction."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        model = CrawlRunLot if run_id else Lot
        last_id = None
        while True:
            query = select(model.id, model.payload).order_by(model.id).limit(batch_size)
            if run_id:
                query = query.where(model.run_id == run_id)
            if valid_only:
                query = query.where(model.is_valid.is_(True))
            if last_id is not None:
                query = query.where(model.id > last_id)
            async with self.session_factory() as session:
                rows = (await session.execute(query)).all()
            if not rows:
                return
            for row in rows:
                yield LotData.model_validate(row.payload)
            last_id = rows[-1].id

    async def existing_source_ids(self, run_id: UUID, source_ids: list[str]) -> set[str]:
        """Return canonical source identities already stored in this particular run."""
        identities = list(set(source_ids))
        found: set[str] = set()
        async with self.session_factory() as session:
            for offset in range(0, len(identities), 500):
                query = (
                    select(Lot.source_id)
                    .join(CrawlRunLot, CrawlRunLot.lot_id == Lot.id)
                    .where(
                        CrawlRunLot.run_id == run_id,
                        Lot.source_id.in_(identities[offset : offset + 500]),
                    )
                )
                found.update((await session.scalars(query)).all())
        return found

    async def _save_duplicate_observations(
        self, session: AsyncSession, run_id: UUID, source_ids: Sequence[str]
    ) -> None:
        rows = [
            _stamp(
                run_id=run_id,
                source_id=source_id,
                is_valid=False,
                reason="DUPLICATE",
                data={
                    "source_id": source_id,
                    "validation": {"valid": False, "reasons": ["DUPLICATE"]},
                },
            )
            for source_id in dict.fromkeys(source_ids)
        ]
        await self._upsert(session, DuplicateObservation, rows, ["run_id", "source_id"])

    async def record_duplicates(self, run_id: UUID, source_ids: Sequence[str]) -> None:
        """Idempotently record invalid observations without invalidating canonical lots.

        Crawlers should pass duplicate_source_ids to save_batch instead, so the
        observation and page checkpoint share the same database transaction.
        """
        async with self.session_factory.begin() as session:
            await self._save_duplicate_observations(session, run_id, source_ids)

    async def record_export(
        self, path: Path, format: str, row_count: int, sha256: str, run_id: UUID | None = None
    ) -> None:
        async with self.session_factory.begin() as session:
            session.add(
                ExportHistory(
                    run_id=run_id, format=format, path=str(path), row_count=row_count, sha256=sha256
                )
            )
