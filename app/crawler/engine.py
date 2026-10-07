"""Transactional crawl orchestration with bounded work and durable page snapshots."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

import httpx
from loguru import logger

from app.config import Settings
from app.crawler.documents import DocumentProcessor
from app.crawler.http import OfficialPageUnavailable, OfficialTransport, SourceChanged
from app.crawler.pagination.checkpoint import Checkpoint
from app.models.domain import CrawlStatistics, ListingPage, LotData, SearchFilters
from app.parser.specification import attach_specification
from app.repository import CrawlRepository
from app.risk import assess_risk
from app.validator import validate_lot
from app.validator.filters import matches


class Source(Protocol):
    name: str

    async def fetch_page(
        self, filters: SearchFilters, cursor: str | None = None
    ) -> ListingPage: ...

    async def enrich(self, lot: LotData) -> LotData: ...


def canonical_id(lot: LotData, source: str) -> str:
    # The same full lot number can occur in different announcement revisions.
    # Only merge across interfaces when both official numbers establish identity.
    if lot.lot_number and lot.announcement_number:
        return f"lot:{lot.announcement_number}:{lot.lot_number}"
    return f"{source}:{lot.source_id}"


class CrawlEngine:
    def __init__(
        self,
        settings: Settings,
        source: Source,
        repository: CrawlRepository,
        transport: OfficialTransport,
        processor: DocumentProcessor | None = None,
    ):
        self.settings, self.source, self.repository, self.transport = (
            settings,
            source,
            repository,
            transport,
        )
        self.processor = processor or DocumentProcessor(settings)
        self._semaphore = asyncio.Semaphore(settings.concurrency)

    async def _lot(self, listed: LotData, filters: SearchFilters) -> LotData:
        async with self._semaphore:
            try:
                lot = await self.source.enrich(listed)
            except (OfficialPageUnavailable, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {
                    404,
                    410,
                }:
                    raise
                lot = listed.model_copy(deep=True)
                lot.official_page_available = False
                lot.raw["_detail_error"] = "OFFICIAL_PAGE_UNAVAILABLE"
                logger.warning("official_page_unavailable lot={}", lot.lot_number)
            lot.documents = await self.processor.process_many(lot.documents, self.transport)
            lot = attach_specification(lot, self.settings.summary_characters)
            pavlodar = (filters.city or "").casefold() in {"pavlodar", "павлодар", "г. павлодар"}
            lot.validation = validate_lot(lot, self.settings, pavlodar=pavlodar)
            lot.risk = assess_risk(lot, self.settings)
            lot.raw["_matches_filters"] = matches(lot, filters, self.settings)
            if not lot.raw["_matches_filters"]:
                logger.info("skipped_lot filter_mismatch={}", lot.lot_number)
            lot.raw["_source_id"] = lot.source_id
            lot.raw["_source_adapter"] = self.source.name
            lot.source_id = canonical_id(lot, self.source.name)
            if not lot.validation.valid:
                logger.warning(
                    "validation_error lot={} reasons={}", lot.lot_number, lot.validation.reasons
                )
            return lot

    async def run(
        self, filters: SearchFilters, *, resume: bool = False, limit: int | None = None
    ) -> tuple[UUID, CrawlStatistics]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        state = await self.repository.start_run(filters, resume=resume, source=self.source.name)
        statistics = state.statistics
        checkpoint: Checkpoint | None = None
        invocation_count = 0
        try:
            checkpoint = Checkpoint.decode(state.cursor)
            while True:
                if not checkpoint.pending:
                    if checkpoint.exhausted:
                        await self.repository.finish_run(state.id)
                        break
                    page = await self.source.fetch_page(filters, checkpoint.cursor)
                    if not page.lots and page.next_cursor:
                        raise SourceChanged("An empty page advertises more results")
                    if page.next_cursor and page.next_cursor == checkpoint.cursor:
                        raise SourceChanged("Source pagination cursor did not advance")
                    fingerprint = hashlib.sha256(
                        "\n".join(lot.source_id for lot in page.lots).encode()
                    ).hexdigest()
                    if page.lots and fingerprint in checkpoint.recent_page_hashes:
                        raise SourceChanged("Source repeated a previous page; crawl is incomplete")
                    checkpoint.recent_page_hashes = [*checkpoint.recent_page_hashes, fingerprint][
                        -100:
                    ]
                    checkpoint.pending = page.lots
                    checkpoint.cursor = page.next_cursor
                    checkpoint.exhausted = page.next_cursor is None
                    statistics.pages += 1
                    # Store discovered page before work. Resume has the exact rows even
                    # if the upstream offset list changes while the process is stopped.
                    await self.repository.save_batch(state.id, [], checkpoint.encode(), statistics)
                    if not checkpoint.pending:
                        continue
                if limit is not None and invocation_count >= limit:
                    await self.repository.finish_run(state.id, "paused")
                    break
                size = min(
                    self.settings.database_batch_size,
                    self.settings.concurrency,
                    limit - invocation_count if limit is not None else self.settings.concurrency,
                )
                candidates = checkpoint.pending[:size]
                keys = [canonical_id(lot, self.source.name) for lot in candidates]
                existing = await self.repository.existing_source_ids(state.id, keys)
                seen: set[str] = set(existing)
                unique: list[LotData] = []
                duplicates = 0
                duplicate_ids: set[str] = set()
                for listed, key in zip(candidates, keys, strict=True):
                    if key in seen:
                        duplicates += 1
                        duplicate_ids.add(key)
                        logger.info("skipped_lot duplicate={}", key)
                    else:
                        seen.add(key)
                        unique.append(listed)
                results = await asyncio.gather(
                    *(self._lot(lot, filters) for lot in unique), return_exceptions=True
                )
                failures = [result for result in results if isinstance(result, BaseException)]
                if failures:
                    statistics.failed += len(failures)
                    # No cursor is advanced on error. Successful siblings are safely
                    # replayed, and content-addressed attachments are reused.
                    raise RuntimeError(
                        f"Batch failed; resume will retry its pending lots: {failures[0]}"
                    ) from failures[0]
                enriched = [result for result in results if isinstance(result, LotData)]
                enriched_seen = await self.repository.existing_source_ids(
                    state.id, [lot.source_id for lot in enriched]
                )
                lots = []
                for lot in enriched:
                    if lot.source_id in enriched_seen:
                        duplicates += 1
                        duplicate_ids.add(lot.source_id)
                        logger.info("skipped_lot duplicate={}", lot.source_id)
                    else:
                        enriched_seen.add(lot.source_id)
                        lots.append(lot)
                statistics.duplicates += duplicates
                statistics.processed += len(candidates)
                statistics.saved += len(lots)
                statistics.invalid += sum(not lot.validation.valid for lot in lots)
                statistics.filtered += sum(not lot.raw["_matches_filters"] for lot in lots)
                checkpoint.pending = checkpoint.pending[len(candidates) :]
                await self.repository.save_batch(
                    state.id,
                    lots,
                    checkpoint.encode(),
                    statistics,
                    duplicate_source_ids=sorted(duplicate_ids),
                )
                invocation_count += len(candidates)
                logger.info("statistics run={} {}", state.id, statistics.model_dump_json())
        except asyncio.CancelledError:
            await asyncio.shield(
                self.repository.finish_run(
                    state.id, "interrupted", "Process interrupted; checkpoint preserved"
                )
            )
            raise
        except Exception as exc:
            # Read the last committed state: the failed operation might have
            # mutated our in-memory cursor before its database transaction failed.
            try:
                durable = await self.repository.get_run(state.id)
                durable.statistics.failed += max(1, statistics.failed - durable.statistics.failed)
                await self.repository.save_batch(state.id, [], durable.cursor, durable.statistics)
            except Exception:
                logger.error(
                    "Could not save failure statistics; prior checkpoint remains authoritative"
                )
            await self.repository.finish_run(state.id, "failed", str(exc))
            raise
        return state.id, statistics


async def exportable_lots(
    repository: CrawlRepository,
    run_id: UUID | None = None,
    valid_only: bool = False,
    include_filtered: bool = False,
) -> AsyncIterator[LotData]:
    async for lot in repository.stream_lots(run_id=run_id, valid_only=valid_only):
        if include_filtered or lot.raw.get("_matches_filters", True):
            yield lot
