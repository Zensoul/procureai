# ==============================================================================
# PROCUREAI — models/payment.py
# Payment Invoice model — tracks money Ramesh is owed by his customers.
#
# FRESHER EXPLANATION:
# The purchases table tracks money Ramesh SPENDS (supplier invoices).
# This table tracks money Ramesh is OWED (customer invoices).
#
# When Ramesh supplies 500 auto components to Sansera Engineering
# and raises an invoice for ₹1,20,000 — that goes here.
# If Sansera doesn't pay within 30 days — we send them a reminder.
# If they don't pay within 60 days — we escalate the reminder tone.
# ==============================================================================

import uuid
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from procureai.database import Base
from procureai.models.base import BaseModel


class PaymentInvoice(Base, BaseModel):
    """
    Represents an invoice Ramesh has raised to one of his customers.
    Tracks payment status and drives automated reminders.
    """

    __tablename__ = "payment_invoices"

    # Which MSME owner raised this invoice
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ==========================================================================
    # BUYER INFORMATION
    # Who owes Ramesh money?
    # ==========================================================================

    buyer_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Name of the customer who owes payment"
    )

    buyer_gstin: Mapped[str | None] = mapped_column(
        String(15),
        nullable=True,
        comment="Buyer GSTIN for GST compliance"
    )

    buyer_phone: Mapped[str | None] = mapped_column(
        String(15),
        nullable=True,
        comment="Buyer phone for automated payment reminders"
    )

    buyer_email: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Buyer email for payment reminders"
    )

    # ==========================================================================
    # INVOICE DETAILS
    # ==========================================================================

    invoice_number: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Invoice number raised by Ramesh"
    )

    invoice_date: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="Invoice date YYYY-MM-DD"
    )

    due_date: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="Payment due date YYYY-MM-DD"
    )

    # What was supplied
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Description of goods/services supplied"
    )

    # ==========================================================================
    # AMOUNTS
    # ==========================================================================

    taxable_value: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        comment="Invoice value before GST"
    )

    gst_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        comment="GST charged on this invoice"
    )

    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        comment="Total invoice amount including GST"
    )

    # Amount received so far (partial payments possible)
    amount_received: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        comment="Amount received so far"
    )

    # ==========================================================================
    # PAYMENT STATUS
    # ==========================================================================

    is_paid: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether invoice is fully paid"
    )

    paid_date: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="Date payment was received YYYY-MM-DD"
    )

    # How many reminder messages sent
    reminders_sent: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        comment="Number of payment reminders sent"
    )

    last_reminder_date: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="Date of last reminder sent"
    )

    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Internal notes about payment status"
    )

    def __repr__(self) -> str:
        return (
            f"PaymentInvoice("
            f"buyer={self.buyer_name}, "
            f"amount=₹{self.total_amount}, "
            f"paid={self.is_paid}"
            f")"
        )

    @property
    def outstanding_amount(self) -> Decimal:
        """Amount still unpaid."""
        return self.total_amount - self.amount_received

    @property
    def is_overdue(self) -> bool:
        """True if due date has passed and not fully paid."""
        from datetime import date
        if self.is_paid:
            return False
        try:
            due = date.fromisoformat(self.due_date)
            return date.today() > due
        except Exception:
            return False

    @property
    def days_overdue(self) -> int:
        """How many days past the due date."""
        from datetime import date
        if not self.is_overdue:
            return 0
        try:
            due = date.fromisoformat(self.due_date)
            return (date.today() - due).days
        except Exception:
            return 0
