"""Async database engine / session factory and schema bootstrap.

Uses SQLModel on top of SQLAlchemy's async engine (aiosqlite driver) so DB
access from FastAPI async routes never blocks the event loop.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

logger = logging.getLogger("db")

# check_same_thread is irrelevant for aiosqlite; future=True enables 2.0 style.
# aiosqlite accepts check_same_thread via connect_args; timeout raises the
# busy-wait window so a long-running writer (the background image worker) does
# not immediately throw "database is locked" against short API reads.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args={"timeout": 30, "check_same_thread": False},
)

async_session_maker = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def init_db() -> None:
    """Create all tables if they do not yet exist (idempotent)."""
    # Import models so they register on SQLModel.metadata before create_all.
    from app import models  # noqa: F401

    # Log the resolved DB path so it is obvious whether it lives inside the
    # Docker-mounted volume (/app/data) or leaked into an ephemeral layer.
    logger.info("SQLite database at: %s", settings.resolved_db_path)

    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: models.SQLModel.metadata.create_all(sync_conn))
        # WAL journal mode lets readers proceed without blocking on the
        # background image-generation writer, eliminating the intermittent
        # "database is locked" 500s on GET endpoints. busy_timeout makes a
        # contended writer wait/retry instead of failing immediately.
        await conn.run_sync(
            lambda sync_conn: (
                sync_conn.execute(text("PRAGMA journal_mode=WAL")),
                sync_conn.execute(text("PRAGMA busy_timeout=30000")),
                sync_conn.execute(text("PRAGMA synchronous=NORMAL")),
            )
        )
    await _migrate()


async def _migrate() -> None:
    """Add columns introduced after the initial release (idempotent).

    SQLModel's ``create_all`` only creates missing *tables*, never alters
    existing ones, so installs that were created before the ``model`` /
    ``cost_usd`` columns existed would otherwise break when the worker reads
    them. This safely adds the columns if absent.
    """
    # (table, column, sqlite type) tuples to add when missing.
    _ADD_COLUMNS = [
        ("generation_tasks", "model", "VARCHAR"),
        ("generation_tasks", "env", "VARCHAR"),
        ("generation_tasks", "cost_usd", "REAL"),
        ("generation_tasks", "full_prompt", "TEXT"),
        ("generation_tasks", "is_white_bg", "INTEGER"),
        ("generation_tasks", "mode", "VARCHAR"),
        ("task_items", "cost_usd", "REAL"),
        ("task_items", "original_paths", "TEXT"),
        ("users", "total_cost_usd", "REAL"),
    ]
    # Rows that predate a migration keep NULL in the new numeric columns.
    # ``UserRead``/``TaskSummary`` etc. require ``float`` (not None), so a NULL
    # would raise ValidationError and 500 the /me and task endpoints. Backfill
    # NULLs to 0.0 so legacy rows serialize cleanly. Idempotent & harmless.
    _BACKFILL_ZERO = [
        ("generation_tasks", "cost_usd"),
        ("generation_tasks", "is_white_bg"),
        ("task_items", "cost_usd"),
        ("users", "total_cost_usd"),
    ]
    async with engine.begin() as conn:
        def _sync(sync_conn) -> None:
            for table, column, col_type in _ADD_COLUMNS:
                cols = {c["name"] for c in inspect(sync_conn).get_columns(table)}
                if column not in cols:
                    sync_conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                    )
            for table, column in _BACKFILL_ZERO:
                sync_conn.execute(
                    text(f"UPDATE {table} SET {column} = 0.0 WHERE {column} IS NULL")
                )
        await conn.run_sync(_sync)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a managed async session."""
    async with async_session_maker() as session:
        yield session
