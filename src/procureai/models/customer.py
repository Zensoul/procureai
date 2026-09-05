# ==============================================================================
# PROCUREAI — models/customer.py
# The Customer model — represents an MSME owner enrolled in ProcureAI.
#
# FRESHER EXPLANATION:
# This file defines the "customers" table in PostgreSQL.
# Every class attribute you see below becomes a column in the table.
# Every instance of the Customer class represents one row — one MSME owner.
#
# When Ramesh from Bommasandra signs up, a new Customer row is created.
# When we send him an ITC alert, we look up his row to get his phone number.
# When we calculate his ITC recovery, we update his row with the result.
# ==============================================================================

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from procureai.database import Base
from procureai.models.base import BaseModel


class Customer(Base, BaseModel):
    """
    Represents one MSME owner enrolled in ProcureAI.

    FRESHER NOTE:
    This class inherits from two parents:
    1. Base — tells SQLAlchemy "this class is a database table"
    2. BaseModel — gives us id, created_at, updated_at for free
       (we defined these in models/base.py)

    So our Customer table automatically has:
    - id (from BaseModel)
    - created_at (from BaseModel)
    - updated_at (from BaseModel)
    - Plus all the columns we define below
    """

    # __tablename__ tells SQLAlchemy what to name the table in PostgreSQL.
    # Convention: lowercase, plural, underscores. "customers" not "Customer".
    __tablename__ = "customers"

    # ==========================================================================
    # IDENTITY COLUMNS
    # Who is this customer? How do we identify and contact them?
    # ==========================================================================

    # The owner's full name — "Ramesh Kumar"
    # Mapped[str] means this column holds text and cannot be NULL.
    # mapped_column() is how we configure the column.
    # String(100) means maximum 100 characters — enough for any name.
    owner_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Full name of the MSME owner"
    )

    # The business name — "Ramesh Auto Parts Pvt Ltd"
    business_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Registered business name"
    )

    # GSTIN — the 15-character GST Identification Number.
    # This is our PRIMARY way to identify a business in India.
    # Format: 29ABCDE1234F1Z5
    # - First 2 digits: state code (29 = Karnataka)
    # - Next 10: PAN number
    # - Next: entity number
    # - Next: Z (always)
    # - Last: checksum
    #
    # unique=True means no two customers can have the same GSTIN.
    # index=True creates a database index — makes lookups by GSTIN
    # extremely fast (milliseconds instead of seconds for large tables).
    gstin: Mapped[str] = mapped_column(
        String(15),
        unique=True,
        nullable=False,
        index=True,
        comment="15-character GST Identification Number — primary business identifier"
    )

    # Phone number — used for RCS/SMS/voice call delivery.
    # Stored as string, not integer, because:
    # 1. Phone numbers can start with 0 — integers drop leading zeros
    # 2. We sometimes store with country code: "+919845123456"
    # 3. We never do math on phone numbers
    phone: Mapped[str] = mapped_column(
        String(15),
        nullable=False,
        comment="Mobile number for RCS/SMS/voice alerts"
    )

    # Email — optional. Some owners have it, many don't.
    # Mapped[str | None] means this column CAN be NULL.
    # We never make email mandatory for MSME owners —
    # many run their entire business on a phone.
    email: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Email address — optional"
    )

    # ==========================================================================
    # CHANNEL PREFERENCE
    # HOW does this owner prefer to receive alerts?
    # Different owners engage differently — some read RCS messages,
    # some only answer voice calls. We track this per customer.
    # ==========================================================================

    # Which delivery channel to use first for this owner.
    # Values: "rcs" | "sms" | "voice" | "telegram"
    # Default is "rcs" — RCS rich cards are the best experience.
    # The system falls back automatically if RCS fails.
    preferred_channel: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="rcs",
        comment="Primary delivery channel: rcs, sms, voice, telegram"
    )

    # Telegram chat ID — only populated if owner uses Telegram (Option A).
    # None means they're on RCS/IVR (Option B).
    # Mapped[int | None] because Telegram chat IDs are large integers.
    telegram_chat_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Telegram chat ID — populated only for Option A customers"
    )

    # Which language to use for alerts and voice calls.
    # Values: "kn" (Kannada) | "hi" (Hindi) | "en" (English)
    # Default "kn" — most Bommasandra manufacturers speak Kannada.
    preferred_language: Mapped[str] = mapped_column(
        String(5),
        nullable=False,
        default="kn",
        comment="Alert language: kn=Kannada, hi=Hindi, en=English"
    )

    # ==========================================================================
    # BUSINESS PROFILE
    # Information about the business itself — used for GeM eligibility
    # checks, cluster price benchmarking, and feature customisation.
    # ==========================================================================

    # Which industrial cluster is this factory in?
    # Values: "bommasandra" | "peenya" | "electronic_city" | "whitefield"
    # This drives the cluster price benchmarking — Ramesh's MS rod price
    # is compared against other Bommasandra manufacturers, not Peenya ones.
    cluster: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
        comment="Industrial cluster location for price benchmarking"
    )

    # Udyam Registration Number — government MSME registration.
    # Format: UDYAM-KA-00-0000000
    # Required for GeM portal participation and government schemes.
    udyam_number: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        comment="Udyam Registration Number for GeM eligibility"
    )

    # Annual turnover band — used for GeM tender eligibility filtering.
    # We store a band, not exact figure (privacy + it changes annually).
    # Values: "under_1cr" | "1cr_to_5cr" | "5cr_to_10cr" | "above_10cr"
    turnover_band: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="Annual turnover band for scheme eligibility"
    )

    # What does this factory manufacture?
    # Free text — "auto components, precision machined parts"
    # Used for GeM tender keyword matching.
    product_categories: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Comma-separated product categories for GeM matching"
    )

    # ==========================================================================
    # CA (Chartered Accountant) RELATIONSHIP
    # The CA is our distribution partner — they introduce customers to us
    # and get 20% of fees. We track which CA brought which customer.
    # ==========================================================================

    # The CA's customer ID in our system.
    # This is a FOREIGN KEY — it references the id column of the "cas" table.
    # We'll create the CA model later. For now it stores a UUID or None.
    #
    # FRESHER NOTE: A foreign key creates a link between two tables.
    # Like how a purchase order has a "customer_id" that links back
    # to the customer who placed the order.
    ca_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        comment="ID of the CA who enrolled this customer — for revenue sharing"
    )

    # ==========================================================================
    # SUBSCRIPTION & STATUS
    # Is this customer active? When did they join? Are they in trial?
    # ==========================================================================

    # Is this customer currently active?
    # False = churned, suspended, or onboarding not complete.
    # We never delete customer records — we deactivate them.
    # WHY: Deleted records break audit trails and historical reports.
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether this customer is actively receiving alerts"
    )

    # Is this customer in the free trial period?
    # Trial customers get all features but we don't charge fees yet.
    # After first ITC recovery is confirmed, trial ends automatically.
    is_trial: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether customer is in trial — no fees charged during trial"
    )

    # When did the trial end (or will end)?
    # None means trial is still active.
    trial_ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the trial period ended — NULL if still in trial"
    )

    # When was the customer onboarded?
    # Different from created_at — a customer record might be created
    # during signup but onboarding (Tally access, GSTIN verification)
    # might complete days later.
    onboarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When onboarding was fully completed"
    )

    # ==========================================================================
    # ENCRYPTED SENSITIVE FIELDS
    # GSTIN portal credentials — stored encrypted using Fernet.
    # We need these to fetch GSTR-2B data on the customer's behalf.
    #
    # FRESHER NOTE ON SECURITY:
    # We NEVER store passwords in plain text. Ever.
    # These fields store encrypted bytes. The encryption key lives
    # in environment variables — never in the database.
    # Even if someone steals your database, they cannot read these values
    # without also stealing the encryption key from your server.
    # ==========================================================================

    # Encrypted GST portal username (usually the GSTIN itself)
    gstn_username_encrypted: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Fernet-encrypted GSTN portal username"
    )

    # Encrypted GST portal password
    gstn_password_encrypted: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Fernet-encrypted GSTN portal password"
    )

    # ==========================================================================
    # RELATIONSHIPS
    # SQLAlchemy can automatically load related records.
    # When you load a Customer, you can also access their purchases,
    # ITC records, etc. without writing extra queries.
    #
    # FRESHER NOTE:
    # relationship() doesn't create a column. It tells SQLAlchemy:
    # "When I access customer.purchases, go find all Purchase rows
    #  where purchase.customer_id = this customer's id"
    #
    # lazy="select" means: don't load purchases automatically.
    # Only load them when we explicitly access customer.purchases.
    # This prevents accidentally loading thousands of records.
    # ==========================================================================

    purchases: Mapped[list["Purchase"]] = relationship(
        "Purchase",
        back_populates="customer",
        lazy="select",
        cascade="all, delete-orphan",
    )

    itc_records: Mapped[list["ITCTracking"]] = relationship(
        "ITCTracking",
        back_populates="customer",
        lazy="select",
        cascade="all, delete-orphan",
    )

    # ==========================================================================
    # PYTHON METHODS
    # Regular Python methods on the model class.
    # These are NOT stored in the database — they're just convenient
    # helpers for working with Customer objects in your code.
    # ==========================================================================

    def __repr__(self) -> str:
        """
        Controls what you see when you print a Customer object.
        Without this, you'd see: <procureai.models.customer.Customer object at 0x...>
        With this, you see: Customer(gstin=29ABCDE1234F1Z5, name=Ramesh Kumar)
        Much more useful for debugging.
        """
        return f"Customer(gstin={self.gstin}, name={self.owner_name})"

    @property
    def display_name(self) -> str:
        """
        Returns the best name to show in alerts and messages.
        Uses business name if available, falls back to owner name.

        Example:
            customer.display_name
            → "Ramesh Auto Parts" (if business_name is set)
            → "Ramesh Kumar" (if only owner_name is set)
        """
        return self.business_name or self.owner_name

    @property
    def is_gstn_connected(self) -> bool:
        """
        Returns True if this customer has connected their GSTN credentials.
        Used to decide whether to use GSTN API or PDF upload fallback.
        """
        return (
            self.gstn_username_encrypted is not None
            and self.gstn_password_encrypted is not None
        )
