"""Async engine construction; schema migrations are a deployment responsibility."""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import Settings
from app.database.models import Base


class Database:
    def __init__(self, settings: Settings) -> None:
        url = settings.database_url.get_secret_value()
        options: dict = {"pool_pre_ping": True}
        if not url.startswith("sqlite"):
            options.update(
                pool_size=settings.database_pool_size, max_overflow=settings.database_pool_size
            )
        self.engine = create_async_engine(url, **options)
        if url.startswith("sqlite"):
            from sqlalchemy import event

            @event.listens_for(self.engine.sync_engine, "connect")
            def enable_foreign_keys(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        """Create a fresh test/development schema; production must run Alembic."""
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
