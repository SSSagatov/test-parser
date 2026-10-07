"""CLI for official ingestion, database migrations, export, and diagnostics."""

import asyncio
import json
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

import typer
from alembic import command
from alembic.config import Config
from rich.console import Console
from sqlalchemy import text

from app.config import Settings
from app.crawler.engine import CrawlEngine, exportable_lots
from app.crawler.http import OfficialTransport
from app.database import Database
from app.export import Exporter
from app.models.domain import SearchFilters
from app.repository import CrawlRepository
from app.utils.logging import configure_logging
from app.utils.time import parse_datetime

cli = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    help="Collect and audit REAL official Kazakhstan procurement records.",
)
console = Console()


def run(coroutine):
    try:
        return asyncio.run(coroutine)
    except KeyboardInterrupt:
        console.print(
            "[yellow]Interrupted. Committed checkpoints are available with --resume.[/yellow]"
        )
        raise typer.Exit(130) from None
    except Exception as exc:
        console.print(f"{type(exc).__name__}: {exc}", markup=False, style="red")
        raise typer.Exit(1) from None


@cli.command("init-db")
def init_db() -> None:
    """Apply versioned Alembic migrations to the configured database."""
    package_root = Path(__file__).resolve().parent
    packaged_config = package_root / "alembic.ini"
    config = Config(
        str(packaged_config if packaged_config.exists() else package_root.parent / "alembic.ini")
    )
    try:
        command.upgrade(config, "head")
    except Exception as exc:
        console.print(f"Database migration failed: {exc}", markup=False)
        raise typer.Exit(1) from None
    console.print("Database migrations applied.")


@cli.command()
def crawl(
    region: str | None = None,
    city: str | None = None,
    customer: str | None = None,
    supplier: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    method: str | None = None,
    budget_min: str | None = None,
    budget_max: str | None = None,
    keyword: str | None = None,
    cpv: str | None = None,
    announcement_number: str | None = None,
    lot_number: str | None = None,
    export: str | None = typer.Option(None, help="excel, csv, json, markdown"),
    resume: bool = False,
    limit: int | None = typer.Option(
        None, min=1, help="Maximum candidates this invocation; pending page is saved."
    ),
    source: str | None = typer.Option(None, help="auto, public, ows, browser"),
) -> None:
    """Crawl all pages, validate every lot, persist progress, optionally export."""
    settings = Settings()
    if source:
        settings = Settings(**{**settings.model_dump(), "source_mode": source})
    if export and export not in {"excel", "xlsx", "csv", "json", "markdown", "md"}:
        raise typer.BadParameter("export must be excel, csv, json, or markdown")
    if cpv:
        raise typer.BadParameter(
            "The official sources expose ENSTRU, not CPV. CPV cannot be verified or substituted."
        )
    try:
        filters = SearchFilters(
            region=region,
            city=city,
            customer=customer,
            supplier=supplier,
            date_from=parse_datetime(date_from, settings.timezone),
            date_to=parse_datetime(date_to, settings.timezone),
            status=status,
            method=method,
            budget_min=Decimal(budget_min) if budget_min is not None else None,
            budget_max=Decimal(budget_max) if budget_max is not None else None,
            keyword=keyword,
            cpv=cpv,
            announcement_number=announcement_number,
            lot_number=lot_number,
        )
    except (ValueError, InvalidOperation) as exc:
        raise typer.BadParameter(str(exc)) from exc
    configure_logging(settings)

    async def execute():
        from app.crawler.sources import select_source

        database, transport = Database(settings), OfficialTransport(settings)
        adapter = None
        try:
            # Fail before launching a browser if database is unavailable/unmigrated.
            async with database.engine.connect() as connection:
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            adapter = await select_source(settings, transport)
            repository = CrawlRepository(database.session_factory)
            identifier, statistics = await CrawlEngine(
                settings, adapter, repository, transport
            ).run(filters, resume=resume, limit=limit)
            console.print(
                f"Run: {identifier}\n{statistics.model_dump_json(indent=2)}", markup=False
            )
            if export:
                path = await Exporter(settings.export_folder, repository).export(
                    exportable_lots(repository, identifier), export, run_id=identifier
                )
                console.print(f"Export: {path.resolve()}", markup=False)
        finally:
            if adapter and hasattr(adapter, "close"):
                await adapter.close()
            await transport.close()
            await database.dispose()

    run(execute())


@cli.command("export")
def export_saved(
    format: str = typer.Option("excel", help="excel, csv, json, markdown"),
    run_id: UUID | None = None,
    valid_only: bool = False,
    include_filtered: bool = typer.Option(
        False, help="Include rejected filter matches for an audit export."
    ),
) -> None:
    """Stream committed records to an auditable export, including an interrupted run."""
    settings = Settings()
    configure_logging(settings)

    async def execute():
        database = Database(settings)
        try:
            repository = CrawlRepository(database.session_factory)
            if run_id:
                await repository.get_run(run_id)
            path = await Exporter(settings.export_folder, repository).export(
                exportable_lots(repository, run_id, valid_only, include_filtered),
                format,
                run_id=run_id,
            )
            console.print(str(path.resolve()), markup=False)
        finally:
            await database.dispose()

    run(execute())


@cli.command()
def discover() -> None:
    """Observe JSON endpoints from normal page rendering and check their robots policy."""
    settings = Settings()
    configure_logging(settings)

    async def execute():
        from app.crawler.browser import LegacyBrowserSource

        transport = OfficialTransport(settings)
        browser = LegacyBrowserSource(settings, transport)
        try:
            endpoints = await browser.discover()
            results = [
                {"url": url, "direct_requests_allowed": await transport.allowed(url)}
                for url in endpoints
            ]
            console.print(json.dumps(results, ensure_ascii=False, indent=2), markup=False)
        finally:
            await browser.close()
            await transport.close()

    run(execute())


@cli.command()
def doctor() -> None:
    """Report installed external parsers and configured access without printing secrets."""
    settings = Settings()
    report = {
        "source_mode": settings.source_mode,
        "ows_token_configured": bool(settings.ows_token),
        "respect_robots": settings.respect_robots,
        "tesseract": shutil.which(settings.tesseract_binary),
        "libreoffice": shutil.which(settings.libreoffice_binary),
        "rar_backend": shutil.which("unar") or shutil.which("unrar") or shutil.which("bsdtar"),
    }
    console.print(json.dumps(report, ensure_ascii=False, indent=2), markup=False)
