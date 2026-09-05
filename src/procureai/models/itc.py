# ==============================================================================
# PROCUREAI — models/itc.py
# ITC Tracking model — monthly ITC summary per customer.
#
# FRESHER EXPLANATION:
# The purchases table has individual invoices — hundreds of rows per customer.
# This table has monthly summaries — one row per customer per month.
#
# ANALOGY:
# purchases table = individual bank transactions
# itc_tracking table = monthly bank statement summary
#
# This table answers three questions every month:
# 1. How much ITC was Ramesh entitled to? (eligible_itc)
# 2. How much actually appeared in GSTR-2B? (matched_itc)
# 3. How much did we recover that was initially missing? (recovered_itc)
#
# Question 3 is how we calculate our fee.
# ==============================================================================

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from procureai.database import Base
from procureai.models.base import BaseModel


class ITCTracking(Base, BaseModel):
    """
    Monthly ITC summary for one customer.

    One row = one customer + one tax period (month).
    Example: Ramesh's ITC summary for August 2026.

    FRESHER NOTE:
    We have a UNIQUE constraint on (customer_id, tax_period).
    This means there can only be ONE record per customer per month.
    If August 2026 already has a record for Ramesh and we run
    the reconciliation again, we UPDATE the existing record —
    we don't create a duplicate.
    """

    __tablename__ = "itc_tracking"

    # ==========================================================================
    # RELATIONSHIP TO CUSTOMER
    # Every ITC record belongs to exactly one customer.
    # ==========================================================================

    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Which customer this ITC summary belongs to"
    )

    customer: Mapped["Customer"] = relationship(
        "Customer",
        back_populates="itc_records",
    )

    # ==========================================================================
    # TIME PERIOD
    # Which month does this summary cover?
    # ==========================================================================

    # Tax period in MMYYYY format — "082026" = August 2026
    # This matches the format used by GSTN API and our purchases table
    tax_period: Mapped[str] = mapped_column(
        String(6),
        nullable=False,
        comment="GST tax period: MMYYYY format e.g. 082026 for August 2026"
    )

    # Human readable period — "August 2026"
    # Stored separately so alerts don't need to parse MMYYYY format
    # "Your ITC summary for August 2026 is ready"
    period_display: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Human readable period name e.g. August 2026"
    )

    # ==========================================================================
    # ITC AMOUNTS
    # The core financial data — all amounts in Indian Rupees.
    #
    # FRESHER NOTE ON THE FLOW:
    # eligible_itc = total GST on all eligible purchases this month
    #                (calculated from purchases table)
    #
    # matched_itc  = GST that appeared in GSTR-2B
    #                (fetched from GSTN API)
    #
    # at_risk_itc  = eligible_itc - matched_itc
    #                (calculated: what's missing)
    #
    # recovered_itc = ITC that was at_risk last month but appeared
    #                 in GSTR-2B this month after we alerted Ramesh
    #                 (this is what we charge our fee on)
    # ==========================================================================

    # Total ITC Ramesh is entitled to claim this month
    # Sum of gst_amount from all eligible purchases in this tax period
    eligible_itc: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        comment="Total ITC eligible for this period from all purchases"
    )

    # ITC that has appeared in GSTR-2B
    # Fetched from GSTN API on the 16th of every month
    matched_itc: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        comment="ITC amount that appeared in GSTR-2B — safe to claim"
    )

    # ITC at risk = eligible - matched
    # This is what we alert Ramesh about
    # "₹59,000 of your ITC has not appeared in GSTR-2B"
    at_risk_itc: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        comment="ITC not yet in GSTR-2B — at risk of being lost"
    )

    # ITC we helped recover this month
    # = ITC that was at_risk in a previous period but now appears in GSTR-2B
    # This is the number our fee is based on
    recovered_itc: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        comment="ITC recovered this period that was previously at risk"
    )

    # How many supplier invoices are missing from GSTR-2B
    # Used in alerts: "2 suppliers have not filed GSTR-1"
    missing_invoice_count: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        comment="Number of invoices not yet in GSTR-2B"
    )

    # ==========================================================================
    # FEE CALCULATION
    # Our business model: 20% of recovered ITC.
    # We calculate and store the fee here for transparency and audit.
    #
    # FRESHER NOTE:
    # Storing calculated values (fee = recovered × 0.20) in the database
    # seems redundant — you could always recalculate it.
    # But we store it because:
    # 1. The fee rate might change in future (20% → 15%)
    # 2. We need an audit trail of exactly what was charged and when
    # 3. Disputes are resolved by looking at what was recorded at the time
    # ==========================================================================

    # Fee rate applied — stored as decimal: 0.20 = 20%
    # Stored per record because rate may change for different customers
    fee_rate: Mapped[Decimal] = mapped_column(
        Numeric(4, 2),
        nullable=False,
        default=Decimal("0.20"),
        comment="Fee rate applied: 0.20 = 20% of recovered ITC"
    )

    # Calculated fee = recovered_itc × fee_rate
    # This is what we invoice Ramesh
    fee_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        comment="Fee charged = recovered_itc × fee_rate"
    )

    # Has the fee been invoiced to the customer?
    # False = recovery happened, fee not yet invoiced
    # True = invoice sent to customer
    fee_invoiced: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        comment="Whether fee invoice has been sent to customer"
    )

    # Has the fee been paid by the customer?
    fee_paid: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        comment="Whether customer has paid the fee"
    )

    # ==========================================================================
    # RECONCILIATION RUN TRACKING
    # When did we run the reconciliation? What was the result?
    # This gives us an audit trail of every automated run.
    # ==========================================================================

    # Current status of this month's reconciliation
    # "pending"    = not yet run for this period
    # "running"    = currently being processed
    # "completed"  = reconciliation finished successfully
    # "failed"     = reconciliation failed — check error_message
    # "alerted"    = customer has been notified of results
    reconciliation_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        comment="Status: pending, running, completed, failed, alerted"
    )

    # When was the last reconciliation run for this period?
    # None = never run yet
    last_reconciled_at: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="ISO datetime of last reconciliation run"
    )

    # When was the customer alerted about this period's results?
    # None = alert not yet sent
    alert_sent_at: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="ISO datetime when alert was sent to customer"
    )

    # If reconciliation failed — what went wrong?
    # "GSTN API timeout after 30 seconds"
    # "Customer GSTN credentials invalid"
    # Stored here so we can debug and retry
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Error details if reconciliation_status is failed"
    )

    # ==========================================================================
    # GSTR-2B RAW DATA
    # Store the raw response from GSTN API for debugging.
    #
    # FRESHER NOTE:
    # Raw API responses are stored as Text (JSON string).
    # Why store raw data we've already processed?
    # Because GSTN sometimes has data quality issues.
    # When Ramesh says "your numbers are wrong", we can check
    # the exact response GSTN gave us on that date.
    # Without this, disputes are impossible to resolve.
    # ==========================================================================

    gstr2b_raw_response: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Raw GSTR-2B API response stored for audit and debugging"
    )

    # ==========================================================================
    # CA REVENUE SHARE
    # 20% of our fee goes to the CA who introduced this customer.
    # We track this separately for CA payment processing.
    # ==========================================================================

    ca_share_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        comment="CA revenue share amount = fee_amount × 0.20"
    )

    ca_share_paid: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        comment="Whether CA revenue share has been paid"
    )

    # ==========================================================================
    # DATABASE CONSTRAINTS AND INDEXES
    # ==========================================================================

    __table_args__ = (
        # UNIQUE constraint — one record per customer per month
        # If we try to insert a duplicate, PostgreSQL raises an error
        # Our code handles this with "upsert" — update if exists
        Index(
            "uq_itc_customer_period",
            "customer_id",
            "tax_period",
            unique=True,
        ),

        # For finding all pending reconciliations to run
        Index(
            "ix_itc_status",
            "reconciliation_status",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"ITCTracking("
            f"period={self.period_display}, "
            f"eligible=₹{self.eligible_itc}, "
            f"at_risk=₹{self.at_risk_itc}, "
            f"recovered=₹{self.recovered_itc}"
            f")"
        )

    @property
    def recovery_rate(self) -> Decimal:
        """
        What percentage of eligible ITC was successfully matched?
        100% = all ITC appeared in GSTR-2B — perfect.
        70% = 30% of ITC is still missing.

        Used in monthly reports to show improvement over time.
        """
        if self.eligible_itc == 0:
            return Decimal("100")
        return (self.matched_itc / self.eligible_itc * 100).quantize(
            Decimal("0.1")
        )

    @property
    def net_benefit_to_customer(self) -> Decimal:
        """
        What Ramesh actually gained after paying our fee.
        recovered_itc - fee_amount

        If we recovered ₹59,000 and charged ₹11,800 —
        Ramesh's net benefit = ₹47,200.
        This is the number we show in customer-facing reports.
        """
        return self.recovered_itc - self.fee_amount
