
# ==============================================================================
# PROCUREAI — services/delivery_tracker.py
# Delivery Tracking Service — the core of Feature 2.
#
# FRESHER EXPLANATION:
# This service prevents shop floor stoppages by monitoring
# every open purchase order and alerting before problems occur.
#
# TWO JOBS:
#
# JOB 1 — Supplier Follow-up (daily 9:30 AM)
# "Delivery due in 48 hours. Has supplier confirmed?"
# If NO → send automated message to supplier
# "Dear Sharma Metals, your delivery of 500kg MS Rod is
#  due Thursday. Please confirm. Reply YES."
#
# JOB 2 — Owner Alert (daily 8:00 AM)
# "Are any deliveries overdue or unconfirmed?"
# If YES → alert Ramesh
# "Sharma Metals MS Rod delivery is 2 days overdue."
#
# WHY TWO SEPARATE JOBS:
# Job 1 runs BEFORE the delivery is due — preventive.
# Job 2 runs AFTER the delivery should have arrived — reactive.
# Together they cover both sides of the delivery window.
# ==============================================================================

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from loguru import logger
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from procureai.integrations.msg91 import msg91_client
from procureai.models.customer import Customer
from procureai.models.purchase import Purchase
from procureai.schemas.purchase import PurchaseUpdate


# ==============================================================================
# DELIVERY STATUS THRESHOLDS
# How many days before delivery do we send the follow-up?
# How many days overdue before we escalate?
# ==============================================================================

FOLLOWUP_DAYS_BEFORE = 2    # Send follow-up 48 hours before delivery
ALERT_DAYS_OVERDUE = 0      # Alert owner same day as missed delivery
ESCALATE_DAYS_OVERDUE = 2   # Escalate if still not received after 2 days


class DeliveryTrackerService:
    """
    Monitors purchase order delivery status and sends alerts.

    FRESHER NOTE:
    This service has two entry points:
    1. check_and_followup_suppliers() — called daily at 9:30 AM
       Sends automated messages to suppliers about upcoming deliveries
    2. check_and_alert_owners() — called daily at 8:00 AM
       Alerts MSME owners about overdue or unconfirmed deliveries
    """

    async def check_and_followup_suppliers(
        self, db: AsyncSession
    ) -> dict:
        """
        Find all deliveries due in next 48 hours and
        send automated follow-up to suppliers.

        Called by Celery Beat at 9:30 AM daily.

        FRESHER NOTE ON DATE ARITHMETIC:
        today = 2026-09-05
        cutoff = today + 2 days = 2026-09-07

        We find all purchases where:
        - expected_delivery_date is between today and cutoff
        - delivery_confirmed = False (supplier hasn't confirmed)
        - delivery_received = False (not yet received)
        """
        today = date.today().isoformat()
        cutoff = (date.today() + timedelta(days=FOLLOWUP_DAYS_BEFORE)).isoformat()

        logger.info(
            f"Checking supplier follow-ups: "
            f"deliveries due between {today} and {cutoff}"
        )

        # Find pending deliveries due in next 48 hours
        result = await db.execute(
            select(Purchase).where(
                and_(
                    Purchase.expected_delivery_date.isnot(None),
                    Purchase.expected_delivery_date >= today,
                    Purchase.expected_delivery_date <= cutoff,
                    Purchase.delivery_confirmed == False,
                    Purchase.delivery_received == False,
                )
            )
        )
        pending_deliveries = result.scalars().all()

        logger.info(
            f"Found {len(pending_deliveries)} deliveries "
            f"needing supplier follow-up"
        )

        results = {
            "total": len(pending_deliveries),
            "followups_sent": 0,
            "failed": 0,
        }

        for purchase in pending_deliveries:
            try:
                await self._send_supplier_followup(purchase)
                results["followups_sent"] += 1

            except Exception as e:
                logger.error(
                    f"Failed to send follow-up for purchase "
                    f"{purchase.id}: {e}"
                )
                results["failed"] += 1
                continue

        logger.info(
            f"Supplier follow-up complete: "
            f"{results['followups_sent']} sent, "
            f"{results['failed']} failed"
        )

        return results

    async def check_and_alert_owners(
        self, db: AsyncSession
    ) -> dict:
        """
        Find all overdue or unconfirmed deliveries and
        alert the MSME owner.

        Called by Celery Beat at 8:00 AM daily.

        We alert the owner when:
        1. Delivery date has passed but material not received
        2. Delivery date is today but supplier hasn't confirmed
        """
        today = date.today().isoformat()

        logger.info(
            f"Checking overdue deliveries for {today}"
        )

        # Find overdue deliveries
        result = await db.execute(
            select(Purchase).where(
                and_(
                    Purchase.expected_delivery_date.isnot(None),
                    Purchase.expected_delivery_date <= today,
                    Purchase.delivery_received == False,
                    or_(
                        Purchase.delivery_confirmed == False,
                        Purchase.expected_delivery_date < today,
                    )
                )
            )
        )
        overdue_purchases = result.scalars().all()

        logger.info(
            f"Found {len(overdue_purchases)} overdue deliveries"
        )

        if not overdue_purchases:
            return {"total": 0, "alerts_sent": 0, "failed": 0}

        # Group by customer — one alert per customer
        # with all their overdue deliveries listed
        customer_deliveries: dict[str, list[Purchase]] = {}
        for purchase in overdue_purchases:
            cid = str(purchase.customer_id)
            if cid not in customer_deliveries:
                customer_deliveries[cid] = []
            customer_deliveries[cid].append(purchase)

        results = {
            "total": len(overdue_purchases),
            "alerts_sent": 0,
            "failed": 0,
            "customers_alerted": 0,
        }

        for customer_id, purchases in customer_deliveries.items():
            try:
                # Load customer
                customer = await self._get_customer(customer_id, db)
                if not customer or not customer.is_active:
                    continue

                # Send one consolidated alert per customer
                await self._send_owner_delivery_alert(
                    customer, purchases, today
                )
                results["alerts_sent"] += len(purchases)
                results["customers_alerted"] += 1

            except Exception as e:
                logger.error(
                    f"Failed to alert customer {customer_id}: {e}"
                )
                results["failed"] += 1
                continue

        logger.info(
            f"Owner alerts complete: "
            f"{results['customers_alerted']} customers alerted, "
            f"{results['alerts_sent']} deliveries flagged"
        )

        return results

    async def mark_delivery_confirmed(
        self,
        purchase_id: str,
        db: AsyncSession,
    ) -> Purchase:
        """
        Mark a delivery as confirmed by the supplier.

        Called when:
        1. Supplier replies YES to our follow-up SMS
        2. Owner taps "Confirmed" button on RCS card
        3. n8n webhook receives supplier reply

        FRESHER NOTE:
        When the supplier replies "YES" to our SMS,
        MSG91 sends a webhook to our API.
        The router receives it and calls this method.
        This is called "two-way SMS" — we send, they reply,
        we process the reply automatically.
        """
        import uuid as uuid_lib

        result = await db.execute(
            select(Purchase).where(
                Purchase.id == uuid_lib.UUID(purchase_id)
            )
        )
        purchase = result.scalar_one_or_none()

        if not purchase:
            raise ValueError(f"Purchase not found: {purchase_id}")

        purchase.delivery_confirmed = True
        db.add(purchase)
        await db.flush()

        logger.info(
            f"Delivery confirmed: {purchase.supplier_name} "
            f"{purchase.material_name} — {purchase.expected_delivery_date}"
        )

        return purchase

    async def mark_delivery_received(
        self,
        purchase_id: str,
        actual_date: Optional[str],
        db: AsyncSession,
    ) -> Purchase:
        """
        Mark a delivery as physically received at the factory.

        Called when owner taps "Received" on the RCS alert
        or sends "RECEIVED" via SMS.

        This closes the delivery tracking loop and feeds
        the supplier reliability scorecard.
        """
        import uuid as uuid_lib

        result = await db.execute(
            select(Purchase).where(
                Purchase.id == uuid_lib.UUID(purchase_id)
            )
        )
        purchase = result.scalar_one_or_none()

        if not purchase:
            raise ValueError(f"Purchase not found: {purchase_id}")

        today = date.today().isoformat()

        purchase.delivery_received = True
        purchase.delivery_confirmed = True
        purchase.actual_delivery_date = actual_date or today
        db.add(purchase)
        await db.flush()

        # Calculate if delivery was on time or late
        days_diff = self._calculate_days_difference(
            purchase.expected_delivery_date,
            purchase.actual_delivery_date,
        )

        if days_diff > 0:
            logger.info(
                f"Late delivery received: {purchase.supplier_name} "
                f"was {days_diff} days late"
            )
        else:
            logger.info(
                f"On-time delivery received: {purchase.supplier_name}"
            )

        return purchase

    async def get_pending_deliveries(
        self,
        customer_id: str,
        db: AsyncSession,
    ) -> list[Purchase]:
        """
        Get all pending deliveries for a customer.
        Used by the customer-facing dashboard and weekly brief.
        """
        import uuid as uuid_lib

        result = await db.execute(
            select(Purchase).where(
                and_(
                    Purchase.customer_id == uuid_lib.UUID(customer_id),
                    Purchase.expected_delivery_date.isnot(None),
                    Purchase.delivery_received == False,
                )
            ).order_by(Purchase.expected_delivery_date)
        )
        return result.scalars().all()

    async def get_supplier_reliability_score(
        self,
        customer_id: str,
        supplier_gstin: str,
        db: AsyncSession,
    ) -> dict:
        """
        Calculate reliability score for a specific supplier.

        Score based on:
        - On-time delivery rate
        - Confirmation rate (did they respond to follow-ups?)
        - Average days late (when they are late)

        FRESHER NOTE:
        This data feeds the supplier scorecard feature —
        "Sharma Metals: 87% on-time, avg 1.2 days late when delayed"
        Ramesh uses this to decide which suppliers to prioritise.
        """
        import uuid as uuid_lib

        result = await db.execute(
            select(Purchase).where(
                and_(
                    Purchase.customer_id == uuid_lib.UUID(customer_id),
                    Purchase.supplier_gstin == supplier_gstin,
                    Purchase.expected_delivery_date.isnot(None),
                    Purchase.delivery_received == True,
                )
            )
        )
        completed_deliveries = result.scalars().all()

        if not completed_deliveries:
            return {
                "supplier_gstin": supplier_gstin,
                "total_deliveries": 0,
                "on_time_count": 0,
                "on_time_rate": None,
                "avg_days_late": None,
                "score": None,
            }

        on_time = 0
        late_days = []

        for delivery in completed_deliveries:
            if not delivery.actual_delivery_date:
                continue

            days_diff = self._calculate_days_difference(
                delivery.expected_delivery_date,
                delivery.actual_delivery_date,
            )

            if days_diff <= 0:
                on_time += 1
            else:
                late_days.append(days_diff)

        total = len(completed_deliveries)
        on_time_rate = (on_time / total * 100) if total > 0 else 0
        avg_late = (
            sum(late_days) / len(late_days) if late_days else 0
        )

        # Simple reliability score out of 100
        # 100% on-time = 100 score
        # Each day of average lateness = -5 points
        score = max(0, on_time_rate - (avg_late * 5))

        return {
            "supplier_gstin": supplier_gstin,
            "total_deliveries": total,
            "on_time_count": on_time,
            "on_time_rate": round(on_time_rate, 1),
            "avg_days_late": round(avg_late, 1),
            "score": round(score, 1),
        }

    # ==========================================================================
    # PRIVATE HELPERS
    # ==========================================================================

    async def _send_supplier_followup(
        self, purchase: Purchase
    ) -> None:
        """
        Send automated follow-up message to supplier.

        FRESHER NOTE:
        We send to the supplier's phone, not the owner's phone.
        The supplier gets an SMS — not RCS — because
        suppliers are not our customers and may have basic phones.
        """
        if not purchase.supplier_phone:
            logger.warning(
                f"No phone for supplier {purchase.supplier_name} — "
                f"skipping follow-up"
            )
            return

        logger.info(
            f"Sending follow-up to {purchase.supplier_name} "
            f"({purchase.supplier_phone}): "
            f"{purchase.material_name} due {purchase.expected_delivery_date}"
        )

        await msg91_client.send_supplier_followup(
            supplier_phone=purchase.supplier_phone,
            supplier_name=purchase.supplier_name,
            customer_business_name="Your Customer",
            material_name=purchase.material_name,
            quantity=float(purchase.quantity),
            unit=purchase.unit,
            expected_date=purchase.expected_delivery_date,
        )

    async def _send_owner_delivery_alert(
        self,
        customer: Customer,
        overdue_purchases: list[Purchase],
        today: str,
    ) -> None:
        """
        Send delivery overdue alert to MSME owner.

        If only one delivery is overdue — specific alert.
        If multiple — consolidated summary alert.

        FRESHER NOTE ON USER EXPERIENCE:
        One alert per delivery would be annoying.
        If Ramesh has 3 overdue deliveries, he gets ONE message:
        "3 deliveries are overdue:
         - Sharma Metals MS Rod (2 days late)
         - Kumar Tools Carbide Insert (1 day late)
         - ABC Supplier Coolant (due today)"

        One message. One action needed. Not three separate alerts.
        """
        if len(overdue_purchases) == 1:
            purchase = overdue_purchases[0]
            days_overdue = self._calculate_days_difference(
                today, purchase.expected_delivery_date
            )

            await msg91_client.send_delivery_warning(
                phone=customer.phone,
                owner_name=customer.owner_name,
                supplier_name=purchase.supplier_name,
                material_name=purchase.material_name,
                days_overdue=max(0, days_overdue),
                supplier_phone=purchase.supplier_phone,
                channel=customer.preferred_channel,
            )
        else:
            # Multiple overdue — consolidated message
            summary_lines = []
            for p in overdue_purchases:
                days = self._calculate_days_difference(
                    today, p.expected_delivery_date
                )
                if days > 0:
                    summary_lines.append(
                        f"• {p.supplier_name} {p.material_name} "
                        f"({days} day{'s' if days > 1 else ''} late)"
                    )
                else:
                    summary_lines.append(
                        f"• {p.supplier_name} {p.material_name} "
                        f"(due today — unconfirmed)"
                    )

            message = (
                f"ProcureAI — Delivery Alert\n\n"
                f"{len(overdue_purchases)} deliveries need attention:\n"
                + "\n".join(summary_lines)
            )

            await msg91_client.send_sms(
                phone=customer.phone,
                message=message,
            )

        logger.info(
            f"Delivery alert sent to {customer.owner_name} "
            f"({customer.phone}): "
            f"{len(overdue_purchases)} overdue deliveries"
        )

    async def _get_customer(
        self, customer_id: str, db: AsyncSession
    ) -> Optional[Customer]:
        """Load customer from database."""
        import uuid as uuid_lib
        result = await db.execute(
            select(Customer).where(
                Customer.id == uuid_lib.UUID(customer_id)
            )
        )
        return result.scalar_one_or_none()

    def _calculate_days_difference(
        self,
        date1_str: str,
        date2_str: str,
    ) -> int:
        """
        Calculate days between two date strings.
        Positive = date1 is after date2 (overdue).
        Negative = date1 is before date2 (not yet due).

        FRESHER NOTE:
        date.fromisoformat("2026-09-07") converts string to date object.
        (date1 - date2).days gives signed integer difference.
        """
        try:
            date1 = date.fromisoformat(date1_str)
            date2 = date.fromisoformat(date2_str)
            return (date1 - date2).days
        except (ValueError, TypeError):
            return 0


# ==============================================================================
# MODULE-LEVEL SERVICE INSTANCE
# ==============================================================================

delivery_service = DeliveryTrackerService()