"""Opt-in native PostgreSQL integration confined to a unique temporary schema.

Set TEST_DATABASE_URL=postgresql+asyncpg://... and run pytest -m postgres.
The account must be allowed to create and drop a schema in that database.
Existing schemas and their data are never modified.
"""

import os
import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.database.models import DuplicateObservation, Lot
from app.models.domain import CrawlStatistics, LotData, SearchFilters, ValidationResult
from app.repository import CrawlRepository

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set"
    ),
]


@pytest.mark.asyncio
async def test_native_postgresql_migrations_upsert_json_uuid_and_rollback(monkeypatch):
    url = make_url(os.environ["TEST_DATABASE_URL"])
    if url.get_backend_name() != "postgresql":
        pytest.fail("TEST_DATABASE_URL must point to PostgreSQL")
    url = url.set(drivername="postgresql+asyncpg")
    schema = f"procurement_test_{uuid4().hex}"
    assert re.fullmatch(r"procurement_test_[a-f0-9]{32}", schema)
    admin = create_async_engine(url, pool_pre_ping=True)
    isolated = None
    created = False
    try:
        async with admin.begin() as connection:
            await connection.execute(CreateSchema(schema))
        created = True
        isolated = create_async_engine(
            url, pool_pre_ping=True, connect_args={"server_settings": {"search_path": schema}}
        )

        def migrate(connection):
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        async with isolated.begin() as connection:
            await connection.run_sync(migrate)
            assert await connection.scalar(text("SELECT current_schema()")) == schema
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0002_duplicate_observations"
            types = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name, data_type FROM information_schema.columns "
                            "WHERE table_schema = :schema AND table_name = 'lots' "
                            "AND column_name IN ('id', 'payload')"
                        ),
                        {"schema": schema},
                    )
                ).all()
            )
            assert types == {"id": "uuid", "payload": "jsonb"}

        sessions = async_sessionmaker(isolated, expire_on_commit=False)
        repository = CrawlRepository(sessions)
        filters = SearchFilters(keyword="isolated PostgreSQL unit test")
        run = await repository.start_run(filters, source="postgres-unit-test")
        now = datetime.now(UTC)
        lot = LotData(
            source_id="postgres-unit-only",
            source_url="https://goszakup.gov.kz/unit-postgres",
            title="Constructed PostgreSQL unit record",
            collected_at=now,
            specification_original="Original unit-test text: Қазақша Русский",
            validation=ValidationResult(valid=True, verified_at=now),
            raw={"nested": {"unicode": "Павлодар", "values": [1, None, True]}},
        )
        await repository.save_batch(run.id, [lot], "page-2", CrawlStatistics(saved=1))
        async with sessions() as session:
            persisted = await session.scalar(select(Lot))
            assert isinstance(persisted.id, UUID)
            original_id = persisted.id
            assert persisted.payload["raw"] == lot.raw
        updated = lot.model_copy(update={"title": "Updated PostgreSQL unit record"}, deep=True)
        await repository.save_batch(
            run.id, [updated], "page-3", CrawlStatistics(saved=1, duplicates=1), [lot.source_id]
        )
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Lot)) == 1
            assert (await session.scalar(select(Lot))).id == original_id
            duplicate = await session.scalar(select(DuplicateObservation))
            assert duplicate.reason == "DUPLICATE"
            assert not duplicate.is_valid

        def fail_after_upsert(*args, **kwargs):
            raise RuntimeError("Injected failure after PostgreSQL lot upsert")

        with monkeypatch.context() as patch:
            patch.setattr(repository, "_documents", fail_after_upsert)
            rejected = lot.model_copy(update={"source_id": "must-rollback"}, deep=True)
            with pytest.raises(RuntimeError, match="Injected failure"):
                await repository.save_batch(
                    run.id, [rejected], "must-not-advance", CrawlStatistics(saved=2)
                )
        assert (await repository.get_run(run.id)).cursor == "page-3"
        assert [item async for item in repository.stream_lots(run.id)] == [updated]
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Lot)) == 1
        await repository.finish_run(run.id, "paused")
        resumed = await repository.start_run(filters, resume=True, source="postgres-unit-test")
        assert resumed.id == run.id
        assert resumed.cursor == "page-3"
    finally:
        if isolated is not None:
            await isolated.dispose()
        try:
            if created:
                # This exact random schema is the only deletion target.
                assert re.fullmatch(r"procurement_test_[a-f0-9]{32}", schema)
                async with admin.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))
        finally:
            await admin.dispose()
