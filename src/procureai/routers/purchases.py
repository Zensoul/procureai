# ==============================================================================
# PROCUREAI — routers/purchases.py
# API endpoints for purchase invoice management.
#
# FRESHER EXPLANATION:
# This router handles the INPUT side of ProcureAI.
# Before we can tell Ramesh "you're overpaying for MS Rod"
# we need to know what he paid. That data comes through here.
#
# THREE INPUT METHODS:
# 1. POST /purchases — single invoice, manual data entry
# 2. POST /purchases/upload-pdf — upload PDF, Claude extracts data
# 3. POST /purchases/bulk — multiple invoices at once
#
# AFTER EACH PURCHASE IS LOGGED:
# We immediately trigger a price check (Celery task).
# Ramesh gets instant feedback if he overpaid.
# Not next Monday — right now.
# ==============================================================================

import base64
import uuid
from decimal import Decimal
from typing import Optional

import anthropic
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from loguru import logger
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from procureai.config import get_settings
from procureai.database import get_db
from procureai.models.customer import Customer
from procureai.models.purchase import Purchase
from procureai.schemas.purchase import (
    PurchaseCreate,
    PurchaseResponse,
    PurchaseUpdate,
)
from procureai.tasks.price_tasks import check_single_customer_prices

settings = get_settings()

router = APIRouter(
    prefix="/purchases",
    tags=["purchases"],
)


# ==============================================================================
# LOG A SINGLE PURCHASE INVOICE
# ==============================================================================

@router.post(
    "/",
    response_model=PurchaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Log a purchase invoice",
)
async def log_purchase(
    purchase_data: PurchaseCreate,
    db: AsyncSession = Depends(get_db),
) -> PurchaseResponse:
    """
    Log a new supplier invoice.

    After logging, immediately triggers a price check.
    If the price is 8%+ above market — customer gets alerted.

    FRESHER NOTE ON IMMEDIATE PRICE CHECK:
    We don't wait until Monday for price feedback.
    The moment a purchase is logged, we check the price.
    If Ramesh paid ₹195/kg for MS Rod (market is ₹174),
    he gets an alert within 60 seconds of logging the purchase.
    This is more useful than a weekly summary.
    """
    # Verify customer exists
    customer = await _get_customer_or_404(
        purchase_data.customer_id, db
    )

    # Check for duplicate invoice
    existing = await db.execute(
        select(Purchase).where(
            and_(
                Purchase.customer_id == uuid.UUID(purchase_data.customer_id),
                Purchase.supplier_gstin == purchase_data.supplier_gstin,
                Purchase.invoice_number == purchase_data.invoice_number,
            )
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Invoice {purchase_data.invoice_number} from "
                   f"{purchase_data.supplier_gstin} already logged.",
        )

    # Create purchase record
    purchase = Purchase(
        customer_id=uuid.UUID(purchase_data.customer_id),
        supplier_gstin=purchase_data.supplier_gstin,
        supplier_name=purchase_data.supplier_name,
        supplier_phone=purchase_data.supplier_phone,
        invoice_number=purchase_data.invoice_number,
        invoice_date=purchase_data.invoice_date,
        tax_period=purchase_data.tax_period,
        material_name=purchase_data.material_name,
        material_grade=purchase_data.material_grade,
        hsn_code=purchase_data.hsn_code,
        quantity=purchase_data.quantity,
        unit=purchase_data.unit,
        unit_price=purchase_data.unit_price,
        taxable_value=purchase_data.taxable_value,
        gst_rate=purchase_data.gst_rate,
        gst_amount=purchase_data.gst_amount,
        total_value=purchase_data.total_value,
        itc_eligible=purchase_data.itc_eligible,
        data_source=purchase_data.data_source,
        source_filename=purchase_data.source_filename,
        expected_delivery_date=purchase_data.expected_delivery_date,
    )

    db.add(purchase)
    await db.flush()

    logger.info(
        f"Purchase logged: {customer.gstin} — "
        f"{purchase.supplier_name} {purchase.material_name} "
        f"₹{purchase.unit_price}/kg"
    )

    # Trigger immediate price check in background
    # Customer gets price alert within 60 seconds if overpaying
    check_single_customer_prices.delay(
        customer_id=str(customer.id),
        days_back=1,  # Only check today's purchase
    )

    return _purchase_to_response(purchase)


# ==============================================================================
# UPLOAD PDF INVOICE
# ==============================================================================

@router.post(
    "/upload-pdf",
    response_model=PurchaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload PDF invoice — Claude extracts data automatically",
)
async def upload_pdf_invoice(
    customer_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> PurchaseResponse:
    """
    Upload a supplier PDF invoice.
    Claude API extracts all purchase data automatically.

    FRESHER NOTE ON HOW THIS WORKS:
    1. Ramesh forwards the PDF to our system
    2. We send the PDF to Claude API
    3. Claude reads the invoice and extracts:
       - Supplier name and GSTIN
       - Invoice number and date
       - Material name, quantity, unit price
       - GST rate and amount
    4. We validate the extracted data
    5. We log it as a purchase record

    This means Ramesh never types anything.
    He just forwards the PDF — we do the rest.

    FRESHER NOTE ON FILE UPLOAD:
    UploadFile is FastAPI's file upload handler.
    File(...) means the file is required.
    The file content is read as bytes: await file.read()
    We then base64-encode it to send to Claude API.
    """
    # Verify customer exists
    customer = await _get_customer_or_404(customer_id, db)

    # Validate file type
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only PDF files are accepted.",
        )

    # Read file content
    pdf_bytes = await file.read()

    if len(pdf_bytes) > 10 * 1024 * 1024:  # 10MB limit
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="PDF file too large. Maximum size: 10MB",
        )

    logger.info(
        f"PDF upload received: {file.filename} "
        f"({len(pdf_bytes)} bytes) for customer {customer_id}"
    )

    # Extract data using Claude API
    extracted_data = await _extract_invoice_from_pdf(
        pdf_bytes=pdf_bytes,
        filename=file.filename,
    )

    if not extracted_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Could not extract invoice data from PDF. "
                   "Please ensure it is a valid supplier invoice.",
        )

    # Determine tax period from invoice date
    tax_period = _date_to_tax_period(extracted_data.get("invoice_date", ""))

    # Build purchase create schema with extracted data
    try:
        purchase_data = PurchaseCreate(
            customer_id=customer_id,
            supplier_gstin=extracted_data.get("supplier_gstin", ""),
            supplier_name=extracted_data.get("supplier_name", ""),
            invoice_number=extracted_data.get("invoice_number", ""),
            invoice_date=extracted_data.get("invoice_date", ""),
            tax_period=tax_period,
            material_name=extracted_data.get("material_name", ""),
            material_grade=extracted_data.get("material_grade"),
            hsn_code=extracted_data.get("hsn_code"),
            quantity=Decimal(str(extracted_data.get("quantity", 0))),
            unit=extracted_data.get("unit", "nos"),
            unit_price=Decimal(str(extracted_data.get("unit_price", 0))),
            gst_rate=int(extracted_data.get("gst_rate", 18)),
            data_source="pdf_upload",
            source_filename=file.filename,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Extracted data validation failed: {str(e)}",
        )

    # Create purchase record
    purchase = Purchase(
        customer_id=uuid.UUID(customer_id),
        supplier_gstin=purchase_data.supplier_gstin,
        supplier_name=purchase_data.supplier_name,
        invoice_number=purchase_data.invoice_number,
        invoice_date=purchase_data.invoice_date,
        tax_period=purchase_data.tax_period,
        material_name=purchase_data.material_name,
        material_grade=purchase_data.material_grade,
        hsn_code=purchase_data.hsn_code,
        quantity=purchase_data.quantity,
        unit=purchase_data.unit,
        unit_price=purchase_data.unit_price,
        taxable_value=purchase_data.taxable_value,
        gst_rate=purchase_data.gst_rate,
        gst_amount=purchase_data.gst_amount,
        total_value=purchase_data.total_value,
        data_source="pdf_upload",
        source_filename=file.filename,
        data_notes=f"Extracted by Claude AI from {file.filename}",
    )

    db.add(purchase)
    await db.flush()

    logger.info(
        f"PDF invoice processed: {purchase.invoice_number} "
        f"from {purchase.supplier_name} "
        f"for {customer.gstin}"
    )

    return _purchase_to_response(purchase)


# ==============================================================================
# LIST PURCHASES FOR A CUSTOMER
# ==============================================================================

@router.get(
    "/customer/{customer_id}",
    response_model=list[PurchaseResponse],
    summary="List purchases for a customer",
)
async def list_customer_purchases(
    customer_id: str,
    tax_period: Optional[str] = Query(
        default=None,
        description="Filter by tax period MMYYYY"
    ),
    in_gstr2b: Optional[bool] = Query(
        default=None,
        description="Filter by GSTR-2B match status"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[PurchaseResponse]:
    """
    List all purchases for a customer.
    Optionally filter by tax period or GSTR-2B status.

    FRESHER NOTE ON USE CASES:
    - CA runs reconciliation: filter by tax_period="082026"
    - Show unmatched invoices: filter by in_gstr2b=False
    - Full purchase history: no filters
    """
    await _get_customer_or_404(customer_id, db)

    query = select(Purchase).where(
        Purchase.customer_id == uuid.UUID(customer_id)
    )

    if tax_period:
        query = query.where(Purchase.tax_period == tax_period)

    if in_gstr2b is not None:
        query = query.where(Purchase.in_gstr2b == in_gstr2b)

    query = query.order_by(
        Purchase.invoice_date.desc()
    ).limit(limit)

    result = await db.execute(query)
    purchases = result.scalars().all()

    return [_purchase_to_response(p) for p in purchases]


# ==============================================================================
# UPDATE PURCHASE
# ==============================================================================

@router.patch(
    "/{purchase_id}",
    response_model=PurchaseResponse,
    summary="Update purchase details",
)
async def update_purchase(
    purchase_id: str,
    update_data: PurchaseUpdate,
    db: AsyncSession = Depends(get_db),
) -> PurchaseResponse:
    """
    Update purchase details.

    Common use cases:
    - Add expected delivery date
    - Mark delivery as confirmed or received
    - Update GSTR-2B match status after manual check
    """
    purchase = await _get_purchase_or_404(purchase_id, db)

    update_dict = update_data.model_dump(exclude_none=True)
    for field, value in update_dict.items():
        if hasattr(purchase, field):
            setattr(purchase, field, value)

    db.add(purchase)
    await db.flush()

    return _purchase_to_response(purchase)


# ==============================================================================
# GET UNMATCHED INVOICES (ITC AT RISK)
# ==============================================================================

@router.get(
    "/customer/{customer_id}/at-risk",
    response_model=list[PurchaseResponse],
    summary="Get invoices not yet in GSTR-2B — ITC at risk",
)
async def get_at_risk_invoices(
    customer_id: str,
    tax_period: Optional[str] = Query(
        default=None,
        description="Filter by tax period"
    ),
    db: AsyncSession = Depends(get_db),
) -> list[PurchaseResponse]:
    """
    Get all purchase invoices not yet appearing in GSTR-2B.
    These represent ITC at risk.

    Used by the CA dashboard to show clients their exposure.
    "You have 3 invoices not in GSTR-2B totalling ₹59,000 at risk."
    """
    await _get_customer_or_404(customer_id, db)

    query = select(Purchase).where(
        and_(
            Purchase.customer_id == uuid.UUID(customer_id),
            Purchase.itc_eligible == True,
            Purchase.in_gstr2b == False,
        )
    )

    if tax_period:
        query = query.where(Purchase.tax_period == tax_period)

    query = query.order_by(Purchase.gst_amount.desc())

    result = await db.execute(query)
    purchases = result.scalars().all()

    return [_purchase_to_response(p) for p in purchases]


# ==============================================================================
# PRIVATE HELPERS
# ==============================================================================

async def _extract_invoice_from_pdf(
    pdf_bytes: bytes,
    filename: str,
) -> Optional[dict]:
    """
    Use Claude API to extract structured data from PDF invoice.

    FRESHER NOTE:
    This is the only place in Phase 1 where we use Claude API directly.
    We send the PDF and ask Claude to extract specific fields.
    Claude returns JSON which we parse into a Python dict.
    """
    try:
        client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key
        )

        pdf_base64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

        message = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_base64,
                        }
                    },
                    {
                        "type": "text",
                        "text": """Extract the following from this Indian supplier invoice.
Return ONLY valid JSON, no explanation, no markdown:
{
  "supplier_name": "business name of the seller",
  "supplier_gstin": "15-char GSTIN of seller",
  "invoice_number": "invoice number",
  "invoice_date": "YYYY-MM-DD format",
  "material_name": "main item purchased",
  "material_grade": "grade/specification if mentioned",
  "hsn_code": "HSN/SAC code if mentioned",
  "quantity": numeric value only,
  "unit": "kg/nos/mtr/ltr",
  "unit_price": numeric value before GST,
  "gst_rate": numeric rate like 5/12/18/28,
  "total_gst": numeric GST amount,
  "total_value": numeric total including GST
}
If any field is not found, use null."""
                    }
                ]
            }]
        )

        response_text = message.content[0].text.strip()

        # Remove markdown code blocks if present
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            response_text = "\n".join(lines[1:-1])

        import json
        extracted = json.loads(response_text)

        logger.info(
            f"Claude extracted invoice data from {filename}: "
            f"supplier={extracted.get('supplier_name')}, "
            f"inv={extracted.get('invoice_number')}"
        )

        return extracted

    except Exception as e:
        logger.error(f"Claude PDF extraction failed for {filename}: {e}")
        return None


def _date_to_tax_period(invoice_date: str) -> str:
    """
    Convert invoice date to tax period.
    "2026-08-15" → "082026"
    """
    try:
        parts = invoice_date.split("-")
        year = parts[0]
        month = parts[1]
        return f"{month}{year}"
    except Exception:
        from datetime import datetime
        now = datetime.now()
        return f"{now.month:02d}{now.year}"


async def _get_customer_or_404(
    customer_id: str,
    db: AsyncSession,
) -> Customer:
    """Load customer or raise 404."""
    try:
        customer_uuid = uuid.UUID(customer_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid customer ID: {customer_id}",
        )

    result = await db.execute(
        select(Customer).where(Customer.id == customer_uuid)
    )
    customer = result.scalar_one_or_none()

    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer not found: {customer_id}",
        )
    return customer


async def _get_purchase_or_404(
    purchase_id: str,
    db: AsyncSession,
) -> Purchase:
    """Load purchase or raise 404."""
    try:
        purchase_uuid = uuid.UUID(purchase_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid purchase ID: {purchase_id}",
        )

    result = await db.execute(
        select(Purchase).where(Purchase.id == purchase_uuid)
    )
    purchase = result.scalar_one_or_none()

    if not purchase:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Purchase not found: {purchase_id}",
        )
    return purchase


def _purchase_to_response(purchase: Purchase) -> PurchaseResponse:
    """Convert Purchase model to PurchaseResponse schema."""
    return PurchaseResponse(
        id=str(purchase.id),
        customer_id=str(purchase.customer_id),
        created_at=purchase.created_at,
        supplier_gstin=purchase.supplier_gstin,
        supplier_name=purchase.supplier_name,
        supplier_phone=purchase.supplier_phone,
        invoice_number=purchase.invoice_number,
        invoice_date=purchase.invoice_date,
        tax_period=purchase.tax_period,
        material_name=purchase.material_name,
        material_grade=purchase.material_grade,
        hsn_code=purchase.hsn_code,
        quantity=purchase.quantity,
        unit=purchase.unit,
        unit_price=purchase.unit_price,
        taxable_value=purchase.taxable_value,
        gst_rate=purchase.gst_rate,
        gst_amount=purchase.gst_amount,
        total_value=purchase.total_value,
        in_gstr2b=purchase.in_gstr2b,
        itc_eligible=purchase.itc_eligible,
        expected_delivery_date=purchase.expected_delivery_date,
        delivery_confirmed=purchase.delivery_confirmed,
        delivery_received=purchase.delivery_received,
        actual_delivery_date=purchase.actual_delivery_date,
        data_source=purchase.data_source,
    )
