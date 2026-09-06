# ==============================================================================
# PROCUREAI — models/sales.py
# Sales Invoice model — tracks every invoice Ramesh raises to his customers.
#
# FRESHER EXPLANATION:
# The purchases table = money going OUT (Ramesh buys from suppliers)
# The sales table = money coming IN (Ramesh sells to customers)
#
# Together they give the complete GST picture:
# Output GST (from sales) - Input GST (from purchases) = GST liability
#
# This model is the foundation for:
# 1. GST liability calculation — accurate output GST
# 2. Payment tracking — who owes Ramesh money
# 3. Customer analytics — which customers buy most
# 4. Revenue reporting — monthly/annual revenue
# ==============================================================================

import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from procureai.database import Base
from procureai.models.base import BaseModel


class SalesInvoice(Base, BaseModel):
    """
    Represents one sales invoice raised by Ramesh to his customer.

    One SalesInvoice = one invoice sent to one buyer.
    Multiple line items can be on one invoice (stored as JSON in description).
    """

    __tablename__ = "sales_invoices"

    # Which MSME owner raised this invoice
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Which enrolled customer raised this invoice"
    )

    # ==========================================================================
    # BUYER INFORMATION
    # Who is Ramesh selling to?
    # ==========================================================================

    buyer_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Name of buyer — Sansera Engineering, Bajaj Auto etc"
    )

    buyer_gstin: Mapped[str | None] = mapped_column(
        String(15),
        nullable=True,
        index=True,
        comment="Buyer GSTIN — required for B2B GST filing"
    )

    buyer_phone: Mapped[str | None] = mapped_column(
        String(15),
        nullable=True,
        comment="Buyer phone for payment reminders"
    )

    buyer_email: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Buyer email for payment reminders"
    )

    buyer_address: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Buyer address for invoice"
    )

    # Is this buyer in the same state? (Karnataka to Karnataka)
    # Determines CGST+SGST vs IGST
    is_interstate: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="True if interstate supply — IGST applies"
    )

    # ==========================================================================
    # INVOICE DETAILS
    # ==========================================================================

    invoice_number: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Invoice number in Ramesh's numbering system"
    )

    invoice_date: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="Invoice date YYYY-MM-DD"
    )

    # Tax period this invoice belongs to
    tax_period: Mapped[str] = mapped_column(
        String(6),
        nullable=False,
        index=True,
        comment="GST tax period MMYYYY"
    )

    due_date: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="Payment due date YYYY-MM-DD"
    )

    # Purchase Order reference from buyer
    po_number: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Buyer's Purchase Order number"
    )

    # ==========================================================================
    # GOODS/SERVICES SUPPLIED
    # ==========================================================================

    # What was supplied — can be multiple items
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Description of goods/services supplied"
    )

    # Main product category — for GeM matching and analytics
    product_category: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        comment="Product category for analytics and GeM matching"
    )

    hsn_code: Mapped[str | None] = mapped_column(
        String(8),
        nullable=True,
        comment="HSN/SAC code for GST classification"
    )

    quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 3),
        nullable=True,
        comment="Quantity supplied"
    )

    unit: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="Unit of measurement"
    )

    # ==========================================================================
    # AMOUNTS — All in INR
    # ==========================================================================

    taxable_value: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        comment="Invoice value before GST"
    )

    gst_rate: Mapped[int] = mapped_column(
        nullable=False,
        default=18,
        comment="GST rate percentage: 0, 5, 12, 18, 28"
    )

    # For intrastate: CGST + SGST (split equally)
    cgst_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        comment="CGST amount (intrastate only)"
    )

    sgst_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        comment="SGST amount (intrastate only)"
    )

    # For interstate: IGST only
    igst_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        comment="IGST amount (interstate only)"
    )

    # Total GST = CGST + SGST + IGST
    total_gst: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        comment="Total GST charged on this invoice"
    )

    # Total invoice value
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        comment="Total invoice value including GST"
    )

    # ==========================================================================
    # PAYMENT TRACKING
    # ==========================================================================

    amount_received: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        comment="Amount received so far (partial payments supported)"
    )

    is_paid: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether invoice is fully paid"
    )

    paid_date: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="Date payment was received"
    )

    # Payment terms
    payment_terms_days: Mapped[int] = mapped_column(
        nullable=False,
        default=30,
        comment="Payment terms in days — 30, 45, 60, 90"
    )

    # ==========================================================================
    # GSTR-1 FILING STATUS
    # Ramesh must file GSTR-1 for all his sales invoices.
    # This tracks whether he has filed.
    # ===========================================================================

    gstr1_filed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether this invoice has been included in GSTR-1"
    )

    gstr1_filed_period: Mapped[str | None] = mapped_column(
        String(6),
        nullable=True,
        comment="Which GSTR-1 period this was filed in"
    )

    # ==========================================================================
    # REMINDER TRACKING
    # ==========================================================================

    reminders_sent: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        comment="Number of payment reminders sent to buyer"
    )

    last_reminder_date: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="Date of last payment reminder"
    )

    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Internal notes"
    )

    # ==========================================================================
    # DATABASE INDEXES
    # ==========================================================================

    __table_args__ = (
        Index(
            "ix_sales_customer_period",
            "customer_id",
            "tax_period",
        ),
        Index(
            "ix_sales_payment_status",
            "customer_id",
            "is_paid",
            "due_date",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"SalesInvoice("
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

    @property
    def days_until_due(self) -> int:
        """Days until payment is due (negative if overdue)."""
        from datetime import date
        try:
            due = date.fromisoformat(self.due_date)
            return (due - date.today()).days
        except Exception:
            return 0
