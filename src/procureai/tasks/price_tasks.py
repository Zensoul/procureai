
# ==============================================================================
# PROCUREAI — tasks/price_tasks.py
# Celery tasks for price intelligence.
#
# FRESHER EXPLANATION:
# Two tasks — both thin wrappers around the price service.
#
# TASK 1 — run_price_pulse (every Monday 9 AM IST)
# Checks all customers' recent purchases against market rates.
# Sends alert if any material is priced 8%+ above market.
# "MS Rod: You paid ₹188/kg. Market: ₹174/kg. Saving: ₹4,200"
#
# TASK 2 — check_single_customer_prices (on-demand)
# Check prices for one specific customer immediately.
# Called from API when customer requests a price check.
# ==============================================================================

import asyncio

from loguru import logger

from procureai.celery_app import celery_app
from procureai.database import get_sync_session
from procureai.services.price_engine import price_service


@celery_app.task(
    name="procureai.tasks.price_tasks.run_price_pulse",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def run_price_pulse(self) -> dict:
    """
    Run weekly price pulse for ALL active customers.
    Called by Celery Beat every Monday at 9:00 AM IST (3:30 AM UTC).

    FRESHER NOTE ON WHAT HAPPENS:
    For each active customer:
    1. Get their purchases in the last 7 days
    2. For each material — get cluster average or MCX price
    3. If they paid more than 8% above market → send alert
    4. Alert shows: material, their price, market price, saving

    Customers paying fair prices get NO alert — we only
    message when there is actionable information.
    Silence = good news. Alert = action needed.
    """
    logger.info(
        f"[Task {self.request.id}] Starting weekly price pulse"
    )

    async def _run():
        SessionLocal = get_sync_session()
        async with SessionLocal() as db:
            return await price_service.run_weekly_price_pulse(db)

    try:
        results = asyncio.run(_run())
        logger.info(
            f"[Task {self.request.id}] Price pulse complete: "
            f"{results.get('alerts_sent', 0)} alerts sent, "
            f"{results.get('no_overpayment', 0)} paying fair price, "
            f"{results.get('no_purchases', 0)} no recent purchases"
        )
        return results

    except Exception as e:
        logger.error(
            f"[Task {self.request.id}] Price pulse failed: {e}"
        )
        raise self.retry(
            exc=e,
            countdown=300 * (2 ** self.request.retries),
        )


@celery_app.task(
    name="procureai.tasks.price_tasks.check_single_customer_prices",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def check_single_customer_prices(
    self,
    customer_id: str,
    days_back: int = 7,
) -> dict:
    """
    Check prices for one specific customer immediately.

    Called from:
    - API when customer asks "am I paying fair prices?"
    - After a new purchase is logged (instant feedback)
    - Manual trigger from CA dashboard

    Args:
        customer_id: Customer UUID string
        days_back: How many days of purchases to check (default 7)

    FRESHER NOTE ON ON-DEMAND vs SCHEDULED:
    run_price_pulse runs on a schedule — every Monday for everyone.
    check_single_customer_prices runs on demand — for one customer now.

    Use case: Ramesh just logged a purchase of MS Rod at ₹195/kg.
    The router immediately triggers this task.
    Within seconds Ramesh gets:
    "MS Rod: You just paid ₹195/kg. Market avg: ₹174/kg.
    That's 12% above market. Potential saving: ₹6,300 on this order."

    Instant feedback at point of purchase — not next Monday.
    """
    logger.info(
        f"[Task {self.request.id}] Checking prices for "
        f"customer={customer_id}, days_back={days_back}"
    )

    async def _run():
        SessionLocal = get_sync_session()
        async with SessionLocal() as db:
            return await price_service.check_customer_prices(
                customer_id=customer_id,
                db=db,
                days_back=days_back,
            )

    try:
        results = asyncio.run(_run())
        logger.info(
            f"[Task {self.request.id}] Single check complete: "
            f"{results.get('purchases_checked', 0)} purchases checked, "
            f"{results.get('alerts_sent', 0)} alerts sent"
        )
        return results

    except Exception as e:
        logger.error(
            f"[Task {self.request.id}] Single check failed: {e}"
        )
        raise self.retry(exc=e)


@celery_app.task(
    name="procureai.tasks.price_tasks.get_price_history",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def get_price_history(
    self,
    material_name: str,
    cluster: str,
    weeks: int = 12,
) -> dict:
    """
    Get 12-week price history for a material in a cluster.

    Used by CA dashboard to show price trends to clients.
    "MS Rod in Bommasandra has gone up 6% in 12 weeks
    while MCX steel index only went up 2%. Your suppliers
    are increasing margins faster than the market."

    FRESHER NOTE ON TREND DATA:
    Raw price comparison tells you if you're overpaying today.
    Trend data tells you if the problem is getting worse.
    Both together give Ramesh ammunition for supplier negotiation.
    """
    logger.info(
        f"[Task {self.request.id}] Getting price history: "
        f"{material_name} in {cluster}, {weeks} weeks"
    )

    async def _run():
        SessionLocal = get_sync_session()
        async with SessionLocal() as db:
            history = await price_service.get_price_history(
                material_name=material_name,
                cluster=cluster,
                db=db,
                weeks=weeks,
            )
            return {
                "material_name": material_name,
                "cluster": cluster,
                "weeks": weeks,
                "data_points": len(history),
                "history": history,
            }

    try:
        return asyncio.run(_run())
    except Exception as e:
        logger.error(
            f"[Task {self.request.id}] Price history failed: {e}"
        )
        raise self.retry(exc=e)