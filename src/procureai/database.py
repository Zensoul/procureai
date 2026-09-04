
# ==============================================================================
# PROCUREAI — database.py
# Database engine, session management, and base model.
#
# WHY THIS FILE EXISTS:
# SQLAlchemy needs three things set up before any database operation can happen:
# 1. An ENGINE — the actual connection to PostgreSQL
# 2. A SESSION FACTORY — creates database sessions per request
# 3. A BASE MODEL — the parent class all ORM models inherit from
#
# This file sets up all three and exposes them to the rest of the application.
# Every router, service, and background job imports from here.
# ==============================================================================

from collections.abc import AsyncGenerator

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from procureai.config import get_settings

settings = get_settings()


# ==============================================================================
# DATABASE ENGINE
#
# The engine is the core connection to PostgreSQL. Think of it as the
# "gateway" — it manages the connection pool and handles the actual
# TCP connections to the database server.
#
# WHY THESE SPECIFIC SETTINGS:
#
# pool_size=5 — Keep 5 connections permanently open and ready.
# Each connection can serve one request at a time. 5 is enough for
# early stage with 30 customers. Increase when you see connection
# wait times in logs.
#
# max_overflow=10 — Allow 10 extra connections during traffic spikes.
# Total max = pool_size + max_overflow = 15 connections.
# Supabase free tier allows 60 connections — we're well within limits.
#
# pool_pre_ping=True — Before using a connection from the pool, send
# a lightweight "SELECT 1" to verify it's still alive. Without this,
# connections that died due to network issues or database restarts
# cause cryptic errors on the first request after the restart.
#
# echo=False in production — SQLAlchemy can log every SQL query it
# executes. Useful for debugging, but in production it floods your
# logs with noise. We control this via the environment setting.
# ==============================================================================

def _create_engine():
    """
    Create the async SQLAlchemy engine with appropriate settings
    for the current environment.

    Testing uses NullPool — no connection pooling — because pytest
    creates and destroys databases rapidly. Pooling causes "connection
    already closed" errors in tests.
    """
    # In testing, disable connection pooling entirely
    # NullPool creates a new connection for every operation and
    # closes it immediately — correct behaviour for test isolation
    if settings.is_testing:
        return create_async_engine(
            settings.database_url,
            echo=False,
            poolclass=NullPool,
        )

    # Production and development use connection pooling
    return create_async_engine(
        settings.database_url,

        # Log SQL queries in development only — never in production
        echo=settings.is_development,

        # Keep this many connections permanently open
        pool_size=settings.db_pool_size,

        # Allow this many extra connections during spikes
        max_overflow=settings.db_max_overflow,

        # Verify connections are alive before using them
        pool_pre_ping=True,

        # Close idle connections after 30 minutes
        # Prevents stale connections from accumulating
        pool_recycle=1800,
    )


# The single engine instance for the entire application
# Created once at module import time
engine = _create_engine()


# ==============================================================================
# SESSION FACTORY
#
# async_sessionmaker creates AsyncSession objects on demand.
# Each HTTP request gets its own session — its own isolated view
# of the database with its own transaction.
#
# WHY expire_on_commit=False:
# By default, SQLAlchemy "expires" all ORM objects after a commit —
# meaning accessing any attribute after commit triggers a new database
# query to refresh it. In an async context this causes "MissingGreenlet"
# errors because the refresh happens outside the async context.
# Setting expire_on_commit=False prevents this — objects keep their
# values after commit without re-querying.
# ==============================================================================

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,   # We manage transactions manually
    autoflush=False,    # We control when changes are sent to DB
)


# ==============================================================================
# BASE MODEL
#
# All SQLAlchemy ORM models (Customer, Purchase, ITCTracking, etc.)
# inherit from this Base class. It provides:
# - Table name inference from class name
# - Metadata registry (alembic uses this to detect schema changes)
# - Common column definitions (we'll add id, created_at, updated_at
#   to a mixin class in models/)
# ==============================================================================

class Base(DeclarativeBase):
    """
    Base class for all SQLAlchemy ORM models.

    Usage:
        from procureai.database import Base

        class Customer(Base):
            __tablename__ = "customers"
            id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    """
    pass


# ==============================================================================
# SESSION DEPENDENCY
#
# This is the FastAPI dependency that provides a database session
# to every route handler that needs one.
#
# HOW IT WORKS:
# FastAPI calls get_db() as a generator. The yield gives the session
# to the route handler. After the route handler finishes (success or
# error), execution resumes after yield — where we commit or rollback.
#
# WHY try/except/finally:
# - SUCCESS: route handler runs → yield → commit → session closed
# - ERROR: route handler raises exception → yield interrupted →
#          except catches it → rollback → session closed → exception
#          re-raised → FastAPI returns 500 error
#
# This guarantees the session is ALWAYS closed and the transaction
# is ALWAYS resolved — no leaked connections, no hanging transactions.
#
# USAGE IN ROUTES:
#   from procureai.database import get_db
#
#   @router.get("/customers")
#   async def list_customers(db: AsyncSession = Depends(get_db)):
#       result = await db.execute(select(Customer))
#       return result.scalars().all()
# ==============================================================================

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that provides a database session per request.

    Automatically commits on success, rolls back on error,
    and always closes the session when done.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            # If we reach here, the route handler succeeded
            await session.commit()
            logger.debug("Database transaction committed successfully")

        except SQLAlchemyError as e:
            # Database-specific error — rollback and log
            await session.rollback()
            logger.error(f"Database error, transaction rolled back: {e}")
            raise

        except Exception as e:
            # Any other error — still rollback to keep DB clean
            await session.rollback()
            logger.error(f"Unexpected error, transaction rolled back: {e}")
            raise


# ==============================================================================
# DATABASE HEALTH CHECK
#
# Used by the /health endpoint and Uptime Robot monitoring.
# Returns True if the database is reachable, False otherwise.
# Never raises an exception — callers check the return value.
# ==============================================================================

async def check_database_health() -> bool:
    """
    Verify the database is reachable and responding.
    Used by health check endpoint and startup validation.

    Returns:
        True if database is healthy, False otherwise.
    """
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(__import__('sqlalchemy').text("SELECT 1"))
            logger.debug("Database health check passed")
            return True
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return False


# ==============================================================================
# STARTUP & SHUTDOWN
#
# Called by FastAPI's lifespan context manager in main.py.
# connect_db() validates the database is reachable at startup —
# failing fast is better than discovering problems on the first request.
# disconnect_db() cleanly closes all connections on shutdown.
# ==============================================================================

async def connect_db() -> None:
    """
    Called at application startup.
    Validates database connectivity before accepting requests.
    Raises RuntimeError if database is not reachable.
    """
    logger.info("Connecting to database...")
    healthy = await check_database_health()
    if not healthy:
        raise RuntimeError(
            "Cannot connect to database. "
            "Check DATABASE_URL in your environment variables."
        )
    logger.info("Database connection established successfully")


async def disconnect_db() -> None:
    """
    Called at application shutdown.
    Closes all connections in the pool gracefully.
    Prevents 'connection was already closed' errors in logs.
    """
    logger.info("Closing database connections...")
    await engine.dispose()
    logger.info("Database connections closed")