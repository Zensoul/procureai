# ==============================================================================
# PROCUREAI — models/base.py
# Shared column mixin for all database models.
#
# WHY THIS FILE EXISTS:
# Every table in ProcureAI needs the same three columns:
#   - id: unique identifier for every row
#   - created_at: when the record was created
#   - updated_at: when the record was last modified
#
# Without a mixin, we'd copy these three columns into every model.
# That's 3 columns × 6 models = 18 repeated definitions.
# One change (e.g. switching from integer to UUID primary keys)
# would require editing 6 files.
#
# The mixin defines them once. Every model inherits them automatically.
# ==============================================================================

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column


class TimestampMixin:
    """
    Adds created_at and updated_at columns to any model.

    created_at: Set once when the row is first inserted. Never changes.
    updated_at: Updated automatically every time the row is modified.

    Both store timezone-aware UTC datetimes.
    WHY UTC: Ramesh's factory is in Bangalore (IST = UTC+5:30).
    Your server runs in UTC. Always store UTC, convert to IST only
    when displaying to the user. Mixing timezones in the database
    causes bugs that are nearly impossible to debug.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        # Set to current UTC time when row is first inserted
        default=lambda: datetime.now(timezone.utc),
        # Never update this column after insert
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        # Set to current UTC time on insert
        default=lambda: datetime.now(timezone.utc),
        # Also update every time the row is modified
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class UUIDMixin:
    """
    Adds a UUID primary key to any model.

    WHY UUID instead of integer auto-increment:
    1. No information leakage — an integer ID reveals how many
       customers you have. Customer #4 means you have ~4 customers.
       A UUID reveals nothing.
    2. Safe to generate client-side — you can create the ID before
       inserting to the database. Useful for distributed systems.
    3. No collision risk when merging databases or importing data.

    WHY generate in Python (default=uuid.uuid4) not PostgreSQL (gen_random_uuid()):
    When you create a Customer object in Python before inserting it,
    the id is immediately available — no round-trip to the database
    needed to get the generated ID.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )


class BaseModel(UUIDMixin, TimestampMixin):
    """
    Combined mixin: UUID primary key + timestamps.

    Every ProcureAI model inherits from this.
    Provides: id, created_at, updated_at

    Usage:
        from procureai.models.base import BaseModel
        from procureai.database import Base

        class Customer(Base, BaseModel):
            __tablename__ = "customers"
            name: Mapped[str] = mapped_column()
            # id, created_at, updated_at are automatically included
    """
    pass
