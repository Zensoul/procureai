
# ==============================================================================
# PROCUREAI — tasks/itc_tasks.py
# Celery tasks for ITC reconciliation.
#
# FRESHER EXPLANATION:
# This file is intentionally thin — just 3 tasks.
# Each task does ONE thing: call the service.
#
# WHY SEPARATE TASKS FROM SERVICES?
# Services contain business logic — they work with any caller.
# Tasks contain Celery-specific code — retry logic, scheduling.
#
# This means:
# - You can call itc_service.reconcile_customer() directly in tests
#   without needing Celery running
# - You can call itc_service from an API endpoint too
# - Celery tasks are just one of many ways to trigger the service
#
# ASYNC IN CELERY:
# Celery workers are synchronous by default.
# Our services are async (they use await).
# We bridge this with asyncio.run() — runs the async function
# in a new event loop inside the synchronous Celery worker.
# ==============================================================================

import asyncio
from datetime import datetime, timezone

from celery import shared_task
from loguru import logger

from procureai.celery_app import celery_app
from procureai.database import AsyncSessionLocal
from procureai.services.itc_reconciliation import itc_service
from procureai.services.messaging import messaging_service


@celery_app.task(
    name="procureai.tasks.itc_tasks.run_itc_single",
    bind=True,
    max_retries=3,
    default_retry_delay=300,
)
def run_itc_single(
    self,
    customer_id: str,
    tax_period: str,
    force_rerun: bool = False,
) -> dict:
    """
    Run ITC reconciliation for one customer.

    bind=True gives us self — the task instance.
    self.retry() retries with exponential backoff.
    max_retries=3 means 3 attempts total before FAILED.
    """
    logger.info(
        f"[Task {self.request.id}] Starting ITC reconciliation: "
        f"customer={customer_id}, period={tax_period}"
    )

    async def _run():
        async with AsyncSessionLocal() as db:
            try:
                result = await itc_service.reconcile_customer(
                    customer_id=customer_id,
                    tax_period=tax_period,
                    db=db,
                    force_rerun=force_rerun,
                )

                from procureai.models.customer import Customer
                from sqlalchemy import select
                import uuid

                customer_result = await db.execute(
                    select(Customer).where(
                        Customer.id == uuid.UUID(customer_id)
                    )
                )
                customer = customer_result.scalar_one_or_none()

                if customer and result.has_risk:
                    alert_data = itc_service.build_alert_data(
                        customer=customer,
                        result=result,
                    )
                    if alert_data:
                        await messaging_service.send_itc_alert(
                            alert_data=alert_data,
                            db=db,
                        )

                return {
                    "status": "success",
                    "customer_id": customer_id,
                    "tax_period": tax_period,
                    "eligible_itc": str(result.eligible_itc),
                    "at_risk_itc": str(result.at_risk_itc),
                    "recovered_itc": str(result.recovered_itc),
                    "alert_sent": result.has_risk,
                }

            except ValueError as e:
                logger.info(f"[Task {self.request.id}] Skipped: {e}")
                return {
                    "status": "skipped",
                    "customer_id": customer_id,
                    "reason": str(e),
                }

            except Exception as e:
                logger.error(f"[Task {self.request.id}] Failed: {e}")
                raise self.retry(
                    exc=e,
                    countdown=300 * (2 ** self.request.retries),
                )

    return asyncio.run(_run())


@celery_app.task(
    name="procureai.tasks.itc_tasks.run_itc_batch",
    bind=True,
    max_retries=1,
)
def run_itc_batch(
    self,
    tax_period: str = None,
    cluster_filter: str = None,
) -> dict:
    """
    Run ITC reconciliation for ALL active customers.
    Called by Celery Beat on 16th of every month.

    If tax_period is None, calculates previous month automatically.
    16th September 2026 → reconciles August 2026 (082026).
    """
    if tax_period is None:
        now = datetime.now(timezone.utc)
        month = now.month - 1
        year = now.year
        if month == 0:
            month = 12
            year -= 1
        tax_period = f"{month:02d}{year}"

    logger.info(
        f"[Task {self.request.id}] Starting batch ITC "
        f"for period {tax_period}"
    )

    async def _run():
        async with AsyncSessionLocal() as db:
            return await itc_service.reconcile_all_customers(
                tax_period=tax_period,
                db=db,
                cluster_filter=cluster_filter,
            )

    results = asyncio.run(_run())

    logger.info(
        f"[Task {self.request.id}] Batch complete: "
        f"{results['success']} success, "
        f"{results['failed']} failed, "
        f"{results['skipped']} skipped"
    )

    return results


@celery_app.task(
    name="procureai.tasks.itc_tasks.calculate_itc_fee",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def calculate_itc_fee(
    self,
    customer_id: str,
    tax_period: str,
    recovered_itc: str,
) -> dict:
    """
    Calculate ProcureAI fee for ITC recovery.

    recovered_itc passed as string because Celery
    serializes to JSON which doesn't support Decimal.
    Convert inside the task: Decimal(recovered_itc)
    """
    from decimal import Decimal

    recovered = Decimal(recovered_itc)

    logger.info(
        f"[Task {self.request.id}] Calculating fee: "
        f"customer={customer_id}, recovered=Rs.{recovered:,.2f}"
    )

    fee_calc = itc_service.calculate_fee(
        customer_id=customer_id,
        tax_period=tax_period,
        recovered_itc=recovered,
    )

    logger.info(
        f"[Task {self.request.id}] Fee: "
        f"Rs.{fee_calc.fee_amount:,.2f} "
        f"(CA: Rs.{fee_calc.ca_share_amount:,.2f})"
    )

    return {
        "customer_id": customer_id,
        "tax_period": tax_period,
        "recovered_itc": str(fee_calc.recovered_itc),
        "fee_amount": str(fee_calc.fee_amount),
        "ca_share_amount": str(fee_calc.ca_share_amount),
        "net_revenue": str(fee_calc.net_revenue),
        "customer_net_benefit": str(fee_calc.customer_net_benefit),
    }