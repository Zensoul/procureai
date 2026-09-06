# ==============================================================================
# PROCUREAI — routers/sales.py
# API endpoints for sales invoice management.
# ==============================================================================

import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from procureai.database import get_db
from procureai.integrations.telegram_bot import telegram_client
from procureai.models.customer import Customer
from procureai.models.sales import SalesInvoice

router = APIRouter(
    prefix="/sales",
    tags=["sales"],
)

MONTH_NAMES = {
    "01": "January", "02": "February", "03": "March",
    "04": "April", "05": "May", "06": "June",
    "07": "July", "08": "August", "09": "September",
    "10": "October", "11": "November", "12": "December",
}


# ==============================================================================
# SCHEMAS
# ==============================================================================

class SalesInvoiceCreate(BaseModel):
    customer_id: str
    buyer_name: str = Field(..., min_length=2)
    buyer_gstin: Optional[str] = None
    buyer_phone: Optional[str] = None
    buyer_email: Optional[str] = None
    buyer_address: Optional[str] = None
    is_interstate: bool = False
    invoice_number: str = Field(..., min_length=1)
    invoice_date: str
    tax_period: Optional[str] = None
    due_date: str
    po_number: Optional[str] = None
    description: str = Field(..., min_length=2)
    product_category: Optional[str] = None
    hsn_code: Optional[str] = None
    quantity: Optional[Decimal] = None
    unit: Optional[str] = None
    taxable_value: Decimal = Field(..., gt=0)
    gst_rate: int = Field(default=18)
    payment_terms_days: int = Field(default=30)
    notes: Optional[str] = None


class SalesInvoiceUpdate(BaseModel):
    amount_received: Optional[Decimal] = None
    is_paid: Optional[bool] = None
    paid_date: Optional[str] = None
    gstr1_filed: Optional[bool] = None
    gstr1_filed_period: Optional[str] = None
    notes: Optional[str] = None


class SalesInvoiceResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: str
    customer_id: str
    buyer_name: str
    buyer_gstin: Optional[str] = None
    buyer_phone: Optional[str] = None
    invoice_number: str
    invoice_date: str
    tax_period: str
    due_date: str
    po_number: Optional[str] = None
    description: str
    product_category: Optional[str] = None
    hsn_code: Optional[str] = None
    quantity: Optional[Decimal] = None
    unit: Optional[str] = None
    taxable_value: Decimal
    gst_rate: int
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    total_gst: Decimal
    total_amount: Decimal
    amount_received: Decimal
    outstanding_amount: Decimal
    is_paid: bool
    paid_date: Optional[str] = None
    is_overdue: bool
    days_overdue: int
    days_until_due: int
    gstr1_filed: bool
    reminders_sent: int
    payment_terms_days: int


# ==============================================================================
# ENDPOINTS
# ==============================================================================

@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    summary="Create a sales invoice",
)
async def create_sales_invoice(
    data: SalesInvoiceCreate,
    db: AsyncSession = Depends(get_db),
) -> SalesInvoiceResponse:
    """
    Log a sales invoice Ramesh has raised to a customer.
    Auto-calculates GST split (CGST+SGST for intrastate, IGST for interstate).
    """
    result = await db.execute(
        select(Customer).where(
            Customer.id == uuid.UUID(data.customer_id)
        )
    )
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer not found: {data.customer_id}"
        )

    # Calculate GST
    total_gst = (
        data.taxable_value * Decimal(data.gst_rate) / Decimal("100")
    ).quantize(Decimal("0.01"))

    if data.is_interstate:
        igst = total_gst
        cgst = Decimal("0")
        sgst = Decimal("0")
    else:
        igst = Decimal("0")
        cgst = (total_gst / 2).quantize(Decimal("0.01"))
        sgst = (total_gst - cgst).quantize(Decimal("0.01"))

    total_amount = (
        data.taxable_value + total_gst
    ).quantize(Decimal("0.01"))

    # Determine tax period from invoice date if not provided
    tax_period = data.tax_period
    if not tax_period:
        try:
            parts = data.invoice_date.split("-")
            tax_period = f"{parts[1]}{parts[0]}"
        except Exception:
            today = date.today()
            tax_period = f"{today.month:02d}{today.year}"

    invoice = SalesInvoice(
        customer_id=uuid.UUID(data.customer_id),
        buyer_name=data.buyer_name,
        buyer_gstin=data.buyer_gstin,
        buyer_phone=data.buyer_phone,
        buyer_email=data.buyer_email,
        buyer_address=data.buyer_address,
        is_interstate=data.is_interstate,
        invoice_number=data.invoice_number,
        invoice_date=data.invoice_date,
        tax_period=tax_period,
        due_date=data.due_date,
        po_number=data.po_number,
        description=data.description,
        product_category=data.product_category,
        hsn_code=data.hsn_code,
        quantity=data.quantity,
        unit=data.unit,
        taxable_value=data.taxable_value,
        gst_rate=data.gst_rate,
        cgst_amount=cgst,
        sgst_amount=sgst,
        igst_amount=igst,
        total_gst=total_gst,
        total_amount=total_amount,
        payment_terms_days=data.payment_terms_days,
        notes=data.notes,
    )

    db.add(invoice)
    await db.flush()

    logger.info(
        f"Sales invoice created: {data.buyer_name} "
        f"Rs.{total_amount} due {data.due_date}"
    )

    return _to_response(invoice)


@router.get(
    "/customer/{customer_id}",
    summary="List all sales invoices for a customer",
)
async def list_sales_invoices(
    customer_id: str,
    tax_period: Optional[str] = Query(default=None),
    unpaid_only: bool = Query(default=False),
    overdue_only: bool = Query(default=False),
    gstr1_pending: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List all sales invoices with summary statistics."""
    query = select(SalesInvoice).where(
        SalesInvoice.customer_id == uuid.UUID(customer_id)
    )

    if tax_period:
        query = query.where(SalesInvoice.tax_period == tax_period)
    if unpaid_only:
        query = query.where(SalesInvoice.is_paid == False)
    if gstr1_pending:
        query = query.where(SalesInvoice.gstr1_filed == False)

    query = query.order_by(SalesInvoice.invoice_date.desc())
    result = await db.execute(query)
    invoices = result.scalars().all()

    if overdue_only:
        invoices = [i for i in invoices if i.is_overdue]

    total_revenue = sum(i.taxable_value for i in invoices)
    total_gst_collected = sum(i.total_gst for i in invoices)
    total_outstanding = sum(
        i.outstanding_amount for i in invoices if not i.is_paid
    )
    overdue_amount = sum(
        i.outstanding_amount for i in invoices if i.is_overdue
    )

    return {
        "customer_id": customer_id,
        "total_invoices": len(invoices),
        "total_revenue": float(total_revenue),
        "total_gst_collected": float(total_gst_collected),
        "total_outstanding": float(total_outstanding),
        "overdue_amount": float(overdue_amount),
        "overdue_count": len([i for i in invoices if i.is_overdue]),
        "gstr1_pending_count": len(
            [i for i in invoices if not i.gstr1_filed]
        ),
        "invoices": [_to_response(i) for i in invoices],
    }


@router.patch(
    "/{invoice_id}",
    summary="Update sales invoice",
)
async def update_sales_invoice(
    invoice_id: str,
    data: SalesInvoiceUpdate,
    db: AsyncSession = Depends(get_db),
) -> SalesInvoiceResponse:
    """Update payment status, GSTR-1 filing status, or notes."""
    result = await db.execute(
        select(SalesInvoice).where(
            SalesInvoice.id == uuid.UUID(invoice_id)
        )
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invoice not found: {invoice_id}"
        )

    update_dict = data.model_dump(exclude_none=True)
    for field, value in update_dict.items():
        setattr(invoice, field, value)

    if (
        data.amount_received and
        invoice.amount_received >= invoice.total_amount
    ):
        invoice.is_paid = True
        invoice.paid_date = invoice.paid_date or date.today().isoformat()

    db.add(invoice)
    await db.flush()

    return _to_response(invoice)


@router.get(
    "/customer/{customer_id}/gst-summary",
    summary="Get output GST summary for GST liability calculation",
)
async def get_output_gst_summary(
    customer_id: str,
    tax_period: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get output GST collected from all sales for a tax period."""
    if not tax_period:
        today = date.today()
        tax_period = f"{today.month:02d}{today.year}"

    month = tax_period[:2]
    year = tax_period[2:]
    period_display = f"{MONTH_NAMES.get(month, month)} {year}"

    result = await db.execute(
        select(
            func.sum(SalesInvoice.taxable_value).label("total_taxable"),
            func.sum(SalesInvoice.total_gst).label("total_gst"),
            func.sum(SalesInvoice.cgst_amount).label("total_cgst"),
            func.sum(SalesInvoice.sgst_amount).label("total_sgst"),
            func.sum(SalesInvoice.igst_amount).label("total_igst"),
            func.count(SalesInvoice.id).label("invoice_count"),
        ).where(
            and_(
                SalesInvoice.customer_id == uuid.UUID(customer_id),
                SalesInvoice.tax_period == tax_period,
            )
        )
    )
    row = result.fetchone()

    return {
        "customer_id": customer_id,
        "tax_period": tax_period,
        "period_display": period_display,
        "invoice_count": int(row.invoice_count or 0),
        "total_taxable_value": float(row.total_taxable or 0),
        "total_output_gst": float(row.total_gst or 0),
        "cgst_collected": float(row.total_cgst or 0),
        "sgst_collected": float(row.total_sgst or 0),
        "igst_collected": float(row.total_igst or 0),
    }


@router.post(
    "/{invoice_id}/send-reminder",
    summary="Send payment reminder to buyer",
)
async def send_payment_reminder(
    invoice_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Send automated payment reminder — notifies owner via Telegram."""
    result = await db.execute(
        select(SalesInvoice).where(
            SalesInvoice.id == uuid.UUID(invoice_id)
        )
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invoice not found: {invoice_id}"
        )

    if invoice.is_paid:
        return {"status": "skipped", "reason": "Invoice already paid"}

    customer_result = await db.execute(
        select(Customer).where(Customer.id == invoice.customer_id)
    )
    customer = customer_result.scalar_one_or_none()

    days = invoice.days_overdue

    if days == 0:
        urgency = "due_today"
        subject = "Payment due today"
    elif days <= 7:
        urgency = "overdue"
        subject = f"Payment {days} days overdue"
    elif days <= 30:
        urgency = "urgent"
        subject = f"Payment {days} days overdue — URGENT"
    else:
        urgency = "critical"
        subject = f"Payment {days} days overdue — CRITICAL"

    invoice.reminders_sent += 1
    invoice.last_reminder_date = date.today().isoformat()
    db.add(invoice)
    await db.flush()

    if customer and customer.telegram_chat_id:
        await telegram_client.send_message(
            chat_id=customer.telegram_chat_id,
            text=(
                f"💵 <b>Payment Reminder Alert</b>\n\n"
                f"📋 Invoice: #{invoice.invoice_number}\n"
                f"🏢 Buyer: {invoice.buyer_name}\n"
                f"💰 Outstanding: Rs.{invoice.outstanding_amount:,.0f}\n"
                f"📅 Due date: {invoice.due_date}\n"
                f"⏰ Status: {subject}\n"
                f"📤 Reminder #{invoice.reminders_sent} sent"
            )
        )

    logger.info(
        f"Payment reminder #{invoice.reminders_sent}: "
        f"{invoice.buyer_name} Rs.{invoice.outstanding_amount:,.0f}"
    )

    return {
        "status": "sent",
        "urgency": urgency,
        "subject": subject,
        "days_overdue": days,
        "reminders_sent": invoice.reminders_sent,
    }


@router.get(
    "/customer/{customer_id}/receivables-aging",
    summary="Receivables aging report",
)
async def get_receivables_aging(
    customer_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Aging report — overdue invoices grouped by 0-30, 31-60, 61-90, 90+ days."""
    result = await db.execute(
        select(SalesInvoice).where(
            and_(
                SalesInvoice.customer_id == uuid.UUID(customer_id),
                SalesInvoice.is_paid == False,
            )
        )
    )
    invoices = result.scalars().all()

    buckets = {
        "current": {"count": 0, "amount": Decimal("0"), "invoices": []},
        "0_30_days": {"count": 0, "amount": Decimal("0"), "invoices": []},
        "31_60_days": {"count": 0, "amount": Decimal("0"), "invoices": []},
        "61_90_days": {"count": 0, "amount": Decimal("0"), "invoices": []},
        "over_90_days": {"count": 0, "amount": Decimal("0"), "invoices": []},
    }

    for inv in invoices:
        days = inv.days_overdue
        amount = inv.outstanding_amount
        summary = {
            "invoice_number": inv.invoice_number,
            "buyer_name": inv.buyer_name,
            "amount": float(amount),
            "due_date": inv.due_date,
            "days_overdue": days,
        }

        if days == 0:
            bucket = "current"
        elif days <= 30:
            bucket = "0_30_days"
        elif days <= 60:
            bucket = "31_60_days"
        elif days <= 90:
            bucket = "61_90_days"
        else:
            bucket = "over_90_days"

        buckets[bucket]["count"] += 1
        buckets[bucket]["amount"] += amount
        buckets[bucket]["invoices"].append(summary)

    total_outstanding = sum(i.outstanding_amount for i in invoices)

    return {
        "customer_id": customer_id,
        "total_outstanding": float(total_outstanding),
        "total_invoices": len(invoices),
        "aging": {
            k: {
                "count": v["count"],
                "amount": float(v["amount"]),
                "invoices": v["invoices"],
            }
            for k, v in buckets.items()
        },
    }


# ==============================================================================
# HELPER
# ==============================================================================

def _to_response(invoice: SalesInvoice) -> SalesInvoiceResponse:
    return SalesInvoiceResponse(
        id=str(invoice.id),
        customer_id=str(invoice.customer_id),
        buyer_name=invoice.buyer_name,
        buyer_gstin=invoice.buyer_gstin,
        buyer_phone=invoice.buyer_phone,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        tax_period=invoice.tax_period,
        due_date=invoice.due_date,
        po_number=invoice.po_number,
        description=invoice.description,
        product_category=invoice.product_category,
        hsn_code=invoice.hsn_code,
        quantity=invoice.quantity,
        unit=invoice.unit,
        taxable_value=invoice.taxable_value,
        gst_rate=invoice.gst_rate,
        cgst_amount=invoice.cgst_amount,
        sgst_amount=invoice.sgst_amount,
        igst_amount=invoice.igst_amount,
        total_gst=invoice.total_gst,
        total_amount=invoice.total_amount,
        amount_received=invoice.amount_received,
        outstanding_amount=invoice.outstanding_amount,
        is_paid=invoice.is_paid,
        paid_date=invoice.paid_date,
        is_overdue=invoice.is_overdue,
        days_overdue=invoice.days_overdue,
        days_until_due=invoice.days_until_due,
        gstr1_filed=invoice.gstr1_filed,
        reminders_sent=invoice.reminders_sent,
        payment_terms_days=invoice.payment_terms_days,
    )
