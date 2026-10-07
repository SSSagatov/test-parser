"""Orchestration integration tests use constructed records, never exported live data."""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import Settings
from app.crawler.engine import CrawlEngine, canonical_id, exportable_lots
from app.crawler.http import OfficialPageUnavailable, SourceChanged
from app.crawler.pagination.checkpoint import Checkpoint
from app.database import Database
from app.database.models import CrawlRun, DuplicateObservation, ExportHistory
from app.export import Exporter
from app.models.domain import CrawlStatistics, ListingPage, LotData, SearchFilters
from app.repository import CrawlRepository


def unit_lot(identity: str, **changes) -> LotData:
    now = datetime.now(UTC)
    values = dict(
        source_id=identity,
        lot_number=f"UNIT-{identity}",
        announcement_number="UNIT-ANN",
        source_url=f"https://goszakup.gov.kz/ru/lot/view/{identity}",
        title="Unit test paper",
        specification_original="Бумага стандартная. Unit test specification.",
        status="active",
        official_page_available=True,
        submission_start=now - timedelta(days=1),
        submission_deadline=now + timedelta(days=10),
        delivery_place="г. Павлодар, test location",
        location_source_url=f"https://goszakup.gov.kz/ru/lot/view/{identity}",
        collected_at=now,
    )
    values.update(changes)
    return LotData(**values)


class MemorySource:
    """Deterministic test adapter, deliberately independent of real HTTP sources."""

    name = "unit-test-source"

    def __init__(self, pages, fail_once=(), enrich_changes=None):
        self.pages = pages
        self.fail_once = set(fail_once)
        self.enrich_changes = enrich_changes or {}
        self.visited = []
        self.enriched = Counter()

    async def fetch_page(self, filters, cursor=None):
        self.visited.append(cursor)
        lots, next_cursor = self.pages[cursor]
        return ListingPage(
            lots=[lot.model_copy(deep=True) for lot in lots],
            next_cursor=next_cursor,
            source_url="https://goszakup.gov.kz/unit-test-listing",
        )

    async def enrich(self, lot):
        self.enriched[lot.source_id] += 1
        if lot.source_id in self.fail_once:
            self.fail_once.remove(lot.source_id)
            raise RuntimeError("Simulated detail unavailable")
        result = lot.model_copy(deep=True)
        for field, value in self.enrich_changes.get(lot.source_id, {}).items():
            setattr(result, field, value)
        return result


class NoDocuments:
    async def process_many(self, documents, transport):
        assert not documents
        return []


@pytest_asyncio.fixture
async def context(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'crawler.sqlite'}",
        concurrency=2,
        database_batch_size=2,
        _env_file=None,
    )
    database = Database(settings)
    await database.create_schema()
    repository = CrawlRepository(database.session_factory)
    try:
        yield settings, database, repository
    finally:
        await database.dispose()


def engine(context, source):
    settings, _database, repository = context
    return CrawlEngine(settings, source, repository, None, NoDocuments())


async def latest_run(database):
    async with database.session_factory() as session:
        return await session.scalar(select(CrawlRun).order_by(CrawlRun.created_at.desc()))


@pytest.mark.asyncio
async def test_visits_all_pages_and_keeps_invalid_records(context):
    _, _, repo = context
    source = MemorySource(
        {
            None: ([unit_lot("1"), unit_lot("2", status="cancelled")], "second"),
            "second": ([unit_lot("3")], "last"),
            "last": ([unit_lot("4")], None),
        }
    )
    run_id, stats = await engine(context, source).run(SearchFilters())
    assert source.visited == [None, "second", "last"]
    assert stats.pages == 3
    assert stats.processed == stats.saved == 4
    assert stats.invalid == 1
    assert (await repo.get_run(run_id)).status == "completed"
    lots = [lot async for lot in repo.stream_lots(run_id)]
    assert len(lots) == 4
    invalid = next(lot for lot in lots if lot.lot_number == "UNIT-2")
    assert "STATUS_NOT_ACTIVE" in invalid.validation.reasons
    assert len([lot async for lot in exportable_lots(repo, run_id, valid_only=True)]) == 3


@pytest.mark.asyncio
async def test_limit_resume_replays_saved_page_after_listing_changes(context):
    _, _, repo = context
    source = MemorySource(
        {
            None: ([unit_lot("1"), unit_lot("2"), unit_lot("3")], "next"),
            "next": ([unit_lot("4")], None),
        }
    )
    run_id, stats = await engine(context, source).run(SearchFilters(), limit=1)
    state = await repo.get_run(run_id)
    assert state.status == "paused"
    checkpoint = Checkpoint.decode(state.cursor)
    assert [lot.source_id for lot in checkpoint.pending] == ["2", "3"]
    assert checkpoint.cursor == "next"
    source.pages[None] = ([unit_lot("new-unrelated")], "changed")
    resumed_id, final_stats = await engine(context, source).run(SearchFilters(), resume=True)
    assert resumed_id == run_id
    assert source.visited == [None, "next"]
    assert final_stats.saved == 4
    assert final_stats.pages == 2
    assert {lot.lot_number async for lot in repo.stream_lots(run_id)} == {
        "UNIT-1",
        "UNIT-2",
        "UNIT-3",
        "UNIT-4",
    }


@pytest.mark.asyncio
async def test_failed_detail_preserves_pending_page_and_retries_without_skipping(context):
    _, database, repo = context
    source = MemorySource(
        {
            None: ([unit_lot("1"), unit_lot("2")], "next"),
            "next": ([unit_lot("3")], None),
        },
        fail_once={"2"},
    )
    with pytest.raises(RuntimeError, match="resume will retry"):
        await engine(context, source).run(SearchFilters())
    failed = await latest_run(database)
    assert failed.status == "failed"
    checkpoint = Checkpoint.decode(failed.cursor)
    assert [lot.source_id for lot in checkpoint.pending] == ["1", "2"]
    assert not [lot async for lot in repo.stream_lots(failed.id)]
    run_id, stats = await engine(context, source).run(SearchFilters(), resume=True)
    assert run_id == failed.id
    assert source.visited == [None, "next"]
    assert source.enriched == {"1": 2, "2": 2, "3": 1}
    assert stats.saved == 3
    assert stats.failed == 1
    assert len([lot async for lot in repo.stream_lots(run_id)]) == 3


@pytest.mark.asyncio
async def test_database_failure_does_not_advance_pending_snapshot(context, monkeypatch):
    _, database, repo = context
    source = MemorySource({None: ([unit_lot("1"), unit_lot("2")], None)})
    original_save = repo.save_batch
    should_fail = True

    async def interrupted_save(run_id, lots, next_cursor, statistics, duplicate_source_ids=()):
        nonlocal should_fail
        if lots and should_fail:
            should_fail = False
            raise RuntimeError("Simulated database commit failure")
        return await original_save(run_id, lots, next_cursor, statistics, duplicate_source_ids)

    monkeypatch.setattr(repo, "save_batch", interrupted_save)
    with pytest.raises(RuntimeError, match="database commit failure"):
        await engine(context, source).run(SearchFilters())
    failed = await latest_run(database)
    assert failed.status == "failed"
    assert [lot.source_id for lot in Checkpoint.decode(failed.cursor).pending] == ["1", "2"]
    assert failed.statistics["saved"] == 0
    run_id, stats = await engine(context, source).run(SearchFilters(), resume=True)
    assert run_id == failed.id
    assert stats.saved == 2
    assert len([lot async for lot in repo.stream_lots(run_id)]) == 2


@pytest.mark.asyncio
async def test_invalid_persisted_checkpoint_marks_resumed_run_failed(context):
    _, _, repo = context
    source = MemorySource({})
    run = await repo.start_run(SearchFilters(), source=source.name)
    await repo.save_batch(run.id, [], '{"version":2}', CrawlStatistics())
    await repo.finish_run(run.id, "paused")
    with pytest.raises(ValueError, match="Unsupported crawl checkpoint version"):
        await engine(context, source).run(SearchFilters(), resume=True)
    assert (await repo.get_run(run.id)).status == "failed"
    assert not source.visited


@pytest.mark.asyncio
async def test_cancellation_preserves_pending_page_for_resume(context):
    _, database, repo = context
    started = asyncio.Event()

    class BlockedSource(MemorySource):
        async def enrich(self, lot):
            started.set()
            await asyncio.Event().wait()

    pages = {None: ([unit_lot("1"), unit_lot("2")], None)}
    task = asyncio.create_task(engine(context, BlockedSource(pages)).run(SearchFilters()))
    await asyncio.wait_for(started.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    interrupted = await latest_run(database)
    assert interrupted.status == "interrupted"
    assert len(Checkpoint.decode(interrupted.cursor).pending) == 2
    source = MemorySource({})
    run_id, stats = await engine(context, source).run(SearchFilters(), resume=True)
    assert run_id == interrupted.id
    assert stats.saved == 2
    assert not source.visited
    assert len([lot async for lot in repo.stream_lots(run_id)]) == 2


@pytest.mark.asyncio
async def test_duplicates_within_and_across_pages_preserve_first_record(context):
    _, database, repo = context
    first = unit_lot("1", title="First official observation")
    duplicate = first.model_copy(update={"title": "Duplicate observation"}, deep=True)
    source = MemorySource(
        {
            None: ([first, duplicate, unit_lot("2")], "next"),
            "next": ([duplicate, unit_lot("3")], None),
        }
    )
    run_id, stats = await engine(context, source).run(SearchFilters())
    assert stats.processed == 5
    assert stats.saved == 3
    assert stats.duplicates == 2
    assert source.enriched["1"] == 1
    restored = {lot.lot_number: lot async for lot in repo.stream_lots(run_id)}
    assert restored["UNIT-1"].title == "First official observation"
    async with database.session_factory() as session:
        audit = await session.scalar(select(DuplicateObservation))
        assert audit is not None
        assert audit.run_id == run_id
        assert audit.source_id == "lot:UNIT-ANN:UNIT-1"
        assert not audit.is_valid
        assert audit.reason == "DUPLICATE"


@pytest.mark.asyncio
async def test_identity_revealed_by_details_is_deduplicated(context):
    _, _, repo = context
    source = MemorySource(
        {
            None: (
                [unit_lot("raw-a", lot_number=None), unit_lot("raw-b", lot_number=None)],
                "next",
            ),
            "next": ([unit_lot("raw-c", lot_number=None)], None),
        },
        enrich_changes={
            "raw-a": {"lot_number": "UNIT-SHARED", "title": "First observation"},
            "raw-b": {"lot_number": "UNIT-SHARED", "title": "Second observation"},
            "raw-c": {"lot_number": "UNIT-SHARED", "title": "Third observation"},
        },
    )
    run_id, stats = await engine(context, source).run(SearchFilters())
    lots = [lot async for lot in repo.stream_lots(run_id)]
    assert len(lots) == stats.saved == 1
    assert stats.duplicates == 2
    assert lots[0].title == "First observation"


@pytest.mark.asyncio
async def test_same_lot_number_in_distinct_announcement_revisions_is_preserved(context):
    _, _, repo = context
    source = MemorySource(
        {
            None: (
                [
                    unit_lot(
                        "revision-a", lot_number="UNIT-SHARED", announcement_number="UNIT-ANN-1"
                    ),
                    unit_lot(
                        "revision-b", lot_number="UNIT-SHARED", announcement_number="UNIT-ANN-2"
                    ),
                ],
                None,
            )
        }
    )
    run_id, stats = await engine(context, source).run(SearchFilters())
    assert stats.saved == 2
    assert stats.duplicates == 0
    assert {lot.source_id async for lot in repo.stream_lots(run_id)} == {
        "lot:UNIT-ANN-1:UNIT-SHARED",
        "lot:UNIT-ANN-2:UNIT-SHARED",
    }


@pytest.mark.asyncio
async def test_missing_announcement_does_not_collapse_distinct_raw_identities(context):
    _, _, repo = context
    first = unit_lot("raw-a", lot_number="UNIT-SHARED", announcement_number=None)
    second = unit_lot("raw-b", lot_number="UNIT-SHARED", announcement_number=None)
    source = MemorySource({None: ([first, second], None)})
    run_id, stats = await engine(context, source).run(SearchFilters())
    assert stats.saved == 2
    assert stats.duplicates == 0
    assert {lot.source_id async for lot in repo.stream_lots(run_id)} == {
        "unit-test-source:raw-a",
        "unit-test-source:raw-b",
    }
    assert canonical_id(first, "another-source") == "another-source:raw-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", 404, 410])
async def test_unavailable_detail_is_stored_invalid_while_crawl_continues(context, failure):
    _, _, repo = context

    class UnavailableDetailSource(MemorySource):
        async def enrich(self, lot):
            if lot.source_id == "missing":
                if failure == "unavailable":
                    raise OfficialPageUnavailable("Official detail no longer exists")
                response = httpx.Response(failure, request=httpx.Request("GET", lot.source_url))
                response.raise_for_status()
            return await super().enrich(lot)

    source = UnavailableDetailSource(
        {
            None: ([unit_lot("missing")], "next"),
            "next": ([unit_lot("available")], None),
        }
    )
    run_id, stats = await engine(context, source).run(SearchFilters())
    assert stats.saved == 2
    assert stats.invalid == 1
    assert stats.failed == 0
    assert source.visited == [None, "next"]
    assert (await repo.get_run(run_id)).status == "completed"
    lots = {lot.lot_number: lot async for lot in repo.stream_lots(run_id)}
    missing = lots["UNIT-missing"]
    assert not missing.official_page_available
    assert not missing.validation.valid
    assert "OFFICIAL_PAGE_UNAVAILABLE" in missing.validation.reasons
    assert missing.raw["_detail_error"] == "OFFICIAL_PAGE_UNAVAILABLE"
    assert lots["UNIT-available"].validation.valid


@pytest.mark.asyncio
async def test_listing_http_404_fails_run_instead_of_completing_empty(context):
    _, database, repo = context

    class MissingListingSource(MemorySource):
        async def fetch_page(self, filters, cursor=None):
            httpx.Response(
                404, request=httpx.Request("GET", "https://goszakup.gov.kz/unit-missing-list")
            ).raise_for_status()

    with pytest.raises(httpx.HTTPStatusError):
        await engine(context, MissingListingSource({})).run(SearchFilters())
    run = await latest_run(database)
    assert run.status == "failed"
    assert run.statistics["saved"] == 0
    assert not [lot async for lot in repo.stream_lots(run.id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["repeat", "stalled", "empty"])
async def test_invalid_pagination_fails_with_prior_checkpoint_intact(context, failure):
    _, database, repo = context
    source = MemorySource(
        {
            None: ([unit_lot("1")], "next"),
            "next": (
                [unit_lot("1")]
                if failure == "repeat"
                else []
                if failure == "empty"
                else [unit_lot("2")],
                "next" if failure == "stalled" else "third",
            ),
        }
    )
    with pytest.raises(SourceChanged):
        await engine(context, source).run(SearchFilters())
    run = await latest_run(database)
    assert run.status == "failed"
    assert Checkpoint.decode(run.cursor).cursor == "next"
    assert len([lot async for lot in repo.stream_lots(run.id)]) == 1


@pytest.mark.asyncio
async def test_filter_statistics_and_export_audit_keep_excluded_rows_in_database(context, tmp_path):
    _, database, repo = context
    source = MemorySource(
        {
            None: (
                [
                    unit_lot("1", title="Software unit test"),
                    unit_lot("2", title="Other unit test"),
                    unit_lot("3", title="Software invalid unit test", status="cancelled"),
                ],
                None,
            )
        }
    )
    run_id, stats = await engine(context, source).run(SearchFilters(keyword="software"))
    assert stats.saved == 3
    assert stats.filtered == 1
    assert stats.invalid == 1
    assert len([lot async for lot in repo.stream_lots(run_id)]) == 3
    assert len([lot async for lot in exportable_lots(repo, run_id)]) == 2
    path = await Exporter(tmp_path / "exports", repo).export(
        exportable_lots(repo, run_id, valid_only=True), "json", run_id
    )
    async with database.session_factory() as session:
        audit = await session.scalar(select(ExportHistory))
        assert audit.row_count == 1
        assert audit.run_id == run_id
        assert audit.path == str(path)


def test_checkpoint_preserves_nested_records_and_rejects_unknown_version():
    checkpoint = Checkpoint(cursor="opaque", pending=[unit_lot("1")], recent_page_hashes=["unit"])
    assert Checkpoint.decode(checkpoint.encode()) == checkpoint
    with pytest.raises(ValueError, match="Unsupported crawl checkpoint version"):
        Checkpoint.decode('{"version":2}')
