
# ==============================================================================
# PROCUREAI — models/purchase.py
# The Purchase model — every raw material invoice Ramesh receives.
#
# FRESHER EXPLANATION:
# Every time Ramesh buys something — MS rods, carbide inserts, coolant —
# the supplier gives him an invoice. That invoice is one row in this table.
#
# This table serves two features:
# 1. ITC Reconciliation — compare purchase GST against GSTR-2B
# 2. Price Benchmarking — compare purchase price against cluster average
#
# Think of this table as a digital copy of Ramesh's purchase register —
# the book where he records everything he buys.
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


class Purchase(Base, BaseModel):
    """
    Represents one supplier invoice received by an MSME customer.

    FRESHER NOTE:
    One Purchase row = one invoice from one supplier.
    If Ramesh buys MS rods AND carbide inserts on the same invoice,
    that is still ONE Purchase row — we store the total.
    If he gets two separate invoices from two suppliers on the same day,
    that is TWO Purchase rows.
    """

    __tablename__ = "purchases"

    # ==========================================================================
    # RELATIONSHIP TO CUSTOMER
    # Every purchase belongs to exactly one customer.
    # This is the FOREIGN KEY — the link between purchases and customers.
    #
    # FRESHER NOTE ON FOREIGN KEYS:
    # Imagine the customers table has Ramesh with id = "abc-123"
    # Every purchase Ramesh makes has customer_id = "abc-123"
    # This is how we know "this purchase belongs to Ramesh"
    # Without foreign keys, purchases would be floating records
    # with no owner — useless.
    # ==========================================================================

    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Which customer made this purchase"
    )

    # relationship() lets us access the full Customer object
    # from a Purchase: purchase.customer.owner_name
    # back_populates connects this to Customer.purchases
    customer: Mapped["Customer"] = relationship(
        "Customer",
        back_populates="purchases",
    )

    # ==========================================================================
    # SUPPLIER INFORMATION
    # Who sold this material to Ramesh?
    # We identify suppliers by their GSTIN — not by name.
    # Why? Because supplier names can be spelled differently
    # ("Sharma Metals", "sharma metals", "Sharma Metal") but
    # GSTIN is always exactly the same 15 characters.
    # ==========================================================================

    # Supplier's GSTIN — used to match against GSTR-2B data
    # GSTR-2B groups ITC by supplier GSTIN — so we need this
    # to know which supplier's ITC is at risk
    supplier_gstin: Mapped[str] = mapped_column(
        String(15),
        nullable=False,
        index=True,
        comment="Supplier's GSTIN — used for GSTR-2B matching"
    )

    # Supplier's business name — for display in alerts
    # "Sharma Metals has not filed GSTR-1 for invoice #INV-2847"
    supplier_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Supplier business name — for readable alerts"
    )

    # Supplier's phone number — for automated follow-up messages
    # "Your delivery of 500kg MS rod is due Thursday. Please confirm."
    supplier_phone: Mapped[str | None] = mapped_column(
        String(15),
        nullable=True,
        comment="Supplier phone for automated delivery follow-up"
    )

    # ==========================================================================
    # INVOICE DETAILS
    # The core information from the paper/PDF invoice.
    # ==========================================================================

    # Invoice number — exactly as printed on the invoice
    # "INV-2847" or "2024-25/KA/001" — supplier's own numbering
    # Used in ITC alerts: "Ask Sharma Metals to file invoice #INV-2847"
    invoice_number: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Invoice number as printed on the supplier's invoice"
    )

    # Invoice date — when the supplier issued the invoice
    # NOT when Ramesh received it or when he paid it
    # GSTN matches ITC based on invoice date and tax period
    invoice_date: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="Invoice date in YYYY-MM-DD format"
    )

    # Tax period — which GST filing month this invoice belongs to
    # Format: "082026" = August 2026
    # GSTR-2B is published monthly — we match by tax period
    tax_period: Mapped[str] = mapped_column(
        String(6),
        nullable=False,
        index=True,
        comment="GST tax period in MMYYYY format e.g. 082026"
    )

    # ==========================================================================
    # MATERIAL INFORMATION
    # What did Ramesh buy? This drives the price benchmarking feature.
    # We need to know the exact material and grade to compare prices
    # with other manufacturers buying the same thing.
    # ==========================================================================

    # Material name — what was purchased
    # "MS Rod" or "Carbide Insert" or "Cutting Tool"
    material_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        index=True,
        comment="Name of material purchased — used for price benchmarking"
    )

    # Material grade — the specification
    # "IS 2062 E250" for MS rod, "K10" for carbide inserts
    # Critical for accurate price comparison —
    # IS 2062 E250 and IS 2062 E350 are different grades with different prices
    material_grade: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Material grade/specification for accurate price comparison"
    )

    # HSN Code — Harmonized System of Nomenclature
    # A 6-8 digit code that classifies every product for GST purposes
    # MS rod = 7214, Carbide inserts = 8209
    # Used for GST rate verification and GSTR-2B matching
    hsn_code: Mapped[str | None] = mapped_column(
        String(8),
        nullable=True,
        comment="HSN code for GST classification and rate verification"
    )

    # ==========================================================================
    # QUANTITY AND PRICING
    # How much was bought and at what price?
    #
    # FRESHER NOTE ON Numeric vs Float:
    # We use Numeric(12, 3) NOT float for money and quantities.
    # Why? Because floats have rounding errors:
    #   0.1 + 0.2 = 0.30000000000000004  (float — WRONG)
    #   Decimal('0.1') + Decimal('0.2') = 0.3  (Numeric — CORRECT)
    # For financial calculations, rounding errors cost real money.
    # Numeric(12, 3) means: up to 12 digits total, 3 after decimal
    # This handles quantities like 1500.500 kg correctly.
    # ==========================================================================

    quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 3),
        nullable=False,
        comment="Quantity purchased"
    )

    # Unit of measurement — "kg", "nos", "mtr", "ltr"
    unit: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="Unit of measurement: kg, nos, mtr, ltr etc"
    )

    # Price per unit BEFORE GST
    # This is what we compare against the cluster average
    # for the price benchmarking feature
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        comment="Price per unit before GST — used for benchmarking"
    )

    # Total value BEFORE GST = quantity × unit_price
    # Stored separately for fast reporting — avoids recalculating
    taxable_value: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        comment="Total taxable value before GST = quantity × unit_price"
    )

    # GST rate applied — 5, 12, 18, or 28 percent
    # Stored as integer: 18 means 18%
    gst_rate: Mapped[int] = mapped_column(
        nullable=False,
        comment="GST rate percentage: 5, 12, 18, or 28"
    )

    # Total GST amount charged on this invoice
    # This is the amount Ramesh paid as GST — what he wants back as ITC
    gst_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        comment="Total GST charged — this is the ITC Ramesh can claim"
    )

    # Total invoice value = taxable_value + gst_amount
    total_value: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        comment="Total invoice value including GST"
    )

    # ==========================================================================
    # ITC TRACKING COLUMNS
    # These columns answer the core question of Feature 1:
    # "Has this purchase's GST appeared in GSTR-2B?"
    # ==========================================================================

    # Has this invoice appeared in GSTR-2B?
    # False = not yet — ITC at risk
    # True = yes — ITC safe to claim
    # Updated monthly when we fetch GSTR-2B data
    in_gstr2b: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether this invoice appears in GSTR-2B — False means ITC at risk"
    )

    # When did this invoice appear in GSTR-2B?
    # None = hasn't appeared yet
    # Used to calculate how long ITC was at risk before resolution
    gstr2b_matched_at: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="Date when invoice appeared in GSTR-2B — NULL if not yet matched"
    )

    # Is this invoice eligible for ITC at all?
    # Some purchases are NOT eligible for ITC:
    # - Personal expenses
    # - Motor vehicles (in most cases)
    # - Food and beverages
    # We track this to avoid alerting on non-eligible purchases
    itc_eligible: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether this purchase is eligible for ITC claim"
    )

    # ==========================================================================
    # DATA SOURCE TRACKING
    # How did this purchase record enter our system?
    # Different sources have different reliability levels.
    # ==========================================================================

    # Where did this purchase data come from?
    # "pdf_upload" — customer uploaded invoice PDF, we extracted data
    # "tally_export" — exported from Tally accounting software
    # "manual" — customer entered data manually through the bot
    # "gstn_api" — fetched directly from GSTN portal
    data_source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="manual",
        comment="How this record entered the system: pdf_upload, tally_export, manual, gstn_api"
    )

    # For PDF uploads — store the original filename for reference
    # "sharma_metals_invoice_aug2026.pdf"
    source_filename: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Original filename if uploaded as PDF"
    )

    # Any notes about data quality issues
    # "HSN code not found on invoice — estimated from material type"
    data_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Data quality notes — flags for manual review"
    )

    # ==========================================================================
    # DELIVERY TRACKING
    # Feature 2 — Delivery Early Warning — uses these columns.
    # When is the material expected? Has it been confirmed?
    # ==========================================================================

    # Expected delivery date
    # "2026-09-05" — when Ramesh expects the material to arrive
    expected_delivery_date: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="Expected delivery date in YYYY-MM-DD format"
    )

    # Has the supplier confirmed delivery?
    # False = follow-up message sent, waiting for confirmation
    # True = supplier confirmed, no alert needed
    delivery_confirmed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether supplier has confirmed delivery"
    )

    # Has the material actually been received?
    # True = physically received at factory
    # Used to close the delivery tracking loop
    delivery_received: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether material has been physically received"
    )

    # Actual delivery date — when it actually arrived
    actual_delivery_date: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="Actual delivery date — for supplier reliability scoring"
    )

    # ==========================================================================
    # DATABASE INDEXES
    # Composite indexes for the most common query patterns.
    #
    # FRESHER NOTE ON COMPOSITE INDEXES:
    # A composite index covers TWO columns together.
    # Our most common query is:
    # "Find all purchases for customer X in tax period 082026"
    # A composite index on (customer_id, tax_period) makes this
    # query instant — without it, the database scans every row.
    # ==========================================================================

    __table_args__ = (
        # Used by ITC reconciliation: fetch all purchases for a
        # customer in a specific tax period
        Index(
            "ix_purchases_customer_period",
            "customer_id",
            "tax_period"
        ),

        # Used by price benchmarking: find all purchases of a
        # specific material across a cluster
        Index(
            "ix_purchases_material_period",
            "material_name",
            "tax_period"
        ),

        # Used by delivery tracker: find all unconfirmed deliveries
        # due in the next 48 hours
        Index(
            "ix_purchases_delivery",
            "delivery_confirmed",
            "expected_delivery_date"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"Purchase("
            f"supplier={self.supplier_name}, "
            f"material={self.material_name}, "
            f"amount=₹{self.total_value}"
            f")"
        )

    @property
    def is_itc_at_risk(self) -> bool:
        """
        Returns True if this purchase has ITC that hasn't
        appeared in GSTR-2B yet.

        Used by the ITC reconciliation service to build
        the list of at-risk invoices to alert Ramesh about.
        """
        return self.itc_eligible and not self.in_gstr2b

    @property
    def itc_at_risk_amount(self) -> Decimal:
        """
        The rupee amount of ITC at risk for this purchase.
        Zero if ITC has already appeared in GSTR-2B.
        """
        if self.is_itc_at_risk:
            return self.gst_amount
        return Decimal("0")