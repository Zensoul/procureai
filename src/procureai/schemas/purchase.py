# ==============================================================================
# PROCUREAI — schemas/purchase.py
# Pydantic schemas for Purchase API requests and responses.
#
# FRESHER EXPLANATION:
# A purchase schema validates supplier invoice data before it enters
# the database. This is critical because:
#
# 1. ITC calculation depends on accurate GST amounts
#    Wrong GST amount → wrong ITC → wrong alert → wrong fee
#
# 2. Price benchmarking depends on accurate unit prices
#    Wrong price → wrong cluster average → wrong overpayment alert
#
# 3. Delivery tracking depends on accurate dates
#    Wrong date format → delivery follow-up sent on wrong day
#
# Every rupee of ITC we recover for Ramesh depends on this data
# being correct. Validators are not optional here.
# ==============================================================================

from datetime import date, datetime
from decimal import Decimal
from typing import Optional
import re

from pydantic import BaseModel, Field, field_validator, model_validator


# ==============================================================================
# VALID GST RATES IN INDIA
# Only these four rates are valid under GST law.
# Any other rate means the invoice data is wrong.
# ==============================================================================

VALID_GST_RATES = {0, 5, 12, 18, 28}

# ==============================================================================
# VALID DATA SOURCES
# How did this purchase record enter the system?
# ==============================================================================

VALID_DATA_SOURCES = {"pdf_upload", "tally_export", "manual", "gstn_api"}


class PurchaseBase(BaseModel):
    """
    Shared fields across Purchase schemas.
    """

    # Supplier information
    supplier_gstin: Optional[str] = Field(
        None,
        description="Supplier's 15-character GSTIN"
    )

    supplier_name: Optional[str] = Field(
        None,
        min_length=2,
        max_length=200,
        description="Supplier's business name"
    )

    supplier_phone: Optional[str] = Field(
        None,
        description="Supplier's phone for delivery follow-up"
    )

    # Invoice details
    invoice_number: Optional[str] = Field(
        None,
        max_length=50,
        description="Invoice number as printed on the supplier invoice"
    )

    invoice_date: Optional[str] = Field(
        None,
        description="Invoice date in YYYY-MM-DD format"
    )

    tax_period: Optional[str] = Field(
        None,
        description="GST tax period in MMYYYY format e.g. 082026"
    )

    # Material information
    material_name: Optional[str] = Field(
        None,
        min_length=2,
        max_length=200,
        description="Name of material purchased e.g. MS Rod"
    )

    material_grade: Optional[str] = Field(
        None,
        max_length=100,
        description="Material grade e.g. IS 2062 E250"
    )

    hsn_code: Optional[str] = Field(
        None,
        max_length=8,
        description="HSN code for GST classification"
    )

    # Quantity and pricing
    quantity: Optional[Decimal] = Field(
        None,
        gt=0,
        description="Quantity purchased — must be greater than zero"
    )

    unit: Optional[str] = Field(
        None,
        max_length=10,
        description="Unit of measurement: kg, nos, mtr, ltr"
    )

    unit_price: Optional[Decimal] = Field(
        None,
        gt=0,
        description="Price per unit before GST"
    )

    gst_rate: Optional[int] = Field(
        None,
        description="GST rate: 0, 5, 12, 18, or 28"
    )

    # Delivery tracking
    expected_delivery_date: Optional[str] = Field(
        None,
        description="Expected delivery date in YYYY-MM-DD format"
    )

    # ===========================================================================
    # VALIDATORS
    # ===========================================================================

    @field_validator("supplier_gstin")
    @classmethod
    def validate_supplier_gstin(cls, v: Optional[str]) -> Optional[str]:
        """Validates supplier GSTIN format."""
        if v is None:
            return v
        v = v.strip().upper()
        if not re.match(
            r'^\d{2}[A-Z]{5}\d{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$', v
        ):
            raise ValueError(
                f"Invalid GSTIN: {v}. Must be 15 characters like 29ABCDE1234F1Z5"
            )
        return v

    @field_validator("invoice_date", "expected_delivery_date")
    @classmethod
    def validate_date_format(cls, v: Optional[str]) -> Optional[str]:
        """
        Validates date is in YYYY-MM-DD format.

        FRESHER NOTE:
        We store dates as strings (VARCHAR) not DATE columns
        because GSTN API returns dates as strings and converting
        back and forth causes timezone issues.
        We validate format here so bad dates are caught early.
        """
        if v is None:
            return v
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError(
                f"Invalid date format: {v}. Must be YYYY-MM-DD e.g. 2026-08-15"
            )
        return v

    @field_validator("tax_period")
    @classmethod
    def validate_tax_period(cls, v: Optional[str]) -> Optional[str]:
        """
        Validates tax period is in MMYYYY format.
        082026 = August 2026
        """
        if v is None:
            return v
        v = v.strip()
        if not re.match(r'^\d{6}$', v):
            raise ValueError(
                f"Invalid tax period: {v}. Must be MMYYYY format e.g. 082026"
            )
        # Validate month is 01-12
        month = int(v[:2])
        if month < 1 or month > 12:
            raise ValueError(
                f"Invalid month in tax period: {v[:2]}. Must be 01-12."
            )
        return v

    @field_validator("gst_rate")
    @classmethod
    def validate_gst_rate(cls, v: Optional[int]) -> Optional[int]:
        """
        Validates GST rate is one of the four valid Indian GST slabs.

        FRESHER NOTE:
        India has only four GST rates: 5%, 12%, 18%, 28%
        Plus 0% for exempt goods.
        If a supplier charges 15% or 20%, the invoice is wrong.
        We catch this here so wrong ITC is never calculated.
        """
        if v is None:
            return v
        if v not in VALID_GST_RATES:
            raise ValueError(
                f"Invalid GST rate: {v}%. "
                f"Valid rates are: {sorted(VALID_GST_RATES)}"
            )
        return v

    @field_validator("unit")
    @classmethod
    def validate_unit(cls, v: Optional[str]) -> Optional[str]:
        """Normalises unit to lowercase."""
        if v is None:
            return v
        return v.strip().lower()

    @field_validator("hsn_code")
    @classmethod
    def validate_hsn_code(cls, v: Optional[str]) -> Optional[str]:
        """HSN codes are 4-8 digits."""
        if v is None:
            return v
        v = v.strip()
        if not re.match(r'^\d{4,8}$', v):
            raise ValueError(
                f"Invalid HSN code: {v}. Must be 4-8 digits e.g. 7214"
            )
        return v


# ==============================================================================
# CREATE SCHEMA
# ==============================================================================

class PurchaseCreate(PurchaseBase):
    """
    Schema for logging a new purchase (supplier invoice).

    Required fields: customer_id, supplier_gstin, supplier_name,
    invoice_number, invoice_date, tax_period, material_name,
    quantity, unit, unit_price, gst_rate

    FRESHER NOTE ON model_validator:
    Some validations need to look at MULTIPLE fields together.
    For example: taxable_value should equal quantity × unit_price.
    A field_validator only sees one field at a time.
    A model_validator sees the entire object — all fields at once.
    We use this to auto-calculate derived fields.
    """

    # Required fields
    customer_id: str = Field(
        ...,
        description="Customer's UUID"
    )

    supplier_gstin: str = Field(
        ...,
        description="Supplier's GSTIN"
    )

    supplier_name: str = Field(
        ...,
        min_length=2,
        max_length=200,
        description="Supplier's business name"
    )

    invoice_number: str = Field(
        ...,
        max_length=50,
        description="Invoice number"
    )

    invoice_date: str = Field(
        ...,
        description="Invoice date YYYY-MM-DD"
    )

    tax_period: str = Field(
        ...,
        description="Tax period MMYYYY"
    )

    material_name: str = Field(
        ...,
        min_length=2,
        max_length=200,
        description="Material name"
    )

    quantity: Decimal = Field(
        ...,
        gt=0,
        description="Quantity purchased"
    )

    unit: str = Field(
        ...,
        max_length=10,
        description="Unit of measurement"
    )

    unit_price: Decimal = Field(
        ...,
        gt=0,
        description="Price per unit before GST"
    )

    gst_rate: int = Field(
        ...,
        description="GST rate: 0, 5, 12, 18, or 28"
    )

    # Optional fields
    material_grade: Optional[str] = None
    hsn_code: Optional[str] = None
    supplier_phone: Optional[str] = None
    expected_delivery_date: Optional[str] = None
    data_source: str = Field(default="manual")
    source_filename: Optional[str] = None
    itc_eligible: bool = Field(default=True)

    @model_validator(mode="after")
    def calculate_derived_amounts(self) -> "PurchaseCreate":
        """
        Auto-calculates taxable_value, gst_amount, and total_value
        from quantity, unit_price, and gst_rate.

        FRESHER NOTE:
        We calculate these server-side — not trust the caller to send
        correct values. If Ramesh's CA sends unit_price=188 and
        quantity=300 but total=50000, that's wrong. We recalculate.

        taxable_value = quantity × unit_price
        gst_amount = taxable_value × (gst_rate / 100)
        total_value = taxable_value + gst_amount
        """
        if all([self.quantity, self.unit_price, self.gst_rate is not None]):
            self.taxable_value = (
                self.quantity * self.unit_price
            ).quantize(Decimal("0.01"))

            self.gst_amount = (
                self.taxable_value * Decimal(self.gst_rate) / Decimal("100")
            ).quantize(Decimal("0.01"))

            self.total_value = (
                self.taxable_value + self.gst_amount
            ).quantize(Decimal("0.01"))

        return self

    # These are populated by the model_validator above
    # Optional here because they're calculated, not sent by caller
    taxable_value: Optional[Decimal] = None
    gst_amount: Optional[Decimal] = None
    total_value: Optional[Decimal] = None


# ==============================================================================
# UPDATE SCHEMA
# ==============================================================================

class PurchaseUpdate(PurchaseBase):
    """
    Schema for updating purchase details.
    All fields optional — only send what's changing.

    Common use cases:
    - Mark delivery as confirmed: {delivery_confirmed: true}
    - Add actual delivery date: {actual_delivery_date: "2026-08-20"}
    - Update in_gstr2b status: {in_gstr2b: true}
    """

    in_gstr2b: Optional[bool] = Field(
        None,
        description="Whether invoice appeared in GSTR-2B"
    )

    delivery_confirmed: Optional[bool] = Field(
        None,
        description="Whether supplier confirmed delivery"
    )

    delivery_received: Optional[bool] = Field(
        None,
        description="Whether material was physically received"
    )

    actual_delivery_date: Optional[str] = Field(
        None,
        description="Actual delivery date YYYY-MM-DD"
    )

    gstr2b_matched_at: Optional[str] = Field(
        None,
        description="Date when invoice appeared in GSTR-2B"
    )


# ==============================================================================
# RESPONSE SCHEMA
# ==============================================================================

class PurchaseResponse(BaseModel):
    """
    Schema for API responses containing purchase data.
    Includes calculated fields (is_itc_at_risk, itc_at_risk_amount)
    that are useful for the CA dashboard and n8n workflows.
    """

    model_config = {"from_attributes": True}

    id: str
    customer_id: str
    created_at: datetime

    # Supplier
    supplier_gstin: str
    supplier_name: str
    supplier_phone: Optional[str] = None

    # Invoice
    invoice_number: str
    invoice_date: str
    tax_period: str

    # Material
    material_name: str
    material_grade: Optional[str] = None
    hsn_code: Optional[str] = None

    # Amounts
    quantity: Decimal
    unit: str
    unit_price: Decimal
    taxable_value: Decimal
    gst_rate: int
    gst_amount: Decimal
    total_value: Decimal

    # ITC status
    in_gstr2b: bool
    itc_eligible: bool

    # Delivery
    expected_delivery_date: Optional[str] = None
    delivery_confirmed: bool
    delivery_received: bool
    actual_delivery_date: Optional[str] = None

    # Data source
    data_source: str


# ==============================================================================
# SUMMARY SCHEMAS
# Used by the ITC reconciliation feature to show monthly summaries.
# ==============================================================================

class PurchaseSummary(BaseModel):
    """
    Lightweight summary of purchases for a tax period.
    Used in ITC alert messages — not full purchase details.
    """

    tax_period: str
    total_purchases: int
    total_taxable_value: Decimal
    total_gst_amount: Decimal
    matched_in_gstr2b: int
    not_matched_count: int
    itc_at_risk: Decimal


class MissingGSTR2BItem(BaseModel):
    """
    One invoice missing from GSTR-2B.
    Used to build the ITC alert message:
    "Call Sharma Metals, ask them to file invoice #INV-2847 — ₹34,000 at risk"
    """

    supplier_name: str
    supplier_gstin: str
    supplier_phone: Optional[str] = None
    invoice_number: str
    invoice_date: str
    gst_amount: Decimal