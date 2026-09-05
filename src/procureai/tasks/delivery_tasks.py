
# ==============================================================================
# PROCUREAI — tasks/delivery_tasks.py
# Celery tasks for delivery tracking.
#
# FRESHER EXPLANATION:
# Two tasks — both thin wrappers around the delivery service.
#
# TASK 1 — run_delivery_check (daily 8 AM IST)
# Finds overdue deliveries and alerts MSME owners.
# "Sharma Metals MS Rod is 2 days late."
#
# TASK 2 — run_supplier_followup (daily 9:30 AM IST)
# Finds deliveries due in next 48 hours and messages suppliers.
# "Your delivery of 500kg MS Rod is due Thursday. Reply YES to confirm."
# ==============================================================================

import asyncio

from loguru import logger

from procureai.celery_app import celery_app
from procureai.database import AsyncSessionLocal
from procureai.services.delivery_tracker import delivery_service


@celery_app.task(
    name="procureai.tasks.delivery_tasks.run_delivery_check",
    bind=True,
    max_retries=3,
    default_retry_delay=120,
)
def run_delivery_check(self) -> dict:
    """
    Check all overdue deliveries and alert owners.
    Called by Celery Beat every day at 8:00 AM IST (2:30 AM UTC).

    FRESHER NOTE:
    This task runs AFTER deliveries should have arrived.
    It's the reactive check — finds problems that already occurred.

    If 30 customers have a total of 5 overdue deliveries,
    this task sends 5 alerts to the relevant customers.
    Each customer gets ONE consolidated alert even if they
    have multiple overdue deliveries.
    """
    logger.info(
        f"[Task {self.request.id}] Starting delivery check"
    )

    async def _run():
        async with AsyncSessionLocal() as db:
            return await delivery_service.check_and_alert_owners(db)

    try:
        results = asyncio.run(_run())
        logger.info(
            f"[Task {self.request.id}] Delivery check complete: "
            f"{results.get('customers_alerted', 0)} customers alerted, "
            f"{results.get('alerts_sent', 0)} deliveries flagged"
        )
        return results

    except Exception as e:
        logger.error(
            f"[Task {self.request.id}] Delivery check failed: {e}"
        )
        raise self.retry(
            exc=e,
            countdown=120 * (2 ** self.request.retries),
        )


@celery_app.task(
    name="procureai.tasks.delivery_tasks.run_supplier_followup",
    bind=True,
    max_retries=3,
    default_retry_delay=120,
)
def run_supplier_followup(self) -> dict:
    """
    Send automated follow-up messages to suppliers.
    Called by Celery Beat every day at 9:30 AM IST (4:00 AM UTC).

    FRESHER NOTE:
    This task runs BEFORE deliveries are due.
    It's the preventive check — stops problems before they happen.

    For every purchase order due in the next 48 hours
    where the supplier hasn't confirmed:
    → Send SMS to supplier: "Please confirm your delivery"

    If supplier confirms → delivery_confirmed = True → no owner alert
    If supplier ignores → owner gets alert next morning from run_delivery_check
    """
    logger.info(
        f"[Task {self.request.id}] Starting supplier follow-up"
    )

    async def _run():
        async with AsyncSessionLocal() as db:
            return await delivery_service.check_and_followup_suppliers(db)

    try:
        results = asyncio.run(_run())
        logger.info(
            f"[Task {self.request.id}] Supplier follow-up complete: "
            f"{results.get('followups_sent', 0)} sent, "
            f"{results.get('failed', 0)} failed"
        )
        return results

    except Exception as e:
        logger.error(
            f"[Task {self.request.id}] Supplier follow-up failed: {e}"
        )
        raise self.retry(
            exc=e,
            countdown=120 * (2 ** self.request.retries),
        )


@celery_app.task(
    name="procureai.tasks.delivery_tasks.mark_delivery_confirmed",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def mark_delivery_confirmed(self, purchase_id: str) -> dict:
    """
    Mark a delivery as confirmed by the supplier.

    Called when:
    - Supplier replies YES to our automated SMS
    - Owner taps Confirmed button on RCS card
    - Webhook receives supplier confirmation

    FRESHER NOTE ON WEBHOOKS:
    When our SMS says "Reply YES to confirm delivery"
    and the supplier replies YES, MSG91 sends a POST request
    to our API endpoint /webhooks/sms-reply.
    The router receives it and calls this Celery task.
    The task updates the database asynchronously.
    The supplier gets an instant acknowledgment while
    the database update happens in the background.
    """
    logger.info(
        f"[Task {self.request.id}] Marking delivery confirmed: "
        f"purchase={purchase_id}"
    )

    async def _run():
        async with AsyncSessionLocal() as db:
            purchase = await delivery_service.mark_delivery_confirmed(
                purchase_id=purchase_id,
                db=db,
            )
            return {
                "status": "confirmed",
                "purchase_id": purchase_id,
                "supplier": purchase.supplier_name,
                "material": purchase.material_name,
                "expected_date": purchase.expected_delivery_date,
            }

    try:
        return asyncio.run(_run())
    except Exception as e:
        logger.error(
            f"[Task {self.request.id}] Mark confirmed failed: {e}"
        )
        raise self.retry(exc=e)


@celery_app.task(
    name="procureai.tasks.delivery_tasks.mark_delivery_received",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def mark_delivery_received(
    self,
    purchase_id: str,
    actual_date: str = None,
) -> dict:
    """
    Mark a delivery as physically received at the factory.

    Called when owner taps Received on RCS card
    or sends RECEIVED via SMS reply.

    This closes the delivery loop and feeds the
    supplier reliability scorecard.
    """
    logger.info(
        f"[Task {self.request.id}] Marking delivery received: "
        f"purchase={purchase_id}"
    )

    async def _run():
        async with AsyncSessionLocal() as db:
            purchase = await delivery_service.mark_delivery_received(
                purchase_id=purchase_id,
                actual_date=actual_date,
                db=db,
            )
            return {
                "status": "received",
                "purchase_id": purchase_id,
                "supplier": purchase.supplier_name,
                "material": purchase.material_name,
                "expected_date": purchase.expected_delivery_date,
                "actual_date": purchase.actual_delivery_date,
            }

    try:
        return asyncio.run(_run())
    except Exception as e:
        logger.error(
            f"[Task {self.request.id}] Mark received failed: {e}"
        )
        raise self.retry(exc=e)