"""Synthetic unit records exercise persistence; they are never crawler output."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app.config.settings import Settings
from app.database import Database
from app.database.models import (
    Attachment,
    CrawlRunLot,
    Customer,
    Document,
    DuplicateObservation,
    Lot,
    LotSupplier,
    RiskAssessment,
    Specification,
    Supplier,
)
from app.models.domain import (
    CrawlStatistics,
    DocumentData,
    LotData,
    Party,
    RiskResult,
    SearchFilters,
    ValidationResult,
)
from app.repository import CrawlRepository


def sample_lot(source_id="unit-1", **changes):
    now = datetime.now(UTC)
    values = dict(
        source_id=source_id,
        source_url=f"https://goszakup.gov.kz/ru/lot/view/{source_id}",
        collected_at=now,
        title="Unit test only",
        budget=Decimal("100.12"),
        announcement_id="unit-announcement",
        customer=Party(name="Test customer", bin="123456789012"),
        suppliers=[Party(name="Test supplier", bin="123456789013")],
        validation=ValidationResult(valid=False, reasons=["UNIT_TEST"], verified_at=now),
        risk=RiskResult(level="HIGH", reasons=["Unit test only"], color="red"),
        specification_original="Original specification",
        documents=[
            DocumentData(
                url="https://goszakup.gov.kz/unit.pdf",
                parse_status="parsed",
                original_text="Original document",
                children=[DocumentData(url="archive:unit-child", parse_status="parsed")],
            )
        ],
    )
    values.update(changes)
    return LotData(**values)


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(
        Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite'}", _env_file=None)
    )
    await db.create_schema()
    try:
        yield db
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_upsert_normalized_records_checkpoint_and_resume(database):
    repo = CrawlRepository(database.session_factory)
    filters = SearchFilters(keyword="unit test")
    run = await repo.start_run(filters, source="official")
    lot = sample_lot()
    assert await repo.save_batch(run.id, [lot, lot], "page:2", CrawlStatistics(saved=1)) == 1
    changed = sample_lot(title="Updated unit test")
    await repo.save_batch(run.id, [changed], "page:3", CrawlStatistics(saved=1, pages=2))
    async with database.session_factory() as session:
        for model, expected in (
            (Lot, 1),
            (Customer, 1),
            (Supplier, 1),
            (LotSupplier, 1),
            (Specification, 1),
            (RiskAssessment, 1),
            (Document, 2),
            (Attachment, 2),
            (CrawlRunLot, 1),
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == expected
    await repo.finish_run(run.id, status="failed", error="interrupted")
    resumed = await repo.start_run(filters, resume=True, source="official")
    assert resumed.id == run.id
    assert resumed.cursor == "page:3"
    assert resumed.statistics.pages == 2
    restored = [item async for item in repo.stream_lots(run.id, batch_size=1)]
    assert restored == [changed]
    assert not [item async for item in repo.stream_lots(run.id, valid_only=True)]


@pytest.mark.asyncio
async def test_failed_batch_rolls_back_records_and_checkpoint(database, monkeypatch):
    repo = CrawlRepository(database.session_factory)
    run = await repo.start_run(SearchFilters())
    await repo.save_batch(run.id, [sample_lot()], "page:2", CrawlStatistics(saved=1))

    def explode(*args, **kwargs):
        raise RuntimeError("Simulated write failure")

    monkeypatch.setattr(repo, "_documents", explode)
    with pytest.raises(RuntimeError, match="write failure"):
        await repo.save_batch(run.id, [sample_lot("unit-2")], "page:3", CrawlStatistics(saved=2))
    state = await repo.get_run(run.id)
    assert state.cursor == "page:2"
    assert state.statistics.saved == 1
    assert len([item async for item in repo.stream_lots(run.id)]) == 1
    async with database.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Lot)) == 1


@pytest.mark.asyncio
async def test_run_snapshots_and_resume_matching(database):
    repo = CrawlRepository(database.session_factory)
    filters = SearchFilters(keyword="first")
    first = await repo.start_run(filters, source="portal-a")
    original = sample_lot()
    await repo.save_batch(first.id, [original], "offset", CrawlStatistics(saved=1))
    second = await repo.start_run(filters, source="portal-b")
    await repo.save_batch(
        second.id, [sample_lot(title="Newer official observation")], None, CrawlStatistics(saved=1)
    )
    assert [item async for item in repo.stream_lots(first.id)] == [original]
    assert (await repo.start_run(filters, resume=True, source="portal-a")).id == first.id
    assert await repo.existing_source_ids(first.id, [original.source_id, "missing"]) == {
        original.source_id
    }
    assert await repo.existing_source_ids(first.id, []) == set()
    with pytest.raises(LookupError, match="No unfinished"):
        await repo.start_run(SearchFilters(keyword="different"), resume=True)
    await repo.finish_run(first.id)
    with pytest.raises(LookupError, match="No unfinished"):
        await repo.start_run(filters, resume=True, source="portal-a")


@pytest.mark.asyncio
async def test_unvalidated_records_cannot_advance_checkpoint(database):
    repo = CrawlRepository(database.session_factory)
    run = await repo.start_run(SearchFilters())
    with pytest.raises(ValueError, match="validated"):
        await repo.save_batch(run.id, [sample_lot(validation=None)], "bad", CrawlStatistics())
    assert (await repo.get_run(run.id)).cursor is None


@pytest.mark.asyncio
async def test_duplicate_observations_are_invalid_and_idempotent(database):
    repo = CrawlRepository(database.session_factory)
    run = await repo.start_run(SearchFilters())
    lot = sample_lot()
    lot.validation.valid = True
    lot.validation.reasons = []
    await repo.save_batch(run.id, [lot], "next", CrawlStatistics(saved=1), [lot.source_id])
    await repo.record_duplicates(run.id, [lot.source_id, lot.source_id])
    async with database.session_factory() as session:
        observations = (await session.scalars(select(DuplicateObservation))).all()
        assert len(observations) == 1
        assert not observations[0].is_valid
        assert observations[0].reason == "DUPLICATE"
        assert observations[0].data["validation"]["reasons"] == ["DUPLICATE"]
    assert [item async for item in repo.stream_lots(run.id, valid_only=True)] == [lot]


def test_alembic_upgrade_and_downgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'migration.sqlite'}")
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "base")


def test_postgresql_migration_compiles_native_uuid_jsonb(monkeypatch):
    from io import StringIO

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused:unused@localhost/unused")
    output = StringIO()
    config = Config("alembic.ini", output_buffer=output)
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "JSONB" in sql
    assert "UUID" in sql
    assert "CREATE TABLE crawl_run_lots" in sql
