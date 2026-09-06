# ==============================================================================
# PROCUREAI — routers/payments.py
# API endpoints for payment invoice tracking.
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
from procureai.models.payment import PaymentInvoice

router = APIRouter(
    prefix="/payments",
    tags=["payments"],
)


# ==============================================================================
# SCHEMAS
# ==============================================================================

class PaymentInvoiceCreate(BaseModel):
    customer_id: str
    buyer_name: str = Field(..., min_length=2)
    buyer_gstin: Optional[str] = None
    buyer_phone: Optional[str] = None
    buyer_email: Optional[str] = None
    invoice_number: str = Field(..., min_length=1)
    invoice_date: str
    due_date: str
    description: Optional[str] = None
    taxable_value: Decimal = Field(..., gt=0)
    gst_rate: int = Field(default=18)
    notes: Optional[str] = None


class PaymentUpdate(BaseModel):
    amount_received: Optional[Decimal] = None
    is_paid: Optional[bool] = None
    paid_date: Optional[str] = None
    notes: Optional[str] = None


class PaymentResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: str
    customer_id: str
    buyer_name: str
    buyer_phone: Optional[str] = None
    invoice_number: str
    invoice_date: str
    due_date: str
    description: Optional[str] = None
    taxable_value: Decimal
    gst_amount: Decimal
    total_amount: Decimal
    amount_received: Decimal
    outstanding_amount: Decimal
    is_paid: bool
    paid_date: Optional[str] = None
    is_overdue: bool
    days_overdue: int
    reminders_sent: int
    last_reminder_date: Optional[str] = None


# ==============================================================================
# ENDPOINTS
# ==============================================================================

@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    summary="Log a new customer invoice",
)
async def create_payment_invoice(
    data: PaymentInvoiceCreate,
    db: AsyncSession = Depends(get_db),
) -> PaymentResponse:
    """
    Log an invoice Ramesh has raised to one of his customers.
    Tracks payment status and enables automated reminders.
    """
    # Verify customer exists
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

    # Calculate GST and total
    gst_amount = (
        data.taxable_value * Decimal(data.gst_rate) / Decimal("100")
    ).quantize(Decimal("0.01"))
    total_amount = (data.taxable_value + gst_amount).quantize(
        Decimal("0.01")
    )

    invoice = PaymentInvoice(
        customer_id=uuid.UUID(data.customer_id),
        buyer_name=data.buyer_name,
        buyer_gstin=data.buyer_gstin,
        buyer_phone=data.buyer_phone,
        buyer_email=data.buyer_email,
        invoice_number=data.invoice_number,
        invoice_date=data.invoice_date,
        due_date=data.due_date,
        description=data.description,
        taxable_value=data.taxable_value,
        gst_amount=gst_amount,
        total_amount=total_amount,
        notes=data.notes,
    )

    db.add(invoice)
    await db.flush()

    logger.info(
        f"Payment invoice created: {data.buyer_name} "
        f"₹{total_amount} due {data.due_date}"
    )

    return _to_response(invoice)


@router.get(
    "/customer/{customer_id}",
    summary="List all payment invoices for a customer",
)
async def list_payment_invoices(
    customer_id: str,
    overdue_only: bool = Query(default=False),
    unpaid_only: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    List all outgoing invoices for a customer.
    Shows cash position — money coming in.
    """
    query = select(PaymentInvoice).where(
        PaymentInvoice.customer_id == uuid.UUID(customer_id)
    )

    if unpaid_only:
        query = query.where(PaymentInvoice.is_paid == False)

    query = query.order_by(PaymentInvoice.due_date.asc())
    result = await db.execute(query)
    invoices = result.scalars().all()

    # Filter overdue in Python (computed property)
    if overdue_only:
        invoices = [i for i in invoices if i.is_overdue]

    # Calculate summary
    total_outstanding = sum(
        i.outstanding_amount for i in invoices if not i.is_paid
    )
    total_overdue = sum(
        i.outstanding_amount for i in invoices if i.is_overdue
    )
    overdue_count = len([i for i in invoices if i.is_overdue])

    return {
        "customer_id": customer_id,
        "total_invoices": len(invoices),
        "total_outstanding": float(total_outstanding),
        "total_overdue": float(total_overdue),
        "overdue_count": overdue_count,
        "invoices": [_to_response(i) for i in invoices],
    }


@router.patch(
    "/{invoice_id}",
    summary="Update payment status",
)
async def update_payment(
    invoice_id: str,
    data: PaymentUpdate,
    db: AsyncSession = Depends(get_db),
) -> PaymentResponse:
    """
    Update payment status — mark as paid, record partial payment.
    """
    result = await db.execute(
        select(PaymentInvoice).where(
            PaymentInvoice.id == uuid.UUID(invoice_id)
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

    # Auto-mark as paid if full amount received
    if (
        data.amount_received and
        invoice.amount_received >= invoice.total_amount
    ):
        invoice.is_paid = True
        invoice.paid_date = invoice.paid_date or date.today().isoformat()

    db.add(invoice)
    await db.flush()

    return _to_response(invoice)


@router.post(
    "/{invoice_id}/send-reminder",
    summary="Send payment reminder to buyer",
)
async def send_payment_reminder(
    invoice_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Send automated payment reminder to the buyer.
    Escalates tone based on how many days overdue.
    """
    result = await db.execute(
        select(PaymentInvoice).where(
            PaymentInvoice.id == uuid.UUID(invoice_id)
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

    # Load customer to get their Telegram
    customer_result = await db.execute(
        select(Customer).where(
            Customer.id == invoice.customer_id
        )
    )
    customer = customer_result.scalar_one_or_none()

    days = invoice.days_overdue

    # Build reminder message based on urgency
    if days == 0:
        urgency = "gentle"
        message = (
            f"Dear {invoice.buyer_name},\n\n"
            f"This is a friendly reminder that Invoice "
            f"#{invoice.invoice_number} for ₹{invoice.total_amount:,.0f} "
            f"is due today.\n\n"
            f"Kindly arrange payment at your earliest convenience.\n\n"
            f"— {customer.business_name if customer else 'Your Supplier'}"
        )
    elif days <= 7:
        urgency = "moderate"
        message = (
            f"Dear {invoice.buyer_name},\n\n"
            f"Invoice #{invoice.invoice_number} for "
            f"₹{invoice.total_amount:,.0f} was due {days} day"
            f"{'s' if days > 1 else ''} ago.\n\n"
            f"Outstanding: ₹{invoice.outstanding_amount:,.0f}\n\n"
            f"Please arrange payment immediately.\n\n"
            f"— {customer.business_name if customer else 'Your Supplier'}"
        )
    else:
        urgency = "urgent"
        message = (
            f"URGENT: Dear {invoice.buyer_name},\n\n"
            f"Invoice #{invoice.invoice_number} for "
            f"₹{invoice.total_amount:,.0f} is {days} days overdue.\n\n"
            f"Outstanding: ₹{invoice.outstanding_amount:,.0f}\n\n"
            f"Please arrange immediate payment to avoid further action.\n\n"
            f"— {customer.business_name if customer else 'Your Supplier'}"
        )

    logger.info(
        f"Payment reminder sent: {invoice.buyer_name} "
        f"₹{invoice.outstanding_amount:,.0f} {days} days overdue"
    )

    # Update reminder count
    invoice.reminders_sent += 1
    invoice.last_reminder_date = date.today().isoformat()
    db.add(invoice)
    await db.flush()

    # Notify Ramesh on Telegram that reminder was sent
    if customer and customer.telegram_chat_id:
        await telegram_client.send_message(
            chat_id=customer.telegram_chat_id,
            text=(
                f"📤 <b>Payment reminder sent</b>\n\n"
                f"To: {invoice.buyer_name}\n"
                f"Invoice: #{invoice.invoice_number}\n"
                f"Amount: ₹{invoice.outstanding_amount:,.0f}\n"
                f"Overdue: {days} days\n"
                f"Urgency: {urgency.upper()}"
            )
        )

    return {
        "status": "sent",
        "urgency": urgency,
        "days_overdue": days,
        "reminders_sent": invoice.reminders_sent,
        "message_preview": message[:100] + "...",
    }


@router.get(
    "/customer/{customer_id}/cash-position",
    summary="Get cash flow position for a customer",
)
async def get_cash_position(
    customer_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Show Ramesh his complete cash position:
    - Money he owes suppliers (from purchases)
    - Money customers owe him (from payment invoices)
    - Net position
    """
    from procureai.models.purchase import Purchase
    from datetime import datetime, timedelta

    today = date.today().isoformat()
    next_week = (date.today() + timedelta(days=7)).isoformat()

    # Money Ramesh owes (unpaid purchases)
    purchase_result = await db.execute(
        select(
            func.sum(Purchase.total_value).label("total_payable")
        ).where(
            and_(
                Purchase.customer_id == uuid.UUID(customer_id),
                Purchase.delivery_received == True,
            )
        )
    )
    purchase_row = purchase_result.fetchone()
    total_payable = float(purchase_row.total_payable or 0)

    # Money owed to Ramesh
    receivable_result = await db.execute(
        select(
            func.sum(PaymentInvoice.outstanding_amount).label(
                "total_receivable"
            ),
            func.count(PaymentInvoice.id).label("invoice_count"),
        ).where(
            and_(
                PaymentInvoice.customer_id == uuid.UUID(customer_id),
                PaymentInvoice.is_paid == False,
            )
        )
    )
    receivable_row = receivable_result.fetchone()
    total_receivable = float(receivable_row.total_receivable or 0)
    invoice_count = int(receivable_row.invoice_count or 0)

    # Overdue receivables
    overdue_result = await db.execute(
        select(PaymentInvoice).where(
            and_(
                PaymentInvoice.customer_id == uuid.UUID(customer_id),
                PaymentInvoice.is_paid == False,
                PaymentInvoice.due_date < today,
            )
        )
    )
    overdue_invoices = overdue_result.scalars().all()
    total_overdue = sum(i.outstanding_amount for i in overdue_invoices)

    net_position = total_receivable - total_payable

    return {
        "customer_id": customer_id,
        "as_of_date": today,

        "receivables": {
            "total": total_receivable,
            "overdue": float(total_overdue),
            "overdue_count": len(overdue_invoices),
            "invoice_count": invoice_count,
        },

        "payables": {
            "total": total_payable,
        },

        "net_position": net_position,
        "net_position_label": (
            f"₹{abs(net_position):,.0f} "
            + ("surplus" if net_position >= 0 else "deficit")
        ),

        "alert": (
            f"⚠️ ₹{float(total_overdue):,.0f} overdue from "
            f"{len(overdue_invoices)} customer(s)"
            if total_overdue > 0 else
            "✅ All receivables on track"
        ),
    }


# ==============================================================================
# HELPER
# ==============================================================================

def _to_response(invoice: PaymentInvoice) -> PaymentResponse:
    return PaymentResponse(
        id=str(invoice.id),
        customer_id=str(invoice.customer_id),
        buyer_name=invoice.buyer_name,
        buyer_phone=invoice.buyer_phone,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        description=invoice.description,
        taxable_value=invoice.taxable_value,
        gst_amount=invoice.gst_amount,
        total_amount=invoice.total_amount,
        amount_received=invoice.amount_received,
        outstanding_amount=invoice.outstanding_amount,
        is_paid=invoice.is_paid,
        paid_date=invoice.paid_date,
        is_overdue=invoice.is_overdue,
        days_overdue=invoice.days_overdue,
        reminders_sent=invoice.reminders_sent,
        last_reminder_date=invoice.last_reminder_date,
    )
