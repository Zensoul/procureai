# ==============================================================================
# PROCUREAI — schemas/itc.py
# Pydantic schemas for ITC tracking and reconciliation.
#
# FRESHER EXPLANATION:
# ITC schemas serve three purposes:
#
# 1. TRIGGER — n8n sends a reconciliation request
#    ITCReconciliationRequest tells the system which customer
#    and which period to reconcile
#
# 2. RESULT — reconciliation service returns findings
#    ITCReconciliationResult contains the full breakdown:
#    how much eligible, how much matched, how much at risk,
#    which specific invoices are missing
#
# 3. ALERT — messaging service uses result to build Ramesh's message
#    ITCAlertData contains exactly what goes into the WhatsApp/RCS/SMS
#    alert — supplier names, amounts, what action to take
#
# These schemas are the "language" between your three core services:
# ITC Reconciliation Service → Messaging Service → Customer's Phone
# ==============================================================================

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator, computed_field

from procureai.schemas.purchase import MissingGSTR2BItem


# ==============================================================================
# REQUEST SCHEMAS
# What n8n sends to trigger reconciliation
# ==============================================================================

class ITCReconciliationRequest(BaseModel):
    """
    Request schema for triggering ITC reconciliation.

    Sent by n8n on the 16th of every month for each active customer.
    The 16th is when GSTR-2B becomes available from GSTN.

    FRESHER NOTE:
    This is the simplest schema in the file — just two fields.
    But it's important because it's the entry point for
    Feature 1 — the ITC Recovery Alert.
    Every month, n8n creates one of these for each customer.
    """

    customer_id: str = Field(
        ...,
        description="Customer UUID to reconcile"
    )

    tax_period: str = Field(
        ...,
        description="Tax period in MMYYYY format e.g. 082026"
    )

    # Force re-run even if already reconciled this period?
    # Default False — don't waste API calls if already done
    # Set True when customer reports a discrepancy
    force_rerun: bool = Field(
        default=False,
        description="Force re-reconciliation even if already done this period"
    )

    @field_validator("tax_period")
    @classmethod
    def validate_tax_period(cls, v: str) -> str:
        """Validates MMYYYY format."""
        import re
        if not re.match(r'^\d{6}$', v):
            raise ValueError(f"Invalid tax period: {v}. Use MMYYYY format.")
        month = int(v[:2])
        if month < 1 or month > 12:
            raise ValueError(f"Invalid month: {v[:2]}")
        return v


class ITCBatchReconciliationRequest(BaseModel):
    """
    Request to reconcile ALL active customers for a tax period.

    This is what n8n sends on the 16th of the month —
    one request triggers reconciliation for every customer
    instead of sending individual requests per customer.

    FRESHER NOTE ON BATCH PROCESSING:
    Instead of n8n calling /reconcile 40 times (once per customer),
    it calls /reconcile/batch once.
    The service handles all 40 customers internally,
    running them concurrently where possible.
    This is faster and reduces n8n workflow complexity.
    """

    tax_period: str = Field(
        ...,
        description="Tax period to reconcile for all customers"
    )

    # Optional: reconcile only a specific cluster
    # None = all clusters
    cluster_filter: Optional[str] = Field(
        None,
        description="Only reconcile customers in this cluster"
    )


# ==============================================================================
# RESULT SCHEMAS
# What the reconciliation service returns after processing
# ==============================================================================

class ITCSupplierBreakdown(BaseModel):
    """
    ITC status for one specific supplier in a tax period.

    FRESHER NOTE:
    The reconciliation finds mismatches at the SUPPLIER level.
    If Sharma Metals filed 3 of their 4 invoices, this schema
    shows exactly which invoice is missing and how much is at risk.

    This is the data that goes into the alert:
    "Sharma Metals has not filed invoice #INV-2847 — ₹34,000 at risk"
    """

    supplier_gstin: str
    supplier_name: str
    supplier_phone: Optional[str] = None

    # Total ITC from this supplier this period
    total_itc: Decimal

    # How much appeared in GSTR-2B
    matched_itc: Decimal

    # How much is missing
    at_risk_itc: Decimal

    # The specific invoices that are missing
    missing_invoices: list[MissingGSTR2BItem] = Field(default_factory=list)

    @computed_field
    @property
    def is_fully_matched(self) -> bool:
        """
        True if all ITC from this supplier appeared in GSTR-2B.
        False if any invoice is missing.
        Used to filter suppliers for the alert.
        """
        return self.at_risk_itc == Decimal("0")

    @computed_field
    @property
    def match_percentage(self) -> Decimal:
        """
        What percentage of this supplier's ITC was matched.
        100% = all filed. 0% = nothing filed.
        Used in detailed reports for the CA.
        """
        if self.total_itc == 0:
            return Decimal("100")
        return (
            self.matched_itc / self.total_itc * 100
        ).quantize(Decimal("0.1"))


class ITCReconciliationResult(BaseModel):
    """
    Full result of one ITC reconciliation run.

    This is what the reconciliation service returns after:
    1. Fetching GSTR-2B from GSTN API
    2. Comparing against purchase invoices
    3. Calculating mismatches

    The messaging service reads this to build Ramesh's alert.
    The ITC tracking service reads this to update the database.

    FRESHER NOTE ON THE NUMBERS:
    eligible_itc: Total GST Ramesh paid to all suppliers
    matched_itc:  Total that appeared in GSTR-2B (safe to claim)
    at_risk_itc:  eligible - matched (might be lost)
    recovered_itc: ITC that was at_risk before but now appeared
                   (this is what we charge our fee on)
    """

    # Which customer and period
    customer_id: str
    tax_period: str
    period_display: str = Field(
        description="Human readable e.g. August 2026"
    )

    # Core ITC amounts
    eligible_itc: Decimal = Field(
        description="Total ITC eligible from all purchases"
    )

    matched_itc: Decimal = Field(
        description="ITC that appeared in GSTR-2B — safe to claim"
    )

    at_risk_itc: Decimal = Field(
        description="ITC not yet in GSTR-2B — at risk"
    )

    recovered_itc: Decimal = Field(
        default=Decimal("0"),
        description="ITC recovered vs previous period — fee basis"
    )

    # Supplier-level breakdown
    supplier_breakdown: list[ITCSupplierBreakdown] = Field(
        default_factory=list,
        description="ITC status per supplier"
    )

    # Missing invoice count — for alert summary
    missing_invoice_count: int = Field(
        default=0,
        description="Total invoices missing from GSTR-2B"
    )

    # Reconciliation metadata
    reconciled_at: datetime = Field(
        description="When this reconciliation was run"
    )

    data_source: str = Field(
        default="gstn_api",
        description="Where GSTR-2B data came from: gstn_api or pdf_upload"
    )

    # ===========================================================================
    # COMPUTED FIELDS
    # Derived values used in alerts and reports
    # ===========================================================================

    @computed_field
    @property
    def has_risk(self) -> bool:
        """
        True if any ITC is at risk this period.
        Used by the messaging service to decide whether to send an alert.
        If False — no alert needed, everything filed correctly.
        """
        return self.at_risk_itc > Decimal("0")

    @computed_field
    @property
    def recovery_rate(self) -> Decimal:
        """
        Percentage of eligible ITC that was matched.
        100% = perfect. 80% = 20% still missing.
        """
        if self.eligible_itc == 0:
            return Decimal("100")
        return (
            self.matched_itc / self.eligible_itc * 100
        ).quantize(Decimal("0.1"))

    @computed_field
    @property
    def suppliers_with_risk(self) -> list[ITCSupplierBreakdown]:
        """
        Only the suppliers who have missing invoices.
        Used to build the alert — we don't mention suppliers
        who filed correctly.
        """
        return [s for s in self.supplier_breakdown if not s.is_fully_matched]


# ==============================================================================
# FEE CALCULATION SCHEMA
# Calculates and records our fee for ITC recovery
# ==============================================================================

class ITCFeeCalculation(BaseModel):
    """
    Fee calculation for one period's ITC recovery.

    FRESHER NOTE ON OUR BUSINESS MODEL:
    We charge 20% of ITC we recover.
    "Recovered" means: ITC that was at_risk in a previous period
    but now appears in GSTR-2B after we alerted Ramesh.

    Example:
    August: ₹59,000 at risk → we alert Ramesh → he calls suppliers
    September: ₹59,000 now appears in GSTR-2B
    Our fee: ₹59,000 × 20% = ₹11,800

    Ramesh's net benefit: ₹59,000 - ₹11,800 = ₹47,200
    He pays nothing if we recover nothing.
    """

    customer_id: str
    tax_period: str
    period_display: str

    # The recovery amount — this is the fee basis
    recovered_itc: Decimal

    # Fee rate — stored per calculation for audit trail
    fee_rate: Decimal = Field(
        default=Decimal("0.20"),
        description="Fee rate: 0.20 = 20%"
    )

    # CA share rate — 20% of our fee goes to the CA
    ca_share_rate: Decimal = Field(
        default=Decimal("0.20"),
        description="CA revenue share rate"
    )

    @computed_field
    @property
    def fee_amount(self) -> Decimal:
        """
        Our fee = recovered_itc × fee_rate
        This is what we invoice Ramesh.
        """
        return (
            self.recovered_itc * self.fee_rate
        ).quantize(Decimal("0.01"))

    @computed_field
    @property
    def ca_share_amount(self) -> Decimal:
        """
        CA's cut = fee_amount × ca_share_rate
        This is what we pay the CA who introduced Ramesh.
        """
        return (
            self.fee_amount * self.ca_share_rate
        ).quantize(Decimal("0.01"))

    @computed_field
    @property
    def net_revenue(self) -> Decimal:
        """
        Our actual revenue after paying the CA.
        net_revenue = fee_amount - ca_share_amount
        """
        return (self.fee_amount - self.ca_share_amount).quantize(
            Decimal("0.01")
        )

    @computed_field
    @property
    def customer_net_benefit(self) -> Decimal:
        """
        What Ramesh actually gains after paying our fee.
        This is the number we show in customer-facing reports.
        "You recovered ₹59,000. Our fee: ₹11,800. Your gain: ₹47,200"
        """
        return (self.recovered_itc - self.fee_amount).quantize(
            Decimal("0.01")
        )


# ==============================================================================
# ALERT DATA SCHEMA
# Exactly what goes into the ITC alert message
# ==============================================================================

class ITCAlertData(BaseModel):
    """
    Structured data for building ITC alert messages.

    The messaging service reads this schema and formats it
    into the appropriate message for each channel:
    - RCS: rich card with buttons
    - SMS: plain text summary
    - Voice: Kannada speech script

    FRESHER NOTE:
    This schema is the bridge between the reconciliation service
    and the messaging service. By using a typed schema,
    both services agree on exactly what data is available
    for the alert. No "KeyError: supplier_name" at 2 AM.
    """

    # Customer context
    customer_id: str
    owner_name: str
    business_name: str
    preferred_channel: str
    preferred_language: str
    phone: str

    # Period
    tax_period: str
    period_display: str

    # Summary numbers for the alert headline
    total_at_risk: Decimal = Field(
        description="Total ITC at risk — the big number in the alert"
    )

    missing_supplier_count: int = Field(
        description="How many suppliers haven't filed"
    )

    missing_invoice_count: int = Field(
        description="How many invoices are missing"
    )

    # The specific suppliers to contact
    # Only includes suppliers with missing invoices
    suppliers_to_contact: list[ITCSupplierBreakdown] = Field(
        default_factory=list
    )

    # Recovery from last period — show positive news too
    previous_period_recovery: Decimal = Field(
        default=Decimal("0"),
        description="ITC recovered since last alert"
    )

    @computed_field
    @property
    def alert_headline(self) -> str:
        """
        The main line of the alert — in English.
        Translated to Kannada/Hindi by the messaging service.

        Example: "₹59,000 ITC at risk from 2 suppliers"
        """
        return (
            f"₹{self.total_at_risk:,.0f} ITC at risk "
            f"from {self.missing_supplier_count} supplier"
            f"{'s' if self.missing_supplier_count > 1 else ''}"
        )

    @computed_field
    @property
    def has_good_news(self) -> bool:
        """
        True if there's previous recovery to mention.
        Alerts that lead with good news (recovery) before bad news (risk)
        get better engagement — we always mention recovery first.
        """
        return self.previous_period_recovery > Decimal("0")


# ==============================================================================
# RESPONSE SCHEMA
# What the API returns for ITC tracking records
# ==============================================================================

class ITCTrackingResponse(BaseModel):
    """
    Schema for API responses containing ITC tracking data.
    Used by the CA dashboard and customer-facing reports.
    """

    model_config = {"from_attributes": True}

    id: str
    customer_id: str
    tax_period: str
    period_display: str
    created_at: datetime

    # Core amounts
    eligible_itc: Decimal
    matched_itc: Decimal
    at_risk_itc: Decimal
    recovered_itc: Decimal
    missing_invoice_count: int

    # Fee information
    fee_rate: Decimal
    fee_amount: Decimal
    fee_invoiced: bool
    fee_paid: bool

    # CA share
    ca_share_amount: Decimal
    ca_share_paid: bool

    # Status
    reconciliation_status: str
    last_reconciled_at: Optional[str] = None
    alert_sent_at: Optional[str] = None


class ITCMonthlyReport(BaseModel):
    """
    Monthly ITC summary for one customer — used in reports.

    FRESHER NOTE:
    This is what the CA sees for each of their clients
    at the end of the month. Shows everything in one view:
    - How much ITC was recovered
    - How much is still at risk
    - What the fee is
    - Net benefit to the customer
    """

    customer_id: str
    owner_name: str
    business_name: str
    gstin: str
    tax_period: str
    period_display: str

    eligible_itc: Decimal
    matched_itc: Decimal
    at_risk_itc: Decimal
    recovered_itc: Decimal

    fee_amount: Decimal
    ca_share_amount: Decimal
    customer_net_benefit: Decimal

    recovery_rate: Decimal
    reconciliation_status: str
