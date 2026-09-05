# ==============================================================================
# PROCUREAI — alembic/env.py
# Alembic migration environment configuration.
#
# FRESHER EXPLANATION:
# This file tells Alembic three things:
# 1. Where is the database? (connection URL)
# 2. Which models exist? (our Base metadata)
# 3. How to run migrations? (online mode = connected to real DB)
# ==============================================================================

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from procureai.config import get_settings

# Import ALL models so Alembic can see them
# If a model is not imported here, Alembic won't create its table
from procureai.database import Base
from procureai.models import Customer, ITCTracking, Purchase

# Alembic config object — reads from alembic.ini
config = context.config

# Set up Python logging from alembic.ini config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# This is the metadata Alembic uses to detect schema changes
# It knows about all tables because we imported all models above
target_metadata = Base.metadata

settings = get_settings()


def run_migrations_offline() -> None:
    """
    Run migrations without a live database connection.
    Generates SQL scripts you can run manually.
    Not used in our setup but required by Alembic.
    """
    url = settings.database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """
    Run migrations with a live database connection.
    This is what we use — connects to PostgreSQL and runs migrations.
    """
    connectable = create_async_engine(settings.database_url)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())