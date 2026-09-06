# ==============================================================================
# PROCUREAI — routers/internal.py
# Internal API endpoints for system operations.
#
# FRESHER EXPLANATION:
# These endpoints are NOT for customers or CAs.
# They are called by:
# 1. Celery Beat — to trigger scheduled jobs
# 2. MSG91 webhooks — when suppliers reply to SMS
# 3. RCS webhooks — when customers tap buttons on RCS cards
# 4. Your monitoring tools — health checks
# 5. You — manual testing and debugging
#
# SECURITY:
# All endpoints except /health require a service token.
# The service token is a long-lived JWT stored in your environment.
# n8n and Celery use this token in the Authorization header.
# No public access — these endpoints are for internal systems only.
#
# WHY SEPARATE FROM OTHER ROUTERS:
# Customer-facing endpoints have one security model (customer JWT).
# Internal endpoints have a different model (service token).
# Keeping them separate makes security auditing easier.
# ==============================================================================

from datetime import datetime, timezone
from typing import Optional

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, Header, HTTPException, status
from loguru import logger
from pydantic import BaseModel

from procureai.celery_app import celery_app
from procureai.config import get_settings
from procureai.database import check_database_health, get_db
from sqlalchemy.ext.asyncio import AsyncSession
from procureai.tasks.delivery_tasks import (
    mark_delivery_confirmed,
    mark_delivery_received,
    run_delivery_check,
    run_supplier_followup,
)
from procureai.tasks.itc_tasks import run_itc_batch, run_itc_single
from procureai.tasks.price_tasks import run_price_pulse

settings = get_settings()

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
)


# ==============================================================================
# SERVICE TOKEN AUTHENTICATION
#
# FRESHER NOTE ON API SECURITY:
# Public APIs (customers, CAs) use JWT tokens that expire after 60 minutes.
# Internal APIs use a service token — a fixed secret known only to
# internal systems. It never expires but is never exposed publicly.
#
# In production:
# - n8n stores the service token in its credentials vault
# - Celery Beat passes it in every request header
# - Railway environment variables hold the actual value
#
# Anyone without this token gets HTTP 401 Unauthorized.
# ==============================================================================

async def verify_service_token(
    authorization: str = Header(..., description="Bearer <service_token>"),
) -> bool:
    """
    Verify the internal service token.
    Called as a dependency on all protected internal endpoints.

    FRESHER NOTE ON HEADER DEPENDENCY:
    Header(...) tells FastAPI to read the Authorization header.
    If the header is missing → 422 error automatically.
    If the token is wrong → we raise 401 manually.

    The service token is stored in SECRET_KEY for simplicity.
    In production you'd use a separate INTERNAL_SERVICE_TOKEN env var.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization format. Use: Bearer <token>",
        )

    token = authorization.replace("Bearer ", "")

    # Compare against service token
    # In production: compare against INTERNAL_SERVICE_TOKEN env var
    if token != settings.secret_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service token.",
        )

    return True


# ==============================================================================
# REQUEST/RESPONSE SCHEMAS FOR INTERNAL ENDPOINTS
# Simple schemas — no need for separate schema file
# ==============================================================================

class ITCRunRequest(BaseModel):
    tax_period: Optional[str] = None
    customer_id: Optional[str] = None
    cluster_filter: Optional[str] = None
    force_rerun: bool = False


class TaskResponse(BaseModel):
    task_id: str
    status: str
    message: str


class HealthResponse(BaseModel):
    status: str
    database: bool
    redis: bool
    timestamp: str
    environment: str


class WebhookSMSReply(BaseModel):
    """
    MSG91 sends this when a supplier replies to our SMS.

    FRESHER NOTE ON WEBHOOKS:
    A webhook is a URL that receives automatic notifications.
    When our SMS says "Reply YES to confirm delivery"
    and the supplier texts back YES, MSG91 sends a POST
    to this endpoint with the reply details.
    We then update the delivery status in our database.
    """
    sender: str          # Supplier's phone number
    message: str         # Their reply text ("YES", "NO", etc.)
    request_id: str      # MSG91 message ID
    received_at: Optional[str] = None


class WebhookRCSReply(BaseModel):
    """
    MSG91 sends this when a customer taps a button on an RCS card.

    When Ramesh taps [Check Details] on the ITC alert RCS card,
    MSG91 sends this webhook with postback_data="itc_details".
    """
    sender: str           # Customer's phone number
    postback_data: str    # Which button was tapped
    request_id: str


# ==============================================================================
# HEALTH CHECK — NO AUTH REQUIRED
# ==============================================================================

@router.get(
    "/health",
    response_model=HealthResponse,
    summary="System health check",
)
async def health_check() -> HealthResponse:
    """
    Check system health — database and Redis connectivity.

    FRESHER NOTE:
    This endpoint has NO authentication — it's public.
    Uptime Robot pings this every 5 minutes.
    If it returns anything other than 200, you get an SMS alert.

    We check:
    1. Database: can we connect and run SELECT 1?
    2. Redis: can we ping the Redis server?

    Both must be True for the system to be healthy.
    """
    db_healthy = await check_database_health()

    redis_healthy = False
    try:
        from redis import Redis
        r = Redis.from_url(settings.redis_url)
        redis_healthy = r.ping()
        r.close()
    except Exception as e:
        logger.warning(f"Redis health check failed: {e}")

    overall_status = "healthy" if (db_healthy and redis_healthy) else "degraded"

    return HealthResponse(
        status=overall_status,
        database=db_healthy,
        redis=redis_healthy,
        timestamp=datetime.now(timezone.utc).isoformat(),
        environment=settings.app_env,
    )


# ==============================================================================
# ITC RECONCILIATION TRIGGERS
# ==============================================================================

@router.post(
    "/run-itc-monthly",
    response_model=TaskResponse,
    summary="Trigger monthly ITC reconciliation for all customers",
    dependencies=[Depends(verify_service_token)],
)
async def trigger_itc_monthly(
    request: ITCRunRequest,
) -> TaskResponse:
    """
    Trigger ITC reconciliation for all customers.
    Called by Celery Beat on the 16th of every month.
    Can also be triggered manually for testing.

    FRESHER NOTE ON .delay():
    task.delay(*args) sends the task to Redis queue.
    It returns immediately with a task_id.
    The actual work happens in the Celery worker.
    The caller can check progress with GET /internal/task-status/{task_id}
    """
    task = run_itc_batch.delay(
        tax_period=request.tax_period,
        cluster_filter=request.cluster_filter,
    )

    logger.info(
        f"ITC batch triggered: task_id={task.id}, "
        f"period={request.tax_period or 'auto'}"
    )

    return TaskResponse(
        task_id=task.id,
        status="queued",
        message=f"ITC reconciliation queued for "
                f"period {request.tax_period or 'previous month'}. "
                f"Check status at /internal/task-status/{task.id}",
    )


@router.post(
    "/run-itc-single",
    response_model=TaskResponse,
    summary="Trigger ITC reconciliation for one customer",
    dependencies=[Depends(verify_service_token)],
)
async def trigger_itc_single(
    request: ITCRunRequest,
) -> TaskResponse:
    """
    Trigger ITC reconciliation for one specific customer.
    Used for manual re-runs when a customer reports a discrepancy.
    """
    if not request.customer_id or not request.tax_period:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="customer_id and tax_period are required for single reconciliation.",
        )

    task = run_itc_single.delay(
        customer_id=request.customer_id,
        tax_period=request.tax_period,
        force_rerun=request.force_rerun,
    )

    logger.info(
        f"ITC single triggered: task_id={task.id}, "
        f"customer={request.customer_id}"
    )

    return TaskResponse(
        task_id=task.id,
        status="queued",
        message=f"ITC reconciliation queued for customer "
                f"{request.customer_id}. "
                f"Check status at /internal/task-status/{task.id}",
    )


# ==============================================================================
# DELIVERY CHECK TRIGGERS
# ==============================================================================

@router.post(
    "/run-delivery-check",
    response_model=TaskResponse,
    summary="Trigger daily delivery check",
    dependencies=[Depends(verify_service_token)],
)
async def trigger_delivery_check() -> TaskResponse:
    """
    Trigger delivery overdue check for all customers.
    Called by Celery Beat daily at 8:00 AM IST.
    """
    task = run_delivery_check.delay()

    logger.info(f"Delivery check triggered: task_id={task.id}")

    return TaskResponse(
        task_id=task.id,
        status="queued",
        message=f"Delivery check queued. "
                f"Check status at /internal/task-status/{task.id}",
    )


@router.post(
    "/run-supplier-followup",
    response_model=TaskResponse,
    summary="Trigger supplier follow-up messages",
    dependencies=[Depends(verify_service_token)],
)
async def trigger_supplier_followup() -> TaskResponse:
    """
    Send follow-up messages to suppliers with upcoming deliveries.
    Called by Celery Beat daily at 9:30 AM IST.
    """
    task = run_supplier_followup.delay()

    logger.info(f"Supplier followup triggered: task_id={task.id}")

    return TaskResponse(
        task_id=task.id,
        status="queued",
        message=f"Supplier follow-up queued. "
                f"Check status at /internal/task-status/{task.id}",
    )

@router.post(
    "/run-morning-brief",
    response_model=TaskResponse,
    summary="Send morning brief to all customers",
    dependencies=[Depends(verify_service_token)],
)
async def trigger_morning_brief(
    db: AsyncSession = Depends(get_db),
) -> TaskResponse:
    """
    Send daily morning brief to all customers with Telegram.
    Runs synchronously — no Celery worker needed.
    """
    from procureai.integrations.telegram_bot import telegram_client
    from procureai.models.customer import Customer
    from procureai.models.itc import ITCTracking
    from procureai.models.purchase import Purchase
    from sqlalchemy import select, and_
    from datetime import date
    import uuid as uuid_lib

    today = date.today().isoformat()
    current_month = date.today().strftime("%m%Y")

    result = await db.execute(
        select(Customer).where(
            and_(
                Customer.is_active == True,
                Customer.telegram_chat_id.isnot(None),
            )
        )
    )
    customers = result.scalars().all()

    sent = 0
    failed = 0

    for customer in customers:
        try:
            itc_result = await db.execute(
                select(ITCTracking).where(
                    and_(
                        ITCTracking.customer_id == customer.id,
                        ITCTracking.tax_period == current_month,
                    )
                )
            )
            itc_record = itc_result.scalar_one_or_none()
            itc_at_risk = float(itc_record.at_risk_itc) if itc_record else 0

            delivery_result = await db.execute(
                select(Purchase).where(
                    and_(
                        Purchase.customer_id == customer.id,
                        Purchase.expected_delivery_date <= today,
                        Purchase.delivery_received == False,
                    )
                )
            )
            overdue = delivery_result.scalars().all()

            await telegram_client.send_morning_brief(
                chat_id=customer.telegram_chat_id,
                owner_name=customer.owner_name,
                itc_at_risk=itc_at_risk,
                overdue_deliveries=len(overdue),
            )
            sent += 1
            logger.info(f"Morning brief sent to {customer.owner_name}")

        except Exception as e:
            logger.error(f"Morning brief failed for {customer.gstin}: {e}")
            failed += 1
            continue

    return TaskResponse(
        task_id="sync-execution",
        status="completed",
        message=f"Morning brief sent to {sent} customers. "
                f"{failed} failed.",
    )

# ==============================================================================
# PRICE PULSE TRIGGER
# ==============================================================================

@router.post(
    "/run-price-pulse",
    response_model=TaskResponse,
    summary="Trigger weekly price pulse",
    dependencies=[Depends(verify_service_token)],
)
async def trigger_price_pulse() -> TaskResponse:
    """
    Trigger weekly price pulse for all customers.
    Called by Celery Beat every Monday at 9:00 AM IST.
    """
    task = run_price_pulse.delay()

    logger.info(f"Price pulse triggered: task_id={task.id}")

    return TaskResponse(
        task_id=task.id,
        status="queued",
        message=f"Price pulse queued. "
                f"Check status at /internal/task-status/{task.id}",
    )



@router.post(
    "/run-gem-scan",
    response_model=TaskResponse,
    summary="Trigger GeM tender scan for all customers",
    dependencies=[Depends(verify_service_token)],
)
async def trigger_gem_scan(
    db: AsyncSession = Depends(get_db),
) -> TaskResponse:
    """
    Scan GeM portal for eligible tenders.
    Runs synchronously — sends Telegram alerts immediately.
    Called by GitHub Actions daily at 7 AM IST.
    """
    from procureai.services.gem_service import gem_service

    results = await gem_service.scan_all_customers(db)

    return TaskResponse(
        task_id="sync-gem-scan",
        status="completed",
        message=(
            f"GeM scan complete. "
            f"{results['tenders_found']} tenders found, "
            f"{results['alerts_sent']} alerts sent."
        ),
    )


# ==============================================================================
# TASK STATUS
# ==============================================================================

@router.get(
    "/task-status/{task_id}",
    summary="Check Celery task status",
    dependencies=[Depends(verify_service_token)],
)
async def get_task_status(task_id: str) -> dict:
    """
    Check the status of a Celery task.

    FRESHER NOTE ON CELERY TASK STATES:
    PENDING  — task received, not yet started
    STARTED  — worker picked it up, running now
    SUCCESS  — completed successfully
    FAILURE  — crashed, check result for error
    RETRY    — failed, being retried

    Use this after triggering a job to check if it completed.
    The Flower dashboard (port 5555) shows this visually.
    """
    task_result = AsyncResult(task_id, app=celery_app)

    response = {
        "task_id": task_id,
        "status": task_result.status,
    }

    if task_result.successful():
        response["result"] = task_result.result
    elif task_result.failed():
        response["error"] = str(task_result.result)

    return response


# ==============================================================================
# WEBHOOKS — MSG91 CALLBACKS
# ==============================================================================

@router.post(
    "/webhook/sms-reply",
    summary="Receive supplier SMS replies from MSG91",
    status_code=status.HTTP_200_OK,
)
async def receive_sms_reply(payload: WebhookSMSReply) -> dict:
    """
    Receive incoming SMS replies from MSG91.

    When our automated SMS asks:
    "Your delivery of 500kg MS Rod is due Thursday. Reply YES to confirm."

    And the supplier replies "YES" — MSG91 sends this webhook.
    We then mark the delivery as confirmed automatically.

    FRESHER NOTE ON WEBHOOK SECURITY:
    MSG91 webhooks don't use JWT tokens.
    In production, validate the webhook using MSG91's
    webhook signature header to ensure the request
    is genuinely from MSG91 and not a spoofed request.
    For now we process all incoming webhooks — add
    signature validation before going live.
    """
    sender_phone = payload.sender.strip()
    reply_text = payload.message.strip().upper()

    logger.info(
        f"SMS reply received from {sender_phone}: '{reply_text}'"
    )

    if reply_text in ["YES", "Y", "CONFIRM", "HAA", "HAAN"]:
        # Supplier confirmed delivery
        # Find the purchase order for this supplier phone
        # and mark as confirmed
        mark_delivery_confirmed.delay(
            purchase_id="",  # TODO: lookup by supplier phone
        )
        logger.info(
            f"Delivery confirmation received from {sender_phone}"
        )
        return {"status": "confirmed", "action": "delivery_confirmed"}

    elif reply_text in ["NO", "N", "CANCEL", "ILLA"]:
        logger.info(
            f"Delivery rejection received from {sender_phone}"
        )
        return {"status": "noted", "action": "delivery_rejected"}

    return {"status": "received", "action": "no_action"}


@router.post(
    "/webhook/rcs-reply",
    summary="Receive RCS button taps from MSG91",
    status_code=status.HTTP_200_OK,
)
async def receive_rcs_reply(payload: WebhookRCSReply) -> dict:
    """
    Receive RCS card button taps from MSG91.

    When Ramesh taps a button on an RCS card:
    [Check Details] → postback_data = "itc_details"
    [Dismiss]       → postback_data = "itc_dismiss"
    [Call Supplier] → postback_data = "call_<phone>"
    [Mark Received] → postback_data = "delivery_received"

    FRESHER NOTE ON POSTBACK DATA:
    When we create RCS buttons we set postbackData.
    When the user taps the button, MSG91 sends us that
    exact postbackData value in this webhook.
    We use it to determine what action to take.
    """
    sender_phone = payload.sender.strip()
    action = payload.postback_data.strip()

    logger.info(
        f"RCS reply from {sender_phone}: action='{action}'"
    )

    if action == "delivery_received":
        mark_delivery_received.delay(
            purchase_id="",  # TODO: lookup by customer phone
        )
        return {"status": "noted", "action": "delivery_marked_received"}

    elif action.startswith("call_"):
        supplier_phone = action.replace("call_", "")
        logger.info(
            f"Customer {sender_phone} wants to call supplier {supplier_phone}"
        )
        return {"status": "noted", "action": "call_initiated"}

    elif action == "itc_dismiss":
        return {"status": "noted", "action": "alert_dismissed"}

    elif action == "itc_details":
        return {"status": "noted", "action": "details_requested"}

    return {"status": "received", "action": "unknown"}


# ==============================================================================
# TELEGRAM WEBHOOK
# ==============================================================================

@router.post(
    "/webhook/telegram",
    summary="Receive Telegram bot updates",
    status_code=status.HTTP_200_OK,
)
async def receive_telegram_update(
    payload: dict,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Receive incoming Telegram messages and button taps.
    Handles onboarding conversation flow.
    """
    from procureai.integrations.telegram_bot import telegram_client
    from procureai.models.customer import Customer
    from sqlalchemy import select
    import uuid as uuid_lib
    import re

    try:
        if "message" in payload:
            message = payload["message"]
            chat_id = message["chat"]["id"]
            text = message.get("text", "").strip()
            first_name = message["chat"].get("first_name", "")

            if text == "/start":
                await telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🏭 <b>ProcureAI ge Swagata, {first_name}!</b>\n\n"
                        "Naanu ninna procurement intelligence assistant.\n\n"
                        "<b>Enrol maadalu, kelida details kali:</b>\n\n"
                        "<code>Owner Hesaru\n"
                        "Business Hesaru\n"
                        "GSTIN\n"
                        "Mobile Number\n"
                        "Area (Bommasandra/Peenya/Electronic City)</code>\n\n"
                        "<b>Example:</b>\n"
                        "<code>Ramesh Kumar\n"
                        "Ramesh Auto Parts\n"
                        "29ABCDE1234F1Z5\n"
                        "9845123456\n"
                        "Bommasandra</code>\n\n"
                        "Ee format li reply maadi! 👆"
                    )
                )
                return {"status": "welcome_sent"}

            if text.startswith("/help"):
                await telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        "🆘 <b>Help</b>\n\n"
                        "/start — Enrol maadalu\n"
                        "/itc — ITC status nodi\n"
                        "/delivery — Pending deliveries\n"
                        "/price — Market rates\n"
                        "/status — Account status\n\n"
                        "Invoice PDF iddare forward maadi!"
                    )
                )
                return {"status": "help_sent"}

            if text.startswith("/status"):
                result = await db.execute(
                    select(Customer).where(
                        Customer.telegram_chat_id == chat_id
                    )
                )
                customer = result.scalar_one_or_none()
                if customer:
                    await telegram_client.send_message(
                        chat_id=chat_id,
                        text=(
                            f"✅ <b>Account Active</b>\n\n"
                            f"👤 {customer.owner_name}\n"
                            f"🏭 {customer.business_name}\n"
                            f"📋 GSTIN: {customer.gstin}\n"
                            f"📍 Cluster: {customer.cluster or 'Not set'}\n\n"
                            "Ninna account sari ide!"
                        )
                    )
                else:
                    await telegram_client.send_message(
                        chat_id=chat_id,
                        text="❌ Account found aagilla.\n\n/start send maadi enrol maadalu."
                    )
                return {"status": "status_sent"}

            if text.startswith("/itc"):
                result = await db.execute(
                    select(Customer).where(
                        Customer.telegram_chat_id == chat_id
                    )
                )
                customer = result.scalar_one_or_none()
                if not customer:
                    await telegram_client.send_message(
                        chat_id=chat_id,
                        text="❌ Enrol maadilla. /start send maadi."
                    )
                    return {"status": "not_enrolled"}

                from procureai.models.itc import ITCTracking
                from datetime import date
                from sqlalchemy import and_
                current_month = date.today().strftime("%m%Y")
                itc_result = await db.execute(
                    select(ITCTracking).where(
                        and_(
                            ITCTracking.customer_id == customer.id,
                            ITCTracking.tax_period == current_month,
                        )
                    )
                )
                itc_record = itc_result.scalar_one_or_none()
                if itc_record:
                    await telegram_client.send_message(
                        chat_id=chat_id,
                        text=(
                            f"📊 <b>ITC Status</b>\n\n"
                            f"📅 {itc_record.period_display}\n"
                            f"💰 Eligible: ₹{itc_record.eligible_itc:,.0f}\n"
                            f"✅ Matched: ₹{itc_record.matched_itc:,.0f}\n"
                            f"⚠️ At Risk: ₹{itc_record.at_risk_itc:,.0f}\n"
                            f"🎉 Recovered: ₹{itc_record.recovered_itc:,.0f}"
                        )
                    )
                else:
                    await telegram_client.send_message(
                        chat_id=chat_id,
                        text=(
                            "📊 <b>ITC Status</b>\n\n"
                            "Ee thingli inna reconciliation aagilla.\n"
                            "16th ge automatically run aaguthey."
                        )
                    )
                return {"status": "itc_sent"}

            # Onboarding data — 4+ lines
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if len(lines) >= 4:
                owner_name = lines[0]
                business_name = lines[1]
                gstin = lines[2].upper().strip()
                phone = lines[3].strip()
                cluster_raw = lines[4].lower().strip() if len(lines) > 4 else "bommasandra"

                cluster_map = {
                    "bommasandra": "bommasandra",
                    "peenya": "peenya",
                    "electronic city": "electronic_city",
                    "electronic_city": "electronic_city",
                    "whitefield": "whitefield",
                }
                cluster = cluster_map.get(cluster_raw, cluster_raw)

                gstin_pattern = r'^\d{2}[A-Z]{5}\d{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$'
                if not re.match(gstin_pattern, gstin):
                    await telegram_client.send_message(
                        chat_id=chat_id,
                        text=(
                            "❌ <b>GSTIN format tappu ide.</b>\n\n"
                            "Example: <code>29ABCDE1234F1Z5</code>\n\n"
                            "Correct maadi meelu try maadi."
                        )
                    )
                    return {"status": "invalid_gstin"}

                phone_clean = phone.replace("+91", "").replace(" ", "").replace("-", "")
                if not re.match(r'^[6-9]\d{9}$', phone_clean):
                    await telegram_client.send_message(
                        chat_id=chat_id,
                        text=(
                            "❌ <b>Mobile number tappu ide.</b>\n\n"
                            "Example: <code>9845123456</code>"
                        )
                    )
                    return {"status": "invalid_phone"}

                # Check if already enrolled
                existing = await db.execute(
                    select(Customer).where(Customer.gstin == gstin)
                )
                existing_customer = existing.scalar_one_or_none()

                if existing_customer:
                    if not existing_customer.telegram_chat_id:
                        existing_customer.telegram_chat_id = chat_id
                        db.add(existing_customer)
                        await db.flush()
                    await telegram_client.send_message(
                        chat_id=chat_id,
                        text=(
                            f"✅ <b>Ninna account iddey ide!</b>\n\n"
                            f"👤 {existing_customer.owner_name}\n"
                            f"🏭 {existing_customer.business_name}\n\n"
                            "Telegram connected aayitu!"
                        )
                    )
                    return {"status": "already_enrolled"}

                # Enrol new customer
                customer = Customer(
                    owner_name=owner_name,
                    business_name=business_name,
                    gstin=gstin,
                    phone=phone_clean,
                    preferred_channel="telegram",
                    preferred_language="kn",
                    cluster=cluster,
                    telegram_chat_id=chat_id,
                )
                db.add(customer)
                await db.flush()

                logger.info(
                    f"New customer enrolled via Telegram: "
                    f"{gstin} ({business_name})"
                )

                await telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ <b>Enrolment successful!</b>\n\n"
                        f"👤 {owner_name}\n"
                        f"🏭 {business_name}\n"
                        f"📋 GSTIN: {gstin}\n"
                        f"📍 {cluster.title()}\n\n"
                        f"<b>Nimma services:</b>\n"
                        f"☀️ Daily morning brief — 8 AM\n"
                        f"💰 Monthly ITC reconciliation — 16th\n"
                        f"🏛️ GeM tender alerts — daily\n"
                        f"🚚 Delivery tracking\n"
                        f"📊 Price benchmarking\n\n"
                        f"Naaleyu beleige ninna first brief barthey!\n\n"
                        f"— ProcureAI"
                    )
                )

                # Send sample morning brief immediately
                await telegram_client.send_morning_brief(
                    chat_id=chat_id,
                    owner_name=owner_name,
                    itc_at_risk=0,
                    overdue_deliveries=0,
                )

                return {
                    "status": "enrolled",
                    "customer_id": str(customer.id)
                }

            # Document upload
            if "document" in message:
                result = await db.execute(
                    select(Customer).where(
                        Customer.telegram_chat_id == chat_id
                    )
                )
                customer = result.scalar_one_or_none()
                if not customer:
                    await telegram_client.send_message(
                        chat_id=chat_id,
                        text="❌ Enrol maadilla. /start send maadi."
                    )
                    return {"status": "not_enrolled"}

                file_name = message["document"].get("file_name", "invoice.pdf")
                await telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        f"📄 <b>{file_name} tilikondide!</b>\n\n"
                        "Invoice data extract maadtha ide...\n"
                        "2-3 nimisha wait maadi. ⏳"
                    )
                )
                return {"status": "document_received"}

            # Unknown message
            result = await db.execute(
                select(Customer).where(
                    Customer.telegram_chat_id == chat_id
                )
            )
            customer = result.scalar_one_or_none()
            if not customer:
                await telegram_client.send_message(
                    chat_id=chat_id,
                    text="Namaskara! Enrol maadalu /start send maadi."
                )
            else:
                await telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        f"Namaskara {customer.owner_name.split()[0]} avare!\n\n"
                        "/itc — ITC status\n"
                        "/status — Account details\n"
                        "/help — Full help"
                    )
                )
            return {"status": "message_handled"}

        # Callback queries (button taps)
        if "callback_query" in payload:
            callback = payload["callback_query"]
            chat_id = callback["message"]["chat"]["id"]
            data = callback.get("data", "")
            callback_id = callback["id"]

            from procureai.integrations.telegram_bot import telegram_client
            await telegram_client.answer_callback_query(
                callback_query_id=callback_id,
                text="✅ Noted!",
            )

            if data == "itc_called":
                await telegram_client.send_message(
                    chat_id=chat_id,
                    text="✅ Supplier ge call maadidira! Good luck.",
                )
            elif data == "itc_dismiss":
                await telegram_client.send_message(
                    chat_id=chat_id,
                    text="Dismissed. Next month check maadthivi.",
                )
            elif data == "delivery_received":
                await telegram_client.send_message(
                    chat_id=chat_id,
                    text="✅ Delivery received mark aayitu!",
                )
            elif data.startswith("gem_interested_"):
                bid_number = data.replace("gem_interested_", "")
                await telegram_client.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ <b>Interested noted!</b>\n\n"
                        f"Bid: {bid_number}\n\n"
                        "GeM portal li apply maadi:\n"
                        "👉 https://gem.gov.in"
                    ),
                )
            elif data.startswith("gem_dismiss_"):
                await telegram_client.send_message(
                    chat_id=chat_id,
                    text="Dismissed. Mattonde tender barthey! 👍",
                )

            return {"status": "callback_handled"}

    except Exception as e:
        logger.error(f"Telegram webhook error: {e}")

    return {"status": "ok"}    
