
# ==============================================================================
# PROCUREAI — routers/customers.py
# API endpoints for MSME customer management.
#
# FRESHER EXPLANATION:
# A router is like a receptionist — it receives requests,
# checks they're valid, passes them to the right service,
# and returns the response.
#
# The router does NOT contain business logic.
# It only does three things:
# 1. Validate incoming request (Pydantic schema does this)
# 2. Call the appropriate service or database query
# 3. Return the response in the correct format
#
# WHO CALLS THESE ENDPOINTS:
# - CA dashboard: enrol customers, view ITC history
# - n8n workflows: trigger reconciliation, get customer list
# - Webhook handlers: update delivery status
# - You: testing and debugging
# ==============================================================================

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from procureai.database import get_db
from procureai.models.customer import Customer
from procureai.models.itc import ITCTracking
from procureai.schemas.customer import (
    CustomerCreate,
    CustomerListResponse,
    CustomerResponse,
    CustomerUpdate,
)
from procureai.schemas.itc import ITCTrackingResponse


# ==============================================================================
# ROUTER SETUP
#
# APIRouter groups related endpoints together.
# prefix="/customers" means all routes start with /customers
# tags=["customers"] groups them in the API docs at /docs
# ==============================================================================

router = APIRouter(
    prefix="/customers",
    tags=["customers"],
)


# ==============================================================================
# ENROL A NEW CUSTOMER
# ==============================================================================

@router.post(
    "/",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Enrol a new MSME customer",
    description="Creates a new customer record for an MSME owner.",
)
async def enrol_customer(
    customer_data: CustomerCreate,
    db: AsyncSession = Depends(get_db),
) -> CustomerResponse:
    """
    Enrol a new MSME owner in ProcureAI.

    FRESHER NOTE ON DEPENDS(get_db):
    get_db is a FastAPI dependency — it provides a database session.
    FastAPI calls get_db() automatically before calling this function.
    The session is committed after the function returns (or rolled
    back if an exception is raised). We defined this in database.py.

    FRESHER NOTE ON status_code=201:
    HTTP 200 = OK (request succeeded)
    HTTP 201 = Created (new resource was created)
    We use 201 for POST endpoints that create new records.
    It's more specific than 200 and tells the caller
    "a new resource was created" not just "request succeeded".
    """
    # Check if GSTIN already enrolled
    existing = await db.execute(
        select(Customer).where(
            Customer.gstin == customer_data.gstin
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Customer with GSTIN {customer_data.gstin} "
                   f"already enrolled.",
        )

    # Create new customer
    customer = Customer(
        owner_name=customer_data.owner_name,
        business_name=customer_data.business_name,
        gstin=customer_data.gstin,
        phone=customer_data.phone,
        email=customer_data.email,
        preferred_channel=customer_data.preferred_channel,
        preferred_language=customer_data.preferred_language,
        telegram_chat_id=customer_data.telegram_chat_id,
        cluster=customer_data.cluster,
        udyam_number=customer_data.udyam_number,
        turnover_band=customer_data.turnover_band,
        product_categories=customer_data.product_categories,
        ca_id=uuid.UUID(customer_data.ca_id) if customer_data.ca_id else None,
    )

    db.add(customer)
    await db.flush()  # Get the generated ID without committing

    logger.info(
        f"New customer enrolled: {customer.gstin} "
        f"({customer.business_name})"
    )

    return CustomerResponse(
        id=str(customer.id),
        created_at=customer.created_at,
        owner_name=customer.owner_name,
        business_name=customer.business_name,
        gstin=customer.gstin,
        phone=customer.phone,
        email=customer.email,
        preferred_channel=customer.preferred_channel,
        preferred_language=customer.preferred_language,
        telegram_chat_id=customer.telegram_chat_id,
        cluster=customer.cluster,
        udyam_number=customer.udyam_number,
        turnover_band=customer.turnover_band,
        product_categories=customer.product_categories,
        is_active=customer.is_active,
        is_trial=customer.is_trial,
        onboarded_at=customer.onboarded_at,
    )


# ==============================================================================
# LIST ALL CUSTOMERS
# ==============================================================================

@router.get(
    "/",
    response_model=CustomerListResponse,
    summary="List all enrolled customers",
)
async def list_customers(
    page: int = Query(default=1, ge=1, description="Page number"),
    per_page: int = Query(default=20, ge=1, le=100, description="Results per page"),
    cluster: Optional[str] = Query(default=None, description="Filter by cluster"),
    is_active: Optional[bool] = Query(default=None, description="Filter by active status"),
    db: AsyncSession = Depends(get_db),
) -> CustomerListResponse:
    """
    List all enrolled customers with pagination.

    FRESHER NOTE ON QUERY PARAMETERS:
    Query parameters appear after ? in the URL:
    GET /customers?page=2&per_page=10&cluster=bommasandra

    Query(default=1, ge=1) means:
    - Default value: 1
    - ge=1: must be greater than or equal to 1
    - le=100: must be less than or equal to 100
    FastAPI validates these automatically.
    """
    # Build query with optional filters
    query = select(Customer)
    count_query = select(func.count(Customer.id))

    if cluster:
        query = query.where(Customer.cluster == cluster)
        count_query = count_query.where(Customer.cluster == cluster)

    if is_active is not None:
        query = query.where(Customer.is_active == is_active)
        count_query = count_query.where(Customer.is_active == is_active)

    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Apply pagination
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)
    query = query.order_by(Customer.created_at.desc())

    result = await db.execute(query)
    customers = result.scalars().all()

    items = [
        CustomerResponse(
            id=str(c.id),
            created_at=c.created_at,
            owner_name=c.owner_name,
            business_name=c.business_name,
            gstin=c.gstin,
            phone=c.phone,
            email=c.email,
            preferred_channel=c.preferred_channel,
            preferred_language=c.preferred_language,
            telegram_chat_id=c.telegram_chat_id,
            cluster=c.cluster,
            udyam_number=c.udyam_number,
            turnover_band=c.turnover_band,
            product_categories=c.product_categories,
            is_active=c.is_active,
            is_trial=c.is_trial,
            onboarded_at=c.onboarded_at,
        )
        for c in customers
    ]

    return CustomerListResponse(
        total=total,
        page=page,
        per_page=per_page,
        items=items,
    )


# ==============================================================================
# GET ONE CUSTOMER
# ==============================================================================

@router.get(
    "/{customer_id}",
    response_model=CustomerResponse,
    summary="Get customer details",
)
async def get_customer(
    customer_id: str,
    db: AsyncSession = Depends(get_db),
) -> CustomerResponse:
    """
    Get details for one customer by ID.

    FRESHER NOTE ON PATH PARAMETERS:
    {customer_id} in the route becomes a function argument.
    GET /customers/abc-123 → customer_id = "abc-123"
    FastAPI extracts it automatically.
    """
    customer = await _get_customer_or_404(customer_id, db)

    return CustomerResponse(
        id=str(customer.id),
        created_at=customer.created_at,
        owner_name=customer.owner_name,
        business_name=customer.business_name,
        gstin=customer.gstin,
        phone=customer.phone,
        email=customer.email,
        preferred_channel=customer.preferred_channel,
        preferred_language=customer.preferred_language,
        telegram_chat_id=customer.telegram_chat_id,
        cluster=customer.cluster,
        udyam_number=customer.udyam_number,
        turnover_band=customer.turnover_band,
        product_categories=customer.product_categories,
        is_active=customer.is_active,
        is_trial=customer.is_trial,
        onboarded_at=customer.onboarded_at,
    )


# ==============================================================================
# UPDATE CUSTOMER
# ==============================================================================

@router.patch(
    "/{customer_id}",
    response_model=CustomerResponse,
    summary="Update customer details",
)
async def update_customer(
    customer_id: str,
    update_data: CustomerUpdate,
    db: AsyncSession = Depends(get_db),
) -> CustomerResponse:
    """
    Update customer details.
    Only sends fields you want to change — all fields optional.

    FRESHER NOTE ON PATCH vs PUT:
    PUT replaces the entire resource — you send ALL fields.
    PATCH updates partial fields — you send only what changes.

    PATCH is better for mobile apps and bots that only
    update one thing at a time (e.g. just the phone number).
    """
    customer = await _get_customer_or_404(customer_id, db)

    # Update only fields that were sent (not None)
    update_dict = update_data.model_dump(exclude_none=True)

    for field, value in update_dict.items():
        if hasattr(customer, field):
            setattr(customer, field, value)

    db.add(customer)
    await db.flush()

    logger.info(
        f"Customer updated: {customer.gstin} "
        f"fields: {list(update_dict.keys())}"
    )

    return CustomerResponse(
        id=str(customer.id),
        created_at=customer.created_at,
        owner_name=customer.owner_name,
        business_name=customer.business_name,
        gstin=customer.gstin,
        phone=customer.phone,
        email=customer.email,
        preferred_channel=customer.preferred_channel,
        preferred_language=customer.preferred_language,
        telegram_chat_id=customer.telegram_chat_id,
        cluster=customer.cluster,
        udyam_number=customer.udyam_number,
        turnover_band=customer.turnover_band,
        product_categories=customer.product_categories,
        is_active=customer.is_active,
        is_trial=customer.is_trial,
        onboarded_at=customer.onboarded_at,
    )


# ==============================================================================
# DEACTIVATE CUSTOMER
# ==============================================================================

@router.delete(
    "/{customer_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deactivate a customer",
)
async def deactivate_customer(
    customer_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Deactivate a customer — stops all alerts and reconciliation.

    FRESHER NOTE:
    We never DELETE customer records from the database.
    We set is_active=False — called "soft delete".
    Why? Because their purchase history and ITC records
    are financial data that must be preserved for audit.
    Deleting would break the audit trail.

    HTTP 204 = No Content — success but nothing to return.
    Used for DELETE endpoints where there's nothing to send back.
    """
    customer = await _get_customer_or_404(customer_id, db)

    customer.is_active = False
    db.add(customer)

    logger.info(
        f"Customer deactivated: {customer.gstin} "
        f"({customer.business_name})"
    )


# ==============================================================================
# GET ITC HISTORY FOR CUSTOMER
# ==============================================================================

@router.get(
    "/{customer_id}/itc",
    response_model=list[ITCTrackingResponse],
    summary="Get ITC history for a customer",
)
async def get_customer_itc_history(
    customer_id: str,
    limit: int = Query(default=12, ge=1, le=24),
    db: AsyncSession = Depends(get_db),
) -> list[ITCTrackingResponse]:
    """
    Get ITC reconciliation history for a customer.
    Returns up to 12 months of ITC data by default.

    Used by:
    - CA dashboard: show client's ITC recovery over time
    - Customer report: "Here's what we recovered for you this year"
    - Fee invoicing: calculate total fees owed
    """
    await _get_customer_or_404(customer_id, db)

    result = await db.execute(
        select(ITCTracking)
        .where(ITCTracking.customer_id == uuid.UUID(customer_id))
        .order_by(ITCTracking.tax_period.desc())
        .limit(limit)
    )
    records = result.scalars().all()

    return [
        ITCTrackingResponse(
            id=str(r.id),
            customer_id=str(r.customer_id),
            tax_period=r.tax_period,
            period_display=r.period_display,
            created_at=r.created_at,
            eligible_itc=r.eligible_itc,
            matched_itc=r.matched_itc,
            at_risk_itc=r.at_risk_itc,
            recovered_itc=r.recovered_itc,
            missing_invoice_count=r.missing_invoice_count,
            fee_rate=r.fee_rate,
            fee_amount=r.fee_amount,
            fee_invoiced=r.fee_invoiced,
            fee_paid=r.fee_paid,
            ca_share_amount=r.ca_share_amount,
            ca_share_paid=r.ca_share_paid,
            reconciliation_status=r.reconciliation_status,
            last_reconciled_at=r.last_reconciled_at,
            alert_sent_at=r.alert_sent_at,
        )
        for r in records
    ]


# ==============================================================================
# PRIVATE HELPERS
# ==============================================================================

async def _get_customer_or_404(
    customer_id: str,
    db: AsyncSession,
) -> Customer:
    """
    Load customer by ID or raise 404 if not found.

    FRESHER NOTE ON HTTP 404:
    404 = Not Found. The resource doesn't exist.
    We raise HTTPException(404) when a customer ID doesn't exist.
    FastAPI catches this and returns a proper JSON error response:
    {"detail": "Customer not found: abc-123"}

    This helper is used by multiple endpoints — defined once,
    used everywhere. DRY principle — Don't Repeat Yourself.
    """
    try:
        customer_uuid = uuid.UUID(customer_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid customer ID format: {customer_id}",
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