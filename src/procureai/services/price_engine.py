
# ==============================================================================
# PROCUREAI — services/price_engine.py
# Price Intelligence Service — the core of Feature 3.
#
# FRESHER EXPLANATION:
# This service answers one question every Monday for every customer:
# "Are you paying a fair price for your raw materials?"
#
# HOW IT WORKS:
# Every time Ramesh logs a purchase — MS Rod at ₹188/kg —
# that price goes into our cluster database.
# Every time Suresh (another Bommasandra manufacturer) logs
# MS Rod at ₹179/kg — that also goes in.
#
# Monday morning, the service calculates:
# Bommasandra MS Rod average this week: ₹181/kg
# Ramesh paid: ₹188/kg
# Difference: ₹7/kg = 3.9% above market
# On his 300kg order: ₹2,100 overpayment
#
# Alert sent: "MS Rod: You paid ₹188/kg. Market avg: ₹181/kg.
# Potential saving on next order: ₹2,100"
#
# THE MOAT:
# With 5 customers — weak signal
# With 20 customers — useful benchmark
# With 50 customers — the most accurate local price index
# in Bommasandra. Nobody else has this data.
# ==============================================================================

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from loguru import logger
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from procureai.integrations.mcx import mcx_client, CommodityPrice, PriceComparison
from procureai.models.customer import Customer
from procureai.models.purchase import Purchase


# ==============================================================================
# CONFIGURATION
# ==============================================================================

# Minimum overpayment percentage to trigger an alert
# Below this threshold — not worth alerting (noise)
ALERT_THRESHOLD_PCT = Decimal("8.0")

# Minimum sample count for cluster data to be trusted
# Below this — fall back to MCX data
MIN_CLUSTER_SAMPLES = 5

# How many days back to look for customer's recent purchases
RECENT_PURCHASE_DAYS = 7


class PriceEngineService:
    """
    Calculates price benchmarks and identifies overpayments.

    FRESHER NOTE ON THE TWO MODES:
    Mode 1 — Cluster data available (≥5 samples):
    Use actual transaction prices from enrolled customers.
    More accurate — reflects real local market conditions.

    Mode 2 — Cluster data thin (<5 samples):
    Fall back to MCX commodity prices.
    Less accurate — but better than no benchmark at all.

    As more customers enrol, Mode 1 covers more materials
    and Mode 2 is needed less. This is the data moat in action.
    """

    async def run_weekly_price_pulse(
        self, db: AsyncSession
    ) -> dict:
        """
        Run weekly price pulse for ALL active customers.
        Called by Celery Beat every Monday at 9:00 AM IST.

        FRESHER NOTE ON WHAT HAPPENS:
        1. Get all active customers
        2. For each customer — get their purchases in last 7 days
        3. For each material — get cluster average price
        4. If customer paid >8% above market → send alert
        5. Log results
        """
        logger.info("Starting weekly price pulse for all customers")

        result = await db.execute(
            select(Customer).where(Customer.is_active == True)
        )
        customers = result.scalars().all()

        results = {
            "total_customers": len(customers),
            "alerts_sent": 0,
            "no_purchases": 0,
            "no_overpayment": 0,
            "failed": 0,
        }

        for customer in customers:
            try:
                customer_result = await self.check_customer_prices(
                    customer_id=str(customer.id),
                    db=db,
                )

                if customer_result["alerts_sent"] > 0:
                    results["alerts_sent"] += customer_result["alerts_sent"]
                elif customer_result["purchases_checked"] == 0:
                    results["no_purchases"] += 1
                else:
                    results["no_overpayment"] += 1

            except Exception as e:
                logger.error(
                    f"Price pulse failed for {customer.gstin}: {e}"
                )
                results["failed"] += 1
                continue

        logger.info(
            f"Price pulse complete: "
            f"{results['alerts_sent']} alerts sent, "
            f"{results['no_overpayment']} customers paying fair price, "
            f"{results['no_purchases']} with no recent purchases"
        )

        return results

    async def check_customer_prices(
        self,
        customer_id: str,
        db: AsyncSession,
        days_back: int = RECENT_PURCHASE_DAYS,
    ) -> dict:
        """
        Check prices for one customer's recent purchases.
        Sends alert if any material is priced above threshold.

        Args:
            customer_id: Customer UUID
            db: Database session
            days_back: How many days of purchases to check

        Returns:
            Dict with purchases_checked and alerts_sent counts
        """
        import uuid as uuid_lib

        # Load customer
        result = await db.execute(
            select(Customer).where(
                Customer.id == uuid_lib.UUID(customer_id)
            )
        )
        customer = result.scalar_one_or_none()

        if not customer or not customer.is_active:
            return {"purchases_checked": 0, "alerts_sent": 0}

        # Get recent purchases
        cutoff_date = (
            date.today() - timedelta(days=days_back)
        ).isoformat()

        result = await db.execute(
            select(Purchase).where(
                and_(
                    Purchase.customer_id == uuid_lib.UUID(customer_id),
                    Purchase.invoice_date >= cutoff_date,
                    Purchase.unit_price > 0,
                )
            )
        )
        recent_purchases = result.scalars().all()

        if not recent_purchases:
            logger.info(
                f"No recent purchases for {customer.gstin} "
                f"in last {days_back} days"
            )
            return {"purchases_checked": 0, "alerts_sent": 0}

        logger.info(
            f"Checking prices for {customer.gstin}: "
            f"{len(recent_purchases)} recent purchases"
        )

        # Group by material — analyse each material separately
        # Ramesh might buy 5 different materials in a week
        # We check each one independently
        material_purchases = self._group_by_material(recent_purchases)

        overpayments = []

        for material_name, purchases in material_purchases.items():
            comparison = await self._analyse_material_price(
                material_name=material_name,
                purchases=purchases,
                customer=customer,
                db=db,
            )

            if comparison and comparison.is_overpaying:
                if comparison.difference_pct >= ALERT_THRESHOLD_PCT:
                    overpayments.append(comparison)
                    logger.info(
                        f"Overpayment detected: {customer.gstin} "
                        f"paid {comparison.difference_pct}% above market "
                        f"for {material_name}"
                    )

        alerts_sent = 0

        if overpayments:
            await self._send_price_alert(
                customer=customer,
                overpayments=overpayments,
            )
            alerts_sent = len(overpayments)

        return {
            "purchases_checked": len(recent_purchases),
            "materials_checked": len(material_purchases),
            "overpayments_found": len(overpayments),
            "alerts_sent": alerts_sent,
        }

    async def get_cluster_price(
        self,
        material_name: str,
        cluster: str,
        db: AsyncSession,
        days_back: int = 30,
    ) -> Optional[CommodityPrice]:
        """
        Get cluster average price for a material.

        Queries our own transaction database for actual prices
        paid by enrolled customers in the same cluster.

        FRESHER NOTE ON SQL AGGREGATION:
        func.avg() calculates the average of a column.
        func.count() counts rows.
        We group by nothing — get one row with overall average.

        SELECT AVG(unit_price), COUNT(*), MIN(unit_price), MAX(unit_price)
        FROM purchases
        WHERE material_name ILIKE '%ms rod%'
        AND cluster = 'bommasandra'
        AND invoice_date >= '2026-08-05'
        AND unit = 'kg'
        """
        cutoff_date = (
            date.today() - timedelta(days=days_back)
        ).isoformat()

        result = await db.execute(
            select(
                func.avg(Purchase.unit_price).label("avg_price"),
                func.count(Purchase.id).label("sample_count"),
                func.min(Purchase.unit_price).label("min_price"),
                func.max(Purchase.unit_price).label("max_price"),
            ).join(
                Customer,
                Purchase.customer_id == Customer.id
            ).where(
                and_(
                    func.lower(Purchase.material_name).contains(
                        material_name.lower()
                    ),
                    Customer.cluster == cluster,
                    Purchase.invoice_date >= cutoff_date,
                    Purchase.unit == "kg",
                    Purchase.unit_price > 0,
                )
            )
        )

        row = result.fetchone()

        if not row or not row.avg_price or row.sample_count < MIN_CLUSTER_SAMPLES:
            logger.info(
                f"Insufficient cluster data for {material_name} "
                f"in {cluster}: {row.sample_count if row else 0} samples. "
                f"Falling back to MCX."
            )
            return None

        avg_price = Decimal(str(row.avg_price)).quantize(Decimal("0.01"))

        logger.info(
            f"Cluster price for {material_name} in {cluster}: "
            f"₹{avg_price}/kg "
            f"({row.sample_count} samples)"
        )

        return CommodityPrice(
            material_name=material_name,
            price_per_kg=avg_price,
            price_per_tonne=avg_price * Decimal("1000"),
            source="cluster_data",
            data_date=date.today().isoformat(),
            sample_count=int(row.sample_count),
            is_estimated=False,
        )

    async def get_price_history(
        self,
        material_name: str,
        cluster: str,
        db: AsyncSession,
        weeks: int = 12,
    ) -> list[dict]:
        """
        Get weekly price history for a material in a cluster.

        Used to show Ramesh whether prices are trending up or down
        over the last 12 weeks.

        FRESHER NOTE ON WHY THIS MATTERS:
        If MS rod has gone up 3% over 12 weeks while Ramesh's
        supplier has increased 8%, that's evidence of gouging.
        If the market went up 8% and his supplier went up 8%,
        that's fair — the market moved.
        Context matters. Raw price comparison without trend
        doesn't tell the full story.
        """
        history = []
        today = date.today()

        for i in range(weeks):
            week_end = today - timedelta(weeks=i)
            week_start = week_end - timedelta(days=7)

            result = await db.execute(
                select(
                    func.avg(Purchase.unit_price).label("avg_price"),
                    func.count(Purchase.id).label("sample_count"),
                ).join(
                    Customer,
                    Purchase.customer_id == Customer.id
                ).where(
                    and_(
                        func.lower(Purchase.material_name).contains(
                            material_name.lower()
                        ),
                        Customer.cluster == cluster,
                        Purchase.invoice_date >= week_start.isoformat(),
                        Purchase.invoice_date <= week_end.isoformat(),
                        Purchase.unit == "kg",
                        Purchase.unit_price > 0,
                    )
                )
            )

            row = result.fetchone()

            if row and row.avg_price and row.sample_count >= 2:
                history.append({
                    "week_ending": week_end.isoformat(),
                    "avg_price": float(
                        Decimal(str(row.avg_price)).quantize(
                            Decimal("0.01")
                        )
                    ),
                    "sample_count": int(row.sample_count),
                })

        # Return chronological order (oldest first)
        return list(reversed(history))

    # ==========================================================================
    # PRIVATE HELPERS
    # ==========================================================================

    def _group_by_material(
        self, purchases: list[Purchase]
    ) -> dict[str, list[Purchase]]:
        """
        Group purchases by material name.

        FRESHER NOTE:
        We normalise material names to lowercase for grouping.
        "MS Rod", "ms rod", "MS ROD" all become "ms rod".
        This prevents treating the same material as different items.
        """
        grouped: dict[str, list[Purchase]] = {}

        for purchase in purchases:
            # Normalise: lowercase, strip whitespace
            key = purchase.material_name.lower().strip()
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(purchase)

        return grouped

    async def _analyse_material_price(
        self,
        material_name: str,
        purchases: list[Purchase],
        customer: Customer,
        db: AsyncSession,
    ) -> Optional[PriceComparison]:
        """
        Analyse whether customer overpaid for a specific material.

        Gets market price (cluster or MCX) and compares
        against what the customer actually paid.
        """
        # Calculate customer's average price for this material
        # (they might have bought from 2 different suppliers)
        total_value = sum(
            p.unit_price * p.quantity
            for p in purchases
            if p.unit == "kg"
        )
        total_quantity = sum(
            p.quantity for p in purchases if p.unit == "kg"
        )

        if total_quantity == 0:
            return None

        customer_avg_price = (total_value / total_quantity).quantize(
            Decimal("0.01")
        )

        # Try cluster price first
        market_price = None
        if customer.cluster:
            market_price = await self.get_cluster_price(
                material_name=material_name,
                cluster=customer.cluster,
                db=db,
            )

        # Fall back to MCX if cluster data is thin
        if not market_price:
            market_price = await mcx_client.get_material_price(
                material_name
            )

        if not market_price:
            logger.warning(
                f"No market price available for {material_name}"
            )
            return None

        # Calculate comparison
        return mcx_client.calculate_price_comparison(
            material_name=material_name,
            customer_price_per_kg=customer_avg_price,
            market_price=market_price,
            quantity=total_quantity,
            unit="kg",
        )

    async def _send_price_alert(
        self,
        customer: Customer,
        overpayments: list[PriceComparison],
    ) -> None:
        """
        Send price overpayment alert to customer.

        One alert covers all overpaid materials.
        Lead with the biggest overpayment — highest saving opportunity.

        FRESHER NOTE ON SORTING:
        sorted(..., key=lambda x: x.potential_saving, reverse=True)
        Sorts list by potential_saving from highest to lowest.
        We alert about the biggest saving opportunity first.
        """
        # Sort by potential saving — biggest first
        overpayments_sorted = sorted(
            overpayments,
            key=lambda x: x.potential_saving,
            reverse=True,
        )

        biggest = overpayments_sorted[0]

        logger.info(
            f"Sending price alert to {customer.owner_name} "
            f"({customer.phone}): "
            f"{biggest.material_name} "
            f"₹{biggest.customer_price}/kg vs "
            f"market ₹{biggest.market_price}/kg"
        )

        await msg91_client.send_price_pulse(
            phone=customer.phone,
            owner_name=customer.owner_name,
            material_name=biggest.material_name,
            your_price=float(biggest.customer_price),
            cluster_avg=float(biggest.market_price),
            potential_saving=float(biggest.potential_saving),
            channel=customer.preferred_channel,
        )


# ==============================================================================
# MODULE-LEVEL SERVICE INSTANCE
# ==============================================================================

price_service = PriceEngineService()